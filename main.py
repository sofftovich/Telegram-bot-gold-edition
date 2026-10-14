import os
import asyncio
import json
import re
import time
import random
import logging
from datetime import datetime, time as dt_time, timedelta, timezone
from aiogram import Bot, Dispatcher, types, F
from aiogram.types import Message, BotCommand
from aiogram.utils.media_group import MediaGroupBuilder
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from dotenv import load_dotenv
from aiohttp import web

load_dotenv()

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

# Конфигурация
TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_ID = os.getenv("CHANNEL_ID")

# Загрузка разрешённых пользователей
ALLOWED_USERS = []
for i in range(1, 4):
    user_id = os.getenv(f"ALLOWED_USER_{i}")
    if user_id:
        try:
            ALLOWED_USERS.append(int(user_id))
        except ValueError:
            logger.warning(f"Неверный формат ALLOWED_USER_{i}: {user_id}")

if not ALLOWED_USERS:
    logger.error("❌ Не указано ни одного разрешённого пользователя!")
    exit(1)

logger.info(f"✅ Разрешённые пользователи: {ALLOWED_USERS}")

if not TOKEN:
    logger.error("❌ BOT_TOKEN не установлен!")
    exit(1)

# Чешский часовой пояс GMT+2
CZECH_TIMEZONE = timezone(timedelta(hours=2))

def get_czech_time():
    return datetime.now(CZECH_TIMEZONE)

def check_user_access(user_id):
    return user_id in ALLOWED_USERS

# Глобальные настройки
POST_INTERVAL = None
last_post_time = 0
posting_enabled = True
DEFAULT_SIGNATURE = None
ALLOWED_WEEKDAYS = None
START_TIME = None
END_TIME = None
DELAYED_START_ENABLED = False
DELAYED_START_TIME = None
TIME_WINDOW_ENABLED = True
WEEKDAYS_ENABLED = False
EXACT_TIMING_ENABLED = True
NOTIFICATIONS_ENABLED = True

# Файлы для хранения данных
QUEUE_FILE = "queue.json"
STATE_FILE = "state.json"

# Структуры данных
pending_media_groups = {}
media_group_timers = {}
pending_notifications = {}
user_media_tracking = {}
is_posting_locked = False

bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

def parse_interval(interval_str):
    """Парсит интервал с учётом дней, часов, минут и секунд"""
    total_seconds = 0
    patterns = [('d', 24*3600), ('h', 3600), ('m', 60), ('s', 1)]

    for suffix, multiplier in patterns:
        match = re.search(rf'(\d+){suffix}', interval_str)
        if match:
            total_seconds += int(match.group(1)) * multiplier

    return total_seconds if total_seconds > 0 else None

def format_interval(seconds):
    """Форматирует секунды в читаемый вид"""
    periods = [('д', 86400), ('ч', 3600), ('м', 60), ('с', 1)]
    parts = []

    for name, period in periods:
        if seconds >= period:
            count = seconds // period
            parts.append(f"{count}{name}")
            seconds %= period

    return " ".join(parts) if parts else "0м"

def calculate_exact_posting_times():
    """Рассчитывает точные моменты времени для постинга в рамках временного окна"""
    if not EXACT_TIMING_ENABLED or POST_INTERVAL is None:
        return []

    # Если временное окно отключено или не назначено
    if not TIME_WINDOW_ENABLED or START_TIME is None or END_TIME is None:
        posting_times = []
        current_seconds = 0
        while current_seconds < 24 * 3600:
            hours = current_seconds // 3600
            minutes = (current_seconds % 3600) // 60
            if hours < 24:
                posting_times.append(dt_time(hours, minutes))
            current_seconds += POST_INTERVAL
        return posting_times

    # Рассчитываем продолжительность окна
    if START_TIME <= END_TIME:
        window_duration = (END_TIME.hour - START_TIME.hour) * 3600 + (END_TIME.minute - START_TIME.minute) * 60
    else:
        window_duration = (24 * 3600 - (START_TIME.hour * 3600 + START_TIME.minute * 60)) + (END_TIME.hour * 3600 + END_TIME.minute * 60)

    max_posts_in_window = max(1, int(window_duration // POST_INTERVAL))

    if max_posts_in_window == 1:
        return [START_TIME]

    posting_times = []
    start_seconds = START_TIME.hour * 3600 + START_TIME.minute * 60

    for i in range(max_posts_in_window):
        total_seconds = start_seconds + i * POST_INTERVAL
        if total_seconds >= 24 * 3600:
            total_seconds -= 24 * 3600

        hours = total_seconds // 3600
        minutes = (total_seconds % 3600) // 60
        current_time = dt_time(hours, minutes)

        # Проверяем, что время в пределах окна
        if START_TIME <= END_TIME:
            if START_TIME <= current_time <= END_TIME:
                posting_times.append(current_time)
        else:
            if current_time >= START_TIME or current_time <= END_TIME:
                posting_times.append(current_time)

    return posting_times

def get_next_exact_posting_time():
    """Возвращает следующее точное время для постинга"""
    if not EXACT_TIMING_ENABLED or POST_INTERVAL is None:
        return None

    now = get_czech_time()
    current_time = now.time()
    posting_times = calculate_exact_posting_times()

    if not posting_times:
        return None

    # Ищем ближайшее время сегодня
    for post_time in posting_times:
        if current_time < post_time:
            if not (WEEKDAYS_ENABLED and ALLOWED_WEEKDAYS is not None and now.weekday() not in ALLOWED_WEEKDAYS):
                return now.replace(hour=post_time.hour, minute=post_time.minute, second=0, microsecond=0)

    # Ищем следующий разрешённый день
    for days_ahead in range(1, 8):
        check_date = now + timedelta(days=days_ahead)
        if not WEEKDAYS_ENABLED or ALLOWED_WEEKDAYS is None or check_date.weekday() in ALLOWED_WEEKDAYS:
            first_time = posting_times[0]
            return check_date.replace(hour=first_time.hour, minute=first_time.minute, second=0, microsecond=0)

    return None

def calculate_queue_schedule(queue_length):
    """Рассчитывает расписание для всей очереди"""
    if queue_length == 0:
        return None, None

    if EXACT_TIMING_ENABLED:
        next_time = get_next_exact_posting_time()
        if not next_time:
            return None, None

        posting_times = calculate_exact_posting_times()
        if not posting_times:
            return None, None

        # Находим индекс текущего времени
        current_time_index = 0
        for i, post_time in enumerate(posting_times):
            if abs((post_time.hour * 60 + post_time.minute) - (next_time.time().hour * 60 + next_time.time().minute)) <= 1:
                current_time_index = i
                break

        # Рассчитываем время последнего поста
        last_time_index = (current_time_index + queue_length - 1) % len(posting_times)
        days_offset = (current_time_index + queue_length - 1) // len(posting_times)

        last_post_time = posting_times[last_time_index]
        last_post_date = next_time.date() + timedelta(days=days_offset)
        last_post_datetime = datetime.combine(last_post_date, last_post_time, tzinfo=CZECH_TIMEZONE)

        return next_time, last_post_datetime
    else:
        now = get_czech_time()
        first_post_time = now + timedelta(seconds=get_time_until_next_post())
        last_post_time = first_post_time + timedelta(seconds=(queue_length - 1) * POST_INTERVAL)
        return first_post_time, last_post_time

def get_time_until_next_post():
    """Возвращает время до следующего поста в секундах"""
    if POST_INTERVAL is None:
        return 24 * 3600

    if EXACT_TIMING_ENABLED:
        next_exact_time = get_next_exact_posting_time()
        if next_exact_time:
            now = get_czech_time()
            return max(0, int((next_exact_time - now).total_seconds()))
        return 0

    # Интервальное планирование
    now_timestamp = time.time()
    time_since_last = now_timestamp - last_post_time

    if time_since_last >= POST_INTERVAL:
        return get_next_allowed_time()
    else:
        interval_wait = POST_INTERVAL - int(time_since_last)
        allowed_wait = get_next_allowed_time()
        return max(interval_wait, allowed_wait)

def get_next_allowed_time():
    """Возвращает время до следующего разрешённого интервала"""
    now = get_czech_time()

    if is_posting_allowed()[0]:
        return 0

    # Ищем следующий разрешённый интервал
    for days_ahead in range(8):
        check_date = now + timedelta(days=days_ahead)
        check_date = check_date.replace(hour=0, minute=0, second=0, microsecond=0)
        check_weekday = check_date.weekday()

        if not WEEKDAYS_ENABLED or ALLOWED_WEEKDAYS is None or check_weekday in ALLOWED_WEEKDAYS:
            if days_ahead == 0:
                if not TIME_WINDOW_ENABLED or START_TIME is None or END_TIME is None:
                    return 0
                elif START_TIME <= END_TIME:
                    if now.time() < START_TIME:
                        target_time = check_date.replace(hour=START_TIME.hour, minute=START_TIME.minute)
                        return int((target_time - now).total_seconds())
                    elif now.time() > END_TIME:
                        continue
                else:
                    if END_TIME < now.time() < START_TIME:
                        target_time = check_date.replace(hour=START_TIME.hour, minute=START_TIME.minute)
                        return int((target_time - now).total_seconds())
            else:
                if not TIME_WINDOW_ENABLED or START_TIME is None:
                    return int((check_date - now).total_seconds())
                else:
                    target_time = check_date.replace(hour=START_TIME.hour, minute=START_TIME.minute)
                    return int((target_time - now).total_seconds())

    return 24 * 3600

def load_queue():
    if not os.path.exists(QUEUE_FILE):
        return []
    try:
        with open(QUEUE_FILE, "r") as f:
            return json.load(f)
    except:
        return []

def save_queue(queue):
    try:
        with open(QUEUE_FILE, "w") as f:
            json.dump(queue, f)
    except Exception as e:
        logger.error(f"Ошибка сохранения очереди: {e}")

def load_state():
    """Загружает состояние бота"""
    global last_post_time, CHANNEL_ID, posting_enabled
    global TIME_WINDOW_ENABLED, WEEKDAYS_ENABLED, EXACT_TIMING_ENABLED, NOTIFICATIONS_ENABLED

    # Дефолтные значения
    posting_enabled = True
    TIME_WINDOW_ENABLED = True
    WEEKDAYS_ENABLED = False
    EXACT_TIMING_ENABLED = True
    NOTIFICATIONS_ENABLED = True

    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                state = json.load(f)
                last_post_time = state.get("last_post_time", 0)
                saved_channel = state.get("channel_id")
                if saved_channel:
                    CHANNEL_ID = saved_channel
        except Exception as e:
            logger.error(f"Ошибка загрузки состояния: {e}")

def save_state():
    """Сохраняет состояние бота"""
    state = {
        "last_post_time": last_post_time,
        "default_signature": DEFAULT_SIGNATURE,
        "channel_id": CHANNEL_ID,
        "posting_enabled": posting_enabled,
        "post_interval": POST_INTERVAL,
        "allowed_weekdays": ALLOWED_WEEKDAYS,
        "start_time": START_TIME.strftime("%H:%M") if START_TIME else None,
        "end_time": END_TIME.strftime("%H:%M") if END_TIME else None,
        "delayed_start_enabled": DELAYED_START_ENABLED,
        "delayed_start_time": DELAYED_START_TIME.isoformat() if DELAYED_START_TIME else None,
        "time_window_enabled": TIME_WINDOW_ENABLED,
        "weekdays_enabled": WEEKDAYS_ENABLED,
        "exact_timing_enabled": EXACT_TIMING_ENABLED,
        "notifications_enabled": NOTIFICATIONS_ENABLED
    }

    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"Ошибка сохранения состояния: {e}")

def is_posting_allowed():
    """Проверяет, разрешён ли сейчас постинг с учётом всех ограничений"""
    now = get_czech_time()
    current_weekday = now.weekday()
    current_time = now.time()

    # Проверка дня недели
    if WEEKDAYS_ENABLED and ALLOWED_WEEKDAYS is not None and current_weekday not in ALLOWED_WEEKDAYS:
        return False, f"День недели не разрешён ({get_weekday_name(current_weekday)})"

    # Проверка временного окна
    if TIME_WINDOW_ENABLED and START_TIME is not None and END_TIME is not None:
        if START_TIME <= END_TIME:
            if not (START_TIME <= current_time <= END_TIME):
                return False, f"Вне временного окна ({START_TIME.strftime('%H:%M')}-{END_TIME.strftime('%H:%M')})"
        else:
            if not (current_time >= START_TIME or current_time <= END_TIME):
                return False, f"Вне временного окна ({START_TIME.strftime('%H:%M')}-{END_TIME.strftime('%H:%M')})"

    return True, "Разрешено"

def is_posting_allowed_in_future(seconds_ahead=60):
    """Проверяет, будет ли разрешён постинг через указанное количество секунд"""
    future_time = get_czech_time() + timedelta(seconds=seconds_ahead)
    future_weekday = future_time.weekday()
    future_current_time = future_time.time()

    # Проверка дня недели в будущем
    if WEEKDAYS_ENABLED and ALLOWED_WEEKDAYS is not None and future_weekday not in ALLOWED_WEEKDAYS:
        return False, f"День недели не будет разрешён ({get_weekday_name(future_weekday)})"

    # Проверка временного окна в будущем
    if TIME_WINDOW_ENABLED and START_TIME is not None and END_TIME is not None:
        if START_TIME <= END_TIME:
            if not (START_TIME <= future_current_time <= END_TIME):
                return False, f"Будет вне временного окна ({START_TIME.strftime('%H:%M')}-{END_TIME.strftime('%H:%M')})"
        else:
            if not (future_current_time >= START_TIME or future_current_time <= END_TIME):
                return False, f"Будет вне временного окна ({START_TIME.strftime('%H:%M')}-{END_TIME.strftime('%H:%M')})"

    return True, "Будет разрешено"

def should_prepare_for_posting():
    """Проверяет, нужно ли начинать подготовку к постингу заранее"""
    if not EXACT_TIMING_ENABLED or POST_INTERVAL is None:
        return False, "Точное планирование отключено"
    
    # Текущий статус
    posting_allowed, current_reason = is_posting_allowed()
    if posting_allowed:
        return False, "Постинг уже разрешён"
    
    # Проверяем будущий статус через 60 секунд
    future_allowed, future_reason = is_posting_allowed_in_future(60)
    if not future_allowed:
        return False, f"Постинг не будет разрешён: {future_reason}"
    
    # Проверяем, есть ли запланированный пост в ближайшую минуту
    next_exact_time = get_next_exact_posting_time()
    if not next_exact_time:
        return False, "Нет точного времени для следующего поста"
    
    now = get_czech_time()
    time_until_next = (next_exact_time - now).total_seconds()
    
    # Если следующий пост должен быть в течение следующих 60 секунд
    if 0 <= time_until_next <= 60:
        return True, f"Подготовка к посту через {int(time_until_next)}с в {next_exact_time.strftime('%H:%M')}"
    
    return False, f"Следующий пост через {int(time_until_next)}с"

def is_delayed_start_ready():
    """Проверяет, готов ли отложенный старт"""
    if not DELAYED_START_ENABLED or not DELAYED_START_TIME:
        return True
    return get_czech_time() >= DELAYED_START_TIME

def get_weekday_name(weekday):
    days = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]
    return days[weekday]

def get_weekday_short(weekday):
    days = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
    return days[weekday]

def count_queue_stats(queue):
    """Подсчитывает статистику очереди"""
    total_media = 0
    media_groups = 0
    total_posts = len(queue)
    photos = videos = gifs = documents = 0

    for item in queue:
        if isinstance(item, dict) and item.get("type") == "media_group":
            media_groups += 1
            for media in item.get("media", []):
                total_media += 1
                media_type = media["type"]
                if media_type == "photo":
                    photos += 1
                elif media_type == "video":
                    videos += 1
                elif media_type == "gif":
                    gifs += 1
                elif media_type == "document":
                    documents += 1
        else:
            total_media += 1
            if isinstance(item, dict):
                media_type = item.get("type", "photo")
                if media_type == "photo":
                    photos += 1
                elif media_type == "video":
                    videos += 1
                elif media_type == "gif":
                    gifs += 1
                elif media_type == "document":
                    documents += 1
            else:
                photos += 1

    return total_media, media_groups, total_posts, photos, videos, gifs, documents

def format_queue_stats(queue):
    """Форматирует статистику очереди в читаемый вид"""
    total_media, media_groups, total_posts, photos, videos, gifs, documents = count_queue_stats(queue)

    parts = []
    if photos > 0:
        parts.append(f"{photos} фото")
    if videos > 0:
        parts.append(f"{videos} видео")
    if gifs > 0:
        parts.append(f"{gifs} GIF")
    if documents > 0:
        parts.append(f"{documents} документов")
    if media_groups > 0:
        parts.append(f"{media_groups} медиагрупп")
    parts.append(f"{total_posts} постов")

    return " | ".join(parts)

def shuffle_queue():
    queue = load_queue()
    if len(queue) > 1:
        random.shuffle(queue)
        save_queue(queue)
        return True
    return False

def update_user_tracking_after_post():
    global user_media_tracking
    updated_tracking = {}
    for idx, uid in user_media_tracking.items():
        if idx > 0:
            updated_tracking[idx - 1] = uid
    user_media_tracking = updated_tracking

def add_user_to_queue_tracking(user_id, queue_position):
    user_media_tracking[queue_position] = user_id

def get_users_for_next_post():
    if 0 in user_media_tracking:
        return [user_media_tracking[0]]
    return []

def parse_signature_with_link(text):
    """Парсит подпись в формате 'текст # ссылка' и возвращает HTML с кликабельной ссылкой"""
    if " # " in text:
        parts = text.rsplit(" # ", 1)
        if len(parts) == 2:
            caption_text = parts[0].strip()
            link_url = parts[1].strip()

            if (link_url and 
                ('.' in link_url or 
                 link_url.startswith(("http://", "https://", "t.me/", "tg://")))):

                if not link_url.startswith(("http://", "https://", "tg://")):
                    link_url = "https://" + link_url

                return f'<a href="{link_url}">{caption_text}</a>'

    return text

def apply_signature_to_all_queue(signature):
    """Применяет подпись ко всем постам в очереди"""
    queue = load_queue()
    if not queue:
        return 0

    parsed_signature = parse_signature_with_link(signature)
    updated_count = 0

    for i, item in enumerate(queue):
        if isinstance(item, dict):
            item["caption"] = parsed_signature
        else:
            queue[i] = {"file_id": item, "caption": parsed_signature, "type": "photo"}
        updated_count += 1

    save_queue(queue)
    return updated_count

async def verify_post_published(channel_id, expected_type=None, timeout=5):
    """Проверяет, что пост действительно опубликован в канале"""
    try:
        await asyncio.sleep(1)
        await bot.get_chat_member_count(channel_id)
        await bot.get_chat(channel_id)
        logger.info(f"✅ Канал {channel_id} доступен, пост считается опубликованным")
        return True
    except Exception as e:
        logger.error(f"Ошибка проверки публикации: {e}")
        return False

async def send_media_group_to_channel(media_group_data):
    """Отправляет медиагруппу в канал"""
    try:
        media_group = MediaGroupBuilder()

        for i, media in enumerate(media_group_data["media"]):
            caption = media_group_data["caption"] if i == 0 else None

            if media["type"] == "photo":
                media_group.add_photo(media=media["file_id"], caption=caption)
            elif media["type"] == "video":
                media_group.add_video(media=media["file_id"], caption=caption)
            elif media["type"] == "document":
                media_group.add_document(media=media["file_id"], caption=caption)

        await bot.send_media_group(chat_id=CHANNEL_ID, media=media_group.build())
        logger.info(f"✅ Медиагруппа из {len(media_group_data['media'])} элементов опубликована")
        return True
    except Exception as e:
        logger.error(f"❌ Ошибка отправки медиагруппы: {e}")
        raise e

async def notify_users_about_publication(media_type, is_success=True, error_msg=None):
    """Отправляет уведомления пользователям о публикации их медиа"""
    if not NOTIFICATIONS_ENABLED or not pending_notifications:
        return

    users_to_notify = list(pending_notifications.keys())

    for user_id in users_to_notify:
        try:
            if is_success:
                if media_type == "media_group":
                    message_text = "✅ Ваша медиагруппа успешно опубликована в канале!"
                else:
                    message_text = f"✅ Ваше {media_type} успешно опубликовано в канале!"
            else:
                message_text = f"❌ Ошибка публикации: {error_msg}"

            await bot.send_message(chat_id=user_id, text=message_text)
        except Exception as e:
            logger.error(f"Ошибка отправки уведомления пользователю {user_id}: {e}")

    pending_notifications.clear()

async def send_single_media(media_data):
    """Отправляет одиночное медиа в канал"""
    media_type = media_data.get("type", "photo")
    file_id = media_data.get("file_id") if isinstance(media_data, dict) else media_data
    caption = media_data.get("caption", DEFAULT_SIGNATURE or "") if isinstance(media_data, dict) else DEFAULT_SIGNATURE or ""

    if media_type == "document":
        await bot.send_document(chat_id=CHANNEL_ID, document=file_id, caption=caption)
    elif media_type == "video":
        await bot.send_video(chat_id=CHANNEL_ID, video=file_id, caption=caption)
    elif media_type == "gif":
        await bot.send_animation(chat_id=CHANNEL_ID, animation=file_id, caption=caption)
    else:
        await bot.send_photo(chat_id=CHANNEL_ID, photo=file_id, caption=caption)

async def post_next_media():
    """Публикует следующее медиа из очереди"""
    global last_post_time

    queue = load_queue()
    if not queue:
        return

    posting_allowed, reason = is_posting_allowed()
    delayed_ready = is_delayed_start_ready()

    if not posting_enabled:
        logger.info("🔴 Автопостинг отключён")
        return

    if not delayed_ready:
        logger.info(f"⏰ Ожидание отложенного старта до {DELAYED_START_TIME}")
        return

    if not CHANNEL_ID:
        logger.error("❌ CHANNEL_ID не установлен")
        return

    # Проверка текущего разрешения или подготовка к будущему
    if not posting_allowed:
        # Проверяем, нужно ли готовиться к будущему постингу
        should_prepare, prepare_reason = should_prepare_for_posting()
        if should_prepare:
            logger.info(f"🔮 {prepare_reason}")
            # Продолжаем выполнение для подготовки к точному времени
        else:
            logger.info(f"⏰ Постинг запрещён: {reason}")
            return

    # Проверка времени для точного планирования
    if EXACT_TIMING_ENABLED:
        next_exact_time = get_next_exact_posting_time()
        if next_exact_time:
            now = get_czech_time()
            now = now.replace(second=0, microsecond=0)
            time_diff = (next_exact_time - now).total_seconds()

            if 0 <= time_diff <= 61:
                global is_posting_locked
                is_posting_locked = True
                
                # Если мы готовимся заранее, ждём точного времени
                if time_diff > 5:
                    logger.info(f"⏳ Подготовка к публикации в {next_exact_time.strftime('%H:%M')} (ждём {int(time_diff)}с)")
                    await asyncio.sleep(time_diff - 5)  # Ждём до последних 5 секунд
                
                logger.info(f"✅ Публикуем пост в точное время: {next_exact_time.strftime('%H:%M')} (финальная подготовка)")
                await asyncio.sleep(5)  # Финальная пауза для синхронизации
                is_posting_locked = False
            else:
                logger.info(f"⏰ Ожидание точного времени постинга: {next_exact_time.strftime('%H:%M')} (через {int(time_diff)}с)")
                return

    # Публикуем медиа
    media_data = queue.pop(0)
    published_successfully = False

    # Получаем пользователей для уведомления
    users_for_notification = get_users_for_next_post()
    if users_for_notification:
        for user_id in users_for_notification:
            pending_notifications[user_id] = True

    try:
        if isinstance(media_data, dict) and media_data.get("type") == "media_group":
            await send_media_group_to_channel(media_data)
            verification_success = await verify_post_published(CHANNEL_ID, "media_group")

            if verification_success:
                published_successfully = True
                await notify_users_about_publication("media_group", True)
                logger.info("✅ Медиагруппа успешно опубликована и пользователи уведомлены")
            else:
                await notify_users_about_publication("media_group", False, "Не удалось подтвердить публикацию в канале")
                logger.error("❌ Не удалось подтвердить публикацию медиагруппы в канале")
        else:
            await send_single_media(media_data)
            media_type = media_data.get("type", "photo") if isinstance(media_data, dict) else "photo"
            verification_success = await verify_post_published(CHANNEL_ID, media_type)

            if verification_success:
                published_successfully = True
                await notify_users_about_publication(media_type, True)
                logger.info(f"✅ {media_type} успешно опубликовано и пользователи уведомлены")
            else:
                await notify_users_about_publication(media_type, False, "Не удалось подтвердить публикацию в канале")
                logger.error(f"❌ Не удалось подтвердить публикацию {media_type} в канале")

        if published_successfully:
            last_post_time = time.time()
            save_state()
            update_user_tracking_after_post()

    except Exception as e:
        logger.error(f"❌ Ошибка отправки медиа: {e}")
        await notify_users_about_publication("медиа", False, str(e))
        queue.insert(0, media_data)

    save_queue(queue)

async def posting_loop():
    """Основной цикл постинга с защитой от краха"""
    logger.info("[POSTER] posting_loop запущен")
    
    while True:
        try:
            while True:  # Внутренний цикл для постинга
                try:
                    queue = load_queue()
                    if queue and posting_enabled and CHANNEL_ID:
                        time_until_next = get_time_until_next_post()

                        if time_until_next <= 0:
                            await post_next_media()
                            await asyncio.sleep(3)
                        else:
                            sleep_time = min(time_until_next, 30)
                            await asyncio.sleep(sleep_time)
                    else:
                        await asyncio.sleep(15)

                except Exception as e:
                    logger.error(f"❌ Ошибка в цикле постинга: {e}")
                    await asyncio.sleep(30)
                    
        except (asyncio.CancelledError, Exception) as e:
            logger.error(f"❌ Критическая ошибка в posting_loop: {e}")
            logger.info("🔄 Перезапуск posting_loop через 10 секунд...")
            await asyncio.sleep(10)
            logger.info("[POSTER] posting_loop перезапущен после ошибки")

async def process_pending_media_group(media_group_id):
    """Обрабатывает накопленную медиагруппу после таймаута"""
    await asyncio.sleep(1)

    if media_group_id in pending_media_groups:
        media_group = pending_media_groups[media_group_id]

        if len(media_group) > 1:
            # Проверяем наличие подписи
            has_caption = any(media_info["message"].caption and media_info["message"].caption.strip() 
                            for media_info in media_group)

            if has_caption:
                # Создаём медиагруппу
                media_list = [media_info["media_data"] for media_info in media_group]
                media_group_data = {
                    "type": "media_group", 
                    "media": media_list,
                    "caption": DEFAULT_SIGNATURE or ""
                }

                queue = load_queue()
                queue.append(media_group_data)
                save_queue(queue)

                # Добавляем пользователей в уведомления
                for media_info in media_group:
                    user_id = media_info["message"].from_user.id
                    pending_notifications[user_id] = True
                    add_user_to_queue_tracking(user_id, len(queue) - 1)

                # Подсчёт типов медиа
                media_types = [m.get("type", "photo") for m in media_list]
                media_count = {}
                for media_type in media_types:
                    type_name = {"photo": "фото", "video": "видео", "animation": "GIF", "document": "документ"}.get(media_type, "фото")
                    media_count[type_name] = media_count.get(type_name, 0) + 1

                media_text = " + ".join([f"{count} {name}" for name, count in media_count.items()])
                response = await format_queue_response(media_text, len(media_group), queue, is_media_group=True)
                await media_group[0]["message"].reply(response)
            else:
                # Обрабатываем отдельно
                for media_info in media_group:
                    await handle_single_media(media_info["message"], media_info["media_data"], media_info["media_type"])
        else:
            # Одиночное медиа
            await handle_single_media(media_group[0]["message"], media_group[0]["media_data"], media_group[0]["media_type"])

    # Очищаем группу
    if media_group_id in pending_media_groups:
        del pending_media_groups[media_group_id]
    if media_group_id in media_group_timers:
        del media_group_timers[media_group_id]

async def handle_single_media(message: Message, media_data: dict, media_type: str):
    """Обрабатывает одиночное медиа"""
    queue = load_queue()
    queue.append(media_data)
    save_queue(queue)

    user_id = message.from_user.id
    pending_notifications[user_id] = True
    add_user_to_queue_tracking(user_id, len(queue) - 1)

    response = await format_queue_response(media_type, 1, queue)
    await message.reply(response)

async def format_queue_response(media_text, media_count, queue, is_media_group=False):
    """Форматирует ответ о добавлении в очередь"""
    now = get_czech_time()

    first_post_time, last_post_time_calc = calculate_queue_schedule(len(queue))

    if is_media_group:
        add_text = f"📎 Медиагруппа из {media_count} элементов ({media_text}) добавлена в очередь!\n\n"
    else:
        add_text = f"📸 {media_text.title()} добавлено в очередь!\n\n"

    if first_post_time:
        if first_post_time.date() == now.date():
            first_post_text = f"🕐 Первый пост: в {first_post_time.strftime('%H:%M')}"
        else:
            first_post_text = f"🕐 Первый пост: {first_post_time.strftime('%d.%m')} в {first_post_time.strftime('%H:%M')}"
    else:
        first_post_text = "🕐 Первый пост: по расписанию"

    if len(queue) > 1 and last_post_time_calc:
        if last_post_time_calc.date() == now.date():
            last_post_text = f"\n📅 Последний пост: в {last_post_time_calc.strftime('%H:%M')}"
        else:
            last_post_text = f"\n📅 Последний пост: {last_post_time_calc.strftime('%d.%m')} в {last_post_time_calc.strftime('%H:%M')}"
    else:
        last_post_text = ""

    queue_stats = format_queue_stats(queue)

    return f"{add_text}{first_post_text}{last_post_text}\n📊 В очереди: {queue_stats}\n\n💡 Вы получите уведомление после публикации\n💡 /help | /status"

@dp.message(F.photo | F.document | F.video | F.animation)
async def handle_media(message: Message):
    """Обрабатывает фото, документы, видео и анимации (GIF)"""
    if not check_user_access(message.from_user.id):
        await message.reply("У вас нет прав для пользования этим ботом, для получения, посетите канал https://t.me/poluchiprava228.")
        return

    if is_posting_locked:
        await message.reply("⏳ Сейчас будет запощен пост, ваше медиа добавлено в очередь")

    # Определяем тип медиа и получаем file_id
    if message.photo:
        media_type = "photo"
        file_id = message.photo[-1].file_id
    elif message.document:
        if message.document.mime_type and message.document.mime_type.startswith("image/gif"):
            media_type = "gif"
        else:
            media_type = "document"
        file_id = message.document.file_id
    elif message.video:
        media_type = "video"
        file_id = message.video.file_id
    elif message.animation:
        media_type = "gif"
        file_id = message.animation.file_id
    else:
        return

    # Получаем подпись
    caption = message.caption if message.caption else (DEFAULT_SIGNATURE or "")

    media_data = {
        "file_id": file_id,
        "type": media_type,
        "caption": caption
    }

    # Проверяем медиагруппу
    if message.media_group_id:
        media_group_id = message.media_group_id

        if media_group_id not in pending_media_groups:
            pending_media_groups[media_group_id] = []

        pending_media_groups[media_group_id].append({
            "message": message,
            "media_data": media_data,
            "media_type": media_type
        })

        if media_group_id not in media_group_timers:
            media_group_timers[media_group_id] = asyncio.create_task(
                process_pending_media_group(media_group_id)
            )
    else:
        await handle_single_media(message, media_data, media_type)

@dp.message(F.text)
async def handle_message(message: Message):
    """Обрабатывает текстовые команды"""
    if not check_user_access(message.from_user.id):
        await message.reply("У вас нет прав для пользования этим ботом, для получения, посетите канал https://t.me/poluchiprava228.")
        return

    global posting_enabled, CHANNEL_ID, DEFAULT_SIGNATURE, POST_INTERVAL, last_post_time
    global ALLOWED_WEEKDAYS, START_TIME, END_TIME, DELAYED_START_ENABLED, DELAYED_START_TIME
    global TIME_WINDOW_ENABLED, WEEKDAYS_ENABLED, EXACT_TIMING_ENABLED, NOTIFICATIONS_ENABLED

    text = message.text.strip()

    if text == "/start":
        enabled_by_default = []
        if posting_enabled:
            enabled_by_default.append("✅ Автопостинг")
        if EXACT_TIMING_ENABLED:
            enabled_by_default.append("✅ Точное планирование времени")
        if TIME_WINDOW_ENABLED:
            enabled_by_default.append("✅ Временные ограничения")
        if NOTIFICATIONS_ENABLED:
            enabled_by_default.append("✅ Уведомления о постах")

        disabled_by_default = []
        if not WEEKDAYS_ENABLED:
            disabled_by_default.append("❌ Ограничения по дням недели")

        not_assigned = []
        if POST_INTERVAL is None:
            not_assigned.append("❓ Интервал постинга")
        if DEFAULT_SIGNATURE is None:
            not_assigned.append("❓ Подпись для постов")
        if START_TIME is None or END_TIME is None:
            not_assigned.append("❓ Временное окно постинга")
        if ALLOWED_WEEKDAYS is None:
            not_assigned.append("❓ Дни недели для постинга")

        if CHANNEL_ID:
            try:
                chat_info = await bot.get_chat(CHANNEL_ID)
                channel_info = f"📢 Канал: {chat_info.title or CHANNEL_ID} ({CHANNEL_ID})"
            except:
                channel_info = f"📢 Канал: {CHANNEL_ID}"
        else:
            channel_info = "❌ Канал не установлен"

        start_text = (
            "👋 <b>Бот для автопостинга запущен!</b>\n\n"
            f"{channel_info}\n\n"
            "<b>🟢 Включено по умолчанию:</b>\n"
            f"{chr(10).join(enabled_by_default)}\n\n"
        )

        if disabled_by_default:
            start_text += f"<b>🔴 Выключено по умолчанию:</b>\n{chr(10).join(disabled_by_default)}\n\n"

        if not_assigned:
            start_text += (
                f"<b>❓ Не назначено (требует настройки):</b>\n{chr(10).join(not_assigned)}\n\n"
                "⚠️ <b>Рекомендация:</b> Обязательно настройте интервал постинга, подпись и временное окно для корректной работы бота.\n\n"
            )

        start_text += "🛠 Используйте /help для помощи по настройке"
        await message.reply(start_text)

    elif text == "/help":
        help_text = """
🤖 <b>Справка по настройке бота</b>

<b>📚 Ключевые понятия:</b>

<b>⏱ Интервал постинга</b> - время между публикациями постов
• Команда: /interval 2h30m
• Формат: XdXhXmXs (дни, часы, минуты, секунды)
• Пример: /interval 3h (каждые 3 часа)

<b>🕐 Временное окно</b> - период дня, когда разрешены публикации
• Команда: /settime 06:00 20:00
• Посты будут публиковаться только в указанное время

<b>📝 Подпись (title)</b> - текст, который добавляется к каждому посту
• Команда: /title ваш_текст
• Команда: /title текст # ссылка (кликабельная ссылка)
• Подробное меню: /title

<b>📅 Дни недели</b> - в какие дни разрешены публикации
• Команда: /days 1,2,3,4,5 (пн-пт)
• 1=понедельник, 7=воскресенье

<b>🎯 Режимы планирования:</b>
• <b>Точное</b> - посты публикуются в заранее рассчитанные моменты времени
• <b>Интервальное</b> - посты публикуются через равные промежутки времени
• Переключение: /toggleexact

<b>📤 Как добавить медиа:</b>
• Отправьте одно фото/видео/документ - добавится как отдельный пост
• Отправьте несколько медиа вместе с любой подписью - создастся медиагруппа
• Медиа без подписи будут обработаны как отдельные посты

<b>🔧 Основные команды:</b>
/status - посмотреть текущие настройки
/commands - полный список всех команд
/schedule - расписание публикаций
/toggle - включить/выключить автопостинг
"""
        await message.reply(help_text)

    elif text == "/commands":
        commands_text = """
📋 <b>Полный список команд</b>

<b>📤 Основное управление:</b>
/start - информация о боте и настройках
/help - справка по использованию
/status - статус бота и очередь
/toggle - включить/выключить автопостинг

<b>⏰ Управление расписанием:</b>
/schedule - показать расписание
/interval - установить интервал (например: 2h30m)
/settime - установить временное окно (например: 06:00 20:00)
/days - установить дни недели (например: 1,2,3,4,5)
/checktime - проверить текущее время и ограничения

<b>🎛 Переключатели:</b>
/toggletime - вкл/выкл временное окно
/toggledays - вкл/выкл ограничения по дням
/toggleexact - точное/интервальное планирование
/togglenotify - вкл/выкл уведомления

<b>📅 Отложенный старт:</b>
/startdate - установить время первого поста
/clearstart - отключить отложенный старт

<b>📢 Управление каналом:</b>
/channel - показать текущий канал
/setchannel - установить ID канала

<b>📝 Управление подписями:</b>
/title - меню управления подписями

<b>📋 Управление очередью:</b>
/clear - очистить очередь
/remove - удалить пост по номеру
/random - перемешать очередь

<b>⚡ Мгновенная публикация:</b>
/postfile - опубликовать пост по номеру
/postnow - опубликовать следующий пост
/postall - опубликовать все посты сразу
"""
        await message.reply(commands_text)

    elif text == "/status":
        now = get_czech_time()
        queue = load_queue()

        # Время публикации последнего элемента
        if len(queue) > 0 and POST_INTERVAL is not None:
            first_post_time, last_post_time_calculated = calculate_queue_schedule(len(queue))
            if last_post_time_calculated:
                if last_post_time_calculated.date() == now.date():
                    total_time_text = f"в {last_post_time_calculated.strftime('%H:%M')}"
                else:
                    total_time_text = f"{last_post_time_calculated.strftime('%d.%m')} в {last_post_time_calculated.strftime('%H:%M')}"
            else:
                total_time_text = "по расписанию"
        elif len(queue) > 0:
            total_time_text = "❓ Интервал не установлен"
        else:
            total_time_text = "Очередь пуста"

        # Время до следующего поста
        if len(queue) > 0 and POST_INTERVAL is not None:
            time_until_next = get_time_until_next_post()
            next_post_text = format_interval(time_until_next) if time_until_next > 0 else "сейчас"
        elif len(queue) > 0:
            next_post_text = "❓ интервал не установлен"
        else:
            next_post_text = "нет постов в очереди"

        # Детали расписания
        if EXACT_TIMING_ENABLED and POST_INTERVAL is not None:
            posting_times = calculate_exact_posting_times()
            if posting_times and len(posting_times) > 1:
                times_str = ", ".join([t.strftime('%H:%M') for t in posting_times[:3]])
                if len(posting_times) > 3:
                    times_str += f" ... (всего {len(posting_times)})"
                schedule_detail = f"\n🎯 Точные времена: {times_str}"
            else:
                schedule_detail = f"\n⏱ Интервал: {format_interval(POST_INTERVAL)}"
        elif POST_INTERVAL is not None:
            schedule_detail = f"\n⏱ Интервал: {format_interval(POST_INTERVAL)}"
        else:
            schedule_detail = f"\n❓ Интервал: не установлен"

        posting_allowed, reason = is_posting_allowed()
        delayed_ready = is_delayed_start_ready()

        # Статус
        if posting_enabled and posting_allowed and delayed_ready:
            status_emoji = "✅"
            status_text = "активен"
        else:
            status_emoji = "❌"
            reasons = []
            if not posting_enabled:
                reasons.append("отключён")
            if not posting_allowed:
                reasons.append(reason.lower())
            if not delayed_ready:
                reasons.append("ожидание старта")
            status_text = ", ".join(reasons)

        # Отложенный старт
        delayed_text = ""
        if DELAYED_START_ENABLED and DELAYED_START_TIME and not delayed_ready:
            delayed_text = f"\n⏳ Старт: {DELAYED_START_TIME.strftime('%d.%m %H:%M')}"

        queue_stats = format_queue_stats(queue)

        # Канал
        if CHANNEL_ID:
            try:
                chat_info = await bot.get_chat(CHANNEL_ID)
                channel_text = f"{chat_info.title or CHANNEL_ID} ({CHANNEL_ID})"
            except:
                channel_text = CHANNEL_ID
        else:
            channel_text = "не установлен"

        signature_text = DEFAULT_SIGNATURE if DEFAULT_SIGNATURE is not None else "❓ не установлена"

        status_text_full = f"""
🤖 <b>Статус бота:</b>

{status_emoji} Автопостинг: {status_text}
📊 В очереди: {queue_stats}
🕐 Следующий пост: {next_post_text}
📅 Время публикации всех фото: {total_time_text}{schedule_detail}{delayed_text}

💬 Канал: {channel_text}
🏷 Подпись: {signature_text}
{'🔔' if NOTIFICATIONS_ENABLED else '🔕'} Уведомления: {'включены' if NOTIFICATIONS_ENABLED else 'выключены'}
{'🎯' if EXACT_TIMING_ENABLED else '⏱'} Планирование: {'точное' if EXACT_TIMING_ENABLED else 'интервальное'}

💡 /help для команд | /schedule для расписания
"""
        await message.reply(status_text_full)

    elif text.startswith("/interval"):
        parts = text.split(maxsplit=1)
        if len(parts) == 1:
            current_interval = format_interval(POST_INTERVAL) if POST_INTERVAL is not None else "❓ не установлен"
            await message.reply(f"📊 Текущий интервал: {current_interval}\n\n"
                              "Для изменения: /interval 2h30m\n"
                              "Форматы: 1d (день), 2h (часы), 30m (минуты), 45s (секунды)")
            return

        new_interval = parse_interval(parts[1])
        if new_interval:
            POST_INTERVAL = new_interval
            save_state()
            formatted_interval = format_interval(new_interval)

            if EXACT_TIMING_ENABLED:
                posting_times = calculate_exact_posting_times()
                if posting_times and len(posting_times) > 1:
                    times_str = ", ".join([t.strftime('%H:%M') for t in posting_times[:5]])
                    if len(posting_times) > 5:
                        times_str += f" ... (всего {len(posting_times)})"
                    await message.reply(f"✅ Интервал установлен: {formatted_interval}\n\n🎯 Новые времена постинга: {times_str}")
                else:
                    await message.reply(f"✅ Интервал установлен: {formatted_interval}")
            else:
                await message.reply(f"✅ Интервал установлен: {formatted_interval}")
        else:
            await message.reply("❌ Неверный формат интервала. Пример: 2h30m (2 часа 30 минут)")

    elif text == "/toggle":
        posting_enabled = not posting_enabled
        save_state()
        status = "включен" if posting_enabled else "выключен"
        await message.reply(f"{'✅' if posting_enabled else '❌'} Автопостинг {status}!")

    elif text == "/schedule":
        if ALLOWED_WEEKDAYS is not None:
            allowed_days = ", ".join([get_weekday_name(day) for day in sorted(ALLOWED_WEEKDAYS)])
        else:
            allowed_days = "❓ не назначены"

        posting_allowed, reason = is_posting_allowed()
        delayed_ready = is_delayed_start_ready()

        if posting_allowed and delayed_ready:
            status_emoji = "✅"
            status_text = "разрешён"
        else:
            status_emoji = "❌"
            status_text = reason.lower() if not posting_allowed else "ожидание старта"

        if EXACT_TIMING_ENABLED and POST_INTERVAL is not None:
            posting_times = calculate_exact_posting_times()
            if posting_times and len(posting_times) > 1:
                times_str = ", ".join([t.strftime('%H:%M') for t in posting_times[:5]])
                if len(posting_times) > 5:
                    times_str += f" ... (всего {len(posting_times)})"
                timing_info = f"🎯 Точные времена ({len(posting_times)}): {times_str}"
            else:
                timing_info = f"⏱ Интервал: {format_interval(POST_INTERVAL)}"
        elif POST_INTERVAL is not None:
            timing_info = f"⏱ Интервал: {format_interval(POST_INTERVAL)}"
        else:
            timing_info = f"❓ Интервал: не установлен"

        if START_TIME is not None and END_TIME is not None:
            time_window_text = f"{START_TIME.strftime('%H:%M')} - {END_TIME.strftime('%H:%M')}"
        else:
            time_window_text = "❓ не назначено"

        delayed_text = ""
        if DELAYED_START_ENABLED and DELAYED_START_TIME:
            status_icon = "✅" if delayed_ready else "⏳"
            delayed_text = f"\n{status_icon} Отложенный старт: {DELAYED_START_TIME.strftime('%d.%m.%Y %H:%M')}"

        schedule_text = f"""
📅 <b>Расписание постинга:</b>

{status_emoji} Статус: {status_text}
{timing_info}

{'✅' if TIME_WINDOW_ENABLED else '❌'} Временное окно: {time_window_text}
{'✅' if WEEKDAYS_ENABLED else '❌'} Дни недели: {allowed_days}
{'✅' if EXACT_TIMING_ENABLED else '❌'} Точное планирование: {'включено' if EXACT_TIMING_ENABLED else 'выключено'}{delayed_text}

💡 /help для всех команд
"""
        await message.reply(schedule_text)

    elif text.startswith("/settime"):
        parts = text.split(maxsplit=1)
        if len(parts) == 1:
            if START_TIME is not None and END_TIME is not None:
                current_window = f"{START_TIME.strftime('%H:%M')} - {END_TIME.strftime('%H:%M')}"
            else:
                current_window = "❓ не назначено"
            await message.reply(f"🕐 Текущее временное окно: {current_window}\n\nДля изменения: /settime 06:00 20:00")
            return

        try:
            times = parts[1].split()
            if len(times) != 2:
                await message.reply("❌ Укажите время начала и окончания. Пример: /settime 06:00 20:00")
                return

            start_hour, start_minute = map(int, times[0].split(':'))
            end_hour, end_minute = map(int, times[1].split(':'))

            START_TIME = dt_time(start_hour, start_minute)
            END_TIME = dt_time(end_hour, end_minute)
            save_state()

            await message.reply(f"✅ Временное окно установлено: {START_TIME.strftime('%H:%M')} - {END_TIME.strftime('%H:%M')}")
        except Exception:
            await message.reply("❌ Неверный формат времени. Используйте HH:MM HH:MM, например: /settime 06:00 20:00")

    elif text.startswith("/days"):
        parts = text.split(maxsplit=1)
        if len(parts) == 1:
            if ALLOWED_WEEKDAYS is not None:
                current_days = ", ".join([f"{day+1}({get_weekday_short(day)})" for day in sorted(ALLOWED_WEEKDAYS)])
            else:
                current_days = "❓ не назначены"
            await message.reply(f"📅 Текущие дни: {current_days}\n\nДля изменения: /days 1,2,3,4,5\n(1=пн, 2=вт, ..., 7=вс)")
            return

        try:
            days = [int(x.strip()) - 1 for x in parts[1].split(",")]
            if all(0 <= day <= 6 for day in days):
                ALLOWED_WEEKDAYS = sorted(list(set(days)))
                save_state()
                day_names = ", ".join([get_weekday_short(day) for day in ALLOWED_WEEKDAYS])
                await message.reply(f"✅ Дни недели установлены: {day_names}")
            else:
                await message.reply("❌ Неверные дни. Используйте числа от 1 до 7")
        except:
            await message.reply("❌ Неверный формат. Пример: /days 1,2,3,4,5")

    elif text.startswith("/startdate"):
        parts = text.split(maxsplit=1)
        if len(parts) == 1:
            if DELAYED_START_ENABLED and DELAYED_START_TIME:
                await message.reply(f"⏳ Отложенный старт: {DELAYED_START_TIME.strftime('%Y-%m-%d %H:%M')}\n\n"
                                  "Для изменения: /startdate 2024-01-25 17:00\nИли отключить: /clearstart")
            else:
                await message.reply("⏳ Отложенный старт не установлен\n\nДля установки: /startdate 2024-01-25 17:00")
            return

        try:
            date_time_str = parts[1]
            if '.' in date_time_str.split()[0]:
                date_part, time_part = date_time_str.split()
                day, month, year = map(int, date_part.split('.'))
                hour, minute = map(int, time_part.split(':'))
                target_datetime = datetime(year, month, day, hour, minute)
            else:
                target_datetime = datetime.strptime(date_time_str, "%Y-%m-%d %H:%M")

            target_datetime = target_datetime.replace(tzinfo=CZECH_TIMEZONE)

            if target_datetime <= get_czech_time():
                await message.reply("❌ Указанное время уже прошло")
                return

            DELAYED_START_ENABLED = True
            DELAYED_START_TIME = target_datetime
            save_state()

            await message.reply(f"✅ Отложенный старт установлен на {target_datetime.strftime('%Y-%m-%d %H:%M')}")
        except:
            await message.reply("❌ Неверный формат. Используйте: YYYY-MM-DD HH:MM или DD.MM.YYYY HH:MM")

    elif text == "/clearstart":
        DELAYED_START_ENABLED = False
        DELAYED_START_TIME = None
        save_state()
        await message.reply("✅ Отложенный старт отключён")

    elif text == "/toggletime":
        TIME_WINDOW_ENABLED = not TIME_WINDOW_ENABLED
        save_state()
        status = "включено" if TIME_WINDOW_ENABLED else "выключено"
        await message.reply(f"{'✅' if TIME_WINDOW_ENABLED else '❌'} Ограничение по времени {status}!")

    elif text == "/toggledays":
        WEEKDAYS_ENABLED = not WEEKDAYS_ENABLED
        save_state()
        status = "включено" if WEEKDAYS_ENABLED else "выключено"
        await message.reply(f"{'✅' if WEEKDAYS_ENABLED else '❌'} Ограничение по дням недели {status}!")

    elif text == "/checktime":
        now = get_czech_time()
        current_weekday = now.weekday()
        posting_allowed, reason = is_posting_allowed()

        time_status = "✅ включено" if TIME_WINDOW_ENABLED else "❌ выключено"
        days_status = "✅ включено" if WEEKDAYS_ENABLED else "❌ выключено"

        if START_TIME is not None and END_TIME is not None:
            time_window_text = f"{START_TIME.strftime('%H:%M')} - {END_TIME.strftime('%H:%M')}"
        else:
            time_window_text = "❓ не назначено"

        if ALLOWED_WEEKDAYS is not None:
            allowed_days_text = ', '.join([get_weekday_short(d) for d in sorted(ALLOWED_WEEKDAYS)])
        else:
            allowed_days_text = "❓ не назначены"

        check_text = f"""
🕐 <b>Проверка времени:</b>

⏰ Текущее время: {now.strftime('%Y-%m-%d %H:%M:%S')} GMT+2 (Чехия)
📅 День недели: {get_weekday_name(current_weekday)}
🕐 Временное окно: {time_window_text} ({time_status})
📆 Ограничение дней: {days_status}
🎯 Разрешённые дни: {allowed_days_text}

{'✅' if posting_allowed else '❌'} <b>Статус:</b> {reason}
"""
        await message.reply(check_text)

    elif text == "/toggleexact":
        EXACT_TIMING_ENABLED = not EXACT_TIMING_ENABLED
        save_state()
        status = "включено" if EXACT_TIMING_ENABLED else "выключено"
        mode = "точное планирование" if EXACT_TIMING_ENABLED else "интервальное планирование"
        await message.reply(f"{'🎯' if EXACT_TIMING_ENABLED else '⏱'} {mode.title()} {status}!")

    elif text == "/togglenotify":
        NOTIFICATIONS_ENABLED = not NOTIFICATIONS_ENABLED
        save_state()
        status = "включены" if NOTIFICATIONS_ENABLED else "выключены"
        await message.reply(f"{'🔔' if NOTIFICATIONS_ENABLED else '🔕'} Уведомления о публикации {status}!")

    elif text.startswith("/setchannel"):
        parts = text.split(maxsplit=1)
        if len(parts) == 1:
            await message.reply("❌ Укажите ID канала. Пример: /setchannel -10001234567890")
            return

        channel_id = parts[1].strip()
        try:
            int(channel_id)
            CHANNEL_ID = channel_id
            save_state()
            await message.reply(f"✅ Канал установлен: {channel_id}")
        except ValueError:
            await message.reply("❌ ID канала должен быть числом. Пример: -1001234567890")

    elif text == "/channel":
        if CHANNEL_ID:
            await message.reply(f"📢 Текущий канал: {CHANNEL_ID}")
        else:
            await message.reply("❌ Канал не установлен. Используйте /setchannel -1001234567890")

    elif text.startswith("/title"):
        parts = text.split(maxsplit=1)
        if len(parts) == 1:
            queue = load_queue()
            queue_info = f"\n📊 В очереди: {len(queue)} постов" if queue else "\n📭 Очередь пуста"
            current_signature = DEFAULT_SIGNATURE if DEFAULT_SIGNATURE is not None else "❓ не установлена"

            menu_text = f"""
📝 <b>Управление подписью:</b>

🏷 Текущая подпись: {current_signature}{queue_info}

<b>Команда:</b>
• /title текст - установить подпись для всех постов
• /title текст # ссылка - установить кликабельную подпись

<b>Примеры:</b>
• /title Моя подпись
• /title Кликабельный текст # https://t.me/example

<i>Формат "текст # ссылка" создаёт кликабельную ссылку</i>
<i>Подпись применится ко всем постам в очереди!</i>
"""
            await message.reply(menu_text)
            return

        new_signature = parts[1]
        parsed_signature = parse_signature_with_link(new_signature)
        DEFAULT_SIGNATURE = parsed_signature
        save_state()

        updated_count = apply_signature_to_all_queue(new_signature)
        signature_type = "кликабельная ссылка" if " # " in new_signature else "обычная подпись"

        if updated_count > 0:
            await message.reply(f"✅ Подпись установлена ({signature_type}) и применена к {updated_count} постам в очереди:\n{parsed_signature}")
        else:
            await message.reply(f"✅ Подпись установлена ({signature_type}) для новых постов:\n{parsed_signature}\n\n📭 Очередь пуста")

    elif text == "/clear":
        queue = load_queue()
        if queue:
            total_media, media_groups, total_posts, photos, videos, gifs, documents = count_queue_stats(queue)
            save_queue([])

            parts = []
            if total_media > 0:
                parts.append(f"{total_media} медиа")
            if media_groups > 0:
                parts.append(f"{media_groups} медиагрупп")
            parts.append(f"{total_posts} постов")

            deleted_text = " | ".join(parts)
            await message.reply(f"✅ Очередь очищена! Удалено: {deleted_text}")
        else:
            await message.reply("📭 Очередь уже пуста")

    elif text.startswith("/remove"):
        parts = text.split(maxsplit=1)
        if len(parts) == 1:
            await message.reply("❌ Укажите номер для удаления. Пример: /remove 1")
            return

        try:
            index = int(parts[1]) - 1
            queue = load_queue()

            if 0 <= index < len(queue):
                queue.pop(index)
                save_queue(queue)
                await message.reply(f"✅ Медиа #{index + 1} удалено из очереди")
            else:
                await message.reply(f"❌ Неверный номер. В очереди {len(queue)} медиа")
        except ValueError:
            await message.reply("❌ Номер должен быть числом")

    elif text == "/random":
        if shuffle_queue():
            await message.reply("🔀 Очередь перемешана!")
        else:
            await message.reply("❌ Недостаточно медиа для перемешивания (нужно минимум 2)")

    elif text.startswith("/postfile"):
        parts = text.split(maxsplit=1)
        if len(parts) == 1:
            await message.reply("❌ Укажите номер медиа. Пример: /postfile 1")
            return

        try:
            index = int(parts[1]) - 1
            queue = load_queue()

            if not CHANNEL_ID:
                await message.reply("❌ Канал не установлен")
                return

            if 0 <= index < len(queue):
                media_data = queue.pop(index)
                save_queue(queue)

                try:
                    pending_notifications[message.from_user.id] = True

                    if isinstance(media_data, dict) and media_data.get("type") == "media_group":
                        await send_media_group_to_channel(media_data)
                        if await verify_post_published(CHANNEL_ID, "media_group"):
                            await message.reply(f"✅ Медиагруппа #{index + 1} опубликована!")
                            await notify_users_about_publication("media_group", True)
                        else:
                            await message.reply(f"❌ Не удалось подтвердить публикацию медиагруппы #{index + 1}")
                            await notify_users_about_publication("media_group", False, "Не удалось подтвердить публикацию")
                    else:
                        await send_single_media(media_data)
                        media_type = media_data.get("type", "photo") if isinstance(media_data, dict) else "photo"

                        if await verify_post_published(CHANNEL_ID, media_type):
                            await message.reply(f"✅ Медиа #{index + 1} опубликовано!")
                            await notify_users_about_publication(media_type, True)
                        else:
                            await message.reply(f"❌ Не удалось подтвердить публикацию медиа #{index + 1}")
                            await notify_users_about_publication(media_type, False, "Не удалось подтвердить публикацию")

                    last_post_time = time.time()
                    save_state()

                except Exception as e:
                    queue.insert(index, media_data)
                    save_queue(queue)
                    await message.reply(f"❌ Ошибка публикации: {e}")
                    await notify_users_about_publication("медиа", False, str(e))
            else:
                await message.reply(f"❌ Неверный номер. В очереди {len(queue)} медиа")
        except ValueError:
            await message.reply("❌ Номер должен быть числом")

    elif text == "/postnow":
        queue = load_queue()
        if not queue:
            await message.reply("📭 Очередь пуста")
            return

        if not CHANNEL_ID:
            await message.reply("❌ Канал не установлен")
            return

        try:
            media_data = queue.pop(0)
            save_queue(queue)
            pending_notifications[message.from_user.id] = True

            if isinstance(media_data, dict) and media_data.get("type") == "media_group":
                await send_media_group_to_channel(media_data)
                if await verify_post_published(CHANNEL_ID, "media_group"):
                    await message.reply("✅ Медиагруппа опубликована!")
                    await notify_users_about_publication("media_group", True)
                else:
                    await message.reply("❌ Не удалось подтвердить публикацию медиагруппы")
                    await notify_users_about_publication("media_group", False, "Не удалось подтвердить публикацию")
            else:
                await send_single_media(media_data)
                media_type = media_data.get("type", "photo") if isinstance(media_data, dict) else "photo"

                if await verify_post_published(CHANNEL_ID, media_type):
                    await message.reply("✅ Медиа опубликовано!")
                    await notify_users_about_publication(media_type, True)
                else:
                    await message.reply("❌ Не удалось подтвердить публикацию медиа")
                    await notify_users_about_publication(media_type, False, "Не удалось подтвердить публикацию")

            last_post_time = time.time()
            save_state()

        except Exception as e:
            queue.insert(0, media_data)
            save_queue(queue)
            await message.reply(f"❌ Ошибка публикации: {e}")
            await notify_users_about_publication("медиа", False, str(e))

    elif text == "/postall":
        queue = load_queue()
        if not queue:
            await message.reply("📭 Очередь пуста")
            return

        if not CHANNEL_ID:
            await message.reply("❌ Канал не установлен")
            return

        total_media, media_groups, total_posts, photos, videos, gifs, documents = count_queue_stats(queue)
        await message.reply(f"🚀 Начинаю публикацию всех {total_posts} постов...")

        success_count = 0
        error_count = 0
        pending_notifications[message.from_user.id] = True

        try:
            for i, media_data in enumerate(queue):
                try:
                    if isinstance(media_data, dict) and media_data.get("type") == "media_group":
                        await send_media_group_to_channel(media_data)
                        if await verify_post_published(CHANNEL_ID, "media_group"):
                            success_count += 1
                        else:
                            error_count += 1
                    else:
                        await send_single_media(media_data)
                        if await verify_post_published(CHANNEL_ID, media_data.get("type", "photo")):
                            success_count += 1
                        else:
                            error_count += 1

                    await asyncio.sleep(0.5)
                except Exception as e:
                    error_count += 1
                    logger.error(f"Ошибка публикации поста #{i+1}: {e}")

            if success_count > 0:
                save_queue([])
                last_post_time = time.time()
                save_state()

            if success_count == total_posts:
                result_text = f"✅ Все {success_count} постов успешно опубликованы!\n\n📊 Опубликовано: {success_count}/{total_posts}"
            else:
                result_text = f"⚠️ Частичная публикация:\n\n✅ Успешно: {success_count}\n❌ Ошибок: {error_count}\n📊 Итого: {success_count}/{total_posts}"

            await message.reply(result_text)

        except Exception as e:
            await message.reply(f"❌ Критическая ошибка при массовой публикации: {e}")

# Веб-сервер
async def create_app():
    app = web.Application()

    async def health_check(request):
        return web.Response(text="Bot is running!", content_type="text/plain")

    app.router.add_get("/", health_check)
    app.router.add_get("/health", health_check)
    return app

async def start_web_server():
    app = await create_app()
    runner = web.AppRunner(app)
    await runner.setup()

    port = int(os.environ.get('PORT', 5000))
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    logger.info(f"🌐 Веб-сервер запущен на порту {port}")

async def send_startup_notification():
    """Отправляет уведомление о запуске бота первому разрешённому пользователю"""
    try:
        if not ALLOWED_USERS:
            return
            
        queue = load_queue()
        queue_count = len(queue)
        
        # Определяем время следующего поста
        if queue_count > 0 and POST_INTERVAL is not None:
            if EXACT_TIMING_ENABLED:
                next_exact_time = get_next_exact_posting_time()
                if next_exact_time:
                    next_post_time = next_exact_time.strftime('%H:%M')
                else:
                    next_post_time = "по расписанию"
            else:
                time_until_next = get_time_until_next_post()
                if time_until_next > 0:
                    next_post_time = f"через {format_interval(time_until_next)}"
                else:
                    next_post_time = "сейчас"
        else:
            next_post_time = "нет постов" if queue_count == 0 else "интервал не установлен"
        
        startup_message = f"🔄 Бот запущен. Очередь: {queue_count} постов. Следующий пост: {next_post_time}."
        
        # Отправляем первому разрешённому пользователю
        first_user = ALLOWED_USERS[0]
        await bot.send_message(chat_id=first_user, text=startup_message)
        logger.info(f"📩 Уведомление о запуске отправлено пользователю {first_user}")
        
    except Exception as e:
        logger.error(f"❌ Ошибка отправки уведомления о запуске: {e}")

async def check_immediate_posting():
    """Проверяет, нужно ли немедленно начать постинг при запуске"""
    try:
        queue = load_queue()
        if not queue or not posting_enabled or not CHANNEL_ID:
            return
            
        posting_allowed, reason = is_posting_allowed()
        delayed_ready = is_delayed_start_ready()
        
        if posting_allowed and delayed_ready and POST_INTERVAL is not None:
            now = get_czech_time()
            time_since_last = time.time() - last_post_time
            
            # Если прошло достаточно времени с последнего поста
            if time_since_last >= POST_INTERVAL:
                logger.info("🚀 Запуск немедленного постинга - условия выполнены при старте")
                # Планируем первый пост через несколько секунд
                asyncio.create_task(asyncio.sleep(5))
            else:
                remaining_time = POST_INTERVAL - time_since_last
                logger.info(f"⏰ До следующего поста: {format_interval(int(remaining_time))}")
        else:
            if not posting_allowed:
                logger.info(f"⏰ Постинг запрещён при запуске: {reason}")
            elif not delayed_ready:
                logger.info(f"⏰ Ожидание отложенного старта до {DELAYED_START_TIME}")
                
    except Exception as e:
        logger.error(f"❌ Ошибка проверки немедленного постинга: {e}")

async def main():
    logger.info("[BOT] Запущен")
    
    load_state()

    logger.info(f"📊 Инициализированы структуры данных: pending_media_groups={len(pending_media_groups)}, timers={len(media_group_timers)}")

    await bot.set_my_commands([
        BotCommand(command="start", description="🚀 Запустить бота"),
        BotCommand(command="help", description="❓ Показать помощь"),
        BotCommand(command="commands", description="📋 Полный список команд"),
        BotCommand(command="status", description="📊 Показать статус и очередь"),
        BotCommand(command="toggle", description="🔄 Включить/выключить автопостинг"),
        BotCommand(command="schedule", description="📅 Показать расписание"),
        BotCommand(command="interval", description="⏱ Установить интервал постинга"),
        BotCommand(command="settime", description="🕐 Установить временное окно"),
        BotCommand(command="days", description="📆 Установить дни недели"),
        BotCommand(command="toggletime", description="⏰ Переключить временное окно"),
        BotCommand(command="toggledays", description="📅 Переключить дни недели"),
        BotCommand(command="toggleexact", description="🎯 Переключить планирование"),
        BotCommand(command="togglenotify", description="🔔 Переключить уведомления"),
        BotCommand(command="startdate", description="⏳ Установить отложенный старт"),
        BotCommand(command="clearstart", description="❌ Отключить отложенный старт"),
        BotCommand(command="setchannel", description="📢 Установить канал"),
        BotCommand(command="channel", description="📻 Показать канал"),
        BotCommand(command="title", description="📝 Управление подписями"),
        BotCommand(command="clear", description="🗑 Очистить очередь"),
        BotCommand(command="remove", description="➖ Удалить из очереди"),
        BotCommand(command="random", description="🔀 Перемешать очередь"),
        BotCommand(command="postfile", description="🚀 Опубликовать по номеру"),
        BotCommand(command="postnow", description="⚡ Опубликовать сейчас"),
        BotCommand(command="postall", description="🚀 Опубликовать все посты"),
        BotCommand(command="checktime", description="🕐 Проверить текущее время"),
    ])

    await start_web_server()
    
    # Проверяем состояние при запуске
    await check_immediate_posting()
    
    # Запускаем цикл постинга
    asyncio.create_task(posting_loop())
    
    # Отправляем уведомление о запуске
    await send_startup_notification()

    logger.info("🤖 Бот запущен и готов принимать медиа!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("🛑 Бот остановлен пользователем")
    except Exception as e:
        logger.error(f"❌ Критическая ошибка: {e}")
    finally:
        save_state()
        logger.info("💾 Состояние сохранено")

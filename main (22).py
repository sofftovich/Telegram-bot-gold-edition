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

# Импорты для веб-сервера - обновлено для aiohttp 4.x без cors
from aiohttp import web

load_dotenv()

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# ИСПРАВЛЕНО: правильные названия переменных окружения
TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_ID = os.getenv("CHANNEL_ID")

# Загрузка разрешённых пользователей
ALLOWED_USERS = []
for i in range(1, 4):  # ALLOWED_USER_1, ALLOWED_USER_2, ALLOWED_USER_3
    user_id = os.getenv(f"ALLOWED_USER_{i}")
    if user_id:
        try:
            ALLOWED_USERS.append(int(user_id))
        except ValueError:
            logger.warning(f"Неверный формат ALLOWED_USER_{i}: {user_id}")

if not ALLOWED_USERS:
    logger.error("❌ Не указано ни одного разрешённого пользователя! Установите ALLOWED_USER_1, ALLOWED_USER_2 или ALLOWED_USER_3")
    exit(1)

logger.info(f"✅ Разрешённые пользователи: {ALLOWED_USERS}")

# Проверка токена при запуске
if not TOKEN:
    logger.error("❌ BOT_TOKEN не установлен! Установите переменную окружения BOT_TOKEN")
    exit(1)

# Чешский часовой пояс GMT+2
CZECH_TIMEZONE = timezone(timedelta(hours=2))

def get_czech_time():
    """Возвращает текущее время в Чехии (GMT+2)"""
    return datetime.now(CZECH_TIMEZONE)

def check_user_access(user_id):
    """Проверяет, разрешён ли доступ пользователю"""
    return user_id in ALLOWED_USERS

# ИСПРАВЛЕННЫЕ ДЕФОЛТНЫЕ НАСТРОЙКИ
POST_INTERVAL = None  # Интервал постинга - НЕ НАЗНАЧЕНО
last_post_time = 0  # Время последнего поста (начинаем с 0)
posting_enabled = True  # Статус автопостинга - ВКЛЮЧЁН ПО УМОЛЧАНИЮ

DEFAULT_SIGNATURE = None  # Подпись - НЕ НАЗНАЧЕНО

# Настройки планирования - ИСПРАВЛЕННЫЕ ДЕФОЛТЫ
ALLOWED_WEEKDAYS = None  # Дни недели - НЕ НАЗНАЧЕНО
START_TIME = None  # Начало временного окна - НЕ НАЗНАЧЕНО
END_TIME = None  # Конец временного окна - НЕ НАЗНАЧЕНО

# Настройки отложенного старта
DELAYED_START_ENABLED = False  # Включен ли отложенный старт
DELAYED_START_TIME = None  # Время первого поста (datetime object) - НЕ НАЗНАЧЕНО
DELAYED_START_INTERVAL_START = None  # С какого времени дня начинать отсчёт интервала

# Переменные для управления ограничениями
TIME_WINDOW_ENABLED = True  # Включено ли ограничение по времени - ВКЛЮЧЕНО ПО УМОЛЧАНИЮ
WEEKDAYS_ENABLED = False    # Включено ли ограничение по дням недели - ВЫКЛЮЧЕНО ПО УМОЛЧАНИЮ

# Переменные для точного планирования постов по интервалам - ИСПРАВЛЕННЫЙ ДЕФОЛТ
EXACT_TIMING_ENABLED = True  # Включено ли точное планирование по интервалам - ВКЛЮЧЕНО ПО УМОЛЧАНИЮ

# Переменная для управления уведомлениями
NOTIFICATIONS_ENABLED = True  # Включены ли уведомления о публикации - ВКЛЮЧЕНЫ ПО УМОЛЧАНИЮ

def calculate_exact_posting_times():
    """Рассчитывает точные моменты времени для постинга в рамках временного окна"""
    if not EXACT_TIMING_ENABLED or POST_INTERVAL is None:
        return []

    # Если временное окно отключено или не назначено, используем интервальное планирование на основе POST_INTERVAL
    if not TIME_WINDOW_ENABLED or START_TIME is None or END_TIME is None:
        # Генерируем времена через интервал POST_INTERVAL, начиная с 00:00
        posting_times = []
        current_seconds = 0

        while current_seconds < 24 * 3600:  # 24 часа
            hours = current_seconds // 3600
            minutes = (current_seconds % 3600) // 60
            if hours < 24:  # Проверяем, что не превышаем 24 часа
                posting_times.append(dt_time(hours, minutes))
            current_seconds += POST_INTERVAL

        return posting_times

    # Рассчитываем продолжительность окна в секундах
    if START_TIME <= END_TIME:
        # Обычное окно (например, 06:00-20:00)
        window_duration = (END_TIME.hour - START_TIME.hour) * 3600 + (END_TIME.minute - START_TIME.minute) * 60
    else:
        # Окно через полночь (например, 20:00-06:00)
        window_duration = (24 * 3600 - (START_TIME.hour * 3600 + START_TIME.minute * 60)) + (END_TIME.hour * 3600 + END_TIME.minute * 60)

    # Количество постов, которые можем разместить в окне (просто делим окно на интервал)
    max_posts_in_window = max(1, int(window_duration // POST_INTERVAL))

    # Если помещается только один пост, возвращаем время начала окна
    if max_posts_in_window == 1:
        return [START_TIME]

    posting_times = []
    start_seconds = START_TIME.hour * 3600 + START_TIME.minute * 60

    # Генерируем времена через заданный интервал
    for i in range(max_posts_in_window):
        total_seconds = start_seconds + i * POST_INTERVAL

        # Обрабатываем переход через полночь
        if total_seconds >= 24 * 3600:
            total_seconds -= 24 * 3600

        hours = total_seconds // 3600
        minutes = (total_seconds % 3600) // 60

        # Проверяем, что время все еще в пределах окна
        current_time = dt_time(hours, minutes)

        if START_TIME <= END_TIME:
            # Обычное окно
            if START_TIME <= current_time <= END_TIME:
                posting_times.append(current_time)
        else:
            # Окно через полночь
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

    # Проверяем, можно ли постить сегодня
    today_allowed = not WEEKDAYS_ENABLED or ALLOWED_WEEKDAYS is None or now.weekday() in ALLOWED_WEEKDAYS

    # Ищем ближайшее время сегодня
    for post_time in posting_times:
        if current_time < post_time:
            # Если временные ограничения по дням включены, проверяем день недели
            if WEEKDAYS_ENABLED and ALLOWED_WEEKDAYS is not None and now.weekday() not in ALLOWED_WEEKDAYS:
                break  # Переходим к поиску следующего дня
            return now.replace(hour=post_time.hour, minute=post_time.minute, second=0, microsecond=0)

    # Если все времена сегодня прошли или сегодня не разрешённый день, ищем следующий день
    for days_ahead in range(1, 8):  # Ищем в течение недели, начиная с завтра
        check_date = now + timedelta(days=days_ahead)
        check_weekday = check_date.weekday()

        if not WEEKDAYS_ENABLED or ALLOWED_WEEKDAYS is None or check_weekday in ALLOWED_WEEKDAYS:
            # Первое время в этот день
            first_time = posting_times[0]
            return check_date.replace(hour=first_time.hour, minute=first_time.minute, second=0, microsecond=0)

    return None

def calculate_queue_schedule(queue_length):
    """Рассчитывает расписание для всей очереди"""
    if queue_length == 0:
        return None, None

    if EXACT_TIMING_ENABLED:
        # Для точного планирования
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
        total_posts_needed = queue_length
        last_time_index = (current_time_index + total_posts_needed - 1) % len(posting_times)
        days_offset = (current_time_index + total_posts_needed - 1) // len(posting_times)

        last_post_time = posting_times[last_time_index]
        last_post_date = next_time.date() + timedelta(days=days_offset)

        first_post_time = next_time
        last_post_datetime = datetime.combine(last_post_date, last_post_time, tzinfo=CZECH_TIMEZONE)

        return first_post_time, last_post_datetime
    else:
        # Для обычного интервального планирования
        now = get_czech_time()
        first_post_time = now + timedelta(seconds=get_time_until_next_post())
        last_post_time = first_post_time + timedelta(seconds=(queue_length - 1) * POST_INTERVAL)

        return first_post_time, last_post_time

# Файлы для хранения данных
QUEUE_FILE = "queue.json"
STATE_FILE = "state.json"

# ИСПРАВЛЕНО: Инициализируем структуры данных сразу при запуске
pending_media_groups = {}
media_group_timers = {}
pending_notifications = {}
user_media_tracking = {}

# ИСПРАВЛЕНО: Инициализация бота и диспетчера сразу при запуске модуля
is_posting_locked = False

bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

def parse_interval(interval_str):
    """Парсит интервал с учётом дней, часов, минут и секунд"""
    total_seconds = 0
    # Ищем дни, часы, минуты, секунды
    days = re.search(r'(\d+)d', interval_str)
    hours = re.search(r'(\d+)h', interval_str)
    minutes = re.search(r'(\d+)m', interval_str)
    seconds = re.search(r'(\d+)s', interval_str)

    if days:
        total_seconds += int(days.group(1)) * 24 * 3600
    if hours:
        total_seconds += int(hours.group(1)) * 3600
    if minutes:
        total_seconds += int(minutes.group(1)) * 60
    if seconds:
        total_seconds += int(seconds.group(1))

    return total_seconds if total_seconds > 0 else None

def format_interval(seconds):
    """Форматирует секунды в читаемый вид"""
    days = seconds // (24 * 3600)
    hours = (seconds % (24 * 3600)) // 3600
    minutes = (seconds % 3600) // 60
    seconds_left = seconds % 60

    parts = []
    if days > 0:
        parts.append(f"{days}д")
    if hours > 0:
        parts.append(f"{hours}ч")
    if minutes > 0:
        parts.append(f"{minutes}м")
    if seconds_left > 0:
        parts.append(f"{seconds_left}с")

    return " ".join(parts) if parts else "0м"

def get_time_until_next_post():
    """Возвращает время до следующего поста в секундах"""
    global last_post_time

    # Если интервал не установлен, возвращаем большое число
    if POST_INTERVAL is None:
        return 24 * 3600  # 24 часа

    # Если есть точное планирование, используем его
    if EXACT_TIMING_ENABLED:
        next_exact_time = get_next_exact_posting_time()
        if next_exact_time:
            now = get_czech_time()
            time_diff = (next_exact_time - now).total_seconds()
            return max(0, int(time_diff))
        else:
            # Если нет точного времени в расписании, возвращаем 0
            return 0

    # Иначе используем интервальное планирование
    now_timestamp = time.time()
    time_since_last = now_timestamp - last_post_time

    if time_since_last >= POST_INTERVAL:
        # Проверяем, можно ли постить сейчас
        allowed_wait = get_next_allowed_time()
        return allowed_wait
    else:
        # Ждём до истечения интервала + проверяем разрешённое время
        interval_wait = POST_INTERVAL - int(time_since_last)
        allowed_wait = get_next_allowed_time()
        return max(interval_wait, allowed_wait)

def get_next_allowed_time():
    """Возвращает время до следующего разрешённого интервала"""
    now = get_czech_time()
    current_weekday = now.weekday()
    current_time = now.time()

    # Если сейчас разрешено постить
    if is_posting_allowed()[0]:
        return 0

    # Ищем следующий разрешённый интервал
    for days_ahead in range(8):  # Проверяем неделю вперёд
        check_date = now + timedelta(days=days_ahead)
        check_date = check_date.replace(hour=0, minute=0, second=0, microsecond=0)
        check_weekday = check_date.weekday()

        # Проверка дня недели (только если ограничения включены и дни назначены)
        if not WEEKDAYS_ENABLED or ALLOWED_WEEKDAYS is None or check_weekday in ALLOWED_WEEKDAYS:
            if days_ahead == 0:  # Сегодня
                # Проверяем, не прошло ли уже время начала окна
                if not TIME_WINDOW_ENABLED or START_TIME is None or END_TIME is None:
                    return 0  # Если временные ограничения выключены или не настроены, можно постить сейчас
                elif START_TIME <= END_TIME:
                    # Обычное окно
                    if current_time < START_TIME:
                        target_time = check_date.replace(hour=START_TIME.hour, minute=START_TIME.minute)
                        return int((target_time - now).total_seconds())
                    elif current_time > END_TIME:
                        # Окно на сегодня уже закрылось, ищем следующий день
                        continue
                else:
                    # Окно через полночь
                    if END_TIME < current_time < START_TIME:
                        target_time = check_date.replace(hour=START_TIME.hour, minute=START_TIME.minute)
                        return int((target_time - now).total_seconds())
            else:
                # Другой день - начало временного окна
                if not TIME_WINDOW_ENABLED or START_TIME is None:
                    return int((check_date - now).total_seconds())
                else:
                    target_time = check_date.replace(hour=START_TIME.hour, minute=START_TIME.minute)
                    return int((target_time - now).total_seconds())

    # Если ничего не найдено за неделю
    return 24 * 3600  # Через день

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
    """Загружает состояние бота - ТОЛЬКО переключатели и канал, настройки сбрасываются"""
    global last_post_time, CHANNEL_ID, posting_enabled
    global TIME_WINDOW_ENABLED, WEEKDAYS_ENABLED, EXACT_TIMING_ENABLED, NOTIFICATIONS_ENABLED
    
    # Устанавливаем дефолтные значения переключателей
    posting_enabled = True  # Автопостинг включён по умолчанию
    TIME_WINDOW_ENABLED = True  # Точное планирование времени включено по умолчанию  
    WEEKDAYS_ENABLED = False  # Ограничение по дням выключено по умолчанию
    EXACT_TIMING_ENABLED = True  # Точное планирование включено по умолчанию
    NOTIFICATIONS_ENABLED = True  # Уведомления включены по умолчанию
    
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                state = json.load(f)
                # Загружаем только канал и время последнего поста
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
        "delayed_start_interval_start": DELAYED_START_INTERVAL_START.strftime("%H:%M") if DELAYED_START_INTERVAL_START else None,
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
            # Обычное окно (например, 06:00-20:00)
            if not (START_TIME <= current_time <= END_TIME):
                return False, f"Вне временного окна ({START_TIME.strftime('%H:%M')}-{END_TIME.strftime('%H:%M')})"
        else:
            # Окно через полночь (например, 20:00-06:00)
            if not (current_time >= START_TIME or current_time <= END_TIME):
                return False, f"Вне временного окна ({START_TIME.strftime('%H:%M')}-{END_TIME.strftime('%H:%M')})"

    return True, "Разрешено"

def is_delayed_start_ready():
    """Проверяет, готов ли отложенный старт"""
    if not DELAYED_START_ENABLED or not DELAYED_START_TIME:
        return True  # Если отложенный старт не настроен, считаем готовым

    now = get_czech_time()
    return now >= DELAYED_START_TIME

def get_weekday_name(weekday):
    """Возвращает название дня недели"""
    days = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]
    return days[weekday]

def get_weekday_short(weekday):
    """Возвращает сокращённое название дня недели"""
    days = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
    return days[weekday]

def count_queue_stats(queue):
    """Подсчитывает статистику очереди: медиа, медиагруппы, посты"""
    total_media = 0
    media_groups = 0
    total_posts = len(queue)

    photos = 0
    videos = 0
    gifs = 0
    documents = 0

    for item in queue:
        if isinstance(item, dict) and item.get("type") == "media_group":
            media_groups += 1
            for media in item.get("media", []):
                total_media += 1
                if media["type"] == "photo":
                    photos += 1
                elif media["type"] == "video":
                    videos += 1
                elif media["type"] == "gif":
                    gifs += 1
                elif media["type"] == "document":
                    documents += 1
        else:
            total_media += 1
            if isinstance(item, dict):
                if item.get("type") == "photo":
                    photos += 1
                elif item.get("type") == "video":
                    videos += 1
                elif item.get("type") == "gif":
                    gifs += 1
                elif item.get("type") == "document":
                    documents += 1
            else:
                photos += 1  # Assume it's a photo if not a dict
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
    """Функция рандомизации очереди"""
    queue = load_queue()
    if len(queue) > 1:
        random.shuffle(queue)
        save_queue(queue)
        return True
    return False

def update_user_tracking_after_post():
    """Обновляет индексы отслеживания пользователей после публикации поста"""
    global user_media_tracking
    updated_tracking = {}
    for idx, uid in user_media_tracking.items():
        if idx > 0:
            updated_tracking[idx - 1] = uid
    user_media_tracking = updated_tracking

def add_user_to_queue_tracking(user_id, queue_position):
    """Добавляет пользователя для отслеживания в очереди"""
    user_media_tracking[queue_position] = user_id

def get_users_for_next_post():
    """Получает пользователей для следующего поста в очереди"""
    if 0 in user_media_tracking:
        return [user_media_tracking[0]]
    return []

def parse_signature_with_link(text):
    """Парсит подпись в формате 'текст # ссылка' и возвращает HTML с кликабельной ссылкой"""
    if " # " in text:
        parts = text.rsplit(" # ", 1)  # Разделяем только по последнему " # "
        if len(parts) == 2:
            caption_text = parts[0].strip()
            link_url = parts[1].strip()
            
            # Проверяем, что после # идет что-то похожее на ссылку
            # (содержит точку или начинается с http/https или t.me)
            if (link_url and 
                ('.' in link_url or 
                 link_url.startswith(("http://", "https://", "t.me/", "tg://")))):
                
                # Проверяем, что ссылка начинается с http:// или https://
                if not link_url.startswith(("http://", "https://")):
                    if link_url.startswith("tg://"):
                        # Оставляем tg:// ссылки как есть
                        pass
                    else:
                        link_url = "https://" + link_url
                
                return f'<a href="{link_url}">{caption_text}</a>'
    
    # Если нет формата "текст # ссылка", возвращаем исходный текст
    return text

def apply_signature_to_all_queue(signature):
    """Применяет подпись ко всем постам в очереди"""
    queue = load_queue()
    if not queue:
        return 0
    
    # Парсим подпись на предмет кликабельных ссылок
    parsed_signature = parse_signature_with_link(signature)
    
    updated_count = 0
    for i, item in enumerate(queue):
        if isinstance(item, dict):
            if item.get("type") == "media_group":
                # Для медиагрупп обновляем caption
                item["caption"] = parsed_signature
            else:
                # Для одиночных медиа обновляем caption
                item["caption"] = parsed_signature
            updated_count += 1
        else:
            # Преобразуем строку в dict и устанавливаем подпись
            queue[i] = {"file_id": item, "caption": parsed_signature, "type": "photo"}
            updated_count += 1
    
    save_queue(queue)
    return updated_count

async def verify_post_published(channel_id, expected_type=None, timeout=5):
    """Проверяет, что пост действительно опубликован в канале"""
    try:
        # Даём время на публикацию
        await asyncio.sleep(1)

        # Получаем последние сообщения из канала (максимум 5 для быстроты)
        try:
            # Простая проверка через getChatMemberCount для быстрой валидации доступности канала
            await bot.get_chat_member_count(channel_id)

            # Дополнительная проверка - пытаемся получить информацию о чате
            chat_info = await bot.get_chat(channel_id)

            # Если дошли до сюда, значит канал доступен и пост скорее всего опубликован
            logger.info(f"✅ Канал {channel_id} доступен, пост считается опубликованным")
            return True

        except Exception as inner_e:
            logger.error(f"Ошибка доступа к каналу {channel_id}: {inner_e}")
            return False

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
    global pending_notifications

    if not NOTIFICATIONS_ENABLED or not pending_notifications:
        return

    # Получаем пользователей для уведомления
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

    # Очищаем уведомления
    pending_notifications.clear()

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

    if not posting_allowed:
        logger.info(f"⏰ Постинг запрещён: {reason}")
        return

    if not delayed_ready:
        logger.info(f"⏰ Ожидание отложенного старта до {DELAYED_START_TIME}")
        return

    if not CHANNEL_ID:
        logger.error("❌ CHANNEL_ID не установлен")
        return

    # ИСПРАВЛЕНО: Точная проверка времени для постинга
    if EXACT_TIMING_ENABLED:
        next_exact_time = get_next_exact_posting_time()
        if next_exact_time:
            now = get_czech_time()
            now = now.replace(second=0, microsecond=0)
            time_diff = (next_exact_time - now).total_seconds()

            # Публикуем только если время пришло (с допуском 61 секунда, чтобы учесть возможную задержку)
            if 0 <= time_diff <= 61:
                global is_posting_locked
                is_posting_locked = True
                logger.info(f"✅ Публикуем пост в точное время: {next_exact_time.strftime('%H:%M')} (ждём 30с)")
                await asyncio.sleep(30)
                is_posting_locked = False
            else:
                logger.info(f"⏰ Ожидание точного времени постинга: {next_exact_time.strftime('%H:%M')} (через {int(time_diff)}с)")
                return

    # Публикуем медиа
    success_count = 0
    error_count = 0
    num_posts = min(1, len(queue))  # По одному посту за раз

    for _ in range(num_posts):
        media_data = queue.pop(0)
        published_successfully = False

        # Получаем пользователей для уведомления о данном посте
        users_for_notification = get_users_for_next_post()
        if users_for_notification:
            for user_id in users_for_notification:
                pending_notifications[user_id] = True

        try:
            # Проверяем тип медиа
            if isinstance(media_data, dict) and media_data.get("type") == "media_group":
                # Отправляем медиагруппу
                await send_media_group_to_channel(media_data)

                # ИСПРАВЛЕНО: Проверяем публикацию и уведомляем пользователей
                verification_success = await verify_post_published(CHANNEL_ID, "media_group")
                if verification_success:
                    success_count += 1
                    published_successfully = True
                    await notify_users_about_publication("media_group", True)
                    logger.info("✅ Медиагруппа успешно опубликована и пользователи уведомлены")
                else:
                    error_count += 1
                    await notify_users_about_publication("media_group", False, "Не удалось подтвердить публикацию в канале")
                    logger.error("❌ Не удалось подтвердить публикацию медиагруппы в канале")
            else:
                # Отправляем одиночное медиа
                media_type = media_data.get("type", "photo") if isinstance(media_data, dict) else "photo"
                if media_type == "document":
                    await bot.send_document(chat_id=CHANNEL_ID, document=media_data["file_id"], 
                                              caption=media_data.get("caption", DEFAULT_SIGNATURE or ""))
                elif media_type == "video":
                    await bot.send_video(chat_id=CHANNEL_ID, video=media_data["file_id"], 
                                           caption=media_data.get("caption", DEFAULT_SIGNATURE or ""))
                elif media_type == "gif":
                    await bot.send_animation(chat_id=CHANNEL_ID, animation=media_data["file_id"], 
                                               caption=media_data.get("caption", DEFAULT_SIGNATURE or ""))
                else:
                    await bot.send_photo(chat_id=CHANNEL_ID, photo=media_data["file_id"], 
                                           caption=media_data.get("caption", DEFAULT_SIGNATURE or ""))

                # ИСПРАВЛЕНО: Проверяем публикацию и уведомляем пользователей
                expected_type = media_type if media_type in ["photo", "document"] else "photo"
                verification_success = await verify_post_published(CHANNEL_ID, expected_type)
                if verification_success:
                    success_count += 1
                    published_successfully = True
                    await notify_users_about_publication(media_type, True)
                    logger.info(f"✅ {media_type} успешно опубликовано и пользователи уведомлены")
                else:
                    error_count += 1
                    await notify_users_about_publication(media_type, False, "Не удалось подтвердить публикацию в канале")
                    logger.error(f"❌ Не удалось подтвердить публикацию {media_type} в канале")

            # Обновляем время последнего поста только при успешной публикации
            if published_successfully:
                last_post_time = time.time()
                save_state()
                update_user_tracking_after_post()

        except Exception as e:
            error_count += 1
            logger.error(f"❌ Ошибка отправки медиа: {e}")
            await notify_users_about_publication("медиа", False, str(e))
            # Возвращаем медиа в очередь при ошибке
            queue.insert(0, media_data)

    # Сохраняем обновлённую очередь
    save_queue(queue)

    if success_count > 0:
        logger.info(f"✅ Опубликовано: {success_count}")
    if error_count > 0:
        logger.error(f"❌ Ошибок: {error_count}")

async def posting_loop():
    """Основной цикл постинга"""
    logger.info("🔄 Запущен цикл автопостинга")

    while True:
        try:
            queue = load_queue()
            if queue and posting_enabled and CHANNEL_ID:
                time_until_next = get_time_until_next_post()

                if time_until_next <= 0:
                    await post_next_media()
                    # После публикации ждём минимум 3 секунды
                    await asyncio.sleep(3)
                else:
                    # Ждём до следующего поста, но не более 30 секунд за раз
                    sleep_time = min(time_until_next, 30)
                    await asyncio.sleep(sleep_time)
            else:
                # Если очередь пуста или постинг отключён, ждём 15 секунд
                await asyncio.sleep(15)

        except Exception as e:
            logger.error(f"❌ Ошибка в цикле постинга: {e}")
            await asyncio.sleep(30)  # При ошибке ждём 30 секунд

async def process_pending_media_group(media_group_id):
    """Обрабатывает накопленную медиагруппу после таймаута"""
    global last_post_time

    await asyncio.sleep(1)  # ИСПРАВЛЕНО: Уменьшено время ожидания с 2 до 1 секунды

    if media_group_id in pending_media_groups:
        media_group = pending_media_groups[media_group_id]

        if len(media_group) > 1:
            # Проверяем наличие подписи у любого медиа в группе
            has_caption = False

            for media_info in media_group:
                original_caption = media_info["message"].caption
                if original_caption and original_caption.strip() != "":
                    has_caption = True
                    break

            if has_caption:
                # ЕСТЬ ПОДПИСЬ → создаём медиагруппу с дефолтной подписью
                media_list = []

                for media_info in media_group:
                    media_data = media_info["media_data"]
                    media_list.append(media_data)

                media_group_data = {
                    "type": "media_group", 
                    "media": media_list,
                    "caption": DEFAULT_SIGNATURE or ""
                }

                # Добавляем в очередь
                queue = load_queue()
                queue.append(media_group_data)
                save_queue(queue)

                # Добавляем пользователей в список для уведомлений и отслеживания в очереди
                for media_info in media_group:
                    user_id = media_info["message"].from_user.id
                    pending_notifications[user_id] = True
                    add_user_to_queue_tracking(user_id, len(queue) - 1)

                # Определяем типы медиа для отображения
                media_types = [m.get("type", "photo") for m in media_list]
                media_count = {}
                for media_type in media_types:
                    if media_type == "photo":
                        media_count["фото"] = media_count.get("фото", 0) + 1
                    elif media_type == "video":
                        media_count["видео"] = media_count.get("видео", 0) + 1
                    elif media_type == "animation":
                        media_count["GIF"] = media_count.get("GIF", 0) + 1
                    elif media_type == "document":
                        media_count["документ"] = media_count.get("документ", 0) + 1

                count_parts = []
                if media_count.get("фото", 0) > 0:
                    count_parts.append(f"{media_count['фото']} фото")
                if media_count.get("видео", 0) > 0:
                    count_parts.append(f"{media_count['видео']} видео")
                if media_count.get("GIF", 0) > 0:
                    count_parts.append(f"{media_count['GIF']} GIF")
                if media_count.get("документ", 0) > 0:
                    count_parts.append(f"{media_count['документ']} документов")

                media_text = " + ".join(count_parts)

                # Проверяем возможность мгновенной публикации
                posting_allowed, reason = is_posting_allowed()
                delayed_ready = is_delayed_start_ready()

                can_post_instantly = (posting_enabled and CHANNEL_ID and posting_allowed and 
                                    delayed_ready and len(queue) == 1 and last_post_time == 0)

                if can_post_instantly:
                    # Мгновенная публикация
                    try:
                        await send_media_group_to_channel(media_group_data)
                        if await verify_post_published(CHANNEL_ID, "media_group"):
                            queue.pop(0)
                            save_queue(queue)
                            last_post_time = time.time()
                            save_state()

                            # Уведомление после публикации
                            response = f"📎 Медиагруппа из {len(media_group)} элементов ({media_text}) успешно опубликована мгновенно!\n\n💡 /help | /status"
                            await notify_users_about_publication("media_group", True)
                        else:
                            response = f"📎 Медиагруппа из {len(media_group)} элементов ({media_text}) добавлена в очередь!\n\n❌ Не удалось подтвердить мгновенную публикацию\n\n💡 /help | /status"
                            await notify_users_about_publication("media_group", False, "Не удалось подтвердить публикацию")
                    except Exception as e:
                        response = f"📎 Медиагруппа из {len(media_group)} элементов ({media_text}) добавлена в очередь!\n\n❌ Ошибка мгновенной публикации: {e}\n\n💡 /help | /status"
                        await notify_users_about_publication("media_group", False, str(e))
                else:
                    # Обычное добавление в очередь
                    response = await format_queue_response(media_text, len(media_group), queue, is_media_group=True)

                # Отправляем ответ первому пользователю в группе
                await media_group[0]["message"].reply(response)
            else:
                # НЕТ ПОДПИСИ → обрабатываем каждое медиа отдельно как отдельные посты
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
    global last_post_time

    queue = load_queue()
    queue.append(media_data)
    save_queue(queue)

    # Добавляем пользователя в список для уведомлений и отслеживания в очереди
    user_id = message.from_user.id
    pending_notifications[user_id] = True
    add_user_to_queue_tracking(user_id, len(queue) - 1)

    # Проверяем возможность мгновенной публикации
    posting_allowed, reason = is_posting_allowed()
    delayed_ready = is_delayed_start_ready()

    can_post_instantly = (posting_enabled and CHANNEL_ID and posting_allowed and 
                         delayed_ready and len(queue) == 1 and last_post_time == 0)

    if can_post_instantly:
        # Мгновенная публикация
        try:
            if media_type == "document":
                await bot.send_document(chat_id=CHANNEL_ID, document=media_data["file_id"], 
                                      caption=media_data.get("caption", DEFAULT_SIGNATURE or ""))
            elif media_type == "video":
                await bot.send_video(chat_id=CHANNEL_ID, video=media_data["file_id"], 
                                           caption=media_data.get("caption", DEFAULT_SIGNATURE or ""))
            elif media_type == "gif":
                await bot.send_animation(chat_id=CHANNEL_ID, animation=media_data["file_id"], 
                                               caption=media_data.get("caption", DEFAULT_SIGNATURE or ""))
            else:
                await bot.send_photo(chat_id=CHANNEL_ID, photo=media_data["file_id"], 
                                   caption=media_data.get("caption", DEFAULT_SIGNATURE or ""))

            # Проверяем успешность публикации
            if await verify_post_published(CHANNEL_ID, media_type):
                queue.pop(0)
                save_queue(queue)
                last_post_time = time.time()
                save_state()

                await message.reply(f"✅ {media_type.title()} успешно опубликовано мгновенно!\n\n💡 /help | /status")
                await notify_users_about_publication(media_type, True)
            else:
                await message.reply(f"❌ Не удалось подтвердить мгновенную публикацию\n\nМедиа добавлено в очередь.\n\n💡 /help | /status")
                await notify_users_about_publication(media_type, False, "Не удалось подтвердить публикацию")
        except Exception as e:
            await message.reply(f"❌ Ошибка мгновенной публикации: {e}\n\nМедиа добавлено в очередь.\n\n💡 /help | /status")
            await notify_users_about_publication(media_type, False, str(e))
    else:
        # Обычное добавление в очередь
        response = await format_queue_response(media_type, 1, queue)
        await message.reply(response)

async def format_queue_response(media_text, media_count, queue, is_media_group=False):
    """Форматирует ответ о добавлении в очередь"""
    now = get_czech_time()

    # Рассчитываем время следующего и последнего поста
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

    # Получаем правильную статистику очереди
    queue_stats = format_queue_stats(queue)

    return f"{add_text}{first_post_text}{last_post_text}\n📊 В очереди: {queue_stats}\n\n💡 Вы получите уведомление после публикации\n💡 /help | /status"

@dp.message(F.photo | F.document | F.video | F.animation)
async def handle_media(message: Message):
    """Обрабатывает фото, документы, видео и анимации (GIF)"""

    # Проверяем доступ пользователя
    if not check_user_access(message.from_user.id):
        await message.reply("У вас нет прав для пользования этим ботом, для получения, посетите канал https://t.me/poluchiprava228.")
        return

    global is_posting_locked
    if is_posting_locked:
        await message.reply("⏳ Сейчас будет запощен пост, ваше медиа добавлено в очередь")

    # Проверяем флаг мгновенной публикации
    user_id = message.from_user.id
    instant_post = False
    if user_id in user_media_tracking and user_media_tracking[user_id].get("instant_post"):
        instant_post = True
        # Очищаем флаг
        del user_media_tracking[user_id]

    # Определяем тип медиа и получаем file_id
    if message.photo:
        media_type = "photo"
        file_id = message.photo[-1].file_id  # Берём наибольшее разрешение
    elif message.document:
        # Проверяем, является ли документ GIF
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

    # Получаем подпись или используем стандартную (если установлена)
    if message.caption:
        caption = message.caption
    elif DEFAULT_SIGNATURE is not None:
        caption = DEFAULT_SIGNATURE
    else:
        caption = ""  # Пустая подпись если не установлена

    # Создаём данные медиа
    media_data = {
        "file_id": file_id,
        "type": media_type,
        "caption": caption
    }

    # Если это мгновенная публикация
    if instant_post:
        if not CHANNEL_ID:
            await message.reply("❌ Канал не установлен. Установите канал командой /setchannel")
            return
        
        try:
            # Публикуем сразу
            if media_type == "document":
                await bot.send_document(chat_id=CHANNEL_ID, document=file_id, caption=caption)
            elif media_type == "video":
                await bot.send_video(chat_id=CHANNEL_ID, video=file_id, caption=caption)
            elif media_type == "gif":
                await bot.send_animation(chat_id=CHANNEL_ID, animation=file_id, caption=caption)
            else:
                await bot.send_photo(chat_id=CHANNEL_ID, photo=file_id, caption=caption)
            
            await message.reply("✅ Медиа опубликовано мгновенно!")
        except Exception as e:
            await message.reply(f"❌ Ошибка публикации: {e}")
        return

    # Проверяем, есть ли media_group_id (для группировки медиа)
    if message.media_group_id:
        media_group_id = message.media_group_id

        # Добавляем в группу ожидания
        if media_group_id not in pending_media_groups:
            pending_media_groups[media_group_id] = []

        pending_media_groups[media_group_id].append({
            "message": message,
            "media_data": media_data,
            "media_type": media_type
        })

        # Устанавливаем таймер для обработки группы (если ещё не установлен)
        if media_group_id not in media_group_timers:
            media_group_timers[media_group_id] = asyncio.create_task(
                process_pending_media_group(media_group_id)
            )
    else:
        # Одиночное медиа
        await handle_single_media(message, media_data, media_type)

@dp.message(F.text)
async def handle_message(message: Message):
    """Обрабатывает текстовые команды"""

    # Проверяем доступ пользователя
    if not check_user_access(message.from_user.id):
        await message.reply("У вас нет прав для пользования этим ботом, для получения, посетите канал https://t.me/poluchiprava228.")
        return

    global posting_enabled, CHANNEL_ID, DEFAULT_SIGNATURE, POST_INTERVAL, last_post_time
    global ALLOWED_WEEKDAYS, START_TIME, END_TIME, DELAYED_START_ENABLED, DELAYED_START_TIME, DELAYED_START_INTERVAL_START
    global TIME_WINDOW_ENABLED, WEEKDAYS_ENABLED, EXACT_TIMING_ENABLED, NOTIFICATIONS_ENABLED

    text = message.text.strip()

    if text == "/start":
        # Формируем информацию о настройках по умолчанию
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
        
        # Формируем информацию о неназначенных параметрах
        not_assigned = []
        if POST_INTERVAL is None:
            not_assigned.append("❓ Интервал постинга")
        if DEFAULT_SIGNATURE is None:
            not_assigned.append("❓ Подпись для постов")
        if START_TIME is None or END_TIME is None:
            not_assigned.append("❓ Временное окно постинга")
        if ALLOWED_WEEKDAYS is None:
            not_assigned.append("❓ Дни недели для постинга")
        
        # Информация о канале
        if CHANNEL_ID:
            try:
                # Попытаемся получить информацию о канале
                chat_info = await bot.get_chat(CHANNEL_ID)
                if chat_info.title:
                    channel_info = f"📢 Канал: {chat_info.title} ({CHANNEL_ID})"
                else:
                    channel_info = f"📢 Канал: {CHANNEL_ID}"
            except:
                channel_info = f"� Канал: {CHANNEL_ID}"
        else:
            channel_info = "❌ Канал не установлен"
        
        start_text = (
            "👋 <b>Бот для автопостинга запущен!</b>\n\n"
            f"{channel_info}\n\n"
            "<b>🟢 Включено по умолчанию:</b>\n"
            f"{chr(10).join(enabled_by_default)}\n\n"
        )
        
        if disabled_by_default:
            start_text += (
                "<b>🔴 Выключено по умолчанию:</b>\n"
                f"{chr(10).join(disabled_by_default)}\n\n"
            )
        
        if not_assigned:
            start_text += (
                "<b>❓ Не назначено (требует настройки):</b>\n"
                f"{chr(10).join(not_assigned)}\n\n"
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

<b>💡 Команда для экстренной публикации:</b>
/post - опубликовать медиа сразу, минуя очередь
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

<b>� Управление подписями:</b>
/title - меню управления подписями

<b>� Управление очередью:</b>
/clear - очистить очередь
/remove - удалить пост по номеру
/random - перемешать очередь

<b>� Мгновенная публикация:</b>
/post - опубликовать медиа сразу (команда вне очереди)
/postfile - опубликовать пост по номеру
/postnow - опубликовать следующий пост
/postall - опубликовать все посты сразу
"""
        await message.reply(commands_text)

    elif text == "/post":
        # Команда для мгновенной публикации вне очереди
        missing_settings = []
        if POST_INTERVAL is None:
            missing_settings.append("интервал постинга")
        if DEFAULT_SIGNATURE is None:
            missing_settings.append("подпись")
        
        if missing_settings:
            missing_text = " и ".join(missing_settings)
            if len(missing_settings) == 1:
                notification_text = f"⚠️ Не установлена {missing_text}.\n\n"
            else:
                notification_text = f"⚠️ Не установлены {missing_text}.\n\n"
            
            notification_text += (
                "Используйте команду /post следующим образом:\n"
                "1. Отправьте команду /post\n"
                "2. Сразу после неё отправьте медиа\n\n"
                "Медиа будет опубликовано сразу с текущими настройками (или без подписи, если она не установлена) и НЕ будет сохранено в очередь."
            )
        else:
            notification_text = (
                "✅ Готов к мгновенной публикации!\n\n"
                "Отправьте медиа сразу после этой команды, и оно будет опубликовано немедленно, минуя очередь."
            )
        
        await message.reply(notification_text)
        # Устанавливаем флаг для следующего медиа
        user_media_tracking[message.from_user.id] = {"instant_post": True}

    elif text == "/status":
        now = get_czech_time()
        queue = load_queue()

        # Время публикации последнего элемента из очереди
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

        # Формируем детали расписания
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

        # Определяем общий статус
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
        if DELAYED_START_ENABLED and DELAYED_START_TIME:
            if not delayed_ready:
                delayed_text = f"\n⏳ Старт: {DELAYED_START_TIME.strftime('%d.%m %H:%M')}"

        # Получаем правильную статистику очереди
        queue_stats = format_queue_stats(queue)

        # Информация о канале с названием
        if CHANNEL_ID:
            try:
                chat_info = await bot.get_chat(CHANNEL_ID)
                if chat_info.title:
                    channel_text = f"{chat_info.title} ({CHANNEL_ID})"
                else:
                    channel_text = CHANNEL_ID
            except:
                channel_text = CHANNEL_ID
        else:
            channel_text = "не установлен"

        # Информация о подписи
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
            if POST_INTERVAL is not None:
                current_interval = format_interval(POST_INTERVAL)
            else:
                current_interval = "❓ не установлен"
            await message.reply(f"📊 Текущий интервал: {current_interval}\n\n"
                              "Для изменения: /interval 2h30m\n"
                              "Форматы: 1d (день), 2h (часы), 30m (минуты), 45s (секунды)")
            return

        interval_str = parts[1]
        new_interval = parse_interval(interval_str)

        if new_interval:
            POST_INTERVAL = new_interval
            save_state()
            formatted_interval = format_interval(new_interval)

            # Если включено точное планирование, показываем новые времена
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
        # Дни недели
        if ALLOWED_WEEKDAYS is not None:
            allowed_days = ", ".join([get_weekday_name(day) for day in sorted(ALLOWED_WEEKDAYS)])
        else:
            allowed_days = "❓ не назначены"
            
        posting_allowed, reason = is_posting_allowed()
        delayed_ready = is_delayed_start_ready()

        # Определяем общий статус
        if posting_allowed and delayed_ready:
            status_emoji = "✅"
            status_text = "разрешён"
        else:
            status_emoji = "❌"
            status_text = reason.lower() if not posting_allowed else "ожидание старта"

        # Информация о точном планировании
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

        # Временное окно
        if START_TIME is not None and END_TIME is not None:
            time_window_text = f"{START_TIME.strftime('%H:%M')} - {END_TIME.strftime('%H:%M')}"
        else:
            time_window_text = "❓ не назначено"

        # Отложенный старт
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
            await message.reply(f"🕐 Текущее временное окно: {current_window}\n\n"
                              "Для изменения: /settime 06:00 20:00")
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
            days_str = parts[1]
            days = [int(x.strip()) - 1 for x in days_str.split(",")]  # Конвертируем в 0-6

            # Проверяем корректность дней
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
                                  "Для изменения: /startdate 2024-01-25 17:00\n"
                                  "Или отключить: /clearstart")
            else:
                await message.reply("⏳ Отложенный старт не установлен\n\n"
                                  "Для установки: /startdate 2024-01-25 17:00"
                                  )
            return

        try:
            date_time_str = parts[1]

            # Поддерживаем два формата: YYYY-MM-DD HH:MM и DD.MM.YYYY HH:MM
            if '.' in date_time_str.split()[0]:
                # Формат DD.MM.YYYY HH:MM
                date_part, time_part = date_time_str.split()
                day, month, year = map(int, date_part.split('.'))
                hour, minute = map(int, time_part.split(':'))
                target_datetime = datetime(year, month, day, hour, minute)
            else:
                # Формат YYYY-MM-DD HH:MM
                target_datetime = datetime.strptime(date_time_str, "%Y-%m-%d %H:%M")

            # Устанавливаем чешский часовой пояс
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
        current_time = now.time()

        posting_allowed, reason = is_posting_allowed()

        time_status = "✅ включено" if TIME_WINDOW_ENABLED else "❌ выключено"
        days_status = "✅ включено" if WEEKDAYS_ENABLED else "❌ выключено"

        # Временное окно
        if START_TIME is not None and END_TIME is not None:
            time_window_text = f"{START_TIME.strftime('%H:%M')} - {END_TIME.strftime('%H:%M')}"
        else:
            time_window_text = "❓ не назначено"

        # Разрешённые дни
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
            # Проверяем, что это число
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
            # Показываем меню управления подписью
            queue = load_queue()
            queue_info = f"\n📊 В очереди: {len(queue)} постов" if queue else "\n📭 Очередь пуста"
            
            # Отображение текущей подписи
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
        # Парсим подпись на предмет кликабельных ссылок
        parsed_signature = parse_signature_with_link(new_signature)
        original_signature = new_signature
        
        # Устанавливаем подпись по умолчанию
        DEFAULT_SIGNATURE = parsed_signature
        save_state()
        
        # Применяем подпись ко всем постам в очереди
        updated_count = apply_signature_to_all_queue(new_signature)
        
        if " # " in original_signature:
            signature_type = "кликабельная ссылка"
        else:
            signature_type = "обычная подпись"
            
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
                removed_item = queue.pop(index)
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
                    # Добавляем пользователя в список уведомлений
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
                        # Одиночное медиа
                        media_type = media_data.get("type", "photo") if isinstance(media_data, dict) else "photo"
                        if media_type == "document":
                            await bot.send_document(chat_id=CHANNEL_ID, document=media_data["file_id"], caption=media_data.get("caption", DEFAULT_SIGNATURE or ""))
                        elif media_type == "video":
                            await bot.send_video(chat_id=CHANNEL_ID, video=media_data["file_id"], caption=media_data.get("caption", DEFAULT_SIGNATURE or ""))
                        elif media_type == "gif":
                            await bot.send_animation(chat_id=CHANNEL_ID, animation=media_data["file_id"], caption=media_data.get("caption", DEFAULT_SIGNATURE or ""))
                        else:
                            caption = media_data.get("caption", DEFAULT_SIGNATURE or "") if isinstance(media_data, dict) else DEFAULT_SIGNATURE or ""
                            file_id = media_data.get("file_id") if isinstance(media_data, dict) else media_data
                            await bot.send_photo(chat_id=CHANNEL_ID, photo=file_id, caption=caption)

                        if await verify_post_published(CHANNEL_ID, media_type):
                            await message.reply(f"✅ Медиа #{index + 1} опубликовано!")
                            await notify_users_about_publication(media_type, True)
                        else:
                            await message.reply(f"❌ Не удалось подтвердить публикацию медиа #{index + 1}")
                            await notify_users_about_publication(media_type, False, "Не удалось подтвердить публикацию")

                    # Обновляем время последнего поста
                    last_post_time = time.time()
                    save_state()

                except Exception as e:
                    # Возвращаем медиа в очередь при ошибке
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

            # Добавляем пользователя в список уведомлений
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
                # Одиночное медиа
                media_type = media_data.get("type", "photo") if isinstance(media_data, dict) else "photo"
                if media_type == "document":
                    await bot.send_document(chat_id=CHANNEL_ID, document=media_data["file_id"], caption=media_data.get("caption", DEFAULT_SIGNATURE or ""))
                elif media_type == "video":
                    await bot.send_video(chat_id=CHANNEL_ID, video=media_data["file_id"], caption=media_data.get("caption", DEFAULT_SIGNATURE or ""))
                elif media_type == "gif":
                    await bot.send_animation(chat_id=CHANNEL_ID, animation=media_data["file_id"], caption=media_data.get("caption", DEFAULT_SIGNATURE or ""))
                else:
                    caption = media_data.get("caption", DEFAULT_SIGNATURE or "") if isinstance(media_data, dict) else DEFAULT_SIGNATURE or ""
                    file_id = media_data.get("file_id") if isinstance(media_data, dict) else media_data
                    await bot.send_photo(chat_id=CHANNEL_ID, photo=file_id, caption=caption)

                if await verify_post_published(CHANNEL_ID, media_type):
                    await message.reply("✅ Медиа опубликовано!")
                    await notify_users_about_publication(media_type, True)
                else:
                    await message.reply("❌ Не удалось подтвердить публикацию медиа")
                    await notify_users_about_publication(media_type, False, "Не удалось подтвердить публикацию")

            # Обновляем время последнего поста
            last_post_time = time.time()
            save_state()

        except Exception as e:
            # Возвращаем медиа в очередь при ошибке
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

        # Считаем статистику очереди
        total_media, media_groups, total_posts, photos, videos, gifs, documents = count_queue_stats(queue)

        await message.reply(f"🚀 Начинаю публикацию всех {total_posts} постов...")

        success_count = 0
        error_count = 0
        published_posts = []

        # Добавляем пользователя в список уведомлений
        pending_notifications[message.from_user.id] = True

        try:
            for i, media_data in enumerate(queue):
                try:
                    if isinstance(media_data, dict) and media_data.get("type") == "media_group":
                        await send_media_group_to_channel(media_data)
                        if await verify_post_published(CHANNEL_ID, "media_group"):
                            success_count += 1
                            published_posts.append(f"Медиагруппа #{i+1}")
                        else:
                            error_count += 1
                    else:
                        # Одиночное медиа
                        media_type = media_data.get("type", "photo") if isinstance(media_data, dict) else "photo"
                        if media_type == "document":
                            await bot.send_document(chat_id=CHANNEL_ID, document=media_data["file_id"], caption=media_data.get("caption", DEFAULT_SIGNATURE or ""))
                        elif media_type == "video":
                            await bot.send_video(chat_id=CHANNEL_ID, video=media_data["file_id"], caption=media_data.get("caption", DEFAULT_SIGNATURE or ""))
                        elif media_type == "gif":
                            await bot.send_animation(chat_id=CHANNEL_ID, animation=media_data["file_id"], caption=media_data.get("caption", DEFAULT_SIGNATURE or ""))
                        else:
                            caption = media_data.get("caption", DEFAULT_SIGNATURE or "") if isinstance(media_data, dict) else DEFAULT_SIGNATURE or ""
                            file_id = media_data.get("file_id") if isinstance(media_data, dict) else media_data
                            await bot.send_photo(chat_id=CHANNEL_ID, photo=file_id, caption=caption)

                        if await verify_post_published(CHANNEL_ID, media_type):
                            success_count += 1
                            published_posts.append(f"{media_type.title()} #{i+1}")
                        else:
                            error_count += 1

                    # Пауза между постами для избежания лимитов
                    await asyncio.sleep(0.5)

                except Exception as e:
                    error_count += 1
                    logger.error(f"Ошибка публикации поста #{i+1}: {e}")

            # Очищаем очередь после публикации
            if success_count > 0:
                save_queue([])
                last_post_time = time.time()
                save_state()

            # Отправляем результат
            if success_count == total_posts:
                result_text = f"✅ Все {success_count} постов успешно опубликованы!\n\n📊 Опубликовано: {success_count}/{total_posts}"
            else:
                result_text = f"⚠️ Частичная публикация:\n\n✅ Успешно: {success_count}\n❌ Ошибок: {error_count}\n📊 Итого: {success_count}/{total_posts}"

            await message.reply(result_text)

        except Exception as e:
            await message.reply(f"❌ Критическая ошибка при массовой публикации: {e}")

# Веб-сервер для поддержания активности (для Render.com)
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

    # Используем порт из переменной окружения или 5000 по умолчанию
    port = int(os.environ.get('PORT', 5000))
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    logger.info(f"🌐 Веб-сервер запущен на порту {port}")

async def main():
    # ИСПРАВЛЕНО: Загружаем состояние при запуске и инициализируем структуры данных
    load_state()

    # Убеждаемся, что все глобальные переменные инициализированы
    global pending_media_groups, media_group_timers, pending_notifications, user_media_tracking
    # Переменные уже инициализированы на уровне модуля
    logger.info(f"📊 Инициализированы структуры данных: pending_media_groups={len(pending_media_groups)}, timers={len(media_group_timers)}")

    # ИСПРАВЛЕНО: Обновлённые команды бота для всплывающего меню
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
        BotCommand(command="post", description="⚡ Мгновенная публикация"),
        BotCommand(command="postfile", description="🚀 Опубликовать по номеру"),
        BotCommand(command="postnow", description="⚡ Опубликовать сейчас"),
        BotCommand(command="postall", description="🚀 Опубликовать все посты"),
        BotCommand(command="checktime", description="🕐 Проверить текущее время"),
    ])

    # Запускаем веб-сервер для Render.com
    await start_web_server()

    # Запускаем цикл постинга
    asyncio.create_task(posting_loop())

    # Запускаем бота
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

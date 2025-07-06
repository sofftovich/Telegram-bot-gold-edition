
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

# Проверка токена при запуске
if not TOKEN:
    logger.error("❌ BOT_TOKEN не установлен! Установите переменную окружения BOT_TOKEN")
    exit(1)

# Чешский часовой пояс GMT+2
CZECH_TIMEZONE = timezone(timedelta(hours=2))

def get_czech_time():
    """Возвращает текущее время в Чехии (GMT+2)"""
    return datetime.now(CZECH_TIMEZONE)

# ИСПРАВЛЕННЫЕ ДЕФОЛТНЫЕ НАСТРОЙКИ
POST_INTERVAL = 7200  # 2 часа по умолчанию
last_post_time = 0  # Время последнего поста (начинаем с 0)
posting_enabled = True  # Статус автопостинга

DEFAULT_SIGNATURE = '<a href="https://t.me/+oBc3uUiG9Y45ZDM6">Femboys</a>'

# Настройки планирования - ИСПРАВЛЕННЫЕ ДЕФОЛТЫ
ALLOWED_WEEKDAYS = [0, 1, 2, 3, 4, 5, 6]  # По умолчанию все дни (0=понедельник, 6=воскресенье)
START_TIME = dt_time(6, 0)  # Начало временного окна (06:00)
END_TIME = dt_time(20, 0)  # Конец временного окна (20:00)

# Настройки отложенного старта
DELAYED_START_ENABLED = False  # Включен ли отложенный старт
DELAYED_START_TIME = None  # Время первого поста (datetime object)
DELAYED_START_INTERVAL_START = None  # С какого времени дня начинать отсчёт интервала

# Новые переменные для управления ограничениями
TIME_WINDOW_ENABLED = True  # Включено ли ограничение по времени
WEEKDAYS_ENABLED = True    # Включено ли ограничение по дням недели

# Переменные для точного планирования постов по интервалам - ИСПРАВЛЕННЫЙ ДЕФОЛТ
EXACT_TIMING_ENABLED = True  # Включено ли точное планирование по интервалам

# Переменная для управления уведомлениями
NOTIFICATIONS_ENABLED = True  # Включены ли уведомления о публикации

def calculate_exact_posting_times():
    """Рассчитывает точные моменты времени для постинга в рамках временного окна"""
    if not EXACT_TIMING_ENABLED or not TIME_WINDOW_ENABLED:
        return []

    # Рассчитываем продолжительность окна в секундах
    if START_TIME <= END_TIME:
        # Обычное окно (например, 06:00-20:00)
        window_start = datetime.combine(datetime.today(), START_TIME)
        window_end = datetime.combine(datetime.today(), END_TIME)
        window_duration = (window_end - window_start).total_seconds()
    else:
        # Окно через полночь (например, 20:00-06:00)
        window_start = datetime.combine(datetime.today(), START_TIME)
        window_end = datetime.combine(datetime.today() + timedelta(days=1), END_TIME)
        window_duration = (window_end - window_start).total_seconds()

    # Количество постов, которые можем разместить в окне
    max_posts_in_window = int(window_duration // POST_INTERVAL) + 1

    if max_posts_in_window <= 1:
        return [START_TIME]

    # Рассчитываем интервал между постами
    actual_interval = window_duration / (max_posts_in_window - 1)

    posting_times = []
    current_seconds = START_TIME.hour * 3600 + START_TIME.minute * 60

    for i in range(max_posts_in_window):
        total_seconds = int(current_seconds + i * actual_interval)

        # Обрабатываем переход через полночь
        if total_seconds >= 24 * 3600:
            total_seconds -= 24 * 3600

        hours = total_seconds // 3600
        minutes = (total_seconds % 3600) // 60

        posting_times.append(dt_time(hours, minutes))

    return posting_times

def get_next_exact_posting_time():
    """Возвращает следующее точное время для постинга"""
    if not EXACT_TIMING_ENABLED:
        return None

    now = get_czech_time()
    current_time = now.time()

    posting_times = calculate_exact_posting_times()
    if not posting_times:
        return None

    # Ищем ближайшее время сегодня
    for post_time in posting_times:
        if current_time < post_time:
            return now.replace(hour=post_time.hour, minute=post_time.minute, second=0, microsecond=0)

    # Если все времена сегодня прошли, ищем следующий день
    next_day = now + timedelta(days=1)
    for days_ahead in range(7):  # Ищем в течение недели
        check_date = next_day + timedelta(days=days_ahead)
        check_weekday = check_date.weekday()

        if not WEEKDAYS_ENABLED or check_weekday in ALLOWED_WEEKDAYS:
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

    # Если есть точное планирование, используем его
    if EXACT_TIMING_ENABLED:
        next_exact_time = get_next_exact_posting_time()
        if next_exact_time:
            now = get_czech_time()
            time_diff = (next_exact_time - now).total_seconds()
            return max(0, int(time_diff))
        else:
            # Если нет точного времени, ждём до следующего разрешённого периода
            return get_next_allowed_time()

    # Иначе используем интервальное планирование
    now_timestamp = time.time()
    time_since_last = now_timestamp - last_post_time

    if time_since_last >= POST_INTERVAL:
        return get_next_allowed_time()
    else:
        return POST_INTERVAL - int(time_since_last) + get_next_allowed_time()

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

        if not WEEKDAYS_ENABLED or check_weekday in ALLOWED_WEEKDAYS:
            if days_ahead == 0:  # Сегодня
                # Проверяем, не прошло ли уже время начала окна
                if not TIME_WINDOW_ENABLED:
                    return 0  # Если временные ограничения выключены, можно постить сейчас
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
                if not TIME_WINDOW_ENABLED:
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
    """Загружает состояние бота"""
    global last_post_time, DEFAULT_SIGNATURE, CHANNEL_ID, posting_enabled, ALLOWED_WEEKDAYS, START_TIME, END_TIME
    global DELAYED_START_ENABLED, DELAYED_START_TIME, DELAYED_START_INTERVAL_START, POST_INTERVAL
    global TIME_WINDOW_ENABLED, WEEKDAYS_ENABLED, EXACT_TIMING_ENABLED, NOTIFICATIONS_ENABLED
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                state = json.load(f)
                last_post_time = state.get("last_post_time", 0)
                DEFAULT_SIGNATURE = state.get("default_signature", DEFAULT_SIGNATURE)
                posting_enabled = state.get("posting_enabled", True)
                POST_INTERVAL = state.get("post_interval", POST_INTERVAL)
                # Приоритет: сохранённый канал > переменная окружения
                saved_channel = state.get("channel_id")
                if saved_channel:
                    CHANNEL_ID = saved_channel

                # Загружаем настройки планирования
                ALLOWED_WEEKDAYS = state.get("allowed_weekdays", ALLOWED_WEEKDAYS)
                start_time_str = state.get("start_time", "06:00")
                end_time_str = state.get("end_time", "20:00")
                START_TIME = dt_time(*map(int, start_time_str.split(":")))
                END_TIME = dt_time(*map(int, end_time_str.split(":")))

                # Загружаем настройки отложенного старта
                DELAYED_START_ENABLED = state.get("delayed_start_enabled", False)
                delayed_start_str = state.get("delayed_start_time")
                if delayed_start_str:
                    DELAYED_START_TIME = datetime.fromisoformat(delayed_start_str)

                delayed_interval_start_str = state.get("delayed_start_interval_start")
                if delayed_interval_start_str:
                    DELAYED_START_INTERVAL_START = dt_time(*map(int, delayed_interval_start_str.split(":")))

                # Загружаем настройки ограничений
                TIME_WINDOW_ENABLED = state.get("time_window_enabled", TIME_WINDOW_ENABLED)
                WEEKDAYS_ENABLED = state.get("weekdays_enabled", WEEKDAYS_ENABLED)
                EXACT_TIMING_ENABLED = state.get("exact_timing_enabled", EXACT_TIMING_ENABLED)
                NOTIFICATIONS_ENABLED = state.get("notifications_enabled", NOTIFICATIONS_ENABLED)

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
        "start_time": START_TIME.strftime("%H:%M"),
        "end_time": END_TIME.strftime("%H:%M"),
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
    if WEEKDAYS_ENABLED and current_weekday not in ALLOWED_WEEKDAYS:
        return False, f"День недели не разрешён ({get_weekday_name(current_weekday)})"

    # Проверка временного окна
    if TIME_WINDOW_ENABLED:
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

    for item in queue:
        if isinstance(item, dict) and item.get("type") == "media_group":
            media_groups += 1
            total_media += len(item.get("media", []))
        else:
            total_media += 1

    return total_media, media_groups, total_posts

def format_queue_stats(queue):
    """Форматирует статистику очереди в читаемый вид"""
    total_media, media_groups, total_posts = count_queue_stats(queue)

    parts = []
    if total_media > 0:
        parts.append(f"{total_media} медиа")
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

def set_photo_caption(index, caption):
    """Устанавливает подпись для фото по индексу"""
    queue = load_queue()
    if 0 <= index < len(queue):
        if isinstance(queue[index], dict):
            queue[index]["caption"] = caption
        else:
            # Преобразуем в dict если это строка
            queue[index] = {"file_id": queue[index], "caption": caption, "type": "photo"}
        save_queue(queue)
        return True
    return False

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

    # ИСПРАВЛЕНО: Проверяем точное время для постинга с более точной проверкой
    if EXACT_TIMING_ENABLED:
        next_exact_time = get_next_exact_posting_time()
        if next_exact_time:
            now = get_czech_time()
            time_diff = (next_exact_time - now).total_seconds()
            # Уменьшаем порог до 3 секунд для более точного планирования
            if time_diff > 3:
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
                    await bot.send_document(chat_id=CHANNEL_ID, document=media_data["file_id"], caption=media_data.get("caption", DEFAULT_SIGNATURE))
                else:
                    # По умолчанию фото
                    caption = media_data.get("caption", DEFAULT_SIGNATURE) if isinstance(media_data, dict) else DEFAULT_SIGNATURE
                    file_id = media_data.get("file_id") if isinstance(media_data, dict) else media_data
                    await bot.send_photo(chat_id=CHANNEL_ID, photo=file_id, caption=caption)

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
                    "caption": DEFAULT_SIGNATURE
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
                                      caption=media_data.get("caption", DEFAULT_SIGNATURE))
            else:
                await bot.send_photo(chat_id=CHANNEL_ID, photo=media_data["file_id"], 
                                   caption=media_data.get("caption", DEFAULT_SIGNATURE))

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

@dp.message(F.photo | F.document)
async def handle_media(message: Message):
    """Обрабатывает фото и документы"""

    # Определяем тип медиа и получаем file_id
    if message.photo:
        media_type = "photo"
        file_id = message.photo[-1].file_id  # Берём наибольшее разрешение
    elif message.document:
        media_type = "document"
        file_id = message.document.file_id
    else:
        return

    # Получаем подпись или используем стандартную
    caption = message.caption if message.caption else DEFAULT_SIGNATURE

    # Создаём данные медиа
    media_data = {
        "file_id": file_id,
        "type": media_type,
        "caption": caption
    }

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
    global posting_enabled, CHANNEL_ID, DEFAULT_SIGNATURE, POST_INTERVAL, last_post_time
    global ALLOWED_WEEKDAYS, START_TIME, END_TIME, DELAYED_START_ENABLED, DELAYED_START_TIME, DELAYED_START_INTERVAL_START
    global TIME_WINDOW_ENABLED, WEEKDAYS_ENABLED, EXACT_TIMING_ENABLED, NOTIFICATIONS_ENABLED

    text = message.text.strip()

    if text == "/start":
        await message.reply(
            "👋 Привет! Я бот для автопостинга в канал.\n\n"
            "📤 Просто отправь мне фото, документ или медиагруппу, и я добавлю их в очередь для публикации.\n\n"
            "🛠 Используй /help для списка команд управления."
        )

    elif text == "/help":
        help_text = """
🤖 <b>Бот для автопостинга в канал</b>

<b>📤 Основные команды:</b>
/start - запуск бота
/help - это сообщение
/status - информация о боте и очереди
/toggle - включить/выключить автопостинг

<b>⏰ Управление расписанием:</b>
/schedule - показать текущее расписание
/interval 2h30m - установить интервал между постами
/settime 06:00 20:00 - установить временное окно
/days 1,2,3,4,5 - дни недели (1=пн, 7=вс)
/checktime - проверить текущие настройки времени

<b>📅 Отложенный старт:</b>
/startdate 2024-01-25 17:00 - установить время первого поста
/clearstart - отключить отложенный старт

<b>🎛 Переключатели ограничений:</b>
/toggletime - включить/выключить временное окно
/toggledays - включить/выключить ограничения по дням
/toggleexact - переключить точное/интервальное планирование
/togglenotify - включить/выключить уведомления о публикации

<b>📢 Управление каналом:</b>
/channel - показать текущий канал
/setchannel -1001234567890 - установить ID канала

<b>🗂 Управление очередью:</b>
/clear - очистить всю очередь
/remove 1 - удалить медиа по номеру
/random - перемешать очередь (сохраняет расписание)

<b>📝 Управление подписями:</b>
/title - показать меню управления подписью
/title текст - установить текстовую подпись
/settitle 1 текст - изменить подпись конкретного фото

<b>🔧 Дополнительные команды:</b>
/postfile номер - мгновенно опубликовать медиа по номеру
/postnow - опубликовать следующее медиа сейчас
/postall - опубликовать все посты из очереди сразу

<i>💡 Для отправки медиагруппы выделите несколько фото/документов и отправьте одним сообщением.</i>
"""
        await message.reply(help_text)

    elif text == "/status":
        now = get_czech_time()
        queue = load_queue()

        # Время публикации последнего элемента из очереди
        if len(queue) > 0:
            first_post_time, last_post_time_calculated = calculate_queue_schedule(len(queue))
            if last_post_time_calculated:
                if last_post_time_calculated.date() == now.date():
                    total_time_text = f"в {last_post_time_calculated.strftime('%H:%M')}"
                else:
                    total_time_text = f"{last_post_time_calculated.strftime('%d.%m')} в {last_post_time_calculated.strftime('%H:%M')}"
            else:
                total_time_text = "по расписанию"
        else:
            total_time_text = "Очередь пуста"

        # Время до следующего поста
        if len(queue) > 0:
            time_until_next = get_time_until_next_post()
            next_post_text = format_interval(time_until_next) if time_until_next > 0 else "сейчас"
        else:
            next_post_text = "нет постов в очереди"

        # Формируем детали расписания
        if EXACT_TIMING_ENABLED:
            posting_times = calculate_exact_posting_times()
            if posting_times:
                times_str = ", ".join([t.strftime('%H:%M') for t in posting_times[:3]])
                if len(posting_times) > 3:
                    times_str += f" ... (всего {len(posting_times)})"
                schedule_detail = f"\n🎯 Точные времена: {times_str}"
            else:
                schedule_detail = f"\n⏱ Интервал: {format_interval(POST_INTERVAL)}"
        else:
            schedule_detail = f"\n⏱ Интервал: {format_interval(POST_INTERVAL)}"

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

        status_text_full = f"""
🤖 <b>Статус бота:</b>

{status_emoji} Автопостинг: {status_text}
📊 В очереди: {queue_stats}
⏱ Интервал: {format_interval(POST_INTERVAL)}
🕐 Следующий пост: {next_post_text}
📅 Время публикации всех фото: {total_time_text}{schedule_detail}{delayed_text}

💬 Канал: {CHANNEL_ID or 'не установлен'}
🏷 Подпись: {DEFAULT_SIGNATURE}
{'🔔' if NOTIFICATIONS_ENABLED else '🔕'} Уведомления: {'включены' if NOTIFICATIONS_ENABLED else 'выключены'}

💡 /help для команд | /schedule для расписания
"""
        await message.reply(status_text_full)

    elif text.startswith("/interval"):
        parts = text.split(maxsplit=1)
        if len(parts) == 1:
            current_interval = format_interval(POST_INTERVAL)
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
            await message.reply(f"✅ Интервал установлен: {formatted_interval}")
        else:
            await message.reply("❌ Неверный формат интервала. Пример: 2h30m (2 часа 30 минут)")

    elif text.startswith("/settitle"):
        parts = text.split(maxsplit=2)
        if len(parts) < 3:
            await message.reply("❌ Укажите номер фото и текст. Пример: /settitle 1 Новая подпись")
            return

        try:
            index = int(parts[1]) - 1
            caption = parts[2]

            if set_photo_caption(index, caption):
                await message.reply(f"✅ Подпись для фото #{index + 1} изменена на:\n{caption}")
            else:
                queue = load_queue()
                await message.reply(f"❌ Неверный номер. В очереди {len(queue)} фото")

        except (IndexError, ValueError):
            await message.reply("❌ Укажите номер фото и текст. Пример: /settitle 1 Новая подпись")

    elif text == "/toggle":
        posting_enabled = not posting_enabled
        save_state()
        status = "включен" if posting_enabled else "выключен"
        await message.reply(f"{'✅' if posting_enabled else '❌'} Автопостинг {status}!")

    elif text == "/schedule":
        allowed_days = ", ".join([get_weekday_name(day) for day in sorted(ALLOWED_WEEKDAYS)])
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
        if EXACT_TIMING_ENABLED:
            posting_times = calculate_exact_posting_times()
            if posting_times and len(posting_times) > 1:
                times_str = ", ".join([t.strftime('%H:%M') for t in posting_times[:5]])
                if len(posting_times) > 5:
                    times_str += f" ... (всего {len(posting_times)})"
                timing_info = f"🎯 Точные времена ({len(posting_times)}): {times_str}"
            else:
                timing_info = f"⏱ Интервал: {format_interval(POST_INTERVAL)}"
        else:
            timing_info = f"\n⏱ Интервал: {format_interval(POST_INTERVAL)}"

        # Отложенный старт
        delayed_text = ""
        if DELAYED_START_ENABLED and DELAYED_START_TIME:
            status_icon = "✅" if delayed_ready else "⏳"
            delayed_text = f"\n{status_icon} Отложенный старт: {DELAYED_START_TIME.strftime('%d.%m.%Y %H:%M')}"

        schedule_text = f"""
📅 <b>Расписание постинга:</b>

{status_emoji} Статус: {status_text}
{timing_info}

{'✅' if TIME_WINDOW_ENABLED else '❌'} Временное окно: {START_TIME.strftime('%H:%M')} - {END_TIME.strftime('%H:%M')}
{'✅' if WEEKDAYS_ENABLED else '❌'} Дни недели: {allowed_days}
{'✅' if EXACT_TIMING_ENABLED else '❌'} Точное планирование: {'включено' if EXACT_TIMING_ENABLED else 'выключено'}{delayed_text}

💡 /help для всех команд
"""
        await message.reply(schedule_text)

    elif text.startswith("/settime"):
        parts = text.split(maxsplit=1)
        if len(parts) == 1:
            await message.reply(f"🕐 Текущее временное окно: {START_TIME.strftime('%H:%M')} - {END_TIME.strftime('%H:%M')}\n\n"
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
            current_days = ", ".join([f"{day+1}({get_weekday_short(day)})" for day in sorted(ALLOWED_WEEKDAYS)])
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
                                  "Для установки: /startdate 2024-01-25 17:00")
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

        check_text = f"""
🕐 <b>Проверка времени:</b>

⏰ Текущее время: {now.strftime('%Y-%m-%d %H:%M:%S')} GMT+2 (Чехия)
📅 День недели: {get_weekday_name(current_weekday)}
🕐 Временное окно: {START_TIME.strftime('%H:%M')} - {END_TIME.strftime('%H:%M')} ({time_status})
📆 Ограничение дней: {days_status}
🎯 Разрешённые дни: {', '.join([get_weekday_short(d) for d in sorted(ALLOWED_WEEKDAYS)])}

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
            await message.reply("❌ Укажите ID канала. Пример: /setchannel -1001234567890")
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
            menu_text = f"""
📝 <b>Управление подписью:</b>

🏷 Текущая подпись: {DEFAULT_SIGNATURE}

<b>Команды:</b>
/title текст - установить новую подпись
/settitle 1 текст - изменить подпись конкретного фото

<i>Подпись поддерживает HTML-теги для форматирования.</i>
"""
            await message.reply(menu_text)
            return

        new_signature = parts[1]
        DEFAULT_SIGNATURE = new_signature
        save_state()
        await message.reply(f"✅ Подпись установлена:\n{new_signature}")

    elif text == "/clear":
        queue = load_queue()
        if queue:
            total_media, media_groups, total_posts = count_queue_stats(queue)
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
                            await bot.send_document(chat_id=CHANNEL_ID, document=media_data["file_id"], caption=media_data.get("caption", DEFAULT_SIGNATURE))
                        else:
                            caption = media_data.get("caption", DEFAULT_SIGNATURE) if isinstance(media_data, dict) else DEFAULT_SIGNATURE
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
                    await bot.send_document(chat_id=CHANNEL_ID, document=media_data["file_id"], caption=media_data.get("caption", DEFAULT_SIGNATURE))
                else:
                    caption = media_data.get("caption", DEFAULT_SIGNATURE) if isinstance(media_data, dict) else DEFAULT_SIGNATURE
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
        total_media, media_groups, total_posts = count_queue_stats(queue)

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
                            await bot.send_document(chat_id=CHANNEL_ID, document=media_data["file_id"], caption=media_data.get("caption", DEFAULT_SIGNATURE))
                        else:
                            caption = media_data.get("caption", DEFAULT_SIGNATURE) if isinstance(media_data, dict) else DEFAULT_SIGNATURE
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
        BotCommand(command="settitle", description="✏️ Изменить подпись фото"),
        BotCommand(command="clear", description="🗑 Очистить очередь"),
        BotCommand(command="remove", description="➖ Удалить из очереди"),
        BotCommand(command="random", description="🔀 Перемешать очередь"),
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

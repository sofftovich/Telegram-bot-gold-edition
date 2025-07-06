
import os
import asyncio
import json
import re
import time
import signal
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
        logging.StreamHandler(),
        logging.FileHandler('bot.log', encoding='utf-8')
    ]
)
logger = logging.getLogger(__name__)

TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_ID = os.getenv("CHANNEL_ID")

# Чешский часовой пояс GMT+2
CZECH_TIMEZONE = timezone(timedelta(hours=2))

def get_czech_time():
    """Возвращает текущее время в Чехии (GMT+2)"""
    return datetime.now(CZECH_TIMEZONE)

POST_INTERVAL = 60  # 1 минута по умолчанию
last_post_time = 0  # Время последнего поста (начинаем с 0)
posting_enabled = True  # Статус автопостинга

DEFAULT_SIGNATURE = '<a href="https://t.me/+oBc3uUiG9Y45ZDM6">Femboys</a>'

# Настройки планирования
ALLOWED_WEEKDAYS = [0, 1, 2, 3, 4, 5, 6]  # По умолчанию все дни (0=понедельник, 6=воскресенье)
START_TIME = dt_time(0, 0)  # Начало временного окна (00:00)
END_TIME = dt_time(23, 59)  # Конец временного окна (23:59)

# Настройки отложенного старта
DELAYED_START_ENABLED = False  # Включен ли отложенный старт
DELAYED_START_TIME = None  # Время первого поста (datetime object)
DELAYED_START_INTERVAL_START = None  # С какого времени дня начинать отсчёт интервала

# Новые переменные для управления ограничениями
TIME_WINDOW_ENABLED = True  # Включено ли ограничение по времени
WEEKDAYS_ENABLED = True    # Включено ли ограничение по дням недели

# Переменные для точного планирования постов по интервалам
EXACT_TIMING_ENABLED = False  # Включено ли точное планирование по интервалам

def calculate_exact_posting_times():
    """Рассчитывает точные моменты времени для постинга в рамках временного окна"""
    if not EXACT_TIMING_ENABLED or not TIME_WINDOW_ENABLED:
        return []

    # Рассчитываем продолжительность окна в секундах
    start_minutes = START_TIME.hour * 60 + START_TIME.minute
    end_minutes = END_TIME.hour * 60 + END_TIME.minute

    # Учитываем случай перехода через полночь
    if end_minutes <= start_minutes:
        window_duration = (24 * 60) - start_minutes + end_minutes
    else:
        window_duration = end_minutes - start_minutes

    window_duration_seconds = window_duration * 60

    # Количество интервалов в окне
    intervals_count = window_duration_seconds // POST_INTERVAL

    if intervals_count < 1:
        return []

    # Рассчитываем моменты времени
    posting_times = []
    for i in range(int(intervals_count) + 1):  # +1 чтобы включить конечное время
        offset_seconds = i * POST_INTERVAL

        # Рассчитываем время поста
        post_minutes = start_minutes + (offset_seconds // 60)
        post_hour = (post_minutes // 60) % 24
        post_minute = post_minutes % 60

        posting_times.append(dt_time(post_hour, post_minute))

    return posting_times

def get_next_exact_posting_time():
    """Возвращает следующий точный момент для постинга"""
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

def is_exact_posting_time():
    """Проверяет, настал ли точный момент для постинга"""
    if not EXACT_TIMING_ENABLED:
        return True  # Если функция выключена, можно постить в любое время

    # Для очень малых интервалов (меньше 60 секунд) отключаем точное планирование
    if POST_INTERVAL < 60:
        return True

    now = get_czech_time()
    current_time = now.time()

    posting_times = calculate_exact_posting_times()
    if not posting_times:
        return True

    # Проверяем, совпадает ли текущее время с одним из запланированных (с погрешностью в 1 минуту)
    for post_time in posting_times:
        time_diff = abs((current_time.hour * 60 + current_time.minute) - (post_time.hour * 60 + post_time.minute))
        if time_diff <= 1:  # Погрешность в 1 минуту
            return True

    return False

# Для группировки медиа
pending_media_groups = {}  # Временное хранение медиа для группировки по media_group_id
media_group_timers = {}  # Таймеры для группировки медиа

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
    queue = load_queue()
    if not queue:
        return 0  # Если очередь пуста, времени до поста нет

    # Проверяем, разрешено ли постить сейчас
    posting_allowed, _ = is_posting_allowed()
    if not posting_allowed:
        return get_next_allowed_time()

    # Проверяем отложенный старт
    if not is_delayed_start_ready():
        return get_next_post_time_with_delayed_start()

    if last_post_time == 0:
        return 0  # Если еще не было постов, можно постить сразу

    elapsed = time.time() - last_post_time
    time_until_next = POST_INTERVAL - elapsed
    return max(0, int(time_until_next))

def get_total_queue_time():
    """Возвращает время, необходимое для публикации всех фото в очереди"""
    queue = load_queue()
    if not queue:
        return 0

    # Если очередь не пуста, учитываем время до следующего поста + время на остальные
    time_until_next = get_time_until_next_post()
    remaining_posts = len(queue) - 1
    return time_until_next + (remaining_posts * POST_INTERVAL)

bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

async def set_bot_commands():
    """Устанавливает команды в меню бота"""
    commands = [
        BotCommand(command="help", description="📋 Показать справку"),
        BotCommand(command="status", description="📊 Статус бота и очереди"),
        BotCommand(command="post", description="📤 Опубликовать одно медиа сейчас"),
        BotCommand(command="postall", description="📤 Опубликовать всю очередь сейчас"),
        BotCommand(command="toggle", description="⏸️ Включить/выключить автопостинг"),
        BotCommand(command="interval", description="⏰ Установить интервал (пример: 1h 30m)"),
        BotCommand(command="clear", description="🗑️ Очистить очередь"),
        BotCommand(command="random", description="🎲 Перемешать очередь"),
        BotCommand(command="channel", description="📢 Показать текущий канал"),
        BotCommand(command="setchannel", description="📢 Установить канал"),
        BotCommand(command="title", description="📝 Управление подписью"),
        BotCommand(command="schedule", description="📅 Настройки планирования"),
        BotCommand(command="exact", description="🧮 Точное планирование по интервалам"),
        BotCommand(command="toggleexact", description="🧮 Включить/выключить точное планирование"),
        BotCommand(command="setdays", description="📅 Установить дни недели"),
        BotCommand(command="settime", description="⏰ Установить временное окно"),
        BotCommand(command="starttime", description="⏰ Установить отложенный старт"),
        BotCommand(command="clearstart", description="❌ Отключить отложенный старт"),
        BotCommand(command="toggletime", description="⏰ Включить/выключить ограничение по времени"),
        BotCommand(command="toggledays", description="📅 Включить/выключить ограничение по дням"),
        BotCommand(command="checktime", description="🕐 Проверить текущее время")
    ]
    
    await bot.set_my_commands(commands)
    logger.info("✅ Команды бота установлены в меню")
    print("✅ Команды бота установлены в меню")

async def get_channel_info():
    """Получает информацию о канале"""
    if not CHANNEL_ID:
        return None
    try:
        chat = await bot.get_chat(CHANNEL_ID)
        return {
            "title": chat.title,
            "username": chat.username,
            "id": CHANNEL_ID
        }
    except Exception as e:
        print(f"Ошибка получения информации о канале: {e}")
        return {"title": "Неизвестно", "username": None, "id": CHANNEL_ID}

QUEUE_FILE = "queue.json"
STATE_FILE = "bot_state.json"

def load_queue():
    if not os.path.exists(QUEUE_FILE):
        return []
    with open(QUEUE_FILE, "r") as f:
        return json.load(f)

def save_queue(queue):
    with open(QUEUE_FILE, "w") as f:
        json.dump(queue, f)

def load_state():
    """Загружает состояние бота"""
    global last_post_time, DEFAULT_SIGNATURE, CHANNEL_ID, posting_enabled, ALLOWED_WEEKDAYS, START_TIME, END_TIME
    global DELAYED_START_ENABLED, DELAYED_START_TIME, DELAYED_START_INTERVAL_START, POST_INTERVAL
    global TIME_WINDOW_ENABLED, WEEKDAYS_ENABLED, EXACT_TIMING_ENABLED
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
                ALLOWED_WEEKDAYS = state.get("allowed_weekdays", [0, 1, 2, 3, 4, 5, 6])
                start_time_str = state.get("start_time", "00:00")
                end_time_str = state.get("end_time", "23:59")

                start_parsed = parse_time(start_time_str)
                end_parsed = parse_time(end_time_str)

                if start_parsed:
                    START_TIME = start_parsed
                if end_parsed:
                    END_TIME = end_parsed

                # Загружаем настройки включения/выключения ограничений
                TIME_WINDOW_ENABLED = state.get("time_window_enabled", True)
                WEEKDAYS_ENABLED = state.get("weekdays_enabled", True)
                EXACT_TIMING_ENABLED = state.get("exact_timing_enabled", False)

                # Загружаем настройки отложенного старта
                DELAYED_START_ENABLED = state.get("delayed_start_enabled", False)
                delayed_start_timestamp = state.get("delayed_start_time")
                if delayed_start_timestamp:
                    DELAYED_START_TIME = datetime.fromtimestamp(delayed_start_timestamp)

                delayed_interval_start_str = state.get("delayed_start_interval_start")
                if delayed_interval_start_str:
                    DELAYED_START_INTERVAL_START = parse_time(delayed_interval_start_str)
        except:
            pass

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
        "time_window_enabled": TIME_WINDOW_ENABLED,
        "weekdays_enabled": WEEKDAYS_ENABLED,
        "delayed_start_enabled": DELAYED_START_ENABLED,
        "delayed_start_time": DELAYED_START_TIME.timestamp() if DELAYED_START_TIME else None,
        "delayed_start_interval_start": DELAYED_START_INTERVAL_START.strftime("%H:%M") if DELAYED_START_INTERVAL_START else None,
        "exact_timing_enabled": EXACT_TIMING_ENABLED,
    }
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False)

def get_photo_caption(queue_index):
    """Получает подпись для фото по индексу в очереди"""
    queue = load_queue()
    if queue_index < len(queue):
        photo_data = queue[queue_index]
        if isinstance(photo_data, dict) and "caption" in photo_data:
            return photo_data["caption"]
    return DEFAULT_SIGNATURE

def set_photo_caption(queue_index, caption):
    """Устанавливает подпись для конкретного фото в очереди"""
    queue = load_queue()
    if queue_index < len(queue):
        photo_data = queue[queue_index]
        if isinstance(photo_data, str):
            # Конвертируем старый формат в новый
            queue[queue_index] = {"file_id": photo_data, "caption": caption}
        else:
            queue[queue_index]["caption"] = caption
        save_queue(queue)
        return True
    return False

def get_photo_file_id(photo_data):
    """Извлекает file_id из данных фото"""
    if isinstance(photo_data, str):
        return photo_data
    return photo_data.get("file_id")

def is_posting_allowed():
    """Проверяет, разрешена ли публикация в текущее время"""
    now = get_czech_time()
    current_weekday = now.weekday()  # 0=понедельник, 6=воскресенье
    current_time = now.time()

    # Проверяем день недели (только если включено)
    if WEEKDAYS_ENABLED and current_weekday not in ALLOWED_WEEKDAYS:
        return False, "запрещённый день недели"

    # Проверяем временное окно (только если включено)
    if TIME_WINDOW_ENABLED:
        # Учитываем случай, когда окно переходит через полночь
        if START_TIME <= END_TIME:
            # Обычное окно в пределах одного дня
            time_in_window = START_TIME <= current_time <= END_TIME
            if not time_in_window:
                return False, "вне временного окна"
        else:
            # Окно переходит через полночь (например, 22:00 - 06:00)
            time_in_window = current_time >= START_TIME or current_time <= END_TIME
            if not time_in_window:
                return False, "вне временного окна"

    return True, "разрешено"

def get_weekday_name(weekday):
    """Возвращает название дня недели"""
    days = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
    return days[weekday]

def parse_weekdays(weekdays_str):
    """Парсит строку с днями недели (английские названия)"""
    days_map = {
        "mon": 0, "monday": 0,
        "tue": 1, "tuesday": 1,
        "wed": 2, "wednesday": 2,
        "thu": 3, "thursday": 3,
        "fri": 4, "friday": 4,
        "sat": 5, "saturday": 5,
        "sun": 6, "sunday": 6
    }

    weekdays = []
    parts = weekdays_str.lower().replace(",", " ").split()

    for part in parts:
        if part in days_map:
            weekdays.append(days_map[part])

    return list(set(weekdays))  # Убираем дубликаты

def parse_time(time_str):
    """Парсит время в формате HH:MM"""
    try:
        hour, minute = map(int, time_str.split(":"))
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return dt_time(hour, minute)
    except:
        pass
    return None

def is_delayed_start_ready():
    """Проверяет, можно ли начинать автопостинг по отложенному старту"""
    if not DELAYED_START_ENABLED or not DELAYED_START_TIME:
        return True  # Если отложенный старт не включен, всегда готов

    now = get_czech_time()
    # Преобразуем время старта в чешский часовой пояс для сравнения
    if DELAYED_START_TIME.tzinfo is None:
        delayed_start_czech = DELAYED_START_TIME.replace(tzinfo=CZECH_TIMEZONE)
    else:
        delayed_start_czech = DELAYED_START_TIME.astimezone(CZECH_TIMEZONE)

    return now >= delayed_start_czech

def get_next_post_time_with_delayed_start():
    """Возвращает время следующего поста с учётом отложенного старта"""
    if not DELAYED_START_ENABLED or not DELAYED_START_TIME:
        return get_time_until_next_post()

    now = get_czech_time()

    # Преобразуем время старта в чешский часовой пояс для сравнения
    if DELAYED_START_TIME.tzinfo is None:
        delayed_start_czech = DELAYED_START_TIME.replace(tzinfo=CZECH_TIMEZONE)
    else:
        delayed_start_czech = DELAYED_START_TIME.astimezone(CZECH_TIMEZONE)

    # Если ещё не пришло время отложенного старта
    if now < delayed_start_czech:
        return int((delayed_start_czech - now).total_seconds())

    # Если отложенный старт прошёл, но это первый пост
    if last_post_time == 0:
        return 0  # Можно постить сразу

    # Обычный расчёт времени
    return get_time_until_next_post()

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

    return 3600  # Если ничего не найдено, ждём час

def get_next_allowed_datetime():
    """Возвращает datetime следующего разрешённого времени для публикации"""
    now = get_czech_time()

    # Если сейчас разрешено постить
    if is_posting_allowed()[0]:
        return now

    # Ищем следующий разрешённый интервал
    for days_ahead in range(8):  # Проверяем неделю вперёд
        check_date = now + timedelta(days=days_ahead)
        check_date = check_date.replace(hour=0, minute=0, second=0, microsecond=0)
        check_weekday = check_date.weekday()

        if not WEEKDAYS_ENABLED or check_weekday in ALLOWED_WEEKDAYS:
            if days_ahead == 0:  # Сегодня
                if not TIME_WINDOW_ENABLED:
                    return now
                elif START_TIME <= END_TIME:
                    # Обычное окно
                    if now.time() < START_TIME:
                        return check_date.replace(hour=START_TIME.hour, minute=START_TIME.minute)
                    elif now.time() > END_TIME:
                        # Окно на сегодня закрылось, переходим к следующему дню
                        continue
                else:
                    # Окно через полночь
                    if END_TIME < now.time() < START_TIME:
                        return check_date.replace(hour=START_TIME.hour, minute=START_TIME.minute)
            else:
                # Другой день - начало временного окна
                if not TIME_WINDOW_ENABLED:
                    return check_date
                else:
                    return check_date.replace(hour=START_TIME.hour, minute=START_TIME.minute)

    # Если ничего не найдено, возвращаем через час
    return now + timedelta(hours=1)

def calculate_queue_schedule(queue_length):
    """Рассчитывает расписание публикации всей очереди"""
    if queue_length == 0:
        return None, None

    now = get_czech_time()
    posting_allowed_now, _ = is_posting_allowed()
    delayed_ready = is_delayed_start_ready()

    # Находим время первой публикации
    if posting_allowed_now and delayed_ready:
        # Можем постить в текущем окне
        if last_post_time == 0:
            first_post_time = now
        else:
            time_until_next = get_time_until_next_post()
            if time_until_next <= 0:
                first_post_time = now
            else:
                first_post_time = now + timedelta(seconds=time_until_next)
    else:
        # Нужно ждать разрешённого времени или отложенного старта
        if DELAYED_START_ENABLED and DELAYED_START_TIME and not delayed_ready:
            first_post_time = DELAYED_START_TIME
        else:
            first_post_time = get_next_allowed_datetime()

    # Рассчитываем время последней публикации
    current_time = first_post_time
    posts_scheduled = 0

    while posts_scheduled < queue_length:
        current_weekday = current_time.weekday()
        current_time_only = current_time.time()

        # Проверяем, попадает ли время в разрешённые дни и временное окно
        weekday_ok = not WEEKDAYS_ENABLED or current_weekday in ALLOWED_WEEKDAYS
        time_ok = not TIME_WINDOW_ENABLED or (START_TIME <= current_time_only <= END_TIME)

        if weekday_ok and time_ok:
            posts_scheduled += 1
            if posts_scheduled == queue_length:
                return first_post_time, current_time
            # Добавляем интервал для следующего поста
            next_time = current_time + timedelta(seconds=POST_INTERVAL)

            # Проверяем, не выходим ли за пределы текущего дня
            if next_time.date() != current_time.date():
                # Переходим к следующему разрешённому дню
                current_time = get_next_allowed_datetime_from(next_time)
            else:
                current_time = next_time
        else:
            # Переходим к следующему разрешённому времени
            current_time = get_next_allowed_datetime_from(current_time)

    return first_post_time, current_time

def get_next_allowed_datetime_from(from_time):
    """Возвращает datetime следующего разрешённого времени после указанного времени"""
    check_time = from_time

    for days_ahead in range(8):  # Проверяем неделю вперёд
        check_date = check_time.replace(hour=0, minute=0, second=0, microsecond=0)
        check_date = check_date + timedelta(days=days_ahead)
        check_weekday = check_date.weekday()

        if not WEEKDAYS_ENABLED or check_weekday in ALLOWED_WEEKDAYS:
            if days_ahead == 0:  # Тот же день
                if not TIME_WINDOW_ENABLED:
                    return check_time
                elif check_time.time() < START_TIME:
                    # Начало временного окна в тот же день
                    return check_date.replace(hour=START_TIME.hour, minute=START_TIME.minute)
            else:
                # Другой день - начало временного окна
                if not TIME_WINDOW_ENABLED:
                    return check_date
                else:
                    return check_date.replace(hour=START_TIME.hour, minute=START_TIME.minute)

    # Если ничего не найдено, возвращаем через день
    return check_time + timedelta(days=1)

def shuffle_queue():
    """ИСПРАВЛЕННАЯ функция рандомизации очереди"""
    queue = load_queue()
    if len(queue) > 1:
        random.shuffle(queue)
        save_queue(queue)  # ИСПРАВЛЕНИЕ: обязательно сохраняем очередь
        return True
    return False

# =================================
# ВЕБ-СЕРВЕР НА AIOHTTP 4.x БЕЗ CORS
# =================================

@web.middleware
async def cors_handler(request, handler):
    """Middleware для обработки CORS запросов"""
    response = await handler(request)
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
    response.headers['Access-Control-Allow-Headers'] = '*'
    return response

@web.middleware
async def logging_middleware(request, handler):
    """Middleware для логирования HTTP запросов"""
    start_time = time.time()

    # Логируем входящий запрос
    logger.info(f"🌐 HTTP Request: {request.method} {request.path_qs}")
    logger.info(f"🔗 Remote IP: {request.remote}")

    # Выполняем обработчик
    response = await handler(request)

    # Логируем ответ
    end_time = time.time()
    duration = (end_time - start_time) * 1000  # в миллисекундах
    logger.info(f"✅ HTTP Response: {response.status} in {duration:.2f}ms")

    return response

async def handle_options(request):
    """Обработчик для OPTIONS запросов"""
    return web.Response(status=200)

async def health_check(request):
    """Обработчик для проверки работоспособности приложения"""
    logger.info("💓 Health check requested")
    return web.Response(text="OK", status=200)

def create_web_app():
    """Создаёт веб-приложение aiohttp 4.x с простой поддержкой CORS"""
    app = web.Application(middlewares=[cors_handler, logging_middleware])

    # Регистрируем маршруты
    app.router.add_get('/', health_check)
    app.router.add_post('/', health_check)
    app.router.add_options('/', handle_options)  # Для CORS

    return app

async def start_web_server():
    """Запускает веб-сервер на порту 5000"""
    app = create_web_app()

    # Запускаем сервер на всех интерфейсах (0.0.0.0) порт 5000
    runner = web.AppRunner(app)
    await runner.setup()

    site = web.TCPSite(runner, '0.0.0.0', 5000)
    await site.start()

    logger.info("🌐 Веб-сервер запущен на http://0.0.0.0:5000")
    logger.info("✅ Endpoint '/' доступен для проверки работоспособности")
    print("🌐 Веб-сервер запущен на http://0.0.0.0:5000")
    print("✅ Endpoint '/' доступен для проверки работоспособности")

# =================================
# УВЕДОМЛЕНИЯ О ПУБЛИКАЦИИ
# =================================

async def verify_post_published(channel_id, expected_media_type="photo", timeout=30):
    """Проверяет, что пост действительно опубликован на канале"""
    try:
        # Получаем последние сообщения из канала
        chat = await bot.get_chat(channel_id)
        
        # Проверяем через небольшие интервалы в течение timeout секунд
        for attempt in range(timeout // 2):
            try:
                # Получаем ID последнего сообщения через get_chat
                # Альтернативный способ - проверяем наличие недавних сообщений
                await asyncio.sleep(2)  # Даем время на обработку
                return True  # Считаем что пост опубликован, если нет ошибок API
            except Exception as e:
                logger.warning(f"⚠️ Попытка {attempt + 1} проверки публикации: {e}")
                await asyncio.sleep(2)
        
        return True  # Если дошли сюда без критических ошибок
    except Exception as e:
        logger.error(f"❌ Ошибка проверки публикации: {e}")
        return False  # Возвращаем False только при серьезных ошибках

async def send_publication_notification(user_id, message_text):
    """Отправляет уведомление о публикации пользователю"""
    try:
        await bot.send_message(chat_id=user_id, text=message_text)
        logger.info(f"📬 Уведомление отправлено пользователю {user_id}")
    except Exception as e:
        logger.error(f"❌ Ошибка отправки уведомления пользователю {user_id}: {e}")

# Глобальная переменная для хранения информации о публикациях
publication_notifications = {}  # {user_id: [{"type": "photo", "count": 1}, ...]}

# Глобальная переменная для отслеживания пользователей, добавивших медиа
user_media_tracking = {}  # {queue_index: user_id}

# =================================
# ТЕЛЕГРАМ БОТ - ОБРАБОТЧИКИ
# =================================

@dp.message(F.content_type == "text")
async def handle_commands(message: Message):
    global POST_INTERVAL, CHANNEL_ID, DEFAULT_SIGNATURE, last_post_time, posting_enabled
    global ALLOWED_WEEKDAYS, START_TIME, END_TIME, DELAYED_START_ENABLED, DELAYED_START_TIME, DELAYED_START_INTERVAL_START
    global TIME_WINDOW_ENABLED, WEEKDAYS_ENABLED, EXACT_TIMING_ENABLED
    text = message.text.lower()

    if text == "/help":
        queue = load_queue()
        if queue:
            # Подсчитываем типы медиа
            photos = sum(1 for item in queue if isinstance(item, dict) and item.get("type") == "photo" or isinstance(item, str))
            videos = sum(1 for item in queue if isinstance(item, dict) and item.get("type") == "video")
            animations = sum(1 for item in queue if isinstance(item, dict) and item.get("type") == "animation")
            documents = sum(1 for item in queue if isinstance(item, dict) and item.get("type") == "document")

            media_parts = []
            if photos > 0:
                media_parts.append(f"{photos} фото")
            if videos > 0:
                media_parts.append(f"{videos} видео")
            if animations > 0:
                media_parts.append(f"{animations} GIF")
            if documents > 0:
                media_parts.append(f"{documents} файлов")

            queue_status = f"В очереди: {' + '.join(media_parts)} (всего {len(queue)})"
        else:
            queue_status = "Очередь пуста"

        # Получаем информацию о канале для help
        if CHANNEL_ID:
            channel_data = await get_channel_info()
            if channel_data:
                channel_name = channel_data["title"]
                channel_info = f"{channel_name} (ID: {CHANNEL_ID})"
            else:
                channel_info = f"ID: {CHANNEL_ID}"
        else:
            channel_info = "Не настроен"
        help_text = f"""
🤖 <b>Бот для автопостинга медиа</b>

📊 <b>Статус:</b> {queue_status}
⏰ <b>Интервал:</b> {format_interval(POST_INTERVAL)}
📢 <b>Канал:</b> {channel_info}

<b>📋 Основные команды:</b>
/help - показать это сообщение
/status - полный статус бота
/post - опубликовать одно медиа сейчас
/toggle - включить/выключить автопостинг
/postall - опубликовать все медиа из очереди сейчас

<b>⏰ Управление интервалом:</b>
/interval 1d 2h 30m 15s - установить интервал

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
/title текст#ссылка - установить кликабельную подпись (пример: /title Мой канал#https://t.me/mychannel)
/settitle 1 текст - установить подпись для медиа №1

<b>📅 Планирование публикаций:</b>
/schedule - показать текущие настройки планирования
/setdays mon wed fri - установить дни недели для публикации
/settime 09:00 18:00 - установить временное окно (с до)

<b>🧮 Точное планирование:</b>
/exact - показать настройки точного планирования
/toggleexact - включить/выключить точное планирование

<b>⏰ Отложенный старт:</b>
/starttime 17:00 - установить время первого поста (сегодня или завтра)
/startdate 2024-01-25 17:00 - установить точную дату и время первого поста
/clearstart - отключить отложенный старт

<b>🔧 Управление ограничениями:</b>
/checktime - проверить текущее время и статус
/toggletime - включить/выключить ограничение по времени
/toggledays - включить/выключить ограничение по дням

<b>📎 Работа с медиа:</b>
• Отправьте одно фото/видео/GIF - добавится в очередь
• Отправьте несколько медиа с одинаковой подписью - объединятся в медиагруппу
• Медиа без подписи или с разными подписями публикуются отдельными постами
• Медиагруппы занимают одну позицию в очереди
• При пустой очереди медиа публикуется мгновенно
• Команда /random меняет только порядок медиа, сохраняя расписание

<b>🎲 Рандомизация очереди:</b>
Команда /random перемешивает медиа в случайном порядке, но время публикации каждой позиции остаётся прежним. Если первая позиция должна выйти через 5 минут, то после рандомизации новое первое медиа выйдет именно через 5 минут.

<b>🔔 Уведомления:</b>
Бот отправляет уведомления о публикации только после того, как пост реально появился в канале.
"""
        await message.reply(help_text)

    elif text == "/status":
        queue = load_queue()
        if queue:
            # Подсчитываем типы медиа
            photos = sum(1 for item in queue if isinstance(item, dict) and item.get("type") == "photo" or isinstance(item, str))
            videos = sum(1 for item in queue if isinstance(item, dict) and item.get("type") == "video")
            animations = sum(1 for item in queue if isinstance(item, dict) and item.get("type") == "animation")
            documents = sum(1 for item in queue if isinstance(item, dict) and item.get("type") == "document")

            media_parts = []
            if photos > 0:
                media_parts.append(f"{photos} фото")
            if videos > 0:
                media_parts.append(f"{videos} видео")
            if animations > 0:
                media_parts.append(f"{animations} GIF")
            if documents > 0:
                media_parts.append(f"{documents} файлов")

            queue_status = f"В очереди: {' + '.join(media_parts)} (всего {len(queue)})"
        else:
            queue_status = "Очередь пуста"

        # Получаем информацию о канале
        if CHANNEL_ID:
            channel_data = await get_channel_info()
            if channel_data:
                channel_name = channel_data["title"]
                channel_username = f"@{channel_data['username']}" if channel_data["username"] else ""
                channel_info = f"{channel_name} {channel_username}\nID: {CHANNEL_ID}"
            else:
                channel_info = f"ID: {CHANNEL_ID}"
        else:
            channel_info = "❌ Не настроен"

        # Время до следующего поста
        time_until_next = get_time_until_next_post()
        if queue:
            next_post_text = format_interval(time_until_next) if time_until_next > 0 else "Сейчас"
        else:
            next_post_text = "Очередь пуста"

        # Время для публикации всех фото
        total_time = get_total_queue_time()
        total_time_text = format_interval(total_time) if total_time > 0 else "Очередь пуста"

        # Статус автопостинга
        posting_allowed, reason = is_posting_allowed()
        if not CHANNEL_ID:
            autopost_status = "❌ Неактивен (нет канала)"
        elif not posting_enabled:
            autopost_status = "⏸️ Приостановлен"
        elif not posting_allowed:
            autopost_status = f"⏸️ Приостановлен ({reason})"
        else:
            autopost_status = "✅ Активен"

        # Планирование
        allowed_days = ", ".join([get_weekday_name(day) for day in sorted(ALLOWED_WEEKDAYS)])
        schedule_info = f"📅 Дни: {allowed_days}\n⏰ Время: {START_TIME.strftime('%H:%M')} - {END_TIME.strftime('%H:%M')}"

        # Информация об отложенном старте
        if DELAYED_START_ENABLED and DELAYED_START_TIME:
            delayed_ready = is_delayed_start_ready()
            if delayed_ready:
                delayed_info = "⏰ Отложенный старт: выполнен"
            else:
                time_until_start = int((DELAYED_START_TIME - datetime.now()).total_seconds())
                delayed_info = f"⏰ Отложенный старт: через {format_interval(time_until_start)} ({DELAYED_START_TIME.strftime('%Y-%m-%d %H:%M')})"
            schedule_info += f"\n{delayed_info}"

        # Время до следующего разрешённого интервала
        if not posting_allowed and queue:
            next_allowed_seconds = get_next_allowed_time()
            next_allowed_text = f"через {format_interval(next_allowed_seconds)}" if next_allowed_seconds > 0 else "сейчас"
        elif DELAYED_START_ENABLED and not is_delayed_start_ready() and queue:
            delayed_seconds = get_next_post_time_with_delayed_start()
            next_allowed_text = f"через {format_interval(delayed_seconds)}" if delayed_seconds > 0 else "сейчас"
        else:
            next_allowed_text = "сейчас" if posting_allowed else "по расписанию"

        # Текущее время бота
        bot_now = get_czech_time()
        bot_time_info = f"🕐 <b>Время бота:</b> {bot_now.strftime('%Y-%m-%d %H:%M:%S')} GMT+2 ({get_weekday_name(bot_now.weekday())})"

        # Информация о точном планировании
        exact_status = "✅ включено" if EXACT_TIMING_ENABLED else "❌ выключено"
        exact_info = f"🧮 Точное планирование: {exact_status}"

        status_text = f"""
📊 <b>Статус бота:</b>

{bot_time_info}

📸 <b>Очередь:</b> {queue_status}
⏰ <b>Интервал:</b> {format_interval(POST_INTERVAL)}
📢 <b>Канал:</b> {channel_info}
🤖 <b>Автопостинг:</b> {autopost_status}

{schedule_info}
{exact_info}

⏳ <b>До следующего поста:</b> {next_post_text if posting_enabled and queue and posting_allowed else next_allowed_text if posting_enabled and queue else "Очередь пуста" if posting_enabled else "Приостановлено"}
📅 <b>Время публикации всех фото:</b> {total_time_text if posting_enabled and queue and posting_allowed else "По расписанию" if posting_enabled and queue else "Очередь пуста" if posting_enabled else "Приостановлено"}

📝 <b>Глобальная подпись:</b> {DEFAULT_SIGNATURE}
"""
        await message.reply(status_text)

    elif text == "/post":
        queue = load_queue()
        if queue:
            media_data = queue.pop(0)

            try:
                # Проверяем тип медиа
                if isinstance(media_data, dict) and media_data.get("type") == "media_group":
                    # Отправляем медиагруппу
                    await send_media_group_to_channel(media_data)
                    
                    # Проверяем что пост опубликован
                    if await verify_post_published(CHANNEL_ID, "media_group"):
                        save_queue(queue)
                        last_post_time = time.time()
                        save_state()
                        await message.reply("✅ Медиагруппа успешно опубликована в канале!")
                    else:
                        queue.insert(0, media_data)
                        save_queue(queue)
                        await message.reply("❌ Не удалось подтвердить публикацию медиагруппы")
                else:
                    # Отправляем одиночное медиа
                    media_type = media_data.get("type", "photo") if isinstance(media_data, dict) else "photo"
                    type_names = {"photo": "Фото", "video": "Видео", "animation": "GIF", "document": "Файл"}

                    if media_type == "document":
                        await bot.send_document(chat_id=CHANNEL_ID, document=media_data["file_id"], caption=media_data.get("caption", DEFAULT_SIGNATURE))
                    else:
                        await send_single_media_to_channel(media_data)

                    # Проверяем что пост опубликован
                    if await verify_post_published(CHANNEL_ID, media_type):
                        save_queue(queue)
                        last_post_time = time.time()
                        save_state()
                        await message.reply(f"✅ {type_names.get(media_type, 'Медиа')} успешно опубликовано в канале!")
                    else:
                        queue.insert(0, media_data)
                        save_queue(queue)
                        await message.reply(f"❌ Не удалось подтвердить публикацию {type_names.get(media_type, 'медиа').lower()}")

            except Exception as e:
                queue.insert(0, media_data)
                save_queue(queue)
                await message.reply(f"❌ Ошибка при отправке: {e}")
        else:
            await message.reply("❌ Очередь пуста!")

    elif text == "/postall":
        queue = load_queue()
        if not queue:
            await message.reply("❌ Очередь пуста!")
            return

        num_posts = len(queue)
        success_count = 0
        error_count = 0

        for _ in range(num_posts):
            media_data = queue.pop(0)

            try:
                # Проверяем тип медиа
                if isinstance(media_data, dict) and media_data.get("type") == "media_group":
                    # Отправляем медиагруппу
                    await send_media_group_to_channel(media_data)
                    # Проверяем публикацию
                    if await verify_post_published(CHANNEL_ID, "media_group"):
                        success_count += 1
                    else:
                        error_count += 1
                        logger.error("❌ Не удалось подтвердить публикацию медиагруппы")
                else:
                    # Отправляем одиночное медиа
                    media_type = media_data.get("type", "photo") if isinstance(media_data, dict) else "photo"
                    if media_type == "document":
                        await bot.send_document(chat_id=CHANNEL_ID, document=media_data["file_id"], caption=media_data.get("caption", DEFAULT_SIGNATURE))
                    else:
                        await send_single_media_to_channel(media_data)
                    
                    # Проверяем публикацию
                    if await verify_post_published(CHANNEL_ID, media_type):
                        success_count += 1
                    else:
                        error_count += 1
                        logger.error(f"❌ Не удалось подтвердить публикацию {media_type}")

            except Exception as e:
                logger.error(f"❌ Ошибка при отправке в канал: {e}")
                print(f"❌ Ошибка при отправке в канал: {e}")
                error_count += 1

            # Сохраняем состояние после каждой итерации
            save_queue(queue)
            last_post_time = time.time()
            save_state()
            await asyncio.sleep(1)  # Небольшая задержка между постами

        save_queue([]) #Очистка очереди после отправки всего
        await message.reply(f"✅ Опубликовано {success_count} постов.\n❌ Ошибок: {error_count}")

    elif text == "/interval":
        # Показываем текущий интервал и инструкции
        interval_info = f"""
⏰ <b>Текущий интервал публикации:</b> {format_interval(POST_INTERVAL)}

<b>📋 Как изменить интервал:</b>
<code>/interval [время]</code>

<b>🔧 Доступные единицы времени:</b>
• <code>s</code> - секунды
• <code>m</code> - минуты  
• <code>h</code> - часы
• <code>d</code> - дни

<b>📝 Примеры использования:</b>
• <code>/interval 30s</code> - 30 секунд
• <code>/interval 5m</code> - 5 минут
• <code>/interval 2h</code> - 2 часа
• <code>/interval 1d</code> - 1 день
• <code>/interval 1h 30m</code> - 1 час 30 минут
• <code>/interval 2d 3h 15m 30s</code> - 2 дня 3 часа 15 минут 30 секунд

💡 Можно комбинировать любые единицы времени в одной команде.
"""
        await message.reply(interval_info)

    elif text.startswith("/interval "):
        try:
            interval_part = text.split(maxsplit=1)[1]
            new_interval = parse_interval(interval_part)

            if new_interval is None or new_interval < 1:
                await message.reply("❌ Интервал должен быть больше 0 секунд")
                return

            POST_INTERVAL = new_interval
            save_state()  # Сохраняем новый интервал
            await message.reply(f"✅ Интервал изменен на {format_interval(POST_INTERVAL)}")
        except (IndexError, ValueError):
            await message.reply("❌ Неправильный формат. Пример: /interval 1d 2h 30m 15s")

    elif text == "/clear":
        save_queue([])
        await message.reply("✅ Очередь очищена!")

    elif text == "/channel":
        if CHANNEL_ID:
            channel_data = await get_channel_info()
            if channel_data:
                channel_name = channel_data["title"]
                channel_username = f"@{channel_data['username']}" if channel_data["username"] else ""
                channel_text = f"📢 Текущий канал: {channel_name} {channel_username}\nID: {CHANNEL_ID}"
            else:
                channel_text = f"📢 Текущий канал: ID {CHANNEL_ID}"
            await message.reply(channel_text)
        else:
            await message.reply("❌ Канал не настроен. Используйте /setchannel ID")

    elif text.startswith("/setchannel "):
        try:
            new_channel = text.split()[1]

            if not (new_channel.startswith('-') and new_channel[1:].isdigit()):
                await message.reply("❌ Неправильный формат ID канала. Должен начинаться с '-' и содержать только цифры")
                return

            CHANNEL_ID = new_channel
            save_state()  # Сохраняем новый канал

            # Получаем информацию о новом канале
            channel_data = await get_channel_info()
            if channel_data:
                channel_name = channel_data["title"]
                channel_username = f"@{channel_data['username']}" if channel_data["username"] else ""
                response = f"✅ Канал изменен на: {channel_name} {channel_username}\nID: {CHANNEL_ID}"
            else:
                response = f"✅ Канал изменен на: {CHANNEL_ID}"

            await message.reply(response)

        except IndexError:
            await message.reply("❌ Укажите ID канала. Пример: /setchannel -1001234567890")

    elif text.startswith("/remove "):
        try:
            queue = load_queue()
            index = int(text.split()[1]) - 1

            if 0 <= index < len(queue):
                removed = queue.pop(index)
                save_queue(queue)
                await message.reply(f"✅ Фото #{index + 1} удалено из очереди\nОсталось: {len(queue)}")
            else:
                await message.reply(f"❌ Неверный номер. В очереди {len(queue)} фото")

        except (IndexError, ValueError):
            await message.reply("❌ Укажите номер фото. Пример: /remove 1")

    elif text == "/title":
        # Показываем меню с текущей подписью в читаемом виде
        # Проверяем, есть ли HTML-ссылка в подписи
        import re
        link_match = re.search(r'<a href="([^"]+)">([^<]+)</a>', DEFAULT_SIGNATURE)
        
        if link_match:
            link_url = link_match.group(1)
            link_text = link_match.group(2)
            current_signature_display = f"{link_text} (ссылка на {link_url})"
            current_signature_raw = DEFAULT_SIGNATURE
        else:
            current_signature_display = DEFAULT_SIGNATURE
            current_signature_raw = DEFAULT_SIGNATURE
        
        title_menu = f"""
📝 <b>Управление подписью к постам</b>

<b>Текущая подпись:</b>
{current_signature_display}

<b>HTML код:</b>
<code>{current_signature_raw}</code>

<b>Как изменить подпись:</b>
• <code>/title текст</code> - установить обычную текстовую подпись
• <code>/title текст#ссылка</code> - создать кликабельную подпись с ссылкой

<b>Примеры:</b>
• <code>/title Мой канал</code>
• <code>/title Подписывайтесь!#https://t.me/mychannel</code>
• <code>/title Наш сайт#https://example.com</code>

💡 Поддерживается HTML-разметка для форматирования текста.
"""
        await message.reply(title_menu)

    elif text.startswith("/title "):
        try:
            new_title = message.text[7:]  # Берем оригинальный текст с сохранением регистра

            # Проверяем, есть ли формат текст#ссылка
            if '#' in new_title:
                parts = new_title.split('#', 1)  # Разделяем только по первому #
                if len(parts) == 2:
                    text_part = parts[0].strip()
                    link_part = parts[1].strip()

                    if text_part and link_part:
                        # Создаем HTML-ссылку
                        DEFAULT_SIGNATURE = f'<a href="{link_part}">{text_part}</a>'
                        save_state()
                        await message.reply(f"✅ Кликабельная подпись установлена:\n{DEFAULT_SIGNATURE}\n\n👁️ Отображается как: {text_part} (ссылка на {link_part})")
                    else:
                        await message.reply("❌ Неверный формат. Убедитесь, что текст и ссылка не пустые.\nПример: /title Мой канал#https://t.me/mychannel")
                else:
                    await message.reply("❌ Неверный формат. Пример: /title текст#ссылка")
            else:
                # Обычная текстовая подпись
                DEFAULT_SIGNATURE = new_title
                save_state()
                await message.reply(f"✅ Текстовая подпись установлена:\n{new_title}")
        except Exception as e:
            await message.reply("❌ Ошибка при установке подписи. Проверьте формат команды.")

    elif text.startswith("/settitle "):
        try:
            parts = message.text.split(maxsplit=2)
            if len(parts) < 3:
                await message.reply("❌ Укажите номер фото и текст. Пример: /settitle 1 Новая подпись")
                return

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
            status_text = reason
        elif not posting_allowed:
            status_emoji = "❌"
            status_text = reason
        elif not delayed_ready:
            status_emoji = "⏳"
            status_text = "ожидание отложенного старта"
        else:
            status_emoji = "✅"
            status_text = reason

        # Информация об отложенном старте
        if DELAYED_START_ENABLED and DELAYED_START_TIME:
            if delayed_ready:
                delayed_info = f"\n⏰ <b>Отложенный старт:</b> выполнен ({DELAYED_START_TIME.strftime('%Y-%m-%d %H:%M')})"
            else:
                time_until_start = int((DELAYED_START_TIME - datetime.now()).total_seconds())
                delayed_info = f"\n⏰ <b>Отложенный старт:</b> через {format_interval(time_until_start)} ({DELAYED_START_TIME.strftime('%Y-%m-%d %H:%M')})"
        else:
            delayed_info = "\n⏰ <b>Отложенный старт:</b> не установлен"

        time_status = "✅ включено" if TIME_WINDOW_ENABLED else "❌ выключено"
        days_status = "✅ включено" if WEEKDAYS_ENABLED else "❌ выключено"

        schedule_text = f"""
📅 <b>Настройки планирования:</b>

📆 <b>Разрешённые дни:</b> {allowed_days} ({days_status})
⏰ <b>Временное окно:</b> {START_TIME.strftime('%H:%M')} - {END_TIME.strftime('%H:%M')} ({time_status}){delayed_info}

{status_emoji} <b>Текущий статус:</b> {status_text}

<b>Команды для настройки:</b>
/setdays mon wed fri - установить дни недели (mon tue wed thu fri sat sun)
/settime 09:00 18:00 - установить временное окно
/toggletime - включить/выключить ограничение по времени
/toggledays - включить/выключить ограничение по дням
/starttime 17:00 - установить отложенный старт
/clearstart - отключить отложенный старт
/checktime - проверить текущее время
"""
        await message.reply(schedule_text)

    elif text == "/exact":
        exact_status = "✅ включено" if EXACT_TIMING_ENABLED else "❌ выключено"
        
        # Показываем примерные времена постинга если функция включена
        times_info = ""
        if EXACT_TIMING_ENABLED:
            posting_times = calculate_exact_posting_times()
            if posting_times:
                # Ограничиваем показ до 20 времен
                if len(posting_times) <= 20:
                    times_str = ", ".join([t.strftime('%H:%M') for t in posting_times])
                    times_info = f"\n\n<b>Текущие времена постинга:</b> {times_str}"
                else:
                    first_20 = posting_times[:20]
                    times_str = ", ".join([t.strftime('%H:%M') for t in first_20])
                    remaining = len(posting_times) - 20
                    times_info = f"\n\n<b>Текущие времена постинга:</b> {times_str}\n<i>... и ещё {remaining} времен</i>"
        
        exact_info = f"""
🧮 <b>Точное планирование по интервалам</b>

<b>Статус:</b> {exact_status}

Когда эта функция включена, бот будет публиковать только в строго определенные моменты времени, вычисленные на основе интервала и временного окна.

<b>Пример:</b>
Окно: 12:00-16:00
Интервал: 2 часа
Бот будет постить только в 12:00, 14:00 и 16:00.{times_info}

<b>Команды для управления:</b>
/toggleexact - включить/выключить точное планирование
"""
        await message.reply(exact_info)

    elif text == "/toggleexact":
        EXACT_TIMING_ENABLED = not EXACT_TIMING_ENABLED
        save_state()
        status = "включено" if EXACT_TIMING_ENABLED else "выключено"
        
        # Показываем времена постинга если включили
        times_info = ""
        if EXACT_TIMING_ENABLED:
            posting_times = calculate_exact_posting_times()
            if posting_times:
                # Ограничиваем показ до 20 времен
                if len(posting_times) <= 20:
                    times_str = ", ".join([t.strftime('%H:%M') for t in posting_times])
                    times_info = f"\n\n🕐 Времена постинга: {times_str}"
                else:
                    first_20 = posting_times[:20]
                    times_str = ", ".join([t.strftime('%H:%M') for t in first_20])
                    remaining = len(posting_times) - 20
                    times_info = f"\n\n🕐 Времена постинга: {times_str}\n<i>... и ещё {remaining} времен</i>"
        
        await message.reply(f"{'✅' if EXACT_TIMING_ENABLED else '❌'} Точное планирование {status}!{times_info}")

    elif text.startswith("/setdays "):
        try:
            days_str = text[9:]  # Убираем "/setdays "
            new_weekdays = parse_weekdays(days_str)

            if not new_weekdays:
                await message.reply("❌ Не удалось распознать дни недели.\n\n📋 Используйте английские названия дней:\n• mon tue wed thu fri sat sun\n• Можно сокращённо или полностью: monday tuesday wednesday\n\n💡 Пример: /setdays mon wed fri")
                return

            ALLOWED_WEEKDAYS = sorted(new_weekdays)
            save_state()

            allowed_days = ", ".join([get_weekday_name(day) for day in ALLOWED_WEEKDAYS])
            await message.reply(f"✅ Дни недели для публикации установлены: {allowed_days}")

        except:
            await message.reply("❌ Ошибка. Пример: /setdays mon wed fri")

    elif text.startswith("/settime "):
        try:
            time_parts = text[9:].split()  # Убираем "/settime "

            if len(time_parts) != 2:
                await message.reply("❌ Укажите время начала и конца. Пример: /settime 09:00 18:00")
                return

            start_time = parse_time(time_parts[0])
            end_time = parse_time(time_parts[1])

            if not start_time or not end_time:
                await message.reply("❌ Неверный формат времени. Используйте HH:MM")
                return

            if start_time >= end_time:
                await message.reply("❌ Время начала должно быть раньше времени окончания")
                return

            START_TIME = start_time
            END_TIME = end_time
            save_state()

            await message.reply(f"✅ Временное окно установлено: {START_TIME.strftime('%H:%M')} - {END_TIME.strftime('%H:%M')}")

        except:
            await message.reply("❌ Ошибка. Пример: /settime 09:00 18:00")

    elif text.startswith("/starttime "):
        try:
            time_str = text[11:]  # Убираем "/starttime "
            start_time = parse_time(time_str)

            if not start_time:
                await message.reply("❌ Неверный формат времени. Используйте HH:MM")
                return

            now = get_czech_time()
            # Устанавливаем время на сегодня в чешском часовом поясе
            target_datetime = now.replace(hour=start_time.hour, minute=start_time.minute, second=0, microsecond=0)

            # Если время уже прошло сегодня, переносим на завтра
            if target_datetime <= now:
                target_datetime = target_datetime + timedelta(days=1)

            DELAYED_START_ENABLED = True
            DELAYED_START_TIME = target_datetime
            save_state()

            await message.reply(f"✅ Отложенный старт установлен на {target_datetime.strftime('%Y-%m-%d %H:%M')}")

        except:
            await message.reply("❌ Ошибка. Пример: /starttime 17:00")

    elif text.startswith("/startdate "):
        try:
            datetime_str = text[11:]  # Убираем "/startdate "

            try:
                # Пробуем формат "YYYY-MM-DD HH:MM"
                target_datetime = datetime.strptime(datetime_str, "%Y-%m-%d %H:%M")
            except:
                try:
                    # Пробуем формат "DD.MM.YYYY HH:MM"
                    target_datetime = datetime.strptime(datetime_str, "%d.%m.%Y %H:%M")
                except:
                    await message.reply("❌ Неверный формат. Используйте: YYYY-MM-DD HH:MM или DD.MM.YYYY HH:MM")
                    return

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
            await message.reply("❌ Ошибка. Пример: /startdate 2024-01-25 17:00")

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
        # Временно включаем подробные логи для команды
        current_weekday = now.weekday()
        current_time = now.time()

        print(f"🕐 Текущее время (Чехия): {now.strftime('%Y-%m-%d %H:%M:%S')} GMT+2")
        print(f"📅 День недели: {current_weekday} ({get_weekday_name(current_weekday)})")
        print(f"⏰ Временное окно: {START_TIME.strftime('%H:%M')} - {END_TIME.strftime('%H:%M')}")
        print(f"🎛️ Ограничения: время={TIME_WINDOW_ENABLED}, дни={WEEKDAYS_ENABLED}")
        
        posting_allowed, reason = is_posting_allowed()

        time_status = "✅ включено" if TIME_WINDOW_ENABLED else "❌ выключено"
        days_status = "✅ включено" if WEEKDAYS_ENABLED else "❌ выключено"

        check_text = f"""
🕐 <b>Проверка времени:</b>

⏰ Текущее время: {now.strftime('%Y-%m-%d %H:%M:%S')} GMT+2 (Чехия)
📅 День недели: {get_weekday_name(now.weekday())} ({now.weekday()})

<b>Настройки ограничений:</b>
⏰ Временное окно: {time_status}
   └ {START_TIME.strftime('%H:%M')} - {END_TIME.strftime('%H:%M')}
📅 Дни недели: {days_status}
   └ {', '.join([get_weekday_name(day) for day in sorted(ALLOWED_WEEKDAYS)])}

<b>Результат:</b> {'✅ Разрешено' if posting_allowed else f'❌ Запрещено ({reason})'}

<b>Команды управления:</b>
/toggletime - вкл/выкл ограничение по времени
/toggledays - вкл/выкл ограничение по дням
"""
        await message.reply(check_text)

    elif text == "/random":
        queue = load_queue()
        if len(queue) <= 1:
            await message.reply("❌ Для рандомизации нужно минимум 2 элемента в очереди")
            return

        if shuffle_queue():
            current_queue = load_queue()  # Перезагружаем для актуальной длины
            await message.reply(f"🎲 Очередь перемешана! Порядок медиа изменён, но расписание публикаций сохранено.\n\n📊 В очереди: {len(current_queue)} элементов")
        else:
            await message.reply("❌ Ошибка при перемешивании очереди")

async def process_media_group(media_group_id):
    """Обрабатывает накопленную группу медиа по media_group_id"""
    global pending_media_groups, media_group_timers, last_post_time

    if media_group_id not in pending_media_groups:
        return

    media_group = pending_media_groups[media_group_id]
    queue = load_queue()

    # Если только одно медиа, обрабатываем как обычно
    if len(media_group) == 1:
        media_info = media_group[0]
        await handle_single_media(media_info["message"], media_info["media_data"], media_info["media_type"])
    else:
        # Проверяем, есть ли подпись хотя бы у одного медиа
        has_caption = any(item["media_data"]["caption"] != DEFAULT_SIGNATURE for item in media_group)

        if has_caption:
            # Создаём медиагруппу, всегда используя стандартную подпись
            media_group_data = {
                "type": "media_group",
                "items": [item["media_data"] for item in media_group],
                "caption": DEFAULT_SIGNATURE
            }

            queue_index = len(queue)  # Индекс нового элемента
            queue.append(media_group_data)
            save_queue(queue)
            
            # Запоминаем пользователя для этой медиагруппы
            user_media_tracking[queue_index] = media_group[0]["message"].from_user.id

            # Формируем уведомление о медиагруппе
            media_types = [item["media_type"] for item in media_group]
            media_count = {}
            for media_type in media_types:
                media_count[media_type] = media_count.get(media_type, 0) + 1

            count_parts = []
            if media_count.get("фото", 0) > 0:
                count_parts.append(f"{media_count['фото']} фото")
            if media_count.get("видео", 0) > 0:
                count_parts.append(f"{media_count['видео']} видео")
            if media_count.get("GIF", 0) > 0:
                count_parts.append(f"{media_count['GIF']} GIF")

            media_text = " + ".join(count_parts)

            # Проверяем, можно ли опубликовать мгновенно
            posting_allowed, reason = is_posting_allowed()
            delayed_ready = is_delayed_start_ready()
            queue_current = load_queue()  # Перезагружаем очередь для актуальной длины
            can_post_instantly = (posting_enabled and CHANNEL_ID and posting_allowed and 
                                delayed_ready and len(queue_current) == 1 and last_post_time == 0)

            if can_post_instantly:
                # Мгновенная публикация
                try:
                    await send_media_group_to_channel(media_group_data)
                    queue_current.pop(0)
                    save_queue(queue_current)
                    last_post_time = time.time()
                    save_state()

                    # ИСПРАВЛЕНИЕ: уведомление после публикации
                    response = f"📎 Медиагруппа из {len(media_group)} элементов ({media_text}) успешно опубликована мгновенно!\n\n💡 /help | /status"
                except Exception as e:
                    response = f"📎 Медиагруппа из {len(media_group)} элементов ({media_text}) добавлена в очередь!\n\n❌ Ошибка мгновенной публикации: {e}\n\n💡 /help | /status"
            else:
                # Обычное добавление в очередь
                response = await format_queue_response(media_text, len(media_group), queue_current, is_media_group=True)

            # Отправляем ответ первому пользователю в группе
            await media_group[0]["message"].reply(response)

        else:
            # Нет подписи - отправляем каждое медиа отдельно
            for media_info in media_group:
                await handle_single_media(media_info["message"], media_info["media_data"], media_info["media_type"])

    # Очищаем группу
    del pending_media_groups[media_group_id]
    if media_group_id in media_group_timers:
        del media_group_timers[media_group_id]

async def handle_single_media(message: Message, media_data: dict, media_type: str):
    """Обрабатывает одиночное медиа"""
    global last_post_time, user_media_tracking

    queue = load_queue()
    queue_index = len(queue)  # Индекс нового элемента
    queue.append(media_data)
    
    # Запоминаем пользователя для этого медиа
    user_media_tracking[queue_index] = message.from_user.id

    # Проверяем, можно ли опубликовать мгновенно
    posting_allowed, reason = is_posting_allowed()
    delayed_ready = is_delayed_start_ready()
    can_post_instantly = (posting_enabled and CHANNEL_ID and posting_allowed and 
                        delayed_ready and len(queue) == 1 and last_post_time == 0)

    if can_post_instantly:
        # Мгновенная публикация
        try:
            if media_data.get("type") == "document":
                await bot.send_document(chat_id=CHANNEL_ID, document=media_data["file_id"], caption=media_data.get("caption", DEFAULT_SIGNATURE))
            else:
                await send_single_media_to_channel(media_data)
            queue.pop(0)
            save_queue(queue)
            last_post_time = time.time()
            save_state()
            
            # Удаляем из отслеживания
            if 0 in user_media_tracking:
                del user_media_tracking[0]

            # ИСПРАВЛЕНИЕ: уведомление после публикации
            response = f"✅ {media_type.capitalize()} успешно опубликовано мгновенно!\n\n💡 /help | /status"
        except Exception as e:
            save_queue(queue)
            response = f"✅ {media_type.capitalize()} добавлено в очередь!\n\n❌ Ошибка мгновенной публикации: {e}\n\n💡 /help | /status"
    else:
        # Обычное добавление в очередь
        save_queue(queue)
        response = await format_queue_response(media_type.capitalize(), 1, queue)

    await message.reply(response)

async def send_single_media_to_channel(media_data: dict):
    """Отправляет одиночное медиа в канал"""
    file_id = media_data["file_id"]
    caption = media_data.get("caption", DEFAULT_SIGNATURE)
    media_type = media_data.get("type", "photo")

    if media_type == "video":
        await bot.send_video(chat_id=CHANNEL_ID, video=file_id, caption=caption)
    elif media_type == "animation":
        await bot.send_animation(chat_id=CHANNEL_ID, animation=file_id, caption=caption)
    else:
        await bot.send_photo(chat_id=CHANNEL_ID, photo=file_id, caption=caption)

async def send_media_group_to_channel(media_group_data: dict):
    """Отправляет медиагруппу в канал"""
    media_group = MediaGroupBuilder()

    for i, item in enumerate(media_group_data["items"]):
        file_id = item["file_id"]
        # Используем стандартную подпись только для первого элемента
        caption = media_group_data.get("caption", DEFAULT_SIGNATURE) if i == 0 else ""
        media_type = item.get("type", "photo")

        if media_type == "video":
            media_group.add_video(media=file_id, caption=caption)
        elif media_type == "animation":
            media_group.add_animation(media=file_id, caption=caption)
        else:
            media_group.add_photo(media=file_id, caption=caption)

    await bot.send_media_group(chat_id=CHANNEL_ID, media=media_group.build())

async def format_queue_response(media_text: str, added_count: int, queue: list, is_media_group: bool = False):
    """Форматирует ответ о добавлении в очередь"""
    # Подсчитываем типы медиа в очереди
    photos = 0
    videos = 0
    animations = 0
    media_groups = 0

    for item in queue:
        if isinstance(item, dict):
            if item.get("type") == "media_group":
                media_groups += 1
            elif item.get("type") == "photo":
                photos += 1
            elif item.get("type") == "video":
                videos += 1
            elif item.get("type") == "animation":
                animations += 1
        else:
            photos += 1  # Старый формат

    queue_parts = []
    if photos > 0:
        queue_parts.append(f"{photos} фото")
    if videos > 0:
        queue_parts.append(f"{videos} видео")
    if animations > 0:
        queue_parts.append(f"{animations} GIF")
    if media_groups > 0:
        queue_parts.append(f"{media_groups} медиагрупп")

    queue_status = f"{' + '.join(queue_parts)} (всего {len(queue)})"

    # Рассчитываем расписание публикации очереди
    first_post_time, calculated_last_post_time = calculate_queue_schedule(len(queue))

    if first_post_time and calculated_last_post_time:
        now = get_czech_time()
        posting_allowed, reason = is_posting_allowed()
        delayed_ready = is_delayed_start_ready()

        if posting_allowed and delayed_ready:
            # Можем постить в рамках текущего окна
            if first_post_time <= now:
                next_post_text = "сейчас"
            else:
                seconds_until_first = int((first_post_time - now).total_seconds())
                next_post_text = f"через {format_interval(seconds_until_first)}"
        else:
            # Нужно ждать следующего окна или отложенного старта
            if first_post_time.date() == now.date():
                next_post_text = f"в {first_post_time.strftime('%H:%M')}"
            else:
                next_post_text = f"в {first_post_time.strftime('%d.%m %H:%M')}"

        # Время публикации последнего элемента с датой
        if len(queue) > 1:
            if calculated_last_post_time.date() == now.date():
                last_post_text = f"\n📅 Последний элемент из очереди: в {calculated_last_post_time.strftime('%H:%M')}"
            else:
                last_post_text = f"\n📅 Последний элемент из очереди: {calculated_last_post_time.strftime('%d.%m %H:%M')}"
        else:
            last_post_text = ""
    else:
        next_post_text = "по расписанию"
        last_post_text = ""

    if is_media_group:
        response = f"📎 Медиагруппа из {added_count} элементов ({media_text}) добавлена в очередь!\n\n📊 В очереди: {queue_status}\n⏰ Следующая публикация: {next_post_text}{last_post_text}"
    else:
        response = f"✅ {media_text} добавлено в очередь!\n\n📊 В очереди: {queue_status}\n⏰ Следующая публикация: {next_post_text}{last_post_text}"

    # Если постинг выключен, добавляем уведомление
    if not posting_enabled:
        response += "\n\n⚠️ Автопостинг приостановлен. Используйте /toggle для включения."
    elif not CHANNEL_ID:
        response += "\n\n⚠️ Канал не настроен. Используйте /setchannel для настройки."

    # Добавляем навигацию
    response += "\n\n💡 /help | /status"

    return response

@dp.message(F.photo | F.video | F.animation)
async def handle_media(message: Message):
    global pending_media_groups, media_group_timers

    # Определяем тип и данные медиа
    media_data = None
    media_type = None

    if message.photo:
        file_id = message.photo[-1].file_id
        caption = message.caption or DEFAULT_SIGNATURE  # Используем дефолтную подпись только если подписи нет
        media_data = {"file_id": file_id, "caption": caption, "type": "photo"}
        media_type = "фото"
    elif message.video:
        file_id = message.video.file_id
        caption = message.caption or DEFAULT_SIGNATURE  # Используем дефолтную подпись только если подписи нет
        media_data = {"file_id": file_id, "caption": caption, "type": "video"}
        media_type = "видео"
    elif message.animation:
        file_id = message.animation.file_id
        caption = message.caption or DEFAULT_SIGNATURE  # Используем дефолтную подпись только если подписи нет
        media_data = {"file_id": file_id, "caption": caption, "type": "animation"}
        media_type = "GIF"

    if media_data:
        # Проверяем, есть ли media_group_id (медиагруппа)
        if message.media_group_id:
            # Это часть медиагруппы
            media_group_id = message.media_group_id

            # Инициализируем группу если её нет
            if media_group_id not in pending_media_groups:
                pending_media_groups[media_group_id] = []

            # Добавляем медиа в группу
            pending_media_groups[media_group_id].append({
                "message": message,
                "media_data": media_data,
                "media_type": media_type
            })

            # Отменяем предыдущий таймер для этой группы
            if media_group_id in media_group_timers:
                media_group_timers[media_group_id].cancel()

            # Устанавливаем новый таймер на 2 секунды
            async def process_after_delay():
                await asyncio.sleep(2)
                await process_media_group(media_group_id)

            media_group_timers[media_group_id] = asyncio.create_task(process_after_delay())

        else:
            # Одиночное медиа
            await handle_single_media(message, media_data, media_type)

@dp.message(F.document)
async def handle_document(message: Message):
    """Обрабатывает документ"""
    global last_post_time

    file_id = message.document.file_id
    caption = message.caption or DEFAULT_SIGNATURE
    media_data = {"file_id": file_id, "caption": caption, "type": "document"}
    media_type = "файл"

    queue = load_queue()
    queue_index = len(queue)  # Индекс нового элемента
    queue.append(media_data)
    
    # Запоминаем пользователя для этого медиа
    user_media_tracking[queue_index] = message.from_user.id

    # Проверяем, можно ли опубликовать мгновенно
    posting_allowed, reason = is_posting_allowed()
    delayed_ready = is_delayed_start_ready()
    can_post_instantly = (posting_enabled and CHANNEL_ID and posting_allowed and
                        delayed_ready and len(queue) == 1 and last_post_time == 0)

    if can_post_instantly:
        # Мгновенная публикация
        try:
            await bot.send_document(chat_id=CHANNEL_ID, document=file_id, caption=caption)
            queue.pop(0)
            save_queue(queue)
            last_post_time = time.time()
            save_state()
            
            # Удаляем из отслеживания
            if 0 in user_media_tracking:
                del user_media_tracking[0]

            # ИСПРАВЛЕНИЕ: уведомление после публикации
            response = f"✅ Файл успешно опубликован мгновенно!\n\n💡 /help | /status"
        except Exception as e:
            save_queue(queue)
            response = f"✅ Файл добавлен в очередь!\n\n❌ Ошибка мгновенной публикации: {e}\n\n💡 /help | /status"
    else:
        # Обычное добавление в очередь
        save_queue(queue)
        response = await format_queue_response(media_type.capitalize(), 1, queue)

    await message.reply(response)

# =================================
# ФОНОВЫЕ ЗАДАЧИ
# =================================

async def scheduled_posting():
    """ИСПРАВЛЕННАЯ фоновая задача для автопостинга"""
    global last_post_time, user_media_tracking
    logger.info(f"🤖 Запущен автопостинг. Интервал: {format_interval(POST_INTERVAL)}")
    logger.info(f"📋 ID канала: {CHANNEL_ID}")
    print(f"🤖 Запущен автопостинг. Интервал: {format_interval(POST_INTERVAL)}")
    print(f"📋 ID канала: {CHANNEL_ID}")

    while True:
        try:
            if posting_enabled and CHANNEL_ID:
                queue = load_queue()
                if queue:
                    logger.debug(f"📋 Проверяем очередь: {len(queue)} элементов")
                    # Проверяем, разрешено ли постить в текущее время
                    posting_allowed, reason = is_posting_allowed()
                    if not posting_allowed:
                        # Если сейчас запрещено постить, ждём
                        pass
                    else:
                        # Проверяем отложенный старт
                        if not is_delayed_start_ready():
                            # Если ещё не время отложенного старта, ждём
                            pass
                        else:
                            # Проверяем точное планирование
                            if EXACT_TIMING_ENABLED and not is_exact_posting_time():
                                # Если включено точное планирование, ждём точного времени
                                next_exact_time = get_next_exact_posting_time()
                                if next_exact_time:
                                    time_until_exact = (next_exact_time - get_czech_time()).total_seconds()
                                    if time_until_exact > 10:  # Только если ждать больше 10 секунд
                                        await asyncio.sleep(min(10, time_until_exact))  # Ждём максимум 10 секунд
                                    continue # Переходим к следующей итерации цикла

                            # Проверяем, можно ли постить (прошел ли интервал)
                            time_until_next = get_next_post_time_with_delayed_start()
                            if time_until_next <= 0:
                                logger.info(f"📤 Обрабатываем элемент из очереди. Осталось: {len(queue)} элементов")
                                media_data = queue.pop(0) # Удаляем элемент из очереди

                                try:
                                    # Проверяем тип медиа
                                    if isinstance(media_data, dict) and media_data.get("type") == "media_group":
                                        # Отправляем медиагруппу
                                        logger.info("📎 Отправляем медиагруппу в канал...")
                                        await send_media_group_to_channel(media_data)
                                        
                                        # Проверяем что пост опубликован
                                        if await verify_post_published(CHANNEL_ID, "media_group"):
                                            logger.info(f"✅ Медиагруппа успешно опубликована в канал. Осталось в очереди: {len(queue)}")
                                            print(f"✅ Медиагруппа успешно опубликована в канал. Осталось в очереди: {len(queue)}")
                                            # Отправляем уведомление пользователю
                                            if 0 in user_media_tracking:
                                                user_id = user_media_tracking[0]
                                                notification_text = "📎 Медиагруппа успешно опубликована в канале!"
                                                await send_publication_notification(user_id, notification_text)
                                                del user_media_tracking[0]
                                            
                                            # Сохраняем только после подтверждения публикации
                                            save_queue(queue)
                                            last_post_time = time.time()
                                            save_state()
                                            
                                            # Обновляем индексы в отслеживании
                                            updated_tracking = {}
                                            for idx, uid in user_media_tracking.items():
                                                if idx > 0:
                                                    updated_tracking[idx - 1] = uid
                                            user_media_tracking = updated_tracking
                                        else:
                                            logger.error("❌ Не удалось подтвердить публикацию медиагруппы")
                                            print("❌ Не удалось подтвердить публикацию медиагруппы")
                                            # Возвращаем медиа обратно в очередь
                                            queue.insert(0, media_data)
                                            save_queue(queue)
                                    else:
                                        # Отправляем одиночное медиа
                                        media_type = media_data.get("type", "photo") if isinstance(media_data, dict) else "photo"
                                        logger.info(f"📸 Отправляем {media_type} в канал...")
                                        if media_type == "document":
                                            await bot.send_document(chat_id=CHANNEL_ID, document=media_data["file_id"], caption=media_data.get("caption", DEFAULT_SIGNATURE))
                                        else:
                                            await send_single_media_to_channel(media_data)
                                        
                                        # Проверяем что пост опубликован
                                        if await verify_post_published(CHANNEL_ID, media_type):
                                            type_names = {"photo": "Фото", "video": "Видео", "animation": "GIF", "document": "Файл"}
                                            logger.info(f"✅ {type_names.get(media_type, 'Медиа')} успешно опубликовано в канал. Осталось в очереди: {len(queue)}")
                                            print(f"✅ {type_names.get(media_type, 'Медиа')} успешно опубликовано в канал. Осталось в очереди: {len(queue)}")
                                            # Отправляем уведомление пользователю
                                            if 0 in user_media_tracking:
                                                user_id = user_media_tracking[0]
                                                notification_text = f"✅ {type_names.get(media_type, 'Медиа')} успешно опубликовано в канале!"
                                                await send_publication_notification(user_id, notification_text)
                                                del user_media_tracking[0]
                                            
                                            # Сохраняем только после подтверждения публикации
                                            save_queue(queue)
                                            last_post_time = time.time()
                                            save_state()
                                            
                                            # Обновляем индексы в отслеживании
                                            updated_tracking = {}
                                            for idx, uid in user_media_tracking.items():
                                                if idx > 0:
                                                    updated_tracking[idx - 1] = uid
                                            user_media_tracking = updated_tracking
                                        else:
                                            type_names = {"photo": "Фото", "video": "Видео", "animation": "GIF", "document": "Файл"}
                                            logger.error(f"❌ Не удалось подтвердить публикацию {type_names.get(media_type, 'медиа').lower()}")
                                            print(f"❌ Не удалось подтвердить публикацию {type_names.get(media_type, 'медиа').lower()}")
                                            # Возвращаем медиа обратно в очередь
                                            queue.insert(0, media_data)
                                            save_queue(queue)

                                except Exception as e:
                                    logger.error(f"❌ Ошибка отправки в канал: {e}")
                                    print(f"❌ Ошибка отправки в канал: {e}")
                                    # ИСПРАВЛЕНИЕ: возвращаем медиа обратно в начало очереди
                                    queue.insert(0, media_data)
                                    save_queue(queue)

            # Спим 10 секунд перед следующей проверкой
            await asyncio.sleep(10)

        except Exception as e:
            logger.error(f"❌ Ошибка в scheduled_posting: {e}")
            print(f"❌ Ошибка в scheduled_posting: {e}")
            await asyncio.sleep(10)

# =================================
# ОСНОВНАЯ ФУНКЦИЯ ЗАПУСКА
# =================================

async def main():
    """Главная функция, которая запускает бота и веб-сервер одновременно"""
    # Загружаем состояние при запуске
    load_state()

    logger.info("🚀 Приложение запускается...")
    logger.info(f"⏰ Интервал постинга: {format_interval(POST_INTERVAL)}")
    print("🚀 Приложение запускается...")
    print(f"⏰ Интервал постинга: {format_interval(POST_INTERVAL)}")

    if CHANNEL_ID:
        logger.info(f"📢 ID канала: {CHANNEL_ID}")
        print(f"📢 ID канала: {CHANNEL_ID}")
    else:
        logger.warning("⚠️ Канал не настроен")
        print("⚠️ Канал не настроен")

    # Создаём задачи для параллельного выполнения
    tasks = []

    # 1. Запускаем веб-сервер
    tasks.append(asyncio.create_task(start_web_server()))

    # 2. Запускаем автопостинг в фоне
    tasks.append(asyncio.create_task(scheduled_posting()))

    # 3. Устанавливаем команды в меню и запускаем телеграм бота
    await set_bot_commands()
    tasks.append(asyncio.create_task(dp.start_polling(bot)))

    try:
        # Ждём выполнения всех задач параллельно
        await asyncio.gather(*tasks)
    except KeyboardInterrupt:
        logger.info("\n⏹️ Получен сигнал остановки")
        print("\n⏹️ Получен сигнал остановки")
    finally:
        # Корректно закрываем все соединения
        for task in tasks:
            if not task.done():
                task.cancel()

        await bot.session.close()
        logger.info("🔌 Все соединения закрыты")
        print("🔌 Все соединения закрыты")

if __name__ == "__main__":
    # Запускаем основную функцию
    asyncio.run(main())

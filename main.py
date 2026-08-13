import os
import asyncio
import json
import re
import time
import random
import logging
from datetime import datetime, time as dt_time, timedelta, timezone
from zoneinfo import ZoneInfo
import asyncpg
import aiohttp
from aiogram import Bot, Dispatcher, types, F
from aiogram.types import Message, BotCommand, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery, BufferedInputFile
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
DATABASE_URL = os.getenv("DATABASE_URL")

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

# Часовой пояс по умолчанию
CURRENT_TIMEZONE_STR = "Europe/Kyiv"
CURRENT_TIMEZONE = ZoneInfo(CURRENT_TIMEZONE_STR)

def get_current_time():
    """Возвращает текущее время с учетом выбранного часового пояса"""
    return datetime.now(CURRENT_TIMEZONE)

def check_user_access(user_id):
    return user_id in ALLOWED_USERS

# Глобальные настройки автопостинга
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

# Глобальные настройки парсера и модерации
MODERATION_CHAT_ID = None      # ID группы для перепоста/модерации
PARSER_ENABLED = True           # Включен ли парсинг
PARSER_SPEED = 15               # Интервал проверки сайтов в секундах
PARSER_LIMIT = 20               # Количество постов за один прогон
LABELS = []                     # Список кастомных лейблов

# Локальный файл для резервного фолбэка очереди
QUEUE_FILE = "queue.json"

# Структуры данных
pending_media_groups = {}
media_group_timers = {}
pending_notifications = {}
user_media_tracking = {}
waiting_for_time_input = {}
is_posting_locked = False
db_pool = None  # Пул соединений PostgreSQL

bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

# --- РАБОТА С POSTGRESQL ---

async def init_db():
    """Инициализация базы данных и таблиц"""
    global db_pool
    if not DATABASE_URL:
        logger.error("❌ DATABASE_URL не установлен!")
        return
    
    url = DATABASE_URL
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)

    try:
        db_pool = await asyncpg.create_pool(dsn=url)
        async with db_pool.acquire() as conn:
            # 1. Таблица конфигурации и очереди
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS bot_config (
                    id INT PRIMARY KEY DEFAULT 1,
                    config JSONB NOT NULL,
                    queue JSONB NOT NULL DEFAULT '[]'::jsonb,
                    labels JSONB NOT NULL DEFAULT '[]'::jsonb,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                ALTER TABLE bot_config ADD COLUMN IF NOT EXISTS queue JSONB NOT NULL DEFAULT '[]'::jsonb;
                ALTER TABLE bot_config ADD COLUMN IF NOT EXISTS labels JSONB NOT NULL DEFAULT '[]'::jsonb;
            """)

            # 2. Таблица дедупликации (история спасённых/просмотренных постов)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS seen_posts (
                    source VARCHAR(20) NOT NULL,
                    post_id VARCHAR(50) NOT NULL,
                    file_md5 VARCHAR(64),
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (source, post_id)
                );
                CREATE INDEX IF NOT EXISTS idx_seen_posts_md5 ON seen_posts(file_md5);
            """)
        logger.info("✅ Успешное подключение к PostgreSQL и структура создана!")
    except Exception as e:
        logger.error(f"❌ Ошибка подключения к PostgreSQL: {e}")

async def load_state_from_db():
    """Загружает состояние бота и парсера из PostgreSQL"""
    global last_post_time, CHANNEL_ID, posting_enabled, DEFAULT_SIGNATURE, POST_INTERVAL
    global ALLOWED_WEEKDAYS, START_TIME, END_TIME, DELAYED_START_ENABLED, DELAYED_START_TIME
    global TIME_WINDOW_ENABLED, WEEKDAYS_ENABLED, EXACT_TIMING_ENABLED, NOTIFICATIONS_ENABLED
    global CURRENT_TIMEZONE_STR, CURRENT_TIMEZONE
    global MODERATION_CHAT_ID, PARSER_ENABLED, PARSER_SPEED, PARSER_LIMIT, LABELS

    if not db_pool:
        logger.warning("⚠️ База данных не подключена. Используются значения по умолчанию.")
        return

    try:
        async with db_pool.acquire() as conn:
            row = await conn.fetchrow("SELECT config, labels FROM bot_config WHERE id = 1;")
            if row and row["config"]:
                state = json.loads(row["config"]) if isinstance(row["config"], str) else row["config"]
                
                last_post_time = state.get("last_post_time", 0)
                DEFAULT_SIGNATURE = state.get("default_signature")
                saved_channel = state.get("channel_id")
                if saved_channel:
                    CHANNEL_ID = saved_channel
                
                posting_enabled = state.get("posting_enabled", True)
                POST_INTERVAL = state.get("post_interval")
                ALLOWED_WEEKDAYS = state.get("allowed_weekdays")
                
                st = state.get("start_time")
                START_TIME = datetime.strptime(st, "%H:%M").time() if st else None
                
                et = state.get("end_time")
                END_TIME = datetime.strptime(et, "%H:%M").time() if et else None

                DELAYED_START_ENABLED = state.get("delayed_start_enabled", False)
                dst = state.get("delayed_start_time")
                DELAYED_START_TIME = datetime.fromisoformat(dst) if dst else None

                TIME_WINDOW_ENABLED = state.get("time_window_enabled", True)
                WEEKDAYS_ENABLED = state.get("weekdays_enabled", False)
                EXACT_TIMING_ENABLED = state.get("exact_timing_enabled", True)
                NOTIFICATIONS_ENABLED = state.get("notifications_enabled", True)

                # Настройки Агрегатора
                MODERATION_CHAT_ID = state.get("moderation_chat_id")
                PARSER_ENABLED = state.get("parser_enabled", True)
                PARSER_SPEED = state.get("parser_speed", 15)
                PARSER_LIMIT = state.get("parser_limit", 20)

                tz_str = state.get("timezone", "Europe/Kyiv")
                try:
                    CURRENT_TIMEZONE = ZoneInfo(tz_str)
                    CURRENT_TIMEZONE_STR = tz_str
                except Exception:
                    CURRENT_TIMEZONE_STR = "Europe/Kyiv"
                    CURRENT_TIMEZONE = ZoneInfo("Europe/Kyiv")

            if row and row["labels"]:
                lbls = row["labels"]
                LABELS = json.loads(lbls) if isinstance(lbls, str) else lbls

            logger.info("✅ Настройки и лейблы загружены из PostgreSQL!")
    except Exception as e:
        logger.error(f"❌ Ошибка загрузки состояния из БД: {e}")

def save_state():
    """Сохраняет состояние бота в PostgreSQL"""
    asyncio.create_task(async_save_state())

async def async_save_state():
    """Асинхронное сохранение в PostgreSQL"""
    if not db_pool:
        return

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
        "notifications_enabled": NOTIFICATIONS_ENABLED,
        "timezone": CURRENT_TIMEZONE_STR,
        "moderation_chat_id": MODERATION_CHAT_ID,
        "parser_enabled": PARSER_ENABLED,
        "parser_speed": PARSER_SPEED,
        "parser_limit": PARSER_LIMIT
    }

    try:
        async with db_pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO bot_config (id, config, labels, updated_at)
                VALUES (1, $1::jsonb, $2::jsonb, CURRENT_TIMESTAMP)
                ON CONFLICT (id) 
                DO UPDATE SET config = EXCLUDED.config, labels = EXCLUDED.labels, updated_at = CURRENT_TIMESTAMP;
            """, json.dumps(state, ensure_ascii=False), json.dumps(LABELS, ensure_ascii=False))
            logger.info("💾 Состояние и лейблы сохранены в PostgreSQL!")
    except Exception as e:
        logger.error(f"❌ Ошибка сохранения состояния в БД: {e}")

async def load_queue():
    """Асинхронно загружает очередь из PostgreSQL"""
    if db_pool:
        try:
            async with db_pool.acquire() as conn:
                row = await conn.fetchrow("SELECT queue FROM bot_config WHERE id = 1;")
                if row and row["queue"] is not None:
                    q = row["queue"]
                    return json.loads(q) if isinstance(q, str) else q
        except Exception as e:
            logger.error(f"❌ Ошибка загрузки очереди из PostgreSQL: {e}")

    if not os.path.exists(QUEUE_FILE):
        return []
    try:
        with open(QUEUE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except:
        return []

async def save_queue(queue):
    """Асинхронно сохраняет очередь в PostgreSQL"""
    try:
        with open(QUEUE_FILE, "w", encoding="utf-8") as f:
            json.dump(queue, f, ensure_ascii=False)
    except Exception as e:
        logger.error(f" Ошибка сохранения локального файла очереди: {e}")

    if not db_pool:
        return

    try:
        async with db_pool.acquire() as conn:
            await conn.execute("""
                UPDATE bot_config SET queue = $1::jsonb WHERE id = 1;
            """, json.dumps(queue, ensure_ascii=False))
            logger.info(f"💾 Очередь ({len(queue)} постов) сохранена в PostgreSQL!")
    except Exception as e:
        logger.error(f"❌ Ошибка сохранения очереди в PostgreSQL: {e}")

# --- ФУНКЦИИ ДЕДУПЛИКАЦИИ И ИСТОРИИ ---

async def is_post_seen(source, post_id):
    """Проверяет, выходил ли пост с таким ID"""
    if not db_pool: return False
    try:
        async with db_pool.acquire() as conn:
            row = await conn.fetchrow("SELECT 1 FROM seen_posts WHERE source=$1 AND post_id=$2", source, str(post_id))
            return row is not None
    except Exception as e:
        logger.error(f"Ошибка проверки post_id в БД: {e}")
        return False

async def is_md5_seen(file_md5):
    """Проверяет, выходила ли картинка с таким MD5 (подозрение на дубликат)"""
    if not db_pool or not file_md5: return False
    try:
        async with db_pool.acquire() as conn:
            row = await conn.fetchrow("SELECT source, post_id, created_at FROM seen_posts WHERE file_md5=$1", str(file_md5))
            return row
    except Exception as e:
        logger.error(f"Ошибка проверки MD5 в БД: {e}")
        return None

async def mark_post_as_seen(source, post_id, file_md5=None):
    """Заносит пост в историю дедупликации"""
    if not db_pool: return
    try:
        async with db_pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO seen_posts (source, post_id, file_md5)
                VALUES ($1, $2, $3)
                ON CONFLICT (source, post_id) DO NOTHING;
            """, source, str(post_id), file_md5)
    except Exception as e:
        logger.error(f"Ошибка сохранения в seen_posts: {e}")

# --- ДВИЖОК ПАРСИНГА (RULE34 & GELBOORU) ---

async def fetch_booru_posts(source, tags, limit=20):
    """Запрашивает посты через JSON API Rule34/Gelbooru с защитой от 429 и авторизации"""
    posts = []
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*"
    }

    clean_tags = tags.strip().replace(" ", "+")

    if source == "rule34":
        url = f"https://api.rule34.xxx/index.php?page=dapi&s=post&q=index&json=1&tags={clean_tags}&limit={limit}"
    elif source == "gelbooru":
        url = f"https://gelbooru.com/index.php?page=dapi&s=post&q=index&json=1&tags={clean_tags}&limit={limit}"
    else:
        return posts

    try:
        # Микро-задержка от бана частых вызовов (429)
        await asyncio.sleep(1)

        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, timeout=10) as resp:
                if resp.status == 200:
                    text_data = await resp.text()
                    if text_data.strip():
                        data = json.loads(text_data)
                        if isinstance(data, list):
                            posts = data
                        elif isinstance(data, dict) and "post" in data:
                            posts = data["post"]
                elif resp.status == 429:
                    logger.warning(f"⚠️ {source} ответил 429 (Too Many Requests). Небольшая пауза...")
                    await asyncio.sleep(4)
                else:
                    logger.warning(f"⚠️ {source} ответил статусом: {resp.status}")
    except Exception as e:
        logger.error(f"Ошибка запроса к {source} API ({clean_tags}): {e}")

    return posts

def format_emoji_label(emoji_val, label_name):
    """Форматирует заголовок лейбла с поддержкой Custom Premium Emoji ID"""
    if emoji_val and emoji_val.isdigit():
        return f'<tg-emoji emoji-id="{emoji_val}">🏷</tg-emoji> <b>{label_name}</b>'
    elif emoji_val:
        return f'{emoji_val} <b>{label_name}</b>'
    return f'🏷 <b>{label_name}</b>'

async def process_parsed_post(label, post_data, source):
    """Обрабатывает найденный пост: дедупликация, отправка в группу модерации или автопостинг"""
    post_id = str(post_data.get("id"))
    file_url = post_data.get("file_url")
    file_md5 = post_data.get("md5") or post_data.get("hash")

    if not file_url or not post_id:
        return

    # 1. Проверка на абсолютный повтор ID
    if await is_post_seen(source, post_id):
        return

    # 2. Проверка на дубликат по MD5
    duplicate_info = await is_md5_seen(file_md5)
    is_suspicious = duplicate_info is not None

    # Помечаем пост как просмотренный
    await mark_post_as_seen(source, post_id, file_md5)

    # Формируем брендированную ссылку источника
    if source == "gelbooru":
        source_link = f'<a href="https://gelbooru.com/index.php?page=post&s=view&id={post_id}">🟡 Gelbooru</a>'
    else:
        source_link = f'<a href="https://rule34.xxx/index.php?page=post&s=view&id={post_id}">🟢 Rule34</a>'

    label_header = format_emoji_label(label.get("emoji"), label.get("name"))
    
    # Красивый текст карточки в группе модерации
    card_caption = f"{label_header}\n"
    if is_suspicious:
        card_caption = f"⚠️ <b>СОМНИТЕЛЬНО (Возможен дубликат)</b>\n" + card_caption
        card_caption += f"💡 <i>Ранее выходил из источника {duplicate_info['source']}</i>\n"

    card_caption += f"🔗 <b>Источник:</b> {source_link}\n"
    card_caption += f"🆔 <code>{post_id}</code>"

    mode = label.get("mode", "MANUAL")
    custom_signature = label.get("signature") or DEFAULT_SIGNATURE or ""

    # Кнопки модерации
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="📥 В очередь", callback_data=f"mod:queue:{source}:{post_id}"),
            InlineKeyboardButton(text="🚀 Постить сейчас", callback_data=f"mod:now:{source}:{post_id}")
        ],
        [
            InlineKeyboardButton(text="❌ Скрыть / Удалить", callback_data="mod:delete")
        ]
    ])

    # Если включен режим AUTO и пост НЕ сомнительный -> сразу в очередь
    if mode == "AUTO" and not is_suspicious:
        queue = await load_queue()
        media_item = {
            "file_id": file_url,
            "type": "photo",
            "caption": custom_signature
        }
        queue.append(media_item)
        await save_queue(queue)
        logger.info(f"⚡ Пост {source} #{post_id} автоматически добавлен в очередь по лейблу {label.get('name')}")
        return

    # Иначе отправляем в закрытую группу модерации (если она установлена)
    if MODERATION_CHAT_ID:
        try:
            # Сохраняем временные данные поста для обработчика кнопок
            mod_item = {
                "file_url": file_url,
                "caption": custom_signature,
                "source": source,
                "post_id": post_id
            }
            # Отправляем фото с кнопками
            msg = await bot.send_photo(
                chat_id=MODERATION_CHAT_ID,
                photo=file_url,
                caption=card_caption,
                reply_markup=kb
            )
            # Привязываем данные
            user_media_tracking[f"mod_{msg.message_id}"] = mod_item
        except Exception as e:
            logger.error(f"❌ Ошибка отправки карточки в группу модерации: {e}")

async def parser_loop():
    """Фоновый цикл автосбора постов по лейблам"""
    logger.info("[PARSER] parser_loop запущен")
    while True:
        try:
            if PARSER_ENABLED and LABELS:
                for label in LABELS:
                    sources = label.get("sources", ["rule34", "gelbooru"])
                    tags = label.get("tags", "")

                    if not tags:
                        continue

                    for src in sources:
                        posts = await fetch_booru_posts(src, tags, limit=PARSER_LIMIT)
                        for post in posts:
                            await process_parsed_post(label, post, src)
                            await asyncio.sleep(0.2)

            await asyncio.sleep(max(1, PARSER_SPEED))
        except (asyncio.CancelledError, Exception) as e:
            logger.error(f"❌ Ошибка в parser_loop: {e}")
            await asyncio.sleep(10)

# --- ОБРАБОТЧИКИ КНОПОК МОДЕРАЦИИ В ГРУППЕ ---

@dp.callback_query(F.data.startswith("mod:"))
async def handle_moderation_callback(callback: CallbackQuery):
    if not check_user_access(callback.from_user.id):
        await callback.answer("У вас нет прав для модерации.", show_alert=True)
        return

    parts = callback.data.split(":")
    action = parts[1]

    if action == "delete":
        try:
            await callback.message.delete()
            await callback.answer("Удалено!")
        except Exception:
            await callback.answer("Не удалось удалить сообщение.", show_alert=True)
        return

    msg_id_key = f"mod_{callback.message.message_id}"
    item_data = user_media_tracking.get(msg_id_key)

    # Если метаданные из памяти утеряны, вытаскиваем фото прямо из сообщения
    if not item_data:
        file_url = callback.message.photo[-1].file_id if callback.message.photo else None
        caption = DEFAULT_SIGNATURE or ""
    else:
        file_url = item_data["file_url"]
        caption = item_data["caption"]

    if not file_url:
        await callback.answer("❌ Ошибка: не удалось получить медиа файл.", show_alert=True)
        return

    media_item = {"file_id": file_url, "type": "photo", "caption": caption}

    if action == "queue":
        queue = await load_queue()
        queue.append(media_item)
        await save_queue(queue)
        await callback.message.edit_caption(
            caption=callback.message.caption + "\n\n✅ <b>ОДОБРЕНО И ДОБАВЛЕНО В ОЧЕРЕДЬ</b>"
        )
        await callback.answer("Добавлено в очередь!")

    elif action == "now":
        if not CHANNEL_ID:
            await callback.answer("❌ Канал не установлен!", show_alert=True)
            return
        try:
            await send_single_media(media_item)
            global last_post_time
            last_post_time = time.time()
            save_state()
            await callback.message.edit_caption(
                caption=callback.message.caption + "\n\n🚀 <b>ОПУБЛИКОВАНО В КАНАЛ СЕЙЧАС</b>"
            )
            await callback.answer("Опубликовано!")
        except Exception as e:
            await callback.answer(f"❌ Ошибка отправки: {e}", show_alert=True)

# --- КНОПКИ И СМЕНА РЕГИОНА ---

def get_timezone_keyboard():
    buttons = [
        [
            InlineKeyboardButton(text="🇺🇦 Украина (Kyiv)", callback_data="tz:Europe/Kyiv"),
            InlineKeyboardButton(text="🇨🇿 Чехия (Prague)", callback_data="tz:Europe/Prague")
        ],
        [
            InlineKeyboardButton(text="🇵🇱 Польша (Warsaw)", callback_data="tz:Europe/Warsaw"),
            InlineKeyboardButton(text="🇩🇪 Германия (Berlin)", callback_data="tz:Europe/Berlin")
        ],
        [
            InlineKeyboardButton(text="🇬🇧 Великобритания (London)", callback_data="tz:Europe/London"),
            InlineKeyboardButton(text="🌐 UTC", callback_data="tz:UTC")
        ],
        [
            InlineKeyboardButton(text="🕒 Ввести текущее время вручную", callback_data="tz:input_time")
        ]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

@dp.callback_query(F.data.startswith("tz:"))
async def handle_tz_callback(callback: CallbackQuery):
    if not check_user_access(callback.from_user.id):
        await callback.answer("У вас нет доступа к боту.", show_alert=True)
        return

    global CURRENT_TIMEZONE_STR, CURRENT_TIMEZONE
    action = callback.data.split(":", 1)[1]

    if action == "input_time":
        waiting_for_time_input[callback.from_user.id] = True
        await callback.message.reply(
            "🕒 <b>Введите ваше текущее время в формате HH:MM</b>\n\n"
            "Например: <code>14:30</code> или <code>09:15</code>\n"
            "Бот сам определит подходящий часовой пояс."
        )
        await callback.answer()
        return

    try:
        new_tz = ZoneInfo(action)
        CURRENT_TIMEZONE = new_tz
        CURRENT_TIMEZONE_STR = action
        save_state()
        now_str = get_current_time().strftime("%H:%M:%S")
        await callback.message.edit_text(
            f"✅ Часовой пояс изменён на <b>{CURRENT_TIMEZONE_STR}</b>!\n"
            f"🕒 Текущее время бота: {now_str}"
        )
        await callback.answer("Часовой пояс обновлён!")
    except Exception as e:
        await callback.answer("❌ Ошибка установки часового пояса", show_alert=True)

@dp.callback_query(F.data.startswith("tzconfirm:"))
async def handle_tz_confirm_callback(callback: CallbackQuery):
    if not check_user_access(callback.from_user.id):
        await callback.answer("У вас нет доступа.", show_alert=True)
        return

    global CURRENT_TIMEZONE_STR, CURRENT_TIMEZONE
    tz_str = callback.data.split(":", 1)[1]
    try:
        CURRENT_TIMEZONE = ZoneInfo(tz_str)
        CURRENT_TIMEZONE_STR = tz_str
        save_state()
        now_str = get_current_time().strftime("%H:%M:%S")
        await callback.message.edit_text(
            f"✅ Часовой пояс официально установлен: <b>{CURRENT_TIMEZONE_STR}</b>!\n"
            f"🕒 Время бота совпадает с вашим: {now_str}"
        )
        await callback.answer("Успешно установлено!")
    except Exception as e:
        await callback.answer("❌ Ошибка установки пояса", show_alert=True)

@dp.callback_query(F.data == "tzcancel")
async def handle_tz_cancel_callback(callback: CallbackQuery):
    await callback.message.edit_text("❌ Настройка часового пояса отменена.")
    await callback.answer()

# --- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ---

def parse_interval(interval_str):
    total_seconds = 0
    patterns = [('d', 24*3600), ('h', 3600), ('m', 60), ('s', 1)]
    for suffix, multiplier in patterns:
        match = re.search(rf'(\d+){suffix}', interval_str)
        if match:
            total_seconds += int(match.group(1)) * multiplier
    return total_seconds if total_seconds > 0 else None

def format_interval(seconds):
    periods = [('д', 86400), ('ч', 3600), ('м', 60), ('с', 1)]
    parts = []
    for name, period in periods:
        if seconds >= period:
            count = seconds // period
            parts.append(f"{count}{name}")
            seconds %= period
    return " ".join(parts) if parts else "0м"

def calculate_exact_posting_times():
    if not EXACT_TIMING_ENABLED or POST_INTERVAL is None:
        return []

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

        if START_TIME <= END_TIME:
            if START_TIME <= current_time <= END_TIME:
                posting_times.append(current_time)
        else:
            if current_time >= START_TIME or current_time <= END_TIME:
                posting_times.append(current_time)

    return posting_times

def get_next_exact_posting_time():
    if not EXACT_TIMING_ENABLED or POST_INTERVAL is None:
        return None

    now = get_current_time()
    current_time = now.time()
    posting_times = calculate_exact_posting_times()

    if not posting_times:
        return None

    for post_time in posting_times:
        if current_time < post_time:
            if not (WEEKDAYS_ENABLED and ALLOWED_WEEKDAYS is not None and now.weekday() not in ALLOWED_WEEKDAYS):
                return now.replace(hour=post_time.hour, minute=post_time.minute, second=0, microsecond=0)

    for days_ahead in range(1, 8):
        check_date = now + timedelta(days=days_ahead)
        if not WEEKDAYS_ENABLED or ALLOWED_WEEKDAYS is None or check_date.weekday() in ALLOWED_WEEKDAYS:
            first_time = posting_times[0]
            return check_date.replace(hour=first_time.hour, minute=first_time.minute, second=0, microsecond=0)

    return None

def calculate_queue_schedule(queue_length):
    if queue_length == 0:
        return None, None

    if EXACT_TIMING_ENABLED:
        next_time = get_next_exact_posting_time()
        if not next_time:
            return None, None

        posting_times = calculate_exact_posting_times()
        if not posting_times:
            return None, None

        current_time_index = 0
        for i, post_time in enumerate(posting_times):
            if abs((post_time.hour * 60 + post_time.minute) - (next_time.time().hour * 60 + next_time.time().minute)) <= 1:
                current_time_index = i
                break

        last_time_index = (current_time_index + queue_length - 1) % len(posting_times)
        days_offset = (current_time_index + queue_length - 1) // len(posting_times)

        last_post_time = posting_times[last_time_index]
        last_post_date = next_time.date() + timedelta(days=days_offset)
        last_post_datetime = datetime.combine(last_post_date, last_post_time, tzinfo=CURRENT_TIMEZONE)

        return next_time, last_post_datetime
    else:
        now = get_current_time()
        first_post_time = now + timedelta(seconds=get_time_until_next_post())
        last_post_time = first_post_time + timedelta(seconds=(queue_length - 1) * POST_INTERVAL)
        return first_post_time, last_post_time

def get_time_until_next_post():
    if POST_INTERVAL is None:
        return 24 * 3600

    if EXACT_TIMING_ENABLED:
        next_exact_time = get_next_exact_posting_time()
        if next_exact_time:
            now = get_current_time()
            return max(0, int((next_exact_time - now).total_seconds()))
        return 0

    now_timestamp = time.time()
    time_since_last = now_timestamp - last_post_time

    if time_since_last >= POST_INTERVAL:
        return get_next_allowed_time()
    else:
        interval_wait = POST_INTERVAL - int(time_since_last)
        allowed_wait = get_next_allowed_time()
        return max(interval_wait, allowed_wait)

def get_next_allowed_time():
    now = get_current_time()

    if is_posting_allowed()[0]:
        return 0

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

def is_posting_allowed():
    now = get_current_time()
    current_weekday = now.weekday()
    current_time = now.time()

    if WEEKDAYS_ENABLED and ALLOWED_WEEKDAYS is not None and current_weekday not in ALLOWED_WEEKDAYS:
        return False, f"День недели не разрешён ({get_weekday_name(current_weekday)})"

    if TIME_WINDOW_ENABLED and START_TIME is not None and END_TIME is not None:
        if START_TIME <= END_TIME:
            if not (START_TIME <= current_time <= END_TIME):
                return False, f"Вне временного окна ({START_TIME.strftime('%H:%M')}-{END_TIME.strftime('%H:%M')})"
        else:
            if not (current_time >= START_TIME or current_time <= END_TIME):
                return False, f"Вне временного окна ({START_TIME.strftime('%H:%M')}-{END_TIME.strftime('%H:%M')})"

    return True, "Разрешено"

def is_posting_allowed_in_future(seconds_ahead=60):
    future_time = get_current_time() + timedelta(seconds=seconds_ahead)
    future_weekday = future_time.weekday()
    future_current_time = future_time.time()

    if WEEKDAYS_ENABLED and ALLOWED_WEEKDAYS is not None and future_weekday not in ALLOWED_WEEKDAYS:
        return False, f"День недели не будет разрешён ({get_weekday_name(future_weekday)})"

    if TIME_WINDOW_ENABLED and START_TIME is not None and END_TIME is not None:
        if START_TIME <= END_TIME:
            if not (START_TIME <= future_current_time <= END_TIME):
                return False, f"Будет вне временного окна ({START_TIME.strftime('%H:%M')}-{END_TIME.strftime('%H:%M')})"
        else:
            if not (future_current_time >= START_TIME or future_current_time <= END_TIME):
                return False, f"Будет вне временного окна ({START_TIME.strftime('%H:%M')}-{END_TIME.strftime('%H:%M')})"

    return True, "Будет разрешено"

def should_prepare_for_posting():
    if not EXACT_TIMING_ENABLED or POST_INTERVAL is None:
        return False, "Точное планирование отключено"
    
    posting_allowed, current_reason = is_posting_allowed()
    if posting_allowed:
        return False, "Постинг уже разрешён"
    
    future_allowed, future_reason = is_posting_allowed_in_future(60)
    if not future_allowed:
        return False, f"Постинг не будет разрешён: {future_reason}"
    
    next_exact_time = get_next_exact_posting_time()
    if not next_exact_time:
        return False, "Нет точного времени для следующего поста"
    
    now = get_current_time()
    time_until_next = (next_exact_time - now).total_seconds()
    
    if 0 <= time_until_next <= 60:
        return True, f"Подготовка к посту через {int(time_until_next)}с в {next_exact_time.strftime('%H:%M')}"
    
    return False, f"Следующий пост через {int(time_until_next)}с"

def is_delayed_start_ready():
    if not DELAYED_START_ENABLED or not DELAYED_START_TIME:
        return True
    return get_current_time() >= DELAYED_START_TIME

def get_weekday_name(weekday):
    days = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]
    return days[weekday]

def get_weekday_short(weekday):
    days = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
    return days[weekday]

def count_queue_stats(queue):
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
                if media_type == "photo": photos += 1
                elif media_type == "video": videos += 1
                elif media_type == "gif": gifs += 1
                elif media_type == "document": documents += 1
        else:
            total_media += 1
            if isinstance(item, dict):
                media_type = item.get("type", "photo")
                if media_type == "photo": photos += 1
                elif media_type == "video": videos += 1
                elif media_type == "gif": gifs += 1
                elif media_type == "document": documents += 1
            else:
                photos += 1

    return total_media, media_groups, total_posts, photos, videos, gifs, documents

def format_queue_stats(queue):
    total_media, media_groups, total_posts, photos, videos, gifs, documents = count_queue_stats(queue)
    parts = []
    if photos > 0: parts.append(f"{photos} фото")
    if videos > 0: parts.append(f"{videos} видео")
    if gifs > 0: parts.append(f"{gifs} GIF")
    if documents > 0: parts.append(f"{documents} документов")
    if media_groups > 0: parts.append(f"{media_groups} медиагрупп")
    parts.append(f"{total_posts} постов")
    return " | ".join(parts)

async def shuffle_queue():
    queue = await load_queue()
    if len(queue) > 1:
        random.shuffle(queue)
        await save_queue(queue)
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

async def apply_signature_to_all_queue(signature):
    queue = await load_queue()
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

    await save_queue(queue)
    return updated_count

async def verify_post_published(channel_id, expected_type=None, timeout=5):
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
    global last_post_time

    queue = await load_queue()
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

    if not posting_allowed:
        should_prepare, prepare_reason = should_prepare_for_posting()
        if should_prepare:
            logger.info(f"🔮 {prepare_reason}")
        else:
            logger.info(f"⏰ Постинг запрещён: {reason}")
            return

    if EXACT_TIMING_ENABLED:
        next_exact_time = get_next_exact_posting_time()
        if next_exact_time:
            now = get_current_time()
            now = now.replace(second=0, microsecond=0)
            time_diff = (next_exact_time - now).total_seconds()

            if 0 <= time_diff <= 61:
                global is_posting_locked
                is_posting_locked = True
                
                if time_diff > 5:
                    logger.info(f"⏳ Подготовка к публикации в {next_exact_time.strftime('%H:%M')} (ждём {int(time_diff)}с)")
                    await asyncio.sleep(time_diff - 5)
                
                logger.info(f"✅ Публикуем пост в точное время: {next_exact_time.strftime('%H:%M')} (финальная подготовка)")
                await asyncio.sleep(5)
                is_posting_locked = False
            else:
                logger.info(f"⏰ Ожидание точного времени постинга: {next_exact_time.strftime('%H:%M')} (через {int(time_diff)}с)")
                return

    media_data = queue.pop(0)
    published_successfully = False

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

    await save_queue(queue)

async def posting_loop():
    logger.info("[POSTER] posting_loop запущен")
    
    while True:
        try:
            while True:
                try:
                    queue = await load_queue()
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

async def process_pending_media_group(media_group_id):
    await asyncio.sleep(1)

    if media_group_id in pending_media_groups:
        media_group = pending_media_groups[media_group_id]

        if len(media_group) > 1:
            has_caption = any(media_info["message"].caption and media_info["message"].caption.strip() 
                            for media_info in media_group)

            if has_caption:
                media_list = [media_info["media_data"] for media_info in media_group]
                media_group_data = {
                    "type": "media_group", 
                    "media": media_list,
                    "caption": DEFAULT_SIGNATURE or ""
                }

                queue = await load_queue()
                queue.append(media_group_data)
                await save_queue(queue)

                for media_info in media_group:
                    user_id = media_info["message"].from_user.id
                    pending_notifications[user_id] = True
                    add_user_to_queue_tracking(user_id, len(queue) - 1)

                media_types = [m.get("type", "photo") for m in media_list]
                media_count = {}
                for media_type in media_types:
                    type_name = {"photo": "фото", "video": "видео", "animation": "GIF", "document": "документ"}.get(media_type, "фото")
                    media_count[type_name] = media_count.get(type_name, 0) + 1

                media_text = " + ".join([f"{count} {name}" for name, count in media_count.items()])
                response = await format_queue_response(media_text, len(media_group), queue, is_media_group=True)
                await media_group[0]["message"].reply(response)
            else:
                for media_info in media_group:
                    await handle_single_media(media_info["message"], media_info["media_data"], media_info["media_type"])
        else:
            await handle_single_media(media_group[0]["message"], media_group[0]["media_data"], media_group[0]["media_type"])

    if media_group_id in pending_media_groups:
        del pending_media_groups[media_group_id]
    if media_group_id in media_group_timers:
        del media_group_timers[media_group_id]

async def handle_single_media(message: Message, media_data: dict, media_type: str):
    queue = await load_queue()
    queue.append(media_data)
    await save_queue(queue)

    user_id = message.from_user.id
    pending_notifications[user_id] = True
    add_user_to_queue_tracking(user_id, len(queue) - 1)

    response = await format_queue_response(media_type, 1, queue)
    await message.reply(response)

async def format_queue_response(media_text, media_count, queue, is_media_group=False):
    now = get_current_time()
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
    if not check_user_access(message.from_user.id):
        await message.reply("У вас нет прав для пользования этим ботом.")
        return

    if is_posting_locked:
        await message.reply("⏳ Сейчас будет запощен пост, ваше медиа добавлено в очередь")

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

    caption = message.caption if message.caption else (DEFAULT_SIGNATURE or "")

    media_data = {
        "file_id": file_id,
        "type": media_type,
        "caption": caption
    }

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
    if not check_user_access(message.from_user.id):
        await message.reply("У вас нет прав для пользования этим ботом.")
        return

    global posting_enabled, CHANNEL_ID, DEFAULT_SIGNATURE, POST_INTERVAL, last_post_time
    global ALLOWED_WEEKDAYS, START_TIME, END_TIME, DELAYED_START_ENABLED, DELAYED_START_TIME
    global TIME_WINDOW_ENABLED, WEEKDAYS_ENABLED, EXACT_TIMING_ENABLED, NOTIFICATIONS_ENABLED
    global CURRENT_TIMEZONE_STR, CURRENT_TIMEZONE
    global MODERATION_CHAT_ID, PARSER_ENABLED, PARSER_SPEED, PARSER_LIMIT, LABELS

    user_id = message.from_user.id
    text = message.text.strip()

    # --- ДИАГНОСТИЧЕСКАЯ КОМАНДА ДЛЯ ПРОВЕРКИ 1-го ПОСТА ---
    if text.startswith("/checkpost") or text.startswith("/testpost"):
        parts = text.split(maxsplit=1)
        search_tag = parts[1].strip() if len(parts) > 1 else "femboy"
        
        target_chat_id = MODERATION_CHAT_ID if MODERATION_CHAT_ID else message.chat.id
        await message.reply(f"🔍 Ищу свежие посты на Rule34 по тегу <code>{search_tag}</code>...")
        
        posts = await fetch_booru_posts("rule34", search_tag, limit=10)
        if not posts:
            posts = await fetch_booru_posts("gelbooru", search_tag, limit=10)
            
        if not posts:
            await message.reply(f"❌ На Rule34/Gelbooru не найдено постов по тегу <code>{search_tag}</code>.")
            return

        first_post = posts[0]
        post_id = first_post.get("id")
        file_url = first_post.get("file_url")
        
        caption = (
            f"🧪 <b>Проверочный арт (1-й в поиске)</b>\n\n"
            f"🏷 <b>Запрос:</b> <code>{search_tag}</code>\n"
            f"🆔 <b>ID:</b> <code>{post_id}</code>\n"
            f"🔗 <b>URL:</b> {file_url}"
        )

        if not file_url:
            await message.reply(f"⚠️ Пост #{post_id} найден, но у него нет файла.\n\n{caption}")
            return

        try:
            headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
            async with aiohttp.ClientSession() as session:
                async with session.get(file_url, headers=headers, timeout=12) as resp:
                    if resp.status == 200:
                        img_bytes = await resp.read()
                        photo_file = BufferedInputFile(img_bytes, filename=f"test_{post_id}.jpg")
                        await bot.send_photo(chat_id=target_chat_id, photo=photo_file, caption=caption)
                        if target_chat_id != message.chat.id:
                            await message.reply("✅ 1-й пост с картинкой отправлен в группу модерации!")
                    else:
                        await message.reply(f"❌ Не удалось скачать файл (HTTP {resp.status})")
        except Exception as e:
            await message.reply(f"❌ Ошибка отправки фото: {e}")
        return

    # Проверка, ждём ли мы ввод текущего времени от пользователя
    if user_id in waiting_for_time_input and waiting_for_time_input[user_id]:
        match = re.match(r"^([0-1]?[0-9]|2[0-3]):([0-5][0-9])$", text)
        if match:
            waiting_for_time_input[user_id] = False
            u_hour = int(match.group(1))
            u_min = int(match.group(2))
            
            now_utc = datetime.now(timezone.utc)
            candidates = [
                "Europe/Kyiv", "Europe/Prague", "Europe/Warsaw", 
                "Europe/London", "UTC", "Europe/Berlin", 
                "Europe/Moscow", "Asia/Dubai", "Asia/Tashkent", 
                "Asia/Almaty", "Asia/Bangkok", "Asia/Tokyo", 
                "America/New_York", "America/Los_Angeles"
            ]
            
            best_match = None
            min_diff = 999999
            
            for cand in candidates:
                try:
                    cand_time = now_utc.astimezone(ZoneInfo(cand))
                    cand_minutes = cand_time.hour * 60 + cand_time.minute
                    user_minutes = u_hour * 60 + u_min
                    diff = abs(cand_minutes - user_minutes)
                    if diff > 720:
                        diff = 1440 - diff
                    if diff < min_diff:
                        min_diff = diff
                        best_match = cand
                except Exception:
                    continue

            if best_match:
                cand_now = now_utc.astimezone(ZoneInfo(best_match)).strftime("%H:%M")
                confirm_kb = InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(text="✅ Да, установить", callback_data=f"tzconfirm:{best_match}"),
                    InlineKeyboardButton(text="❌ Отмена", callback_data="tzcancel")
                ]])
                await message.reply(
                    f"🔍 Время <b>{u_hour:02d}:{u_min:02d}</b> соответствует часовой зоне <b>{best_match}</b> (там сейчас {cand_now}).\n\n"
                    f"Установить <b>{best_match}</b> как основной часовой пояс бота?",
                    reply_markup=confirm_kb
                )
                return
            else:
                await message.reply("❌ Не удалось определить часовой пояс. Попробуйте выбрать вручную через /timezone")
                return
        else:
            await message.reply("❌ Неверный формат времени. Введите время в формате HH:MM (например, <code>14:30</code>):")
            return

    if text == "/start":
        enabled_by_default = []
        if posting_enabled: enabled_by_default.append("✅ Автопостинг")
        if EXACT_TIMING_ENABLED: enabled_by_default.append("✅ Точное планирование времени")
        if TIME_WINDOW_ENABLED: enabled_by_default.append("✅ Временные ограничения")
        if NOTIFICATIONS_ENABLED: enabled_by_default.append("✅ Уведомления о постах")
        if PARSER_ENABLED: enabled_by_default.append("✅ Парсер Rule34/Gelbooru")

        disabled_by_default = []
        if not WEEKDAYS_ENABLED: disabled_by_default.append("❌ Ограничения по дням недели")

        not_assigned = []
        if POST_INTERVAL is None: not_assigned.append("❓ Интервал постинга")
        if DEFAULT_SIGNATURE is None: not_assigned.append("❓ Подпись для постов")
        if START_TIME is None or END_TIME is None: not_assigned.append("❓ Временное окно постинга")
        if ALLOWED_WEEKDAYS is None: not_assigned.append("❓ Дни недели для постинга")
        if MODERATION_CHAT_ID is None: not_assigned.append("❓ Группа для модерации (/setmodgroup)")

        if CHANNEL_ID:
            try:
                chat_info = await bot.get_chat(CHANNEL_ID)
                channel_info = f"📢 Канал: {chat_info.title or CHANNEL_ID} ({CHANNEL_ID})"
            except:
                channel_info = f"📢 Канал: {CHANNEL_ID}"
        else:
            channel_info = "❌ Канал не установлен"

        start_text = (
            "👋 <b>Бот для автопостинга и парсера запущен!</b>\n\n"
            f"{channel_info}\n"
            f"🌍 Часовой пояс: <b>{CURRENT_TIMEZONE_STR}</b>\n\n"
            "<b>🟢 Включено по умолчанию:</b>\n"
            f"{chr(10).join(enabled_by_default)}\n\n"
        )

        if disabled_by_default:
            start_text += f"<b>🔴 Выключено по умолчанию:</b>\n{chr(10).join(disabled_by_default)}\n\n"

        if not_assigned:
            start_text += f"<b>❓ Не назначено:</b>\n{chr(10).join(not_assigned)}\n\n"

        start_text += "🛠 Используйте /help для помощи по настройке"
        await message.reply(start_text)

    elif text == "/help":
        help_text = """
🤖 <b>Справка по настройке бота</b>

<b>🌍 Настройка часового пояса:</b>
• /timezone - открыть меню выбора региона

<b>🔍 Агрегатор (Rule34 / Gelbooru):</b>
• /parser - статус и настройки парсера
• /addlabel Имя | теги | источники | эмодзи | режим | подпись
• /labels - список активных лейблов
• /dellabel Имя - удалить лейбл
• /checkpost [тег] - прислать 1-й пост по тегу
• /setmodgroup - привязать группу модерации
• /parserspeed сек - интервал проверки (например: /parserspeed 15)
• /parserlimit колво - лимит постов за раз (например: /parserlimit 20)

<b>⏱ Интервал постинга:</b>
• /interval 2h30m (дни d, часы h, минуты m, секунды s)

<b>🕐 Временное окно:</b>
• /settime 06:00 20:00

<b>📝 Подпись:</b>
• /title текст # ссылка

<b>🔧 Команды:</b>
/status - текущий статус и очередь
/commands - полный список всех команд
"""
        await message.reply(help_text)

    # --- КОМАНДЫ ПАРСЕРА И МОДЕРАЦИИ ---

    elif text == "/parser":
        status_parser = "✅ Включен" if PARSER_ENABLED else "❌ Выключен"
        mod_chat_str = MODERATION_CHAT_ID if MODERATION_CHAT_ID else "❓ Не привязана (/setmodgroup)"
        labels_count = len(LABELS)

        parser_info = f"""
🕵️‍♂️ <b>Статус Агрегатора (Rule34 & Gelbooru):</b>

Статус парсинга: <b>{status_parser}</b>
⏱ Интервал проверки: <b>{PARSER_SPEED} сек</b>
📊 Лимит постов за раз: <b>{PARSER_LIMIT} шт</b>
💬 Группа модерации: <code>{mod_chat_str}</code>
🏷 Активных лейблов: <b>{labels_count} шт</b>

<b>Команды управления:</b>
• /toggleparser - вкл/выкл парсинг
• /labels - список лейблов
• /addlabel - добавить новый лейбл
• /checkpost [тег] - отправить 1-й арт по тегу
• /parserspeed [сек] - установить скорость (от 1 сек)
• /parserlimit [кол-во] - установить лимит постов
"""
        await message.reply(parser_info)

    elif text == "/toggleparser":
        PARSER_ENABLED = not PARSER_ENABLED
        save_state()
        st = "включен" if PARSER_ENABLED else "выключен"
        await message.reply(f"{'✅' if PARSER_ENABLED else '❌'} Парсер Rule34/Gelbooru {st}!")

    elif text.startswith("/parserspeed"):
        parts = text.split(maxsplit=1)
        if len(parts) == 1:
            await message.reply(f"⏱ Текущий интервал парсинга: <b>{PARSER_SPEED} сек</b>\n\nПример смены: <code>/parserspeed 10</code> (проверка каждые 10 секунд)")
            return
        try:
            val = int(parts[1].strip())
            if val < 1:
                await message.reply("❌ Скорость должна быть от 1 секунды и выше.")
                return
            PARSER_SPEED = val
            save_state()
            await message.reply(f"✅ Скорость парсинга изменена: <b>{PARSER_SPEED} сек</b>")
        except ValueError:
            await message.reply("❌ Число должно быть целым. Пример: <code>/parserspeed 15</code>")

    elif text.startswith("/parserlimit"):
        parts = text.split(maxsplit=1)
        if len(parts) == 1:
            await message.reply(f"📊 Текущий лимит глубина: <b>{PARSER_LIMIT} постов</b>\n\nПример смены: <code>/parserlimit 30</code>")
            return
        try:
            val = int(parts[1].strip())
            if val < 1 or val > 100:
                await message.reply("❌ Лимит должен быть в диапазоне от 1 до 100.")
                return
            PARSER_LIMIT = val
            save_state()
            await message.reply(f"✅ Лимит парсера установлен на <b>{PARSER_LIMIT}</b> постов!")
        except ValueError:
            await message.reply("❌ Пример вызова: <code>/parserlimit 20</code>")

    elif text == "/setmodgroup":
        MODERATION_CHAT_ID = message.chat.id
        save_state()
        await message.reply(f"✅ Эта группа привязана как **Группа Модерации**!\nID: <code>{MODERATION_CHAT_ID}</code>\nСюда будут прилетать посты для проверки.")

    elif text.startswith("/addlabel"):
        parts = text.split(maxsplit=1)
        if len(parts) == 1:
            instructions = """
⚙️ <b>Как добавить новый лейбл для парсинга:</b>

<b>Формат:</b>
<code>/addlabel Имя | теги | источники | эмодзи | режим | подпись</code>

<b>Параметры:</b>
1. <b>Имя:</b> Любое название (например: <code>Dross</code>)
2. <b>Теги:</b> Теги для поиска (например: <code>dross rating:explicit</code>)
3. <b>Источники:</b> <code>rule34</code>, <code>gelbooru</code> или <code>all</code>
4. <b>Эмодзи:</b> Обычный (<code>🍆</code>) или ID Custom Emoji (<code>5368324170671202286</code>)
5. <b>Режим:</b> <code>MANUAL</code> (в группу модерации) или <code>AUTO</code> (сразу в очередь)
6. <b>Подпись:</b> Необязательно. Подпись для этого лейбла.

<b>Пример:</b>
<code>/addlabel Dross | dross rating:explicit | rule34 | 🍆 | MANUAL | 🎨 Автор: Dross</code>
"""
            await message.reply(instructions)
            return

        try:
            raw_data = parts[1].split("|")
            if len(raw_data) < 5:
                await message.reply("❌ Недостаточно параметров! Отправьте /addlabel без аргументов для чтения инструкции.")
                return

            lname = raw_data[0].strip()
            ltags = raw_data[1].strip()
            lsources_str = raw_data[2].strip().lower()
            lemoji = raw_data[3].strip()
            lmode = raw_data[4].strip().upper()
            lsig = raw_data[5].strip() if len(raw_data) > 5 else None

            sources = ["rule34", "gelbooru"] if lsources_str == "all" else [lsources_str]
            if lmode not in ["MANUAL", "AUTO"]: lmode = "MANUAL"

            new_label = {
                "name": lname,
                "tags": ltags,
                "sources": sources,
                "emoji": lemoji,
                "mode": lmode,
                "signature": lsig
            }

            # Удаляем старый лейбл с таким же именем, если был
            LABELS[:] = [lbl for lbl in LABELS if lbl["name"].lower() != lname.lower()]
            LABELS.append(new_label)
            save_state()

            await message.reply(f"✅ Лейбл <b>{lname}</b> успешно добавлен/обновлен!")
        except Exception as e:
            await message.reply(f"❌ Ошибка разбора команды: {e}")

    elif text == "/labels":
        if not LABELS:
            await message.reply("📭 Активных лейблов пока нет. Добавьте первый через /addlabel")
            return

        lbl_text = "🏷 <b>Список активных лейблов:</b>\n\n"
        for i, lbl in enumerate(LABELS, 1):
            sources_str = ", ".join(lbl.get("sources", []))
            em = lbl.get("emoji", "🏷")
            lbl_text += (
                f"<b>{i}. {em} {lbl['name']}</b> ({lbl.get('mode')})\n"
                f"• Теги: <code>{lbl['tags']}</code>\n"
                f"• Сайты: {sources_str}\n\n"
            )
        lbl_text += "💡 Удалить лейбл: <code>/dellabel Имя</code>"
        await message.reply(lbl_text)

    elif text.startswith("/dellabel"):
        parts = text.split(maxsplit=1)
        if len(parts) == 1:
            await message.reply("❌ Пример: <code>/dellabel Dross</code>")
            return

        target_name = parts[1].strip().lower()
        initial_len = len(LABELS)
        LABELS[:] = [lbl for lbl in LABELS if lbl["name"].lower() != target_name]

        if len(LABELS) < initial_len:
            save_state()
            await message.reply(f"✅ Лейбл <b>{parts[1].strip()}</b> удалён!")
        else:
            await message.reply("❌ Лейбл с таким именем не найден.")

    # --- СТАНДАРТНЫЕ КОМАНДЫ АВТОПОСТИНГА ---

    elif text.startswith("/timezone") or text.startswith("/tz"):
        now_tz = get_current_time().strftime("%Y-%m-%d %H:%M:%S")
        await message.reply(
            f"🌍 <b>Текущий часовой пояс:</b> {CURRENT_TIMEZONE_STR}\n"
            f"🕒 <b>Время бота:</b> {now_tz}\n\n"
            "Выберите регион из списка или нажмите ввод времени:",
            reply_markup=get_timezone_keyboard()
        )

    elif text == "/commands":
        commands_text = """
📋 <b>Полный список команд</b>

<b>🔍 Агрегатор (Rule34 / Gelbooru):</b>
/parser - статус парсера
/toggleparser - вкл/выкл парсер
/addlabel - добавить лейбл поиска
/labels - список лейблов
/dellabel - удалить лейбл
/checkpost - прислать 1-й арт по тегу
/setmodgroup - сделать чат группой модерации
/parserspeed - скорость парсинга в сек
/parserlimit - глубина проверок

<b>🌍 Часовой пояс:</b>
/timezone - меню настройки региона и времени

<b>📤 Основное управление:</b>
/start - информация о боте и настройках
/help - справка по использованию
/status - статус бота и очередь
/toggle - включить/выключить автопостинг

<b>⏰ Управление расписанием:</b>
/schedule - показать расписание
/interval - установить интервал (2h30m)
/settime - установить временное окно (06:00 20:00)
/days - установить дни недели (1,2,3,4,5)
/checktime - проверить текущее время

<b>📢 Канал и подписи:</b>
/channel - текущий канал
/setchannel - установить ID канала
/title - подписи к постам

<b>📋 Управление очередью:</b>
/clear - очистить очередь
/remove - удалить пост
/random - перемешать очередь

<b>⚡ Публикация:</b>
/postfile - опубликовать по номеру
/postnow - опубликовать сейчас
/postall - опубликовать всё
"""
        await message.reply(commands_text)

    elif text == "/status":
        now = get_current_time()
        queue = await load_queue()

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

        if len(queue) > 0 and POST_INTERVAL is not None:
            time_until_next = get_time_until_next_post()
            next_post_text = format_interval(time_until_next) if time_until_next > 0 else "сейчас"
        elif len(queue) > 0:
            next_post_text = "❓ интервал не установлен"
        else:
            next_post_text = "нет постов в очереди"

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

        if posting_enabled and posting_allowed and delayed_ready:
            status_emoji = "✅"
            status_text = "активен"
        else:
            status_emoji = "❌"
            reasons = []
            if not posting_enabled: reasons.append("отключён")
            if not posting_allowed: reasons.append(reason.lower())
            if not delayed_ready: reasons.append("ожидание старта")
            status_text = ", ".join(reasons)

        delayed_text = ""
        if DELAYED_START_ENABLED and DELAYED_START_TIME and not delayed_ready:
            delayed_text = f"\n⏳ Старт: {DELAYED_START_TIME.strftime('%d.%m %H:%M')}"

        queue_stats = format_queue_stats(queue)

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
🕵️‍♂️ Агрегатор: {'✅ активен' if PARSER_ENABLED else '❌ выключен'} ({len(LABELS)} лейблов)
🌍 Часовой пояс: {CURRENT_TIMEZONE_STR}
📊 В очереди: {queue_stats}
🕐 Следующий пост: {next_post_text}
📅 Время публикации всех фото: {total_time_text}{schedule_detail}{delayed_text}

💬 Канал: {channel_text}
🏷 Подпись: {signature_text}
{'🔔' if NOTIFICATIONS_ENABLED else '🔕'} Уведомления: {'включены' if NOTIFICATIONS_ENABLED else 'выключены'}
{'🎯' if EXACT_TIMING_ENABLED else '⏱'} Планирование: {'точное' if EXACT_TIMING_ENABLED else 'интервальное'}

💡 /help для команд | /parser для настроек сбора
"""
        await message.reply(status_text_full)

    elif text.startswith("/interval"):
        parts = text.split(maxsplit=1)
        if len(parts) == 1:
            current_interval = format_interval(POST_INTERVAL) if POST_INTERVAL is not None else "❓ не установлен"
            await message.reply(f"📊 Текущий интервал: {current_interval}\n\nДля изменения: /interval 2h30m")
            return

        new_interval = parse_interval(parts[1])
        if new_interval:
            POST_INTERVAL = new_interval
            save_state()
            formatted_interval = format_interval(new_interval)
            await message.reply(f"✅ Интервал установлен: {formatted_interval}")
        else:
            await message.reply("❌ Неверный формат интервала. Пример: 2h30m")

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

        schedule_text = f"""
📅 <b>Расписание постинга:</b>

{status_emoji} Статус: {status_text}
🌍 Часовой пояс: {CURRENT_TIMEZONE_STR}
{timing_info}

{'✅' if TIME_WINDOW_ENABLED else '❌'} Временное окно: {time_window_text}
{'✅' if WEEKDAYS_ENABLED else '❌'} Дни недели: {allowed_days}
{'✅' if EXACT_TIMING_ENABLED else '❌'} Точное планирование: {'включено' if EXACT_TIMING_ENABLED else 'выключено'}
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
            await message.reply("❌ Неверный формат времени. Пример: /settime 06:00 20:00")

    elif text.startswith("/days"):
        parts = text.split(maxsplit=1)
        if len(parts) == 1:
            if ALLOWED_WEEKDAYS is not None:
                current_days = ", ".join([f"{day+1}({get_weekday_short(day)})" for day in sorted(ALLOWED_WEEKDAYS)])
            else:
                current_days = "❓ не назначены"
            await message.reply(f"📅 Текущие дни: {current_days}\n\nДля изменения: /days 1,2,3,4,5")
            return

        try:
            days = [int(x.strip()) - 1 for x in parts[1].split(",")]
            if all(0 <= day <= 6 for day in days):
                ALLOWED_WEEKDAYS = sorted(list(set(days)))
                save_state()
                day_names = ", ".join([get_weekday_short(day) for day in ALLOWED_WEEKDAYS])
                await message.reply(f"✅ Дни недели установлены: {day_names}")
            else:
                await message.reply("❌ Используйте числа от 1 до 7")
        except:
            await message.reply("❌ Пример: /days 1,2,3,4,5")

    elif text.startswith("/startdate"):
        parts = text.split(maxsplit=1)
        if len(parts) == 1:
            if DELAYED_START_ENABLED and DELAYED_START_TIME:
                await message.reply(f"⏳ Отложенный старт: {DELAYED_START_TIME.strftime('%Y-%m-%d %H:%M')}")
            else:
                await message.reply("⏳ Отложенный старт не установлен")
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

            target_datetime = target_datetime.replace(tzinfo=CURRENT_TIMEZONE)

            if target_datetime <= get_current_time():
                await message.reply("❌ Указанное время уже прошло")
                return

            DELAYED_START_ENABLED = True
            DELAYED_START_TIME = target_datetime
            save_state()

            await message.reply(f"✅ Отложенный старт установлен на {target_datetime.strftime('%Y-%m-%d %H:%M')}")
        except:
            await message.reply("❌ Формат: YYYY-MM-DD HH:MM или DD.MM.YYYY HH:MM")

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
        now = get_current_time()
        current_weekday = now.weekday()
        posting_allowed, reason = is_posting_allowed()

        time_status = "✅ включено" if TIME_WINDOW_ENABLED else "❌ выключено"
        days_status = "✅ включено" if WEEKDAYS_ENABLED else "❌ выключено"

        time_window_text = f"{START_TIME.strftime('%H:%M')} - {END_TIME.strftime('%H:%M')}" if START_TIME and END_TIME else "❓ не назначено"
        allowed_days_text = ', '.join([get_weekday_short(d) for d in sorted(ALLOWED_WEEKDAYS)]) if ALLOWED_WEEKDAYS else "❓ не назначены"

        check_text = f"""
🕐 <b>Проверка времени:</b>

⏰ Текущее время: {now.strftime('%Y-%m-%d %H:%M:%S')} ({CURRENT_TIMEZONE_STR})
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
        await message.reply(f"{'🎯' if EXACT_TIMING_ENABLED else '⏱'} Точное планирование {status}!")

    elif text == "/togglenotify":
        NOTIFICATIONS_ENABLED = not NOTIFICATIONS_ENABLED
        save_state()
        status = "включены" if NOTIFICATIONS_ENABLED else "выключены"
        await message.reply(f"{'🔔' if NOTIFICATIONS_ENABLED else '🔕'} Уведомления {status}!")

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
            await message.reply("❌ ID канала должен быть числом.")

    elif text == "/channel":
        if CHANNEL_ID:
            await message.reply(f"📢 Текущий канал: {CHANNEL_ID}")
        else:
            await message.reply("❌ Канал не установлен. Используйте /setchannel -1001234567890")

    elif text.startswith("/title"):
        parts = text.split(maxsplit=1)
        if len(parts) == 1:
            queue = await load_queue()
            queue_info = f"\n📊 В очереди: {len(queue)} постов" if queue else "\n📭 Очередь пуста"
            current_signature = DEFAULT_SIGNATURE if DEFAULT_SIGNATURE is not None else "❓ не установлена"

            menu_text = f"""
📝 <b>Управление подписью:</b>

🏷 Текущая подпись: {current_signature}{queue_info}

<b>Команда:</b>
• /title текст - установить подпись для всех постов
• /title текст # ссылка - установить кликабельную подпись
"""
            await message.reply(menu_text)
            return

        new_signature = parts[1]
        parsed_signature = parse_signature_with_link(new_signature)
        DEFAULT_SIGNATURE = parsed_signature
        save_state()

        updated_count = await apply_signature_to_all_queue(new_signature)
        await message.reply(f"✅ Подпись обновлена и применена к {updated_count} постам в очереди!")

    elif text == "/clear":
        queue = await load_queue()
        if queue:
            await save_queue([])
            await message.reply(f"✅ Очередь очищена!")
        else:
            await message.reply("📭 Очередь уже пуста")

    elif text.startswith("/remove"):
        parts = text.split(maxsplit=1)
        if len(parts) == 1:
            await message.reply("❌ Пример: /remove 1")
            return

        try:
            index = int(parts[1]) - 1
            queue = await load_queue()

            if 0 <= index < len(queue):
                queue.pop(index)
                await save_queue(queue)
                await message.reply(f"✅ Медиа #{index + 1} удалено")
            else:
                await message.reply(f"❌ Неверный номер")
        except ValueError:
            await message.reply("❌ Номер должен быть числом")

    elif text == "/random":
        if await shuffle_queue():
            await message.reply("🔀 Очередь перемешана!")
        else:
            await message.reply("❌ Нужно минимум 2 медиа")

    elif text.startswith("/postfile"):
        parts = text.split(maxsplit=1)
        if len(parts) == 1:
            await message.reply("❌ Пример: /postfile 1")
            return

        try:
            index = int(parts[1]) - 1
            queue = await load_queue()

            if not CHANNEL_ID:
                await message.reply("❌ Канал не установлен")
                return

            if 0 <= index < len(queue):
                media_data = queue.pop(index)
                await save_queue(queue)

                try:
                    pending_notifications[message.from_user.id] = True

                    if isinstance(media_data, dict) and media_data.get("type") == "media_group":
                        await send_media_group_to_channel(media_data)
                        await notify_users_about_publication("media_group", True)
                    else:
                        await send_single_media(media_data)
                        await notify_users_about_publication("медиа", True)

                    last_post_time = time.time()
                    save_state()

                except Exception as e:
                    queue.insert(index, media_data)
                    await save_queue(queue)
                    await message.reply(f"❌ Ошибка публикации: {e}")
            else:
                await message.reply(f"❌ Неверный номер")
        except ValueError:
            await message.reply("❌ Номер должен быть числом")

    elif text == "/postnow":
        queue = await load_queue()
        if not queue:
            await message.reply("📭 Очередь пуста")
            return

        if not CHANNEL_ID:
            await message.reply("❌ Канал не установлен")
            return

        try:
            media_data = queue.pop(0)
            await save_queue(queue)
            pending_notifications[message.from_user.id] = True

            if isinstance(media_data, dict) and media_data.get("type") == "media_group":
                await send_media_group_to_channel(media_data)
                await notify_users_about_publication("media_group", True)
            else:
                await send_single_media(media_data)
                await notify_users_about_publication("медиа", True)

            last_post_time = time.time()
            save_state()

        except Exception as e:
            queue.insert(0, media_data)
            await save_queue(queue)
            await message.reply(f"❌ Ошибка публикации: {e}")

    elif text == "/postall":
        queue = await load_queue()
        if not queue:
            await message.reply("📭 Очередь пуста")
            return

        if not CHANNEL_ID:
            await message.reply("❌ Канал не установлен")
            return

        total_media, media_groups, total_posts, photos, videos, gifs, documents = count_queue_stats(queue)
        await message.reply(f"🚀 Начинаю публикацию всех {total_posts} постов...")

        success_count = 0
        try:
            for i, media_data in enumerate(queue):
                try:
                    if isinstance(media_data, dict) and media_data.get("type") == "media_group":
                        await send_media_group_to_channel(media_data)
                    else:
                        await send_single_media(media_data)
                    success_count += 1
                    await asyncio.sleep(0.5)
                except Exception as e:
                    logger.error(f"Ошибка публикации поста #{i+1}: {e}")

            await save_queue([])
            last_post_time = time.time()
            save_state()
            await message.reply(f"✅ Опубликовано: {success_count}/{total_posts}")

        except Exception as e:
            await message.reply(f"❌ Ошибка при массовой публикации: {e}")

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
    try:
        if not ALLOWED_USERS:
            return
            
        queue = await load_queue()
        queue_count = len(queue)
        
        if queue_count > 0 and POST_INTERVAL is not None:
            if EXACT_TIMING_ENABLED:
                next_exact_time = get_next_exact_posting_time()
                next_post_time = next_exact_time.strftime('%H:%M') if next_exact_time else "по расписанию"
            else:
                time_until_next = get_time_until_next_post()
                next_post_time = f"через {format_interval(time_until_next)}" if time_until_next > 0 else "сейчас"
        else:
            next_post_time = "нет постов" if queue_count == 0 else "интервал не установлен"
        
        startup_message = f"🔄 Бот запущен ({CURRENT_TIMEZONE_STR}). Очередь: {queue_count} постов. Парсер: {'✅' if PARSER_ENABLED else '❌'}."
        
        first_user = ALLOWED_USERS[0]
        await bot.send_message(chat_id=first_user, text=startup_message)
        logger.info(f"📩 Уведомление о запуске отправлено пользователю {first_user}")
        
    except Exception as e:
        logger.error(f"❌ Ошибка отправки уведомления о запуске: {e}")

async def check_immediate_posting():
    try:
        queue = await load_queue()
        if not queue or not posting_enabled or not CHANNEL_ID:
            return
            
        posting_allowed, reason = is_posting_allowed()
        delayed_ready = is_delayed_start_ready()
        
        if posting_allowed and delayed_ready and POST_INTERVAL is not None:
            time_since_last = time.time() - last_post_time
            if time_since_last >= POST_INTERVAL:
                logger.info("🚀 Запуск немедленного постинга - условия выполнены при старте")
                asyncio.create_task(asyncio.sleep(5))
            else:
                remaining_time = POST_INTERVAL - time_since_last
                logger.info(f"⏰ До следующего поста: {format_interval(int(remaining_time))}")
    except Exception as e:
        logger.error(f"❌ Ошибка проверки немедленного постинга: {e}")

async def main():
    logger.info("[BOT] Запущен")
    
    # Инициализация БД и загрузка конфигурации
    await init_db()
    await load_state_from_db()

    # Установка понятного всплывающего меню команд для Telegram
    await bot.set_my_commands([
        BotCommand(command="start", description="🚀 Главное меню и информация"),
        BotCommand(command="status", description="📊 Статус бота и очередь"),
        BotCommand(command="parser", description="🕵️‍♂️ Статус и меню парсера"),
        BotCommand(command="labels", description="🏷 Посмотреть активные лейблы"),
        BotCommand(command="addlabel", description="➕ Добавить лейбл для сбора"),
        BotCommand(command="checkpost", description="🔍 Прислать 1-й пост по тегу"),
        BotCommand(command="timezone", description="🌍 Выбрать часовой пояс / время"),
        BotCommand(command="interval", description="⏱ Установить интервал (2h30m)"),
        BotCommand(command="settime", description="🕐 Установить временное окно"),
        BotCommand(command="schedule", description="📅 Расписание публикаций"),
        BotCommand(command="toggle", description="🔄 Вкл/Выкл автопостинг"),
        BotCommand(command="title", description="📝 Настройка подписи постов"),
        BotCommand(command="postnow", description="⚡ Опубликовать следующий пост"),
        BotCommand(command="postall", description="🚀 Опубликовать всю очередь"),
        BotCommand(command="clear", description="🗑 Очистить всю очередь"),
        BotCommand(command="random", description="🔀 Перемешать посты"),
        BotCommand(command="commands", description="📋 Полный список команд"),
        BotCommand(command="help", description="❓ Справка по работе"),
    ])

    await start_web_server()
    await check_immediate_posting()
    
    # Фоновое планирование: автопостинг в канал + сбор с бордов
    asyncio.create_task(posting_loop())
    asyncio.create_task(parser_loop())
    
    await send_startup_notification()

    logger.info("🤖 Бот запущен и готов к работе!")
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

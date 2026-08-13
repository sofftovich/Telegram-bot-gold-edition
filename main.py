import os
import sys
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
from aiogram.types import (
    Message, BotCommand, InlineKeyboardMarkup, InlineKeyboardButton, 
    CallbackQuery, BotCommandScopeAllPrivateChats, BotCommandScopeAllGroupChats,
    BufferedInputFile
)
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from dotenv import load_dotenv
from aiohttp import web

load_dotenv()

# Настройка логирования в stdout
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)

# Конфигурация
TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_ID = os.getenv("CHANNEL_ID")
DATABASE_URL = os.getenv("DATABASE_URL")
PROXY_URL = os.getenv("PROXY_URL", None)

# Загрузка разрешённых пользователей
ALLOWED_USERS = []
for i in range(1, 4):
    user_id = os.getenv(f"ALLOWED_USER_{i}")
    if user_id:
        try:
            ALLOWED_USERS.append(int(user_id))
        except ValueError:
            logger.warning(f"Неверный формат ALLOWED_USER_{i}: {user_id}")

if not ALLOWED_USERS or not TOKEN:
    logger.error("❌ Ошибка конфигурации TOKEN или ALLOWED_USERS!")
    exit(1)

# Часовой пояс по умолчанию
CURRENT_TIMEZONE_STR = "Europe/Kyiv"
CURRENT_TIMEZONE = ZoneInfo(CURRENT_TIMEZONE_STR)

def get_current_time():
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

# Настройки и Статистика Агрегатора
MODERATION_CHAT_ID = None      
PARSER_ENABLED = True           
PARSER_SPEED = 15               
PARSER_LIMIT = 20               
ALLOW_AI_POSTS = False          
LABELS = []                     

PARSER_STATS = {
    "total_scanned": 0,
    "duplicates_filtered": 0,
    "sent_to_mod": 0,
    "auto_queued": 0,
    "network_errors": 0,
    "start_time": time.time()
}

QUEUE_FILE = "queue.json"

# Состояния
add_label_wizard = {}  
waiting_for_time_input = {}
pending_media_groups = {}
media_group_timers = {}
pending_notifications = {}
user_media_tracking = {}
is_posting_locked = False
db_pool = None

bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

# --- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ АВТОПОСТИНГА ---

def format_queue_stats(queue):
    if not queue:
        return "0 постов"
    count = len(queue)
    photos = sum(1 for item in queue if item.get("type") == "photo")
    videos = sum(1 for item in queue if item.get("type") == "video")
    animation = sum(1 for item in queue if item.get("type") == "animation")
    groups = sum(1 for item in queue if item.get("type") == "media_group")
    
    details = []
    if photos: details.append(f"{photos} фото")
    if videos: details.append(f"{videos} видео")
    if animation: details.append(f"{animation} гиф")
    if groups: details.append(f"{groups} альбомов")
    
    return f"{count} шт ({', '.join(details)})" if details else f"{count} шт"

# --- POSTGRESQL ---

async def init_db():
    global db_pool
    if not DATABASE_URL: return
    
    url = DATABASE_URL.replace("postgres://", "postgresql://", 1) if DATABASE_URL.startswith("postgres://") else DATABASE_URL

    try:
        db_pool = await asyncpg.create_pool(dsn=url)
        async with db_pool.acquire() as conn:
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
        logger.info("✅ PostgreSQL инициализирована!")
    except Exception as e:
        logger.error(f"❌ Ошибка подключения к PostgreSQL: {e}")

async def load_state_from_db():
    global last_post_time, CHANNEL_ID, posting_enabled, DEFAULT_SIGNATURE, POST_INTERVAL
    global ALLOWED_WEEKDAYS, START_TIME, END_TIME, DELAYED_START_ENABLED, DELAYED_START_TIME
    global TIME_WINDOW_ENABLED, WEEKDAYS_ENABLED, EXACT_TIMING_ENABLED, NOTIFICATIONS_ENABLED
    global CURRENT_TIMEZONE_STR, CURRENT_TIMEZONE
    global MODERATION_CHAT_ID, PARSER_ENABLED, PARSER_SPEED, PARSER_LIMIT, ALLOW_AI_POSTS, LABELS

    if not db_pool: return

    try:
        async with db_pool.acquire() as conn:
            row = await conn.fetchrow("SELECT config, labels FROM bot_config WHERE id = 1;")
            if row and row["config"]:
                state = json.loads(row["config"]) if isinstance(row["config"], str) else row["config"]
                
                last_post_time = state.get("last_post_time", 0)
                DEFAULT_SIGNATURE = state.get("default_signature")
                saved_channel = state.get("channel_id")
                if saved_channel: CHANNEL_ID = saved_channel
                
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

                MODERATION_CHAT_ID = state.get("moderation_chat_id")
                PARSER_ENABLED = state.get("parser_enabled", True)
                PARSER_SPEED = state.get("parser_speed", 15)
                PARSER_LIMIT = state.get("parser_limit", 20)
                ALLOW_AI_POSTS = state.get("allow_ai_posts", False)

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

            logger.info("✅ Конфигурация и лейблы загружены!")
    except Exception as e:
        logger.error(f"❌ Ошибка загрузки состояния из БД: {e}")

def save_state():
    asyncio.create_task(async_save_state())

async def async_save_state():
    if not db_pool: return

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
        "parser_limit": PARSER_LIMIT,
        "allow_ai_posts": ALLOW_AI_POSTS
    }

    try:
        async with db_pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO bot_config (id, config, labels, updated_at)
                VALUES (1, $1::jsonb, $2::jsonb, CURRENT_TIMESTAMP)
                ON CONFLICT (id) 
                DO UPDATE SET config = EXCLUDED.config, labels = EXCLUDED.labels, updated_at = CURRENT_TIMESTAMP;
            """, json.dumps(state, ensure_ascii=False), json.dumps(LABELS, ensure_ascii=False))
    except Exception as e:
        logger.error(f"❌ Ошибка сохранения состояния в БД: {e}")

async def load_queue():
    if db_pool:
        try:
            async with db_pool.acquire() as conn:
                row = await conn.fetchrow("SELECT queue FROM bot_config WHERE id = 1;")
                if row and row["queue"] is not None:
                    q = row["queue"]
                    return json.loads(q) if isinstance(q, str) else q
        except Exception as e:
            logger.error(f"❌ Ошибка загрузки очереди: {e}")

    if not os.path.exists(QUEUE_FILE): return []
    try:
        with open(QUEUE_FILE, "r", encoding="utf-8") as f: return json.load(f)
    except: return []

async def save_queue(queue):
    try:
        with open(QUEUE_FILE, "w", encoding="utf-8") as f:
            json.dump(queue, f, ensure_ascii=False)
    except Exception as e:
        logger.error(f"Ошибка сохранения локального файла очереди: {e}")

    if not db_pool: return
    try:
        async with db_pool.acquire() as conn:
            await conn.execute("UPDATE bot_config SET queue = $1::jsonb WHERE id = 1;", json.dumps(queue, ensure_ascii=False))
    except Exception as e:
        logger.error(f"❌ Ошибка сохранения очереди в PostgreSQL: {e}")

# --- ДЕДУПЛИКАЦИЯ ---

async def is_post_seen(source, post_id):
    if not db_pool: return False
    try:
        async with db_pool.acquire() as conn:
            row = await conn.fetchrow("SELECT 1 FROM seen_posts WHERE source=$1 AND post_id=$2", source, str(post_id))
            return row is not None
    except Exception: return False

async def is_md5_seen(file_md5):
    if not db_pool or not file_md5: return None
    try:
        async with db_pool.acquire() as conn:
            row = await conn.fetchrow("SELECT source, post_id, created_at FROM seen_posts WHERE file_md5=$1", str(file_md5))
            return row
    except Exception: return None

async def mark_post_as_seen(source, post_id, file_md5=None):
    if not db_pool: return
    try:
        async with db_pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO seen_posts (source, post_id, file_md5)
                VALUES ($1, $2, $3)
                ON CONFLICT (source, post_id) DO NOTHING;
            """, source, str(post_id), file_md5)
    except Exception: pass

# --- ПАРСЕР ПОСТОВ (ЧИСТЫЙ ПРЯМОЙ JSON) ---

AI_TAGS = {"ai_generated", "created_by_ai", "novelai", "stable_diffusion", "midjourney", "ai_art"}

def is_ai_post(post_data):
    tags_str = post_data.get("tags", "")
    if isinstance(tags_str, list):
        tags_str = " ".join(tags_str)
    post_tags = set(str(tags_str).lower().split())
    return bool(post_tags.intersection(AI_TAGS))

def normalize_tags(raw_tags):
    parts = raw_tags.split()
    normalized = []
    
    corrections = {
        "antro": "anthro",
        "-antro": "-anthro"
    }

    for part in parts:
        lower_p = part.lower()
        if lower_p in corrections:
            part = corrections[lower_p]

        if part.startswith("-"):
            normalized.append("-" + part[1:].replace(" ", "_"))
        else:
            normalized.append(part.replace(" ", "_"))

    return " ".join(normalized)

async def fetch_booru_posts(source, tags, limit=20):
    posts = []
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*"
    }
    
    clean_tags = normalize_tags(tags).replace(" ", "+")

    if source == "rule34":
        url = f"https://api.rule34.xxx/index.php?page=dapi&s=post&q=index&json=1&limit={limit}&tags={clean_tags}"
    elif source == "gelbooru":
        url = f"https://gelbooru.com/index.php?page=dapi&s=post&q=index&json=1&limit={limit}&tags={clean_tags}"
    else:
        return posts

    logger.info(f"🔍 [PARSER FETCH] {source} | URL: {url}")

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, proxy=PROXY_URL, timeout=15) as resp:
                logger.info(f"📡 [PARSER RESP] {source} Status: {resp.status}")
                if resp.status == 200:
                    text_data = await resp.text()
                    if not text_data.strip():
                        logger.warning(f"⚠️ {source} вернул пустой ответ (0 байт).")
                        return posts

                    try:
                        data = json.loads(text_data)
                        raw_posts = []
                        if isinstance(data, list):
                            raw_posts = data
                        elif isinstance(data, dict) and "post" in data:
                            raw_posts = data["post"]

                        for p in raw_posts:
                            if not ALLOW_AI_POSTS and is_ai_post(p):
                                continue
                            posts.append(p)
                            
                        PARSER_STATS["total_scanned"] += len(posts)
                        logger.info(f"🎉 [PARSER SUCCESS] {source}: Распознано {len(posts)} постов!")
                    except json.JSONDecodeError:
                        logger.error(f"❌ [НЕ JSON!] Заглушка Cloudflare? Первые 100 симв: {text_data[:100]}")
                        PARSER_STATS["network_errors"] += 1
                else:
                    PARSER_STATS["network_errors"] += 1
    except Exception as e:
        PARSER_STATS["network_errors"] += 1
        logger.error(f"❌ [PARSER REQ ERR] {source}: {e}")

    return posts

def format_emoji_label(emoji_val, label_name):
    if emoji_val and emoji_val.isdigit():
        return f'<tg-emoji emoji-id="{emoji_val}">🏷</tg-emoji> <b>{label_name}</b>'
    elif emoji_val:
        return f'{emoji_val} <b>{label_name}</b>'
    return f'🏷 <b>{label_name}</b>'

async def process_parsed_post(label, post_data, source):
    post_id = str(post_data.get("id"))
    file_url = post_data.get("file_url")
    file_md5 = post_data.get("md5") or post_data.get("hash")

    if not file_url or not post_id: return

    if await is_post_seen(source, post_id):
        PARSER_STATS["duplicates_filtered"] += 1
        return

    duplicate_info = await is_md5_seen(file_md5)
    is_suspicious = duplicate_info is not None

    if source == "gelbooru":
        source_link = f'<a href="https://gelbooru.com/index.php?page=post&s=view&id={post_id}">🟡 Gelbooru</a>'
    else:
        source_link = f'<a href="https://rule34.xxx/index.php?page=post&s=view&id={post_id}">🟢 Rule34</a>'

    label_header = format_emoji_label(label.get("emoji"), label.get("name"))
    
    card_caption = f"{label_header}\n"
    if is_suspicious:
        card_caption = f"⚠️ <b>СОМНИТЕЛЬНО (Возможен дубликат)</b>\n" + card_caption
        card_caption += f"💡 <i>Ранее зафиксирован в БД из источника {duplicate_info['source']}</i>\n"

    card_caption += f"🔗 <b>Источник:</b> {source_link}\n"
    card_caption += f"🆔 <code>{post_id}</code>"

    mode = label.get("mode", "MANUAL")
    custom_signature = label.get("signature") or DEFAULT_SIGNATURE or ""

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="📥 В очередь", callback_data=f"mod:queue:{source}:{post_id}"),
            InlineKeyboardButton(text="🚀 Постить сейчас", callback_data=f"mod:now:{source}:{post_id}")
        ],
        [
            InlineKeyboardButton(text="❌ Скрыть / Удалить", callback_data="mod:delete")
        ]
    ])

    if mode == "AUTO" and not is_suspicious:
        queue = await load_queue()
        queue.append({"file_id": file_url, "type": "photo", "caption": custom_signature})
        await save_queue(queue)
        await mark_post_as_seen(source, post_id, file_md5)
        PARSER_STATS["auto_queued"] += 1
        logger.info(f"⚡ [AUTO POSTED] {source} #{post_id} отправлен в авто-очередь")
        return

    if MODERATION_CHAT_ID:
        try:
            headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
            async with aiohttp.ClientSession() as session:
                async with session.get(file_url, headers=headers, proxy=PROXY_URL, timeout=15) as resp:
                    if resp.status == 200:
                        image_bytes = await resp.read()
                        photo_file = BufferedInputFile(image_bytes, filename=f"{post_id}.jpg")

                        msg = await bot.send_photo(
                            chat_id=MODERATION_CHAT_ID,
                            photo=photo_file,
                            caption=card_caption,
                            reply_markup=kb
                        )
                        await mark_post_as_seen(source, post_id, file_md5)
                        PARSER_STATS["sent_to_mod"] += 1
                        logger.info(f"📩 [SENT TO MOD] {source} #{post_id} успешно доставлен в группу модерации")

                        user_media_tracking[f"mod_{msg.message_id}"] = {
                            "file_url": file_url,
                            "caption": custom_signature,
                            "source": source,
                            "post_id": post_id
                        }
        except Exception as e:
            PARSER_STATS["network_errors"] += 1
            logger.error(f"❌ Ошибка отправки фото {file_url} в группу: {e}")

async def parser_loop():
    logger.info("🚀 [PARSER ENGINE] Фоновый парсер запущен!")
    while True:
        try:
            if PARSER_ENABLED and LABELS:
                for label in LABELS:
                    sources = label.get("sources", ["rule34", "gelbooru"])
                    tags = label.get("tags", "")
                    if not tags: continue

                    for src in sources:
                        posts = await fetch_booru_posts(src, tags, limit=PARSER_LIMIT)
                        for post in posts:
                            await process_parsed_post(label, post, src)
                            await asyncio.sleep(0.2)

            await asyncio.sleep(max(1, PARSER_SPEED))
        except (asyncio.CancelledError, Exception) as e:
            logger.error(f"❌ Ошибка в parser_loop: {e}")
            await asyncio.sleep(10)

# --- ФОНОВЫЙ ЦИКЛ АВТОПОСТИНГА В КАНАЛ ---

async def posting_loop():
    global last_post_time, is_posting_locked
    logger.info("🚀 [POSTING LOOP] Автопостинг в канал запущен!")
    while True:
        try:
            await asyncio.sleep(5)
            if not posting_enabled or is_posting_locked:
                continue
            
            queue = await load_queue()
            if not queue:
                continue
            
            now = get_current_time()
            
            if POST_INTERVAL and (time.time() - last_post_time < POST_INTERVAL):
                continue

            if TIME_WINDOW_ENABLED and START_TIME and END_TIME:
                current_t = now.time()
                if START_TIME <= END_TIME:
                    in_window = START_TIME <= current_t <= END_TIME
                else:
                    in_window = current_t >= START_TIME or current_t <= END_TIME
                if not in_window:
                    continue

            if WEEKDAYS_ENABLED and ALLOWED_WEEKDAYS and (now.weekday() not in ALLOWED_WEEKDAYS):
                continue
                    
            if DELAYED_START_ENABLED and DELAYED_START_TIME and (now < DELAYED_START_TIME):
                continue

            if not CHANNEL_ID:
                continue

            item = queue.pop(0)
            await save_queue(queue)
            
            is_posting_locked = True
            file_id = item.get("file_id")
            caption = item.get("caption", "")
            item_type = item.get("type", "photo")
            
            try:
                if file_id.startswith("http"):
                    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
                    async with aiohttp.ClientSession() as session:
                        async with session.get(file_id, headers=headers, proxy=PROXY_URL, timeout=20) as resp:
                            if resp.status == 200:
                                data = await resp.read()
                                p_file = BufferedInputFile(data, filename="channel_post.jpg")
                                await bot.send_photo(chat_id=CHANNEL_ID, photo=p_file, caption=caption)
                else:
                    if item_type == "photo":
                        await bot.send_photo(chat_id=CHANNEL_ID, photo=file_id, caption=caption)
                    elif item_type == "video":
                        await bot.send_video(chat_id=CHANNEL_ID, video=file_id, caption=caption)
                    elif item_type == "animation":
                        await bot.send_animation(chat_id=CHANNEL_ID, animation=file_id, caption=caption)
                    
                last_post_time = time.time()
                save_state()
                logger.info(f"✅ [CHANNEL POST] Успешно опубликован пост в {CHANNEL_ID}")
            except Exception as post_err:
                logger.error(f"❌ Ошибка публикации поста в канал: {post_err}")
            finally:
                is_posting_locked = False

        except (asyncio.CancelledError, Exception) as e:
            logger.error(f"❌ Ошибка в posting_loop: {e}")
            is_posting_locked = False
            await asyncio.sleep(5)

# --- ИНТЕРАКТИВНЫЙ МАСТЕР СОЗДАНИЯ И РЕДАКТИРОВАНИЯ ЛЕЙБЛОВ ---

@dp.message(F.text == "/addlabel")
async def start_add_label_wizard(message: Message):
    if not check_user_access(message.from_user.id): return
    uid = message.from_user.id
    add_label_wizard[uid] = {"step": "name", "data": {}}
    
    cancel_kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="wizard:cancel")]])
    await message.reply(
        "🛠 <b>Мастер создания лейбла (Шаг 1/5)</b>\n\n"
        "Введите <b>название</b> лейбла (например: <code>Dross Art</code>):",
        reply_markup=cancel_kb
    )

@dp.callback_query(F.data.startswith("wizard:"))
async def handle_wizard_callback(callback: CallbackQuery):
    uid = callback.from_user.id
    action = callback.data.split(":")[1]

    if action == "cancel":
        if uid in add_label_wizard: del add_label_wizard[uid]
        await callback.message.edit_text("❌ Действие отменено.")
        await callback.answer()
        return

    if uid not in add_label_wizard:
        await callback.answer("Сессия мастера истекла.", show_alert=True)
        return

    wdata = add_label_wizard[uid]["data"]

    if action.startswith("src_"):
        src_type = action.replace("src_", "")
        if src_type == "all": wdata["sources"] = ["rule34", "gelbooru"]
        else: wdata["sources"] = [src_type]

        add_label_wizard[uid]["step"] = "emoji"
        await callback.message.edit_text(
            "🛠 <b>Мастер создания лейбла (Шаг 4/5)</b>\n\n"
            "Пришлете <b>эмодзи</b> для маркировки (например, 🍆 или ID Custom Premium Emoji):"
        )
        await callback.answer()

    elif action.startswith("mode_"):
        mode = action.replace("mode_", "").upper()
        wdata["mode"] = mode

        tags = wdata["tags"]
        sources = wdata["sources"]
        
        test_posts = await fetch_booru_posts(sources[0], tags, limit=5)
        count_found = len(test_posts)

        new_label = {
            "name": wdata["name"],
            "tags": tags,
            "sources": sources,
            "emoji": wdata.get("emoji", "🏷"),
            "mode": mode,
            "signature": wdata.get("signature")
        }

        LABELS[:] = [lbl for lbl in LABELS if lbl["name"].lower() != wdata["name"].lower()]
        LABELS.append(new_label)
        save_state()
        del add_label_wizard[uid]

        ai_status_str = " (Фильтр ИИ: ВКЛ)" if not ALLOW_AI_POSTS else " (ИИ: ВКЛ)"
        status_test = f"✅ Найдено постов на {sources[0]}: <b>{count_found}</b>{ai_status_str}" if count_found > 0 else "⚠️ Найдено 0 постов. Проверьте теги через /checkpost."

        await callback.message.edit_text(
            f"🎉 <b>Лейбл «{new_label['name']}» успешно сохранен и запущен!</b>\n\n"
            f"• Теги: <code>{tags}</code>\n"
            f"• Режим: <b>{mode}</b>\n"
            f"• Источники: {', '.join(sources)}\n\n"
            f"{status_test}"
        )
        await callback.answer("Лейбл сохранен!")

@dp.callback_query(F.data.startswith("lblmanage:"))
async def handle_label_management(callback: CallbackQuery):
    if not check_user_access(callback.from_user.id): return
    parts = callback.data.split(":")
    action = parts[1]
    lbl_index = int(parts[2])

    if 0 <= lbl_index < len(LABELS):
        lbl = LABELS[lbl_index]
        if action == "del":
            deleted_name = lbl["name"]
            LABELS.pop(lbl_index)
            save_state()
            await callback.message.edit_text(f"🗑 Лейбл <b>{deleted_name}</b> удален!")
            await callback.answer("Удалено!")

        elif action == "togglemode":
            lbl["mode"] = "AUTO" if lbl.get("mode") == "MANUAL" else "MANUAL"
            save_state()
            await callback.answer(f"Режим изменен на {lbl['mode']}")
            await render_labels_menu(callback.message, edit=True)

        elif action == "rename":
            uid = callback.from_user.id
            add_label_wizard[uid] = {"step": "edit_name", "index": lbl_index}
            await callback.message.reply(f"✏️ Введите новое **название** для лейбла «<b>{lbl['name']}</b>»:")
            await callback.answer()

async def render_labels_menu(message: Message, edit=False):
    if not LABELS:
        txt = "📭 Активных лейблов пока нет. Нажмите /addlabel для создания первого!"
        if edit: await message.edit_text(txt)
        else: await message.reply(txt)
        return

    for i, lbl in enumerate(LABELS):
        em = lbl.get("emoji", "🏷")
        sources_str = ", ".join(lbl.get("sources", []))
        mode_str = "👁 MANUAL (В группу модерации)" if lbl.get("mode") == "MANUAL" else "⚡ AUTO (Сразу в канал)"
        
        card = (
            f"🏷 <b>Лейбл №{i+1}: {em} {lbl['name']}</b>\n"
            f"• Теги: <code>{lbl['tags']}</code>\n"
            f"• Источники: {sources_str}\n"
            f"• Режим: {mode_str}\n"
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="🔄 Сменить режим", callback_data=f"lblmanage:togglemode:{i}"),
                InlineKeyboardButton(text="✏️ Переименовать", callback_data=f"lblmanage:rename:{i}")
            ],
            [
                InlineKeyboardButton(text="🗑 Удалить лейбл", callback_data=f"lblmanage:del:{i}")
            ]
        ])
        if edit and i == 0: await message.edit_text(card, reply_markup=kb)
        else: await message.reply(card, reply_markup=kb)

# --- ОБРАБОТЧИКИ СООБЩЕНИЙ С ИНТЕРФЕЙСОМ ---

@dp.message(F.text)
async def handle_text_messages(message: Message):
    if not check_user_access(message.from_user.id): return
    
    global MODERATION_CHAT_ID, PARSER_ENABLED, PARSER_SPEED, PARSER_LIMIT, ALLOW_AI_POSTS
    global posting_enabled, CHANNEL_ID, DEFAULT_SIGNATURE, POST_INTERVAL
    
    uid = message.from_user.id
    text = message.text.strip()
    is_group = message.chat.type in ["group", "supergroup"]

    # --- ЛОГИКА ПОШАГОВОГО МАСТЕРА И РЕДАКТИРОВАНИЯ ---
    if uid in add_label_wizard:
        step = add_label_wizard[uid]["step"]

        if step == "edit_name":
            lbl_idx = add_label_wizard[uid]["index"]
            if 0 <= lbl_idx < len(LABELS):
                old_name = LABELS[lbl_idx]["name"]
                LABELS[lbl_idx]["name"] = text
                save_state()
                await message.reply(f"✅ Лейбл «{old_name}» переименован в «<b>{text}</b>»!")
            del add_label_wizard[uid]
            return

        wdata = add_label_wizard[uid]["data"]

        if step == "name":
            wdata["name"] = text
            add_label_wizard[uid]["step"] = "tags"
            await message.reply(
                "🛠 <b>Мастер создания лейбла (Шаг 2/5)</b>\n\n"
                "Введите <b>теги</b> поиска через пробел.\n"
                "<i>Пробелы внутри составных имен (например cat girl) бот сам автоматически заменит на cat_girl!</i>\n\n"
                "Пример: <code>dross male_only -guro</code>"
            )
            return

        elif step == "tags":
            wdata["tags"] = normalize_tags(text)
            add_label_wizard[uid]["step"] = "sources"

            kb = InlineKeyboardMarkup(inline_keyboard=[
                [
                    InlineKeyboardButton(text="🟢 Rule34", callback_data="wizard:src_rule34"),
                    InlineKeyboardButton(text="🟡 Gelbooru", callback_data="wizard:src_gelbooru")
                ],
                [
                    InlineKeyboardButton(text="🌐 Оба сайта", callback_data="wizard:src_all")
                ]
            ])
            await message.reply("🛠 <b>Мастер создания лейбла (Шаг 3/5)</b>\n\nВыберите <b>источник</b> сбора:", reply_markup=kb)
            return

        elif step == "emoji":
            wdata["emoji"] = text
            add_label_wizard[uid]["step"] = "mode"

            kb = InlineKeyboardMarkup(inline_keyboard=[
                [
                    InlineKeyboardButton(text="👁 MANUAL (В группу)", callback_data="wizard:mode_manual"),
                    InlineKeyboardButton(text="⚡ AUTO (Сразу в канал)", callback_data="wizard:mode_auto")
                ]
            ])
            await message.reply("🛠 <b>Мастер создания лейбла (Шаг 5/5)</b>\n\nВыберите <b>режим обработки</b> постов:", reply_markup=kb)
            return

    # --- КОМАНДА ОТПРАВКИ 1-го ПОСТА ПО ТЕГУ ---
    if text.startswith("/checkpost") or text.startswith("/testpost"):
        parts = text.split(maxsplit=1)
        search_tag = parts[1].strip() if len(parts) > 1 else "femboy"
        
        target_chat_id = MODERATION_CHAT_ID if MODERATION_CHAT_ID else message.chat.id
        
        await message.reply(f"🔍 Запрашиваю посты по тегу <code>{search_tag}</code>...")
        
        posts = await fetch_booru_posts("rule34", search_tag, limit=10)
        src_name = "Rule34"
        if not posts:
            posts = await fetch_booru_posts("gelbooru", search_tag, limit=10)
            src_name = "Gelbooru"

        if not posts:
            await message.reply(f"❌ Вернулось 0 постов. Если IP заблокирован Cloudflare — используйте Gelbooru или добавьте PROXY_URL в .env.")
            return

        first_post = posts[0]
        post_id = first_post.get("id")
        file_url = first_post.get("file_url")
        tags_attr = str(first_post.get("tags", ""))

        source_link = f'<a href="https://rule34.xxx/index.php?page=post&s=view&id={post_id}">🟢 {src_name} (ID {post_id})</a>'
        caption = (
            f"🧪 <b>Проверочный пост (1-й в списке)</b>\n\n"
            f"🏷 <b>Запрошенный тег:</b> <code>{search_tag}</code>\n"
            f"🔗 <b>Источник:</b> {source_link}\n"
            f"🆔 <b>ID:</b> <code>{post_id}</code>\n"
            f"📝 <b>Первые теги:</b> <code>{' '.join(tags_attr.split()[:8])}...</code>"
        )

        if not file_url:
            await message.reply(f"⚠️ У первого поста нет file_url!\n\n{caption}")
            return

        try:
            headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
            async with aiohttp.ClientSession() as session:
                async with session.get(file_url, headers=headers, proxy=PROXY_URL, timeout=15) as resp:
                    if resp.status == 200:
                        image_bytes = await resp.read()
                        photo_file = BufferedInputFile(image_bytes, filename=f"check_{post_id}.jpg")

                        await bot.send_photo(
                            chat_id=target_chat_id,
                            photo=photo_file,
                            caption=caption
                        )
                        if target_chat_id != message.chat.id:
                            await message.reply("✅ 1-й пост найден и успешно отправлен в группу модерации!")
                    else:
                        await message.reply(f"❌ Не удалось скачать фото {file_url} (HTTP {resp.status})")
        except Exception as e:
            await message.reply(f"❌ Ошибка отправки фото: {e}")
        return

    # --- КОМАНДА ВКЛ/ВЫКЛ НЕЙРО-ПОСТОВ ---
    if text == "/ai":
        ALLOW_AI_POSTS = not ALLOW_AI_POSTS
        save_state()
        st = "РАЗРЕШЕНЫ (фильтр выключен)" if ALLOW_AI_POSTS else "ЗАПРЕЩЕНЫ (фильтр 'ai_generated' включен)"
        await message.reply(f"🤖 <b>Фильтр Нейро/ИИ постов:</b> {st}")
        return

    # --- КОМАНДЫ ГРУППЫ МОДЕРАЦИИ ---
    if is_group:
        if text == "/setmodgroup":
            MODERATION_CHAT_ID = message.chat.id
            save_state()
            await message.reply(f"✅ Группа успешно привязана для перепоста и модерации!\nID: <code>{MODERATION_CHAT_ID}</code>")
            return

        elif text == "/labels":
            await render_labels_menu(message)
            return

        elif text in ["/parser", "/parserinfo"]:
            status_parser = "✅ Включен" if PARSER_ENABLED else "❌ Выключен"
            mod_chat_str = MODERATION_CHAT_ID if MODERATION_CHAT_ID else "❓ Не привязана"
            ai_str = "❌ Заблокированы" if not ALLOW_AI_POSTS else "✅ Разрешены"
            
            uptime_min = int((time.time() - PARSER_STATS["start_time"]) // 60)

            await message.reply(
                f"🕵️‍♂️ <b>Статистика и статус Агрегатора:</b>\n\n"
                f"⚙️ Состояние: <b>{status_parser}</b>\n"
                f"🤖 Нейропосты (ИИ): <b>{ai_str}</b> (/ai)\n"
                f"⏱ Скорость проверки: <b>раз в {PARSER_SPEED} сек</b>\n"
                f"📊 Глубина за прогон: <b>{PARSER_LIMIT} постов</b>\n"
                f"💬 Чат модерации: <code>{mod_chat_str}</code>\n"
                f"🏷 Активных лейблов: <b>{len(LABELS)} шт</b>\n"
                f"⏳ Время работы: <b>{uptime_min} мин</b>\n\n"
                f"<b>📊 Статистика обработки:</b>\n"
                f"🔍 Всего сканировано постов: <b>{PARSER_STATS['total_scanned']}</b>\n"
                f"🚫 Отбраковано дубликатов: <b>{PARSER_STATS['duplicates_filtered']}</b>\n"
                f"📩 Отправлено в группу модерации: <b>{PARSER_STATS['sent_to_mod']}</b>\n"
                f"⚡ В авто-очередь: <b>{PARSER_STATS['auto_queued']}</b>\n"
                f"⚠️ Ошибок сети: <b>{PARSER_STATS['network_errors']}</b>"
            )
            return

        elif text == "/clearseen":
            if db_pool:
                async with db_pool.acquire() as conn:
                    await conn.execute("TRUNCATE TABLE seen_posts;")
                await message.reply("🧹 База просмотренных постов очищена!")
            return

        elif text == "/help":
            await message.reply(
                "🛠 <b>Команды в группе модерации:</b>\n\n"
                "• /setmodgroup - привязать группу для перепоста\n"
                "• /labels - меню лейблов, редактирование и удаление\n"
                "• /addlabel - пошаговый мастер создания лейбла\n"
                "• /checkpost [тег] - прислать 1-й пост по тегу\n"
                "• /parser - детальная статистика сбора\n"
                "• /ai - вкл/выкл Нейро/ИИ арты\n"
                "• /clearseen - сброс базы дубликатов"
            )
            return

    # --- ЛИЧНЫЕ СООБЩЕНИЯ (ПОЛНОЕ УПРАВЛЕНИЕ БОТОМ) ---
    if text == "/start":
        await message.reply("👋 <b>Бот-комбайн запущен!</b>\n\nИспользуйте /help для просмотра всех доступных команд.")

    elif text == "/labels":
        await render_labels_menu(message)

    elif text in ["/parser", "/parserinfo"]:
        status_parser = "✅ Включен" if PARSER_ENABLED else "❌ Выключен"
        ai_str = "❌ Заблокированы" if not ALLOW_AI_POSTS else "✅ Разрешены"
        await message.reply(
            f"🕵️‍♂️ <b>Статистика парсера:</b>\n\n"
            f"🤖 Нейро-арты (ИИ): <b>{ai_str}</b> (/ai)\n"
            f"🔍 Сканировано постов: <b>{PARSER_STATS['total_scanned']}</b>\n"
            f"🚫 Заблокировано повторов: <b>{PARSER_STATS['duplicates_filtered']}</b>\n"
            f"📩 Прилетело в группу: <b>{PARSER_STATS['sent_to_mod']}</b>\n"
            f"⚡ В авто-очередь: <b>{PARSER_STATS['auto_queued']}</b>\n"
            f"⏱ Интервал: <b>{PARSER_SPEED} сек</b> | Лимит: <b>{PARSER_LIMIT}</b>"
        )

    elif text == "/clearseen":
        if db_pool:
            async with db_pool.acquire() as conn:
                await conn.execute("TRUNCATE TABLE seen_posts;")
            await message.reply("🧹 База просмотренных постов очищена!")

    elif text == "/status":
        queue = await load_queue()
        queue_stats = format_queue_stats(queue)
        await message.reply(
            f"🤖 <b>Статус бота:</b>\n\n"
            f"🌍 Часовой пояс: {CURRENT_TIMEZONE_STR}\n"
            f"📊 В очереди: {queue_stats}\n"
            f"🕵️‍♂️ Агрегатор: {'✅ Включен' if PARSER_ENABLED else '❌ Выключен'} ({len(LABELS)} лейблов)\n"
            f"💬 Чат модерации: {MODERATION_CHAT_ID or 'Не привязан'}"
        )

    elif text == "/help":
        await message.reply(
            "🤖 <b>Справка по управлению ботом в личке:</b>\n\n"
            "<b>🔍 Агрегатор:</b>\n"
            "• /labels - список и управление лейблами\n"
            "• /addlabel - пошаговый мастер добавления тегов\n"
            "• /checkpost [тег] - прислать 1-й пост по тегу\n"
            "• /parser - подробная статистика парсинга\n"
            "• /ai - включить/выключить ИИ арты\n"
            "• /parserspeed [сек] - установить интервал проверки\n"
            "• /parserlimit [колво] - глубина парсинга\n"
            "• /clearseen - очистить историю просмотренных постов\n\n"
            "<b>⏱ Автопостинг:</b>\n"
            "• /status - состояние автопостинга и очереди\n"
            "• /timezone - смена часового пояса\n"
            "• /interval 2h30m - установить интервал\n"
            "• /settime 06:00 20:00 - временное окно"
        )

    elif text.startswith("/parserspeed"):
        parts = text.split(maxsplit=1)
        if len(parts) > 1 and parts[1].isdigit():
            PARSER_SPEED = max(1, int(parts[1]))
            save_state()
            await message.reply(f"✅ Скорость сбора установлена: <b>раз в {PARSER_SPEED} сек</b>")
        else:
            await message.reply("Пример: <code>/parserspeed 10</code>")

    elif text.startswith("/parserlimit"):
        parts = text.split(maxsplit=1)
        if len(parts) > 1 and parts[1].isdigit():
            PARSER_LIMIT = max(1, min(100, int(parts[1])))
            save_state()
            await message.reply(f"✅ Лимит глубины сбора установлен: <b>{PARSER_LIMIT} постов</b>")
        else:
            await message.reply("Пример: <code>/parserlimit 30</code>")

# --- ВЕБ-СЕРВЕР И СТАРТ ---

async def create_app():
    app = web.Application()
    async def health_check(request): return web.Response(text="Bot is running!")
    app.router.add_get("/", health_check)
    return app

async def start_web_server():
    app = await create_app()
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get('PORT', 5000))
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()

async def main():
    await init_db()
    await load_state_from_db()

    await bot.set_my_commands([
        BotCommand(command="start", description="🚀 Главное меню"),
        BotCommand(command="status", description="📊 Статус и очередь"),
        BotCommand(command="labels", description="🏷 Управление лейблами"),
        BotCommand(command="addlabel", description="➕ Создать лейбл"),
        BotCommand(command="checkpost", description="🔍 Прислать 1-й пост по тегу"),
        BotCommand(command="parser", description="📊 Статистика парсинга"),
        BotCommand(command="ai", description="🤖 Вкл/Выкл Нейропосты"),
        BotCommand(command="timezone", description="🌍 Настройка времени"),
        BotCommand(command="interval", description="⏱ Интервал публикаций"),
        BotCommand(command="help", description="❓ Помощь"),
    ], scope=BotCommandScopeAllPrivateChats())

    await bot.set_my_commands([
        BotCommand(command="setmodgroup", description="📌 Привязать эту группу для перепоста"),
        BotCommand(command="labels", description="🏷 Список лейблов и удаление"),
        BotCommand(command="addlabel", description="➕ Добавить лейбл сбора"),
        BotCommand(command="checkpost", description="🔍 Прислать 1-й пост по тегу"),
        BotCommand(command="parser", description="📊 Подробная статистика парсера"),
        BotCommand(command="ai", description="🤖 Вкл/Выкл Нейропосты"),
        BotCommand(command="clearseen", description="🧹 Сбросить базу дубликатов"),
        BotCommand(command="help", description="❓ Помощь по группе"),
    ], scope=BotCommandScopeAllGroupChats())

    await start_web_server()
    
    asyncio.create_task(posting_loop())
    asyncio.create_task(parser_loop())

    logger.info("🤖 Бот успешно запущен!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("🛑 Остановлен пользователем")
    except Exception as e:
        logger.error(f"❌ Критическая ошибка: {e}")
    finally:
        save_state()

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
from aiogram.types import Message, BotCommand, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery, BotCommandScopeAllPrivateChats, BotCommandScopeAllGroupChats
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

if not TOKEN:
    logger.error("❌ BOT_TOKEN не установлен!")
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

# Настройки Агрегатора
MODERATION_CHAT_ID = None      # ID группы для перепоста/модерации
PARSER_ENABLED = True           # Включен ли парсинг
PARSER_SPEED = 15               # Интервал проверки сайтов в секундах
PARSER_LIMIT = 20               # Количество постов за один прогон
LABELS = []                     # Список кастомных лейблов

QUEUE_FILE = "queue.json"

# Состояния мастер-диалогов
add_label_wizard = {}  # {user_id: {"step": ..., "data": {...}}}
waiting_for_time_input = {}
pending_media_groups = {}
media_group_timers = {}
pending_notifications = {}
user_media_tracking = {}
is_posting_locked = False
db_pool = None

bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

# --- POSTGRESQL ---

async def init_db():
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
    global MODERATION_CHAT_ID, PARSER_ENABLED, PARSER_SPEED, PARSER_LIMIT, LABELS

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

# --- ВСПОМОГАТЕЛЬНАЯ ОБРАБОТКА ТЕГОВ И ПАРСИНГ ---

def normalize_tags(raw_tags):
    parts = raw_tags.split()
    normalized = []
    for part in parts:
        if part.startswith("-"):
            normalized.append("-" + part[1:].replace(" ", "_"))
        else:
            normalized.append(part.replace(" ", "_"))
    return " ".join(normalized)

async def fetch_booru_posts(source, tags, limit=20):
    posts = []
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    clean_tags = normalize_tags(tags)

    if source == "rule34":
        url = f"https://api.rule34.xxx/index.php?page=dapi&s=post&q=index&json=1&tags={clean_tags}&limit={limit}"
    elif source == "gelbooru":
        url = f"https://gelbooru.com/index.php?page=dapi&s=post&q=index&json=1&tags={clean_tags}&limit={limit}"
    else:
        return posts

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, timeout=10) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if isinstance(data, list): posts = data
                    elif isinstance(data, dict) and "post" in data: posts = data["post"]
    except Exception as e:
        logger.error(f"Ошибка запроса к {source} API ({clean_tags}): {e}")

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
    if await is_post_seen(source, post_id): return

    duplicate_info = await is_md5_seen(file_md5)
    is_suspicious = duplicate_info is not None

    await mark_post_as_seen(source, post_id, file_md5)

    if source == "gelbooru":
        source_link = f'<a href="https://gelbooru.com/index.php?page=post&s=view&id={post_id}">🟡 Gelbooru</a>'
    else:
        source_link = f'<a href="https://rule34.xxx/index.php?page=post&s=view&id={post_id}">🟢 Rule34</a>'

    label_header = format_emoji_label(label.get("emoji"), label.get("name"))
    
    card_caption = f"{label_header}\n"
    if is_suspicious:
        card_caption = f"⚠️ <b>СОМНИТЕЛЬНО (Возможен дубликат)</b>\n" + card_caption
        card_caption += f"💡 <i>Ранее зафиксирован в БД из источников {duplicate_info['source']}</i>\n"

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
        return

    if MODERATION_CHAT_ID:
        try:
            mod_item = {"file_url": file_url, "caption": custom_signature, "source": source, "post_id": post_id}
            msg = await bot.send_photo(
                chat_id=MODERATION_CHAT_ID,
                photo=file_url,
                caption=card_caption,
                reply_markup=kb
            )
            user_media_tracking[f"mod_{msg.message_id}"] = mod_item
        except Exception as e:
            logger.error(f"❌ Ошибка отправки в группу модерации: {e}")

async def parser_loop():
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

# --- ИНТЕРАКТИВНЫЙ МАСТЕР СОЗДАНИЯ ЛЕЙБЛОВ ---

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
        await callback.message.edit_text("❌ Создание лейбла отменено.")
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

        status_test = f"✅ Тестовый поиск успешен! Найдено постов в {sources[0]}: <b>{count_found}</b>" if count_found > 0 else "⚠️ Тестовый поиск выдал 0 постов (проверьте теги в настройках, если результат не тот)."

        await callback.message.edit_text(
            f"🎉 <b>Лейбл «{new_label['name']}» успешно создан и запущен!</b>\n\n"
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
                InlineKeyboardButton(text="🗑 Удалить", callback_data=f"lblmanage:del:{i}")
            ]
        ])
        if edit and i == 0: await message.edit_text(card, reply_markup=kb)
        else: await message.reply(card, reply_markup=kb)

# --- ОБРАБОТЧИКИ СООБЩЕНИЙ С ИНТЕРФЕЙСОМ ---

@dp.message(F.text)
async def handle_text_messages(message: Message):
    if not check_user_access(message.from_user.id): return
    
    # Объявление всех глобальных переменных в самом начале функции
    global MODERATION_CHAT_ID, PARSER_ENABLED, PARSER_SPEED, PARSER_LIMIT
    global posting_enabled, CHANNEL_ID, DEFAULT_SIGNATURE, POST_INTERVAL
    
    uid = message.from_user.id
    text = message.text.strip()
    is_group = message.chat.type in ["group", "supergroup"]

    # --- ЛОГИКА ПОШАГОВОГО МАСТЕРА ---
    if uid in add_label_wizard:
        step = add_label_wizard[uid]["step"]
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

    # --- КОМАНДЫ, ДОСТУПНЫЕ В ЗАКРЫТОЙ ГРУППЕ ---
    if is_group:
        if text == "/setmodgroup":
            MODERATION_CHAT_ID = message.chat.id
            save_state()
            await message.reply(f"✅ Группа успешно привязана для перепоста и модерации!\nID: <code>{MODERATION_CHAT_ID}</code>")
            return

        elif text == "/labels":
            await render_labels_menu(message)
            return

        elif text == "/parser":
            status_parser = "✅ Включен" if PARSER_ENABLED else "❌ Выключен"
            mod_chat_str = MODERATION_CHAT_ID if MODERATION_CHAT_ID else "❓ Не привязана"
            await message.reply(
                f"🕵️‍♂️ <b>Статус Агрегатора:</b>\n\n"
                f"Статус: <b>{status_parser}</b>\n"
                f"⏱ Скорость сбора: <b>раз в {PARSER_SPEED} сек</b>\n"
                f"📊 Глубина за раз: <b>{PARSER_LIMIT} постов</b>\n"
                f"💬 Чат модерации: <code>{mod_chat_str}</code>\n"
                f"🏷 Активных лейблов: <b>{len(LABELS)}</b>"
            )
            return

        elif text == "/help":
            await message.reply(
                "🛠 <b>Команды в группе модерации:</b>\n\n"
                "• /setmodgroup - привязать группу для перепоста\n"
                "• /labels - интерактивное меню и удаление лейблов\n"
                "• /addlabel - пошаговый мастер создания нового лейбла\n"
                "• /parser - статус и параметры сбора"
            )
            return

    # --- ЛИЧНЫЕ СООБЩЕНИЯ (ПОЛНОЕ УПРАВЛЕНИЕ БОТОМ) ---
    if text == "/start":
        await message.reply("👋 <b>Бот-комбайн запущен!</b>\n\nИспользуйте /help для просмотра всех доступных команд.")

    elif text == "/labels":
        await render_labels_menu(message)

    elif text == "/help":
        await message.reply(
            "🤖 <b>Справка по управлению ботом в личке:</b>\n\n"
            "<b>🔍 Агрегатор:</b>\n"
            "• /labels - список и управление лейблами\n"
            "• /addlabel - пошаговый мастер добавления тегов\n"
            "• /parserspeed [сек] - установить интервал проверки\n"
            "• /parserlimit [колво] - глубина парсинга\n\n"
            "<b>⏱ Автопостинг:</b>\n"
            "• /status - состояние автопостинга и очереди\n"
            "• /timezone - смена часового пояса\n"
            "• /interval 2h30m - установить интервал\n"
            "• /settime 06:00 20:00 - временное окно"
        )

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
        BotCommand(command="timezone", description="🌍 Настройка времени"),
        BotCommand(command="interval", description="⏱ Интервал публикаций"),
        BotCommand(command="help", description="❓ Помощь"),
    ], scope=BotCommandScopeAllPrivateChats())

    await bot.set_my_commands([
        BotCommand(command="setmodgroup", description="📌 Привязать эту группу для перепоста"),
        BotCommand(command="labels", description="🏷 Список лейблов и удаление"),
        BotCommand(command="addlabel", description="➕ Добавить лейбл сбора"),
        BotCommand(command="parser", description="🕵️‍♂️ Статус парсера"),
        BotCommand(command="help", description="❓ Помощь по группе"),
    ], scope=BotCommandScopeAllGroupChats())

    await start_web_server()
    
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

import os
import time
import random
import asyncio
import psycopg2
from psycopg2.extras import RealDictCursor
from psycopg2.pool import SimpleConnectionPool
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    ChatMemberHandler,
)
from telegram.error import Forbidden, TimedOut, NetworkError, RetryAfter

# ================= CONFIG =================
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
DATABASE_URL = os.getenv("DATABASE_URL")
LINK_EXPIRE = 15
LINK_COOLDOWN = 1800
LINK_GRACE = 10
LINK_LOCK_SECONDS = 3
WELCOME_IMAGE = "https://image2url.com/r2/default/images/1768635379388-0769fe79-f5b5-4926-97dc-a20e7be08fe0.jpg"

if not BOT_TOKEN or not DATABASE_URL:
    raise RuntimeError("❌ BOT_TOKEN или DATABASE_URL не заданы")
if ADMIN_ID == 0:
    print("⚠️ ADMIN_ID не задан")

DB_POOL = None

# ================= DATABASE =================
def get_db():
    return DB_POOL.getconn()

def release_db(conn):
    DB_POOL.putconn(conn)

def init_db():
    db = get_db()
    try:
        with db.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS settings ( key TEXT PRIMARY KEY, value TEXT );
                CREATE TABLE IF NOT EXISTS bots ( username TEXT PRIMARY KEY );
                CREATE TABLE IF NOT EXISTS sites ( url TEXT PRIMARY KEY );
                CREATE TABLE IF NOT EXISTS active_links ( user_id TEXT PRIMARY KEY, invite_link TEXT, expire INTEGER );
                CREATE TABLE IF NOT EXISTS last_requests ( user_id TEXT PRIMARY KEY, timestamp INTEGER );
                CREATE TABLE IF NOT EXISTS users ( user_id TEXT PRIMARY KEY, username TEXT, first_name TEXT, last_name TEXT, first_used TIMESTAMP );
                CREATE TABLE IF NOT EXISTS link_locks ( user_id TEXT PRIMARY KEY, timestamp INTEGER );
                CREATE TABLE IF NOT EXISTS channels ( name TEXT PRIMARY KEY, url TEXT );
            """)
            db.commit()
    finally:
        release_db(db)

def get_setting(key):
    db = get_db()
    try:
        with db.cursor() as cur:
            cur.execute("SELECT value FROM settings WHERE key=%s", (key,))
            row = cur.fetchone()
            return row["value"] if row else None
    finally:
        release_db(db)

def set_setting(key, value):
    db = get_db()
    try:
        with db.cursor() as cur:
            cur.execute(
                "INSERT INTO settings (key,value) VALUES (%s,%s) "
                "ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value",
                (key, str(value))
            )
            db.commit()
    finally:
        release_db(db)

# ================= UTILS =================
def is_admin(user_id: int) -> bool:
    return user_id == ADMIN_ID

def log_user(user):
    user_id = str(user.id)
    username = user.username or "—"
    first_name = user.first_name or "—"
    last_name = user.last_name or "—"
    now = datetime.utcnow()
    db = get_db()
    try:
        with db.cursor() as cur:
            cur.execute("SELECT 1 FROM users WHERE user_id=%s", (user_id,))
            if cur.fetchone():
                return
            cur.execute("""
                INSERT INTO users (user_id, username, first_name, last_name, first_used)
                VALUES (%s,%s,%s,%s,%s)
            """, (user_id, username, first_name, last_name, now))
            db.commit()
    finally:
        release_db(db)

async def safe_send(func, *args, **kwargs):
    for _ in range(3):
        try:
            await asyncio.sleep(random.uniform(0.3, 1.2))
            return await func(*args, **kwargs)
        except (TimedOut, NetworkError, RetryAfter):
            await asyncio.sleep(2)
        except Forbidden:
            return None
    return None

def user_commands_hint():
    return (
        "\n\n📌 Ваши команды:\n"
        "• /link — получить персональную ссылку 🔑\n"
        "• /bots — список ботов 🤖\n"
        "• /sites — список актуальных сайтов 🌐"
    )

# ================= LISTS =================
async def get_bots_list():
    db = get_db()
    try:
        with db.cursor() as cur:
            cur.execute("SELECT username FROM bots")
            rows = cur.fetchall()
            return "\n".join(f"🟢 онлайн — {r['username']}" for r in rows) if rows else "—"
    finally:
        release_db(db)

async def get_sites_list():
    db = get_db()
    try:
        with db.cursor() as cur:
            cur.execute("SELECT url FROM sites")
            rows = cur.fetchall()
            return "\n".join(f"🔗 {r['url']}" for r in rows) if rows else "—"
    finally:
        release_db(db)

async def get_channel(name):
    db = get_db()
    try:
        with db.cursor() as cur:
            cur.execute("SELECT url FROM channels WHERE name=%s", (name,))
            row = cur.fetchone()
            return row["url"] if row else "—"
    finally:
        release_db(db)

# ================= COMMANDS =================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    log_user(user)
    bots_list = await get_bots_list()
    sites_list = await get_sites_list()
    price_url = await get_channel("price")
    contact_url = await get_channel("contact")
    work_url = await get_channel("work")

    if update.effective_chat.type == "private":
        caption = (
            f"👋 Привет, {user.first_name or 'друг'}!\n\n"
            f"🤖 Актуальные боты:\n{bots_list}\n\n"
            f"🌐 Актуальные сайты:\n{sites_list}\n\n"
            "📌 Каналы:\n"
            f"💰 Прайс-канал: {price_url}\n"
            f"📞 Контакт-канал: {contact_url}\n"
            f"💼 Работа-канал: {work_url}\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "🚪 **ДОСТУП В ПРИВАТНЫЙ ЧАТ**\n\n"
            "🔑 Получи персональную ссылку:\n"
            "1️⃣ Нажми команду /link\n"
            "2️⃣ Ссылка активна 15 секунд ⏳\n"
            "3️⃣ Повтор — через 30 минут ⏰\n"
            "━━━━━━━━━━━━━━━━━━━━━━"
        )
        caption += (
            "\n\n👑 Админ:\n"
            "• /setchat <id>\n"
            "• /addbot <bot>\n"
            "• /removebot <bot>\n"
            "• /addsite <url>\n"
            "• /removesite <url>\n"
            "• /addprice <url>\n"
            "• /addcontact <url>\n"
            "• /addwork <url>\n"
            "• /settings\n"
            "• /broadcast <текст>"
            if is_admin(user.id) else user_commands_hint()
        )
        if WELCOME_IMAGE:
            await safe_send(
                context.bot.send_photo,
                chat_id=update.effective_chat.id,
                photo=WELCOME_IMAGE,
                caption=caption
            )
        else:
            await safe_send(update.message.reply_text, caption)
    else:
        await safe_send(update.message.reply_text, caption)

# ======== /link =========
async def link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        return await safe_send(update.message.reply_text, "❌ Эта команда доступна только в ЛС бота.")
    user = update.effective_user
    user_id = str(user.id)
    log_user(user)
    now = int(time.time())
    db = get_db()
    try:
        with db.cursor() as cur:
            cur.execute("SELECT timestamp FROM link_locks WHERE user_id=%s", (user_id,))
            r = cur.fetchone()
            if r and now - r["timestamp"] < LINK_LOCK_SECONDS:
                return
            cur.execute("""
                INSERT INTO link_locks VALUES (%s,%s)
                ON CONFLICT (user_id) DO UPDATE SET timestamp=EXCLUDED.timestamp
            """, (user_id, now))
            db.commit()
    finally:
        release_db(db)

    chat_id = get_setting("private_chat_id")
    if not chat_id:
        return await safe_send(update.message.reply_text, "❌ Приватный чат не настроен.")

    try:
        invite = await context.bot.create_chat_invite_link(
            chat_id=int(chat_id),
            expire_date=now + LINK_EXPIRE,
            member_limit=1
        )
    except Forbidden:
        return await safe_send(update.message.reply_text, "❌ Бот не администратор чата.")

    db = get_db()
    try:
        with db.cursor() as cur:
            cur.execute("""
                INSERT INTO last_requests VALUES (%s,%s)
                ON CONFLICT (user_id) DO UPDATE SET timestamp=EXCLUDED.timestamp
            """, (user_id, now))
            cur.execute("""
                INSERT INTO active_links VALUES (%s,%s,%s)
                ON CONFLICT (user_id) DO UPDATE SET invite_link=EXCLUDED.invite_link, expire=EXCLUDED.expire
            """, (user_id, invite.invite_link, now + LINK_EXPIRE))
            db.commit()
    finally:
        release_db(db)

    await safe_send(
        update.message.reply_text,
        "✅ Ссылка готова! ⏳ 15 секунд.",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("🚪 Войти", url=invite.invite_link)]]
        )
    )

# ======== /bots =========
async def bots(update: Update, context: ContextTypes.DEFAULT_TYPE):
    bots_list = await get_bots_list()
    await safe_send(update.message.reply_text, f"🤖 Боты:\n{bots_list}" + user_commands_hint())

# ======== /sites =========
async def sites(update: Update, context: ContextTypes.DEFAULT_TYPE):
    sites_list = await get_sites_list()
    await safe_send(update.message.reply_text, f"🌐 Сайты:\n{sites_list}" + user_commands_hint())

# ======== Admin channel setters =========
async def add_channel(name, update, context):
    if update.effective_chat.type != "private" or not is_admin(update.effective_user.id) or not context.args:
        return
    db = get_db()
    try:
        with db.cursor() as cur:
            cur.execute("""
                INSERT INTO channels (name,url) VALUES (%s,%s)
                ON CONFLICT (name) DO UPDATE SET url=EXCLUDED.url
            """, (name, context.args[0]))
            db.commit()
    finally:
        release_db(db)
    await safe_send(update.message.reply_text, f"✅ Канал {name} установлен!")

async def addprice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await add_channel("price", update, context)

async def addcontact(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await add_channel("contact", update, context)

async def addwork(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await add_channel("work", update, context)

# ================= MAIN =================
def main():
    global DB_POOL
    DB_POOL = SimpleConnectionPool(1, 10, dsn=DATABASE_URL, cursor_factory=RealDictCursor)
    init_db()
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # Основные команды
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("link", link))
    app.add_handler(CommandHandler("bots", bots))
    app.add_handler(CommandHandler("sites", sites))

    # Админские команды
    app.add_handler(CommandHandler("setchat", setchat))
    app.add_handler(CommandHandler("addbot", addbot))
    app.add_handler(CommandHandler("removebot", removebot))
    app.add_handler(CommandHandler("addsite", addsite))
    app.add_handler(CommandHandler("removesite", removesite))
    app.add_handler(CommandHandler("settings", settings))
    app.add_handler(CommandHandler("broadcast", broadcast))

    # Новые админские команды для каналов
    app.add_handler(CommandHandler("addprice", addprice))
    app.add_handler(CommandHandler("addcontact", addcontact))
    app.add_handler(CommandHandler("addwork", addwork))

    app.add_handler(ChatMemberHandler(protect_chat, ChatMemberHandler.CHAT_MEMBER))
    print("🚀 Бот запущен (Railway, pooled)")
    app.run_polling()

if __name__ == "__main__":
    main()

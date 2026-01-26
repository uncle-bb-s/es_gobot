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
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT
                );
                CREATE TABLE IF NOT EXISTS bots (
                    username TEXT PRIMARY KEY
                );
                CREATE TABLE IF NOT EXISTS sites (
                    url TEXT PRIMARY KEY
                );
                CREATE TABLE IF NOT EXISTS price_channels (
                    url TEXT PRIMARY KEY
                );
                CREATE TABLE IF NOT EXISTS contact_channels (
                    url TEXT PRIMARY KEY
                );
                CREATE TABLE IF NOT EXISTS job_channels (
                    url TEXT PRIMARY KEY
                );
                CREATE TABLE IF NOT EXISTS active_links (
                    user_id TEXT PRIMARY KEY,
                    invite_link TEXT,
                    expire INTEGER
                );
                CREATE TABLE IF NOT EXISTS last_requests (
                    user_id TEXT PRIMARY KEY,
                    timestamp INTEGER
                );
                CREATE TABLE IF NOT EXISTS users (
                    user_id TEXT PRIMARY KEY,
                    username TEXT,
                    first_name TEXT,
                    last_name TEXT,
                    first_used TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS link_locks (
                    user_id TEXT PRIMARY KEY,
                    timestamp INTEGER
                );
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
        "• /sites — список сайтов 🌐\n"
        "• /price — прайс-канал 📣\n"
        "• /contact — контакты 📞\n"
        "• /jobs — работа 💼"
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

async def get_price_list():
    db = get_db()
    try:
        with db.cursor() as cur:
            cur.execute("SELECT url FROM price_channels")
            rows = cur.fetchall()
        return "\n".join(f"📣 {r['url']}" for r in rows) if rows else "—"
    finally:
        release_db(db)

async def get_contact_list():
    db = get_db()
    try:
        with db.cursor() as cur:
            cur.execute("SELECT url FROM contact_channels")
            rows = cur.fetchall()
        return "\n".join(f"📞 {r['url']}" for r in rows) if rows else "—"
    finally:
        release_db(db)

async def get_job_list():
    db = get_db()
    try:
        with db.cursor() as cur:
            cur.execute("SELECT url FROM job_channels")
            rows = cur.fetchall()
        return "\n".join(f"💼 {r['url']}" for r in rows) if rows else "—"
    finally:
        release_db(db)

# ================= COMMANDS =================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    log_user(user)

    bots_list = await get_bots_list()
    sites_list = await get_sites_list()
    price_list = await get_price_list()
    contact_list = await get_contact_list()
    job_list = await get_job_list()

    if update.effective_chat.type == "private":
        caption = (
            f"👋 Привет, {user.first_name or 'друг'}!\n\n"
            f"🤖 Актуальные боты:\n{bots_list}\n\n"
            f"🌐 Актуальные сайты:\n{sites_list}\n\n"
            f"📣 Прайс-канал:\n{price_list}\n\n"
            f"📞 Контакт-канал:\n{contact_list}\n\n"
            f"💼 Работа-канал:\n{job_list}\n\n"
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
            "• /removeprice <url>\n"
            "• /addcontact <url>\n"
            "• /removecontact <url>\n"
            "• /addjob <url>\n"
            "• /removejob <url>\n"
            "• /settings\n"
            "• /broadcast <текст>"
            if is_admin(user.id)
            else user_commands_hint()
        )

        await safe_send(
            context.bot.send_photo,
            chat_id=update.effective_chat.id,
            photo=WELCOME_IMAGE,
            caption=caption
        )

async def price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    price_list = await get_price_list()
    await safe_send(update.message.reply_text, f"📣 Прайс-канал:\n{price_list}" + user_commands_hint())

async def contact(update: Update, context: ContextTypes.DEFAULT_TYPE):
    contact_list = await get_contact_list()
    await safe_send(update.message.reply_text, f"📞 Контакты:\n{contact_list}" + user_commands_hint())

async def jobs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    job_list = await get_job_list()
    await safe_send(update.message.reply_text, f"💼 Работа:\n{job_list}" + user_commands_hint())

# ================= ADMIN =================
async def addprice(update, context):
    if update.effective_chat.type != "private" or not is_admin(update.effective_user.id) or not context.args:
        return
    db = get_db()
    try:
        with db.cursor() as cur:
            cur.execute("INSERT INTO price_channels (url) VALUES (%s) ON CONFLICT DO NOTHING", (context.args[0],))
        db.commit()
    finally:
        release_db(db)
    await safe_send(update.message.reply_text, "✅ Прайс-канал добавлен")

async def removeprice(update, context):
    if update.effective_chat.type != "private" or not is_admin(update.effective_user.id) or not context.args:
        return
    db = get_db()
    try:
        with db.cursor() as cur:
            cur.execute("DELETE FROM price_channels WHERE url=%s", (context.args[0],))
        db.commit()
    finally:
        release_db(db)
    await safe_send(update.message.reply_text, "🗑 Прайс-канал удалён")

async def addcontact(update, context):
    if update.effective_chat.type != "private" or not is_admin(update.effective_user.id) or not context.args:
        return
    db = get_db()
    try:
        with db.cursor() as cur:
            cur.execute("INSERT INTO contact_channels (url) VALUES (%s) ON CONFLICT DO NOTHING", (context.args[0],))
        db.commit()
    finally:
        release_db(db)
    await safe_send(update.message.reply_text, "✅ Контакт добавлен")

async def removecontact(update, context):
    if update.effective_chat.type != "private" or not is_admin(update.effective_user.id) or not context.args:
        return
    db = get_db()
    try:
        with db.cursor() as cur:
            cur.execute("DELETE FROM contact_channels WHERE url=%s", (context.args[0],))
        db.commit()
    finally:
        release_db(db)
    await safe_send(update.message.reply_text, "🗑 Контакт удалён")

async def addjob(update, context):
    if update.effective_chat.type != "private" or not is_admin(update.effective_user.id) or not context.args:
        return
    db = get_db()
    try:
        with db.cursor() as cur:
            cur.execute("INSERT INTO job_channels (url) VALUES (%s) ON CONFLICT DO NOTHING", (context.args[0],))
        db.commit()
    finally:
        release_db(db)
    await safe_send(update.message.reply_text, "✅ Работа добавлена")

async def removejob(update, context):
    if update.effective_chat.type != "private" or not is_admin(update.effective_user.id) or not context.args:
        return
    db = get_db()
    try:
        with db.cursor() as cur:
            cur.execute("DELETE FROM job_channels WHERE url=%s", (context.args[0],))
        db.commit()
    finally:
        release_db(db)
    await safe_send(update.message.reply_text, "🗑 Работа удалена")

# ================= MAIN =================
def main():
    global DB_POOL
    DB_POOL = SimpleConnectionPool(1, 10, dsn=DATABASE_URL, cursor_factory=RealDictCursor)
    init_db()

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("price", price))
    app.add_handler(CommandHandler("contact", contact))
    app.add_handler(CommandHandler("jobs", jobs))

    app.add_handler(CommandHandler("addprice", addprice))
    app.add_handler(CommandHandler("removeprice", removeprice))
    app.add_handler(CommandHandler("addcontact", addcontact))
    app.add_handler(CommandHandler("removecontact", removecontact))
    app.add_handler(CommandHandler("addjob", addjob))
    app.add_handler(CommandHandler("removejob", removejob))

    print("🚀 Бот запущен (Railway, pooled)")
    app.run_polling()

if __name__ == "__main__":
    main()

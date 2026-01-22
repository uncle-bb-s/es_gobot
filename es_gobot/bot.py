import os
import time
import random
import asyncio
import psycopg2
from psycopg2.extras import RealDictCursor
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
WELCOME_IMAGE = "https://image2url.com/r2/default/images/1768635379388-0769fe79-f5b5-4926-97dc-a20e7be08fe0.jpg"

if not BOT_TOKEN or ADMIN_ID == 0 or not DATABASE_URL:
    raise RuntimeError("❌ BOT_TOKEN, ADMIN_ID или DATABASE_URL не заданы")

# ================= DATABASE =================
pool = []

def get_db():
    if pool:
        return pool.pop()
    return psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)

def release_db(conn):
    pool.append(conn)

def init_db():
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS bots (
                    username TEXT PRIMARY KEY
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS sites (
                    name TEXT PRIMARY KEY,
                    url TEXT
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS active_links (
                    user_id TEXT PRIMARY KEY,
                    invite_link TEXT,
                    expire INTEGER
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS last_requests (
                    user_id TEXT PRIMARY KEY,
                    timestamp INTEGER
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id TEXT PRIMARY KEY,
                    username TEXT,
                    first_name TEXT,
                    last_name TEXT,
                    first_used TIMESTAMP
                )
            """)
        conn.commit()
    finally:
        release_db(conn)

def get_setting(key):
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT value FROM settings WHERE key = %s", (key,))
            row = cur.fetchone()
            return row["value"] if row else None
    finally:
        release_db(conn)

def set_setting(key, value):
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO settings (key, value) VALUES (%s, %s) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
                (key, str(value))
            )
        conn.commit()
    finally:
        release_db(conn)

# ================= UTILS =================
def is_admin(user_id: int) -> bool:
    return user_id == ADMIN_ID

def log_user(user):
    user_id = str(user.id)
    username = user.username or "—"
    first_name = user.first_name or "—"
    last_name = user.last_name or "—"

    now = time.strftime("%Y-%m-%d %H:%M:%S")
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM users WHERE user_id = %s", (user_id,))
            if cur.fetchone():
                return
            cur.execute("""
                INSERT INTO users (user_id, username, first_name, last_name, first_used)
                VALUES (%s, %s, %s, %s, %s)
            """, (user_id, username, first_name, last_name, now))
        conn.commit()
    finally:
        release_db(conn)

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
    return "\n\n📌 Ваши команды:\n• /link — получить персональную ссылку 🔑\n• /bots — список ботов 🤖\n• /sites — список сайтов 🌐"

# ================= BOT & SITE STATUS =================
async def get_bots_list() -> str:
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT username FROM bots")
            bots = [row["username"] for row in cur.fetchall()]
    finally:
        release_db(conn)
    return "\n".join(f"🟢 {b}" for b in bots) if bots else "—"

async def get_sites_list() -> str:
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT name, url FROM sites")
            sites = [f"🌐 {row['name']} — {row['url']}" for row in cur.fetchall()]
    finally:
        release_db(conn)
    return "\n".join(sites) if sites else "—"

# ================= COMMANDS =================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    log_user(user)

    bots_list = await get_bots_list()
    sites_list = await get_sites_list()
    caption = (
        f"👋 Привет, {user.first_name or 'друг'}!\n\n"
        f"🤖 Доступные боты:\n{bots_list}\n\n"
        f"🌐 Актуальные сайты:\n{sites_list}\n\n"
        "🔒 Здесь ты получаешь персональный доступ в приватный чат.\n\n"
        "⚡ Как это работает:\n"
        "1️⃣ Нажми /link 🚪\n"
        "2️⃣ Ссылка активна 15 секунд ⏳\n"
        "3️⃣ Повторный запрос — через 30 минут ⏰"
    )

    caption += (
        "\n\n👑 Админ:\n• /setchat <id>\n• /addbot <bot>\n• /removebot <bot>\n• /addsite <name> <url>\n• /removesite <name>\n• /settings\n• /broadcast <текст>"
        if is_admin(user.id)
        else user_commands_hint()
    )

    await safe_send(
        context.bot.send_photo if WELCOME_IMAGE else update.message.reply_text,
        chat_id=update.effective_chat.id,
        photo=WELCOME_IMAGE,
        caption=caption
    )

# (Твой код link, bots, protect_chat, setchat, addbot, removebot, settings, broadcast без изменений)

# ================= ADMIN SITES =================
async def addsite(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id) or len(context.args) < 2:
        return await safe_send(update.message.reply_text, "❌ Используй: /addsite <name> <url>")
    name = context.args[0]
    url = context.args[1]
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO sites (name, url) VALUES (%s, %s) ON CONFLICT (name) DO UPDATE SET url = EXCLUDED.url",
                (name, url)
            )
        conn.commit()
    finally:
        release_db(conn)
    await safe_send(update.message.reply_text, f"✅ Сайт {name} добавлен/обновлён")

async def removesite(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id) or not context.args:
        return await safe_send(update.message.reply_text, "❌ Используй: /removesite <name>")
    name = context.args[0]
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM sites WHERE name = %s", (name,))
        conn.commit()
    finally:
        release_db(conn)
    await safe_send(update.message.reply_text, f"🗑 Сайт {name} удалён")

async def showsites(update: Update, context: ContextTypes.DEFAULT_TYPE):
    sites_list = await get_sites_list()
    await safe_send(update.message.reply_text, f"🌐 Сайты:\n{sites_list}")

# ================= MAIN =================
def main():
    init_db()
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("link", link))
    app.add_handler(CommandHandler("bots", bots))
    app.add_handler(CommandHandler("setchat", setchat))
    app.add_handler(CommandHandler("addbot", addbot))
    app.add_handler(CommandHandler("removebot", removebot))
    app.add_handler(CommandHandler("settings", settings))
    app.add_handler(CommandHandler("broadcast", broadcast))
    
    # Admin site commands
    app.add_handler(CommandHandler("addsite", addsite))
    app.add_handler(CommandHandler("removesite", removesite))
    app.add_handler(CommandHandler("sites", showsites))

    app.add_handler(ChatMemberHandler(protect_chat, ChatMemberHandler.CHAT_MEMBER))

    print("🚀 Бот запущен (PostgreSQL, Polling, с сохранением пользователей, пул соединений, сайты и боты)")
    app.run_polling()

if __name__ == "__main__":
    main()

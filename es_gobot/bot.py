import os
import time
import random
import asyncio
import psycopg2
from psycopg2.extras import RealDictCursor
from psycopg2 import pool
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

# ================= DATABASE POOL =================
db_pool = pool.SimpleConnectionPool(1, 10, dsn=DATABASE_URL, cursor_factory=RealDictCursor)

def get_db():
    return db_pool.getconn()

def release_db(conn):
    db_pool.putconn(conn)

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
            cur.execute("""
                CREATE TABLE IF NOT EXISTS sites (
                    name TEXT PRIMARY KEY,
                    url TEXT
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

# ================= BOT STATUS =================
async def get_bots_list() -> str:
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT username FROM bots")
            bots = [row["username"] for row in cur.fetchall()]
    finally:
        release_db(conn)
    return "\n".join(f"🟢 онлайн — {b}" for b in bots) if bots else "—"

async def get_sites_list() -> str:
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT name, url FROM sites")
            sites = cur.fetchall()
    finally:
        release_db(conn)
    return "\n".join(f"🔗 {row['name']}: {row['url']}" for row in sites) if sites else "—"

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

async def link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = str(user.id)
    log_user(user)

    now = int(time.time())
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM active_links WHERE expire < %s", (now,))
            cur.execute("SELECT timestamp FROM last_requests WHERE user_id = %s", (user_id,))
            row = cur.fetchone()
            if row and now - row["timestamp"] < LINK_COOLDOWN:
                mins = (LINK_COOLDOWN - (now - row["timestamp"])) // 60
                await safe_send(update.message.reply_text, f"⏳ Повтори через {mins} мин.")
                return
    finally:
        release_db(conn)

    chat_id = get_setting("private_chat_id")
    if not chat_id:
        await safe_send(update.message.reply_text, "❌ Приватный чат не настроен.")
        return

    invite = await context.bot.create_chat_invite_link(
        chat_id=int(chat_id),
        expire_date=now + LINK_EXPIRE,
        member_limit=1
    )

    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO last_requests (user_id, timestamp) VALUES (%s, %s) ON CONFLICT (user_id) DO UPDATE SET timestamp = EXCLUDED.timestamp",
                (user_id, now)
            )
            cur.execute(
                "INSERT INTO active_links (user_id, invite_link, expire) VALUES (%s, %s, %s) ON CONFLICT (user_id) DO UPDATE SET invite_link = EXCLUDED.invite_link, expire = EXCLUDED.expire",
                (user_id, invite.invite_link, now + LINK_EXPIRE)
            )
        conn.commit()
    finally:
        release_db(conn)

    await safe_send(
        update.message.reply_text,
        "✅ Ссылка готова! ⏳ 15 секунд.",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🚪 Войти", url=invite.invite_link)]])
    )

async def bots(update: Update, context: ContextTypes.DEFAULT_TYPE):
    bots_list = await get_bots_list()
    await safe_send(update.message.reply_text, f"🤖 Боты:\n{bots_list}" + user_commands_hint())

async def sites(update: Update, context: ContextTypes.DEFAULT_TYPE):
    sites_list = await get_sites_list()
    await safe_send(update.message.reply_text, f"🌐 Сайты:\n{sites_list}" + user_commands_hint())

# ================= ANTI-SLIV =================
async def protect_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    member = update.chat_member
    if member.new_chat_member.status not in ("member", "restricted"):
        return

    user_id = str(member.new_chat_member.user.id)
    invite_link = member.invite_link.invite_link if member.invite_link else None
    now = int(time.time())

    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT invite_link, expire FROM active_links WHERE user_id = %s", (user_id,))
            row = cur.fetchone()
    finally:
        release_db(conn)

    if not row or now > row["expire"] or invite_link != row["invite_link"]:
        try:
            await context.bot.ban_chat_member(member.chat.id, int(user_id))
            await context.bot.unban_chat_member(member.chat.id, int(user_id))
        except:
            pass
        return

    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM active_links WHERE user_id = %s", (user_id,))
        conn.commit()
    finally:
        release_db(conn)

# ================= ADMIN =================
async def setchat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id) or not context.args:
        return
    set_setting("private_chat_id", context.args[0])
    await safe_send(update.message.reply_text, "✅ Чат установлен")

async def addbot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id) or not context.args:
        return
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO bots (username) VALUES (%s) ON CONFLICT DO NOTHING", (context.args[0],))
        conn.commit()
    finally:
        release_db(conn)
    await safe_send(update.message.reply_text, "✅ Бот добавлен")

async def removebot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id) or not context.args:
        return
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM bots WHERE username = %s", (context.args[0],))
        conn.commit()
    finally:
        release_db(conn)
    await safe_send(update.message.reply_text, "🗑 Бот удалён")

async def addsite(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id) or len(context.args) < 2:
        return
    name, url = context.args[0], context.args[1]
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO sites (name, url) VALUES (%s, %s) ON CONFLICT DO NOTHING", (name, url))
        conn.commit()
    finally:
        release_db(conn)
    await safe_send(update.message.reply_text, f"✅ Сайт {name} добавлен")

async def removesite(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id) or not context.args:
        return
    name = context.args[0]
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM sites WHERE name = %s", (name,))
        conn.commit()
    finally:
        release_db(conn)
    await safe_send(update.message.reply_text, f"🗑 Сайт {name} удалён")

async def settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    chat = get_setting("private_chat_id")
    bots_list = await get_bots_list()
    sites_list = await get_sites_list()
    await safe_send(update.message.reply_text, f"📋 Чат: {chat}\n\n🤖 Боты:\n{bots_list}\n\n🌐 Сайты:\n{sites_list}")

# ================= ADMIN BROADCAST =================
async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return await safe_send(update.message.reply_text, "❌ Только админ может использовать эту команду.")
    
    if not context.args:
        return await safe_send(update.message.reply_text, "❌ Укажи текст для рассылки: /broadcast <сообщение>")
    
    text = " ".join(context.args)

    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT user_id FROM users")
            users = [row["user_id"] for row in cur.fetchall()]
    finally:
        release_db(conn)

    success = 0
    failed = 0
    batch_size = 50

    for i in range(0, len(users), batch_size):
        batch = users[i:i + batch_size]
        tasks = [safe_send(context.bot.send_message, chat_id=int(user_id), text=text) for user_id in batch]
        results = await asyncio.gather(*tasks)
        for r in results:
            if r:
                success += 1
            else:
                failed += 1
        await asyncio.sleep(1)

    await safe_send(update.message.reply_text, f"✅ Рассылка завершена!\nДоставлено: {success}\nНе доставлено: {failed}")

# ================= MAIN =================
def main():
    init_db()
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("link", link))
    app.add_handler(CommandHandler("bots", bots))
    app.add_handler(CommandHandler("sites", sites))
    app.add_handler(CommandHandler("setchat", setchat))
    app.add_handler(CommandHandler("addbot", addbot))
    app.add_handler(CommandHandler("removebot", removebot))
    app.add_handler(CommandHandler("addsite", addsite))
    app.add_handler(CommandHandler("removesite", removesite))
    app.add_handler(CommandHandler("settings", settings))
    app.add_handler(CommandHandler("broadcast", broadcast))
    app.add_handler(ChatMemberHandler(protect_chat, ChatMemberHandler.CHAT_MEMBER))

    print("🚀 Бот запущен (PostgreSQL, Polling, с сохранением пользователей, пул соединений, сайты и боты)")
    app.run_polling()

if __name__ == "__main__":
    main()

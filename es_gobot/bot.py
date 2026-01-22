import os
import time
import random
import asyncio
import psycopg2
from psycopg2.extras import RealDictCursor
from psycopg2.pool import SimpleConnectionPool

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
db_pool = SimpleConnectionPool(
    minconn=1,
    maxconn=10,
    dsn=DATABASE_URL,
    cursor_factory=RealDictCursor
)

def get_db():
    return db_pool.getconn()

def release_db(conn):
    db_pool.putconn(conn)

# ================= INIT DB =================
def init_db():
    db = get_db()
    try:
        with db.cursor() as cur:
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
                    url TEXT PRIMARY KEY
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
        db.commit()
    finally:
        release_db(db)

# ================= SETTINGS =================
def get_setting(key):
    db = get_db()
    try:
        with db.cursor() as cur:
            cur.execute("SELECT value FROM settings WHERE key = %s", (key,))
            row = cur.fetchone()
            return row["value"] if row else None
    finally:
        release_db(db)

def set_setting(key, value):
    db = get_db()
    try:
        with db.cursor() as cur:
            cur.execute("""
                INSERT INTO settings (key, value)
                VALUES (%s, %s)
                ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
            """, (key, str(value)))
        db.commit()
    finally:
        release_db(db)

# ================= UTILS =================
def is_admin(user_id: int) -> bool:
    return user_id == ADMIN_ID

def log_user(user):
    db = get_db()
    try:
        with db.cursor() as cur:
            cur.execute("SELECT 1 FROM users WHERE user_id = %s", (str(user.id),))
            if cur.fetchone():
                return
            cur.execute("""
                INSERT INTO users (user_id, username, first_name, last_name, first_used)
                VALUES (%s, %s, %s, %s, %s)
            """, (
                str(user.id),
                user.username or "—",
                user.first_name or "—",
                user.last_name or "—",
                time.strftime("%Y-%m-%d %H:%M:%S")
            ))
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
    return "\n\n📌 Команды:\n/link\n/bots\n/sites"

# ================= LISTS =================
async def get_bots_list():
    db = get_db()
    try:
        with db.cursor() as cur:
            cur.execute("SELECT username FROM bots")
            rows = cur.fetchall()
        return "\n".join(f"🟢 {r['username']}" for r in rows) if rows else "—"
    finally:
        release_db(db)

async def get_sites_list():
    db = get_db()
    try:
        with db.cursor() as cur:
            cur.execute("SELECT url FROM sites")
            rows = cur.fetchall()
        return "\n".join(f"🌐 {r['url']}" for r in rows) if rows else "—"
    finally:
        release_db(db)

# ================= COMMANDS =================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    log_user(user)

    bots_list = await get_bots_list()
    sites_list = await get_sites_list()

    text = (
        f"👋 Привет, {user.first_name or 'друг'}!\n\n"
        f"🤖 Боты:\n{bots_list}\n\n"
        f"🌐 Сайты:\n{sites_list}\n\n"
        "Для доступа нажми /link"
    )

    await safe_send(
        context.bot.send_photo,
        chat_id=update.effective_chat.id,
        photo=WELCOME_IMAGE,
        caption=text
    )

async def link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = str(user.id)
    log_user(user)

    now = int(time.time())
    db = get_db()
    try:
        with db.cursor() as cur:
            cur.execute("DELETE FROM active_links WHERE expire < %s", (now,))
            cur.execute("SELECT timestamp FROM last_requests WHERE user_id = %s", (user_id,))
            row = cur.fetchone()
            if row and now - row["timestamp"] < LINK_COOLDOWN:
                mins = (LINK_COOLDOWN - (now - row["timestamp"])) // 60
                await safe_send(update.message.reply_text, f"⏳ Повтори через {mins} мин.")
                return
        db.commit()
    finally:
        release_db(db)

    chat_id = get_setting("private_chat_id")
    if not chat_id:
        await safe_send(update.message.reply_text, "❌ Приватный чат не настроен.")
        return

    invite = await context.bot.create_chat_invite_link(
        chat_id=int(chat_id),
        expire_date=now + LINK_EXPIRE,
        member_limit=1
    )

    db = get_db()
    try:
        with db.cursor() as cur:
            cur.execute("""
                INSERT INTO last_requests (user_id, timestamp)
                VALUES (%s, %s)
                ON CONFLICT (user_id) DO UPDATE SET timestamp = EXCLUDED.timestamp
            """, (user_id, now))
            cur.execute("""
                INSERT INTO active_links (user_id, invite_link, expire)
                VALUES (%s, %s, %s)
                ON CONFLICT (user_id)
                DO UPDATE SET invite_link = EXCLUDED.invite_link, expire = EXCLUDED.expire
            """, (user_id, invite.invite_link, now + LINK_EXPIRE))
        db.commit()
    finally:
        release_db(db)

    await safe_send(
        update.message.reply_text,
        "✅ Ссылка готова (15 сек)",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("🚪 Войти", url=invite.invite_link)]]
        )
    )

# ================= PROTECT =================
async def protect_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    member = update.chat_member
    if member.new_chat_member.status not in ("member", "restricted"):
        return

    user_id = str(member.new_chat_member.user.id)
    invite_link = member.invite_link.invite_link if member.invite_link else None
    now = int(time.time())

    db = get_db()
    try:
        with db.cursor() as cur:
            cur.execute("SELECT invite_link, expire FROM active_links WHERE user_id = %s", (user_id,))
            row = cur.fetchone()
    finally:
        release_db(db)

    if not row or now > row["expire"] or invite_link != row["invite_link"]:
        try:
            await context.bot.ban_chat_member(member.chat.id, int(user_id))
            await context.bot.unban_chat_member(member.chat.id, int(user_id))
        except:
            pass
        return

    db = get_db()
    try:
        with db.cursor() as cur:
            cur.execute("DELETE FROM active_links WHERE user_id = %s", (user_id,))
        db.commit()
    finally:
        release_db(db)

# ================= ADMIN =================
async def setchat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_admin(update.effective_user.id) and context.args:
        set_setting("private_chat_id", context.args[0])
        await safe_send(update.message.reply_text, "✅ Чат установлен")

# ================= MAIN =================
def main():
    init_db()
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("link", link))
    app.add_handler(ChatMemberHandler(protect_chat, ChatMemberHandler.CHAT_MEMBER))
    app.add_handler(CommandHandler("setchat", setchat))

    print("🚀 Бот запущен (pool enabled)")
    app.run_polling()

if __name__ == "__main__":
    main()

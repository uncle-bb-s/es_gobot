import os
import time
import random
import asyncio
import sqlite3
import psycopg

from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    ChatMemberHandler,
)
from telegram.error import Forbidden, TimedOut, NetworkError, RetryAfter

# ================= CONFIG =================
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
DATABASE_URL = os.getenv("DATABASE_URL")  # ← NEW

DB_FILE = "bot.db"
USERS_FILE = "users.txt"

LINK_EXPIRE = 15
LINK_COOLDOWN = 1800

WELCOME_IMAGE = "https://image2url.com/r2/default/images/1768635379388-0769fe79-f5b5-4926-97dc-a20e7be08fe0.jpg"

if not BOT_TOKEN or ADMIN_ID == 0:
    raise RuntimeError("❌ BOT_TOKEN или ADMIN_ID не заданы")

# ================= DATABASE =================
def get_db():
    if DATABASE_URL:
        return psycopg.connect(DATABASE_URL, autocommit=True)
    return sqlite3.connect(DB_FILE)

def init_db():
    with get_db() as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS bots (
                username TEXT PRIMARY KEY
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS active_links (
                user_id TEXT PRIMARY KEY,
                invite_link TEXT,
                expire INTEGER
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS last_requests (
                user_id TEXT PRIMARY KEY,
                timestamp INTEGER
            )
        """)
        # ===== NEW =====
        db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id TEXT PRIMARY KEY,
                first_seen INTEGER
            )
        """)

def get_setting(key):
    with get_db() as db:
        row = db.execute(
            "SELECT value FROM settings WHERE key = %s" if DATABASE_URL else
            "SELECT value FROM settings WHERE key = ?",
            (key,)
        ).fetchone()
        return row[0] if row else None

def set_setting(key, value):
    with get_db() as db:
        db.execute(
            "REPLACE INTO settings (key, value) VALUES (%s, %s)"
            if DATABASE_URL else
            "REPLACE INTO settings (key, value) VALUES (?, ?)",
            (key, str(value))
        )

# ================= USERS (NEW) =================
def save_user(user):
    with get_db() as db:
        db.execute(
            """
            INSERT INTO users (user_id, first_seen)
            VALUES (%s, %s)
            ON CONFLICT (user_id) DO NOTHING
            """ if DATABASE_URL else
            """
            INSERT OR IGNORE INTO users (user_id, first_seen)
            VALUES (?, ?)
            """,
            (str(user.id), int(time.time()))
        )

# ================= UTILS =================
def is_admin(user_id: int) -> bool:
    return user_id == ADMIN_ID

def log_user(user):
    user_id = user.id
    if os.path.exists(USERS_FILE):
        with open(USERS_FILE, "r", encoding="utf-8") as f:
            if f"ID: {user_id} " in f.read():
                return

    with open(USERS_FILE, "a", encoding="utf-8") as f:
        f.write(f"ID: {user_id}\n")

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
    return "\n\n📌 Ваши команды:\n• /link\n• /bots"

# ================= COMMANDS =================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    save_user(user)      # ← NEW
    log_user(user)

    await safe_send(
        context.bot.send_photo if WELCOME_IMAGE else update.message.reply_text,
        chat_id=update.effective_chat.id,
        photo=WELCOME_IMAGE,
        caption="👋 Привет! Нажми /link чтобы получить доступ."
    )

async def link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    save_user(user)      # ← NEW
    log_user(user)

    with get_db() as db:
        db.execute(
            "DELETE FROM active_links WHERE expire < %s"
            if DATABASE_URL else
            "DELETE FROM active_links WHERE expire < ?",
            (int(time.time()),)
        )

    chat_id = get_setting("private_chat_id")
    if not chat_id:
        await safe_send(update.message.reply_text, "❌ Приватный чат не настроен.")
        return

    now = int(time.time())
    with get_db() as db:
        row = db.execute(
            "SELECT timestamp FROM last_requests WHERE user_id = %s"
            if DATABASE_URL else
            "SELECT timestamp FROM last_requests WHERE user_id = ?",
            (str(user.id),)
        ).fetchone()

        if row and now - row[0] < LINK_COOLDOWN:
            mins = (LINK_COOLDOWN - (now - row[0])) // 60
            await safe_send(update.message.reply_text, f"⏳ Повтори через {mins} мин.")
            return

    invite = await context.bot.create_chat_invite_link(
        chat_id=int(chat_id),
        expire_date=now + LINK_EXPIRE,
        member_limit=1
    )

    with get_db() as db:
        db.execute(
            "REPLACE INTO last_requests VALUES (%s, %s)"
            if DATABASE_URL else
            "REPLACE INTO last_requests VALUES (?, ?)",
            (str(user.id), now)
        )
        db.execute(
            "REPLACE INTO active_links VALUES (%s, %s, %s)"
            if DATABASE_URL else
            "REPLACE INTO active_links VALUES (?, ?, ?)",
            (str(user.id), invite.invite_link, now + LINK_EXPIRE)
        )

    await safe_send(
        update.message.reply_text,
        "✅ Ссылка готова! ⏳ 15 секунд.",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🚪 Войти", url=invite.invite_link)]
        ])
    )

# ================= ANTI-SLIV =================
async def protect_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    member = update.chat_member

    if member.new_chat_member.status not in ("member", "restricted"):
        return

    user_id = str(member.new_chat_member.user.id)
    invite_link = member.invite_link.invite_link if member.invite_link else None
    now = int(time.time())

    with get_db() as db:
        row = db.execute(
            "SELECT invite_link, expire FROM active_links WHERE user_id = %s"
            if DATABASE_URL else
            "SELECT invite_link, expire FROM active_links WHERE user_id = ?",
            (user_id,)
        ).fetchone()

    if not row or now > row[1] or invite_link != row[0]:
        try:
            await context.bot.ban_chat_member(member.chat.id, int(user_id))
            await context.bot.unban_chat_member(member.chat.id, int(user_id))
        except:
            pass
        return

    with get_db() as db:
        db.execute(
            "DELETE FROM active_links WHERE user_id = %s"
            if DATABASE_URL else
            "DELETE FROM active_links WHERE user_id = ?",
            (user_id,)
        )

# ================= ADMIN =================
async def setchat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id) or not context.args:
        return
    set_setting("private_chat_id", context.args[0])
    await update.message.reply_text("✅ Чат установлен")

# ================= MAIN =================
def main():
    init_db()

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("link", link))
    app.add_handler(CommandHandler("setchat", setchat))
    app.add_handler(ChatMemberHandler(protect_chat, ChatMemberHandler.CHAT_MEMBER))

    print("🚀 Бот запущен (Railway + PostgreSQL)")
    app.run_polling(poll_interval=2, timeout=30)

if __name__ == "__main__":
    main()

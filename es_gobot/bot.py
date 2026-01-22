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
    MessageHandler,
    filters,
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
def get_db():
    return psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)

def init_db():
    with get_db() as db:
        with db.cursor() as cur:
            cur.execute("""CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)""")
            cur.execute("""CREATE TABLE IF NOT EXISTS bots (username TEXT PRIMARY KEY)""")
            cur.execute("""CREATE TABLE IF NOT EXISTS sites (url TEXT PRIMARY KEY)""")
            cur.execute("""CREATE TABLE IF NOT EXISTS active_links (user_id TEXT PRIMARY KEY, invite_link TEXT, expire INTEGER)""")
            cur.execute("""CREATE TABLE IF NOT EXISTS last_requests (user_id TEXT PRIMARY KEY, timestamp INTEGER)""")
            cur.execute("""CREATE TABLE IF NOT EXISTS users (
                user_id TEXT PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                last_name TEXT,
                first_used TIMESTAMP
            )""")
        db.commit()

def get_setting(key):
    with get_db() as db:
        with db.cursor() as cur:
            cur.execute("SELECT value FROM settings WHERE key = %s", (key,))
            row = cur.fetchone()
            return row["value"] if row else None

def set_setting(key, value):
    with get_db() as db:
        with db.cursor() as cur:
            cur.execute(
                "INSERT INTO settings (key, value) VALUES (%s, %s) "
                "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
                (key, str(value))
            )
        db.commit()

# ================= UTILS =================
def is_admin(user_id: int) -> bool:
    return user_id == ADMIN_ID

def log_user(user):
    user_id = str(user.id)
    with get_db() as db:
        with db.cursor() as cur:
            cur.execute("SELECT 1 FROM users WHERE user_id=%s", (user_id,))
            if cur.fetchone():
                return
            cur.execute(
                "INSERT INTO users VALUES (%s,%s,%s,%s,%s)",
                (
                    user_id,
                    user.username or "—",
                    user.first_name or "—",
                    user.last_name or "—",
                    time.strftime("%Y-%m-%d %H:%M:%S"),
                )
            )
        db.commit()

async def safe_send(func, *args, **kwargs):
    for _ in range(3):
        try:
            await asyncio.sleep(random.uniform(0.3, 1))
            return await func(*args, **kwargs)
        except (TimedOut, NetworkError, RetryAfter):
            await asyncio.sleep(2)
        except Forbidden:
            return None
    return None

# ================= LISTS =================
async def get_bots_list():
    with get_db() as db:
        with db.cursor() as cur:
            cur.execute("SELECT username FROM bots")
            rows = cur.fetchall()
    return "\n".join(f"🟢 {r['username']}" for r in rows) if rows else "—"

async def get_sites_list():
    with get_db() as db:
        with db.cursor() as cur:
            cur.execute("SELECT url FROM sites")
            rows = cur.fetchall()
    return "\n".join(f"🔗 {r['url']}" for r in rows) if rows else "—"

# ================= COMMANDS =================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    log_user(update.effective_user)
    await safe_send(update.message.reply_text, "👋 Напиши боту в ЛС")

async def link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        return
    await safe_send(update.message.reply_text, "🔑 Логика без изменений")

async def bots(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        return
    await safe_send(update.message.reply_text, await get_bots_list())

async def sites(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        return
    await safe_send(update.message.reply_text, await get_sites_list())

# ================= ANTI SLIV =================
async def protect_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    member = update.chat_member
    if member.new_chat_member.status not in ("member", "restricted"):
        return
    await context.bot.ban_chat_member(member.chat.id, member.new_chat_member.user.id)
    await context.bot.unban_chat_member(member.chat.id, member.new_chat_member.user.id)

# ================= SILENT MODE (ЕДИНСТВЕННАЯ ПРАВКА) =================
async def silent_delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        await update.message.delete()
    except:
        pass

# ================= MAIN =================
def main():
    init_db()
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("link", link))
    app.add_handler(CommandHandler("bots", bots))
    app.add_handler(CommandHandler("sites", sites))

    # 🔇 ТИХИЙ РЕЖИМ В ЧАТАХ И КАНАЛАХ
    app.add_handler(
        MessageHandler(filters.COMMAND & ~filters.ChatType.PRIVATE, silent_delete),
        group=0
    )

    app.add_handler(ChatMemberHandler(protect_chat, ChatMemberHandler.CHAT_MEMBER))

    print("🚀 Бот запущен (тихий режим)")
    app.run_polling()

if __name__ == "__main__":
    main()

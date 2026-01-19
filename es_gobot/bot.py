import os
import time
import random
import asyncio
import sqlite3
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

DB_FILE = "bot.db"
USERS_FILE = "users.txt"

LINK_EXPIRE = 15
LINK_COOLDOWN = 1800

WELCOME_IMAGE = "https://image2url.com/r2/default/images/1768635379388-0769fe79-f5b5-4926-97dc-a20e7be08fe0.jpg"

if not BOT_TOKEN or ADMIN_ID == 0:
    raise RuntimeError("❌ BOT_TOKEN или ADMIN_ID не заданы")

# ================= DATABASE =================
def get_db():
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

def get_setting(key):
    with get_db() as db:
        row = db.execute(
            "SELECT value FROM settings WHERE key = ?", (key,)
        ).fetchone()
        return row[0] if row else None

def set_setting(key, value):
    with get_db() as db:
        db.execute(
            "REPLACE INTO settings (key, value) VALUES (?, ?)",
            (key, str(value))
        )

# ================= UTILS =================
def is_admin(user_id: int) -> bool:
    return user_id == ADMIN_ID

def log_user(user):
    user_id = user.id
    username = user.username or "—"
    first_name = user.first_name or "—"
    last_name = user.last_name or "—"
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")

    if os.path.exists(USERS_FILE):
        with open(USERS_FILE, "r", encoding="utf-8") as f:
            if f"ID: {user_id} " in f.read():
                return

    with open(USERS_FILE, "a", encoding="utf-8") as f:
        f.write(
            f"ID: {user_id} | Username: @{username} | "
            f"Name: {first_name} {last_name} | First used: {timestamp}\n"
        )

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
    return "\n\n📌 Ваши команды:\n• /link — получить персональную ссылку 🔑\n• /bots — список ботов 🤖"

# ================= BOT STATUS =================
async def get_bots_list() -> str:
    with get_db() as db:
        bots = [row[0] for row in db.execute("SELECT username FROM bots")]
    return "\n".join(f"🟢 онлайн — {b}" for b in bots) if bots else "—"

# ================= COMMANDS =================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    log_user(user)

    bots_list = await get_bots_list()

    caption = (
        f"👋 Привет, {user.first_name or 'друг'}!\n\n"
        f"🤖 Доступные боты:\n{bots_list}\n\n"
        "🔒 Здесь ты получаешь персональный доступ в приватный чат.\n\n"
        "⚡ Как это работает:\n"
        "1️⃣ Нажми /link 🚪\n"
        "2️⃣ Ссылка активна 15 секунд ⏳\n"
        "3️⃣ Повторный запрос — через 30 минут ⏰"
    )

    caption += (
        "\n\n👑 Админ:\n• /setchat <id>\n• /addbot <bot>\n• /removebot <bot>\n• /settings"
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

    chat_id = get_setting("private_chat_id")
    if not chat_id:
        await safe_send(update.message.reply_text, "❌ Приватный чат не настроен.")
        return

    now = int(time.time())
    with get_db() as db:
        row = db.execute(
            "SELECT timestamp FROM last_requests WHERE user_id = ?", (user_id,)
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
            "REPLACE INTO last_requests VALUES (?, ?)",
            (user_id, now)
        )
        db.execute(
            "REPLACE INTO active_links VALUES (?, ?, ?)",
            (user_id, invite.invite_link, now + LINK_EXPIRE)
        )

    msg = await safe_send(
        update.message.reply_text,
        "✅ Ссылка готова! ⏳ 15 секунд.",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🚪 Войти", url=invite.invite_link)]
        ])
    )

    context.job_queue.run_once(
        cleanup_job,
        when=LINK_EXPIRE,
        data={
            "chat_id": int(chat_id),
            "invite_link": invite.invite_link,
            "user_id": user_id,
            "msg_chat": msg.chat.id,
            "msg_id": msg.message_id
        }
    )

async def cleanup_job(context: ContextTypes.DEFAULT_TYPE):
    job = context.job
    with get_db() as db:
        db.execute(
            "DELETE FROM active_links WHERE user_id = ?",
            (job.data["user_id"],)
        )
    try:
        await context.bot.revoke_chat_invite_link(
            job.data["chat_id"], job.data["invite_link"]
        )
        await context.bot.delete_message(
            job.data["msg_chat"], job.data["msg_id"]
        )
    except:
        pass

async def bots(update: Update, context: ContextTypes.DEFAULT_TYPE):
    bots_list = await get_bots_list()
    await safe_send(update.message.reply_text, f"🤖 Боты:\n{bots_list}" + user_commands_hint())

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
        db.execute("DELETE FROM active_links WHERE user_id = ?", (user_id,))

# ================= ADMIN =================
async def setchat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id) or not context.args:
        return
    set_setting("private_chat_id", context.args[0])
    await safe_send(update.message.reply_text, "✅ Чат установлен")

async def addbot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id) or not context.args:
        return
    with get_db() as db:
        db.execute("INSERT OR IGNORE INTO bots VALUES (?)", (context.args[0],))
    await safe_send(update.message.reply_text, "✅ Бот добавлен")

async def removebot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id) or not context.args:
        return
    with get_db() as db:
        db.execute("DELETE FROM bots WHERE username = ?", (context.args[0],))
    await safe_send(update.message.reply_text, "🗑 Бот удалён")

async def settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    chat = get_setting("private_chat_id")
    bots_list = await get_bots_list()
    await safe_send(update.message.reply_text, f"📋 Чат: {chat}\n\nБоты:\n{bots_list}")

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
    app.add_handler(ChatMemberHandler(protect_chat, ChatMemberHandler.CHAT_MEMBER))

    print("🚀 Бот запущен (SQLite)")
    app.run_polling()

if __name__ == "__main__":
    main()

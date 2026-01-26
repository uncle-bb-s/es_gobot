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

# ================= SETTINGS =================
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

def user_commands_hint():
    return (
        "\n\n📌 Ваши команды:\n"
        "• /link — получить ссылку 🔑\n"
        "• /info — вся информация ℹ️"
    )

def log_user(user):
    db = get_db()
    try:
        with db.cursor() as cur:
            cur.execute("SELECT 1 FROM users WHERE user_id=%s", (str(user.id),))
            if cur.fetchone():
                return
            cur.execute("""
                INSERT INTO users (user_id, username, first_name, last_name, first_used)
                VALUES (%s,%s,%s,%s,%s)
            """, (
                str(user.id),
                user.username or "—",
                user.first_name or "—",
                user.last_name or "—",
                datetime.utcnow()
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

async def get_list(table):
    db = get_db()
    try:
        with db.cursor() as cur:
            cur.execute(f"SELECT url FROM {table}")
            rows = cur.fetchall()
        return "\n".join(f"🔗 {r['url']}" for r in rows) if rows else "—"
    finally:
        release_db(db)

# ================= COMMANDS =================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    log_user(user)

    caption = (
        f"👋 Привет, {user.first_name or 'друг'}!\n\n"
        f"🤖 Актуальные боты:\n{await get_bots_list()}\n\n"
        f"🌐 Актуальные сайты:\n{await get_list('sites')}\n\n"
        f"📣 Прайс-канал:\n{await get_list('price_channels')}\n\n"
        f"☎️ Контакт-канал:\n{await get_list('contact_channels')}\n\n"
        f"💼 Работа-канал:\n{await get_list('job_channels')}\n\n"
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

    if WELCOME_IMAGE:
        await safe_send(
            context.bot.send_photo,
            chat_id=update.effective_chat.id,
            photo=WELCOME_IMAGE,
            caption=caption
        )
    else:
        await safe_send(update.message.reply_text, caption)

async def info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        f"🤖 Боты:\n{await get_bots_list()}\n\n"
        f"🌐 Сайты:\n{await get_list('sites')}\n\n"
        f"📣 Прайс-канал:\n{await get_list('price_channels')}\n\n"
        f"☎️ Контакт-канал:\n{await get_list('contact_channels')}\n\n"
        f"💼 Работа-канал:\n{await get_list('job_channels')}"
    )
    await safe_send(update.message.reply_text, text)

# ================= LINK / PROTECT =================
# (ЭТИ ФУНКЦИИ НЕ ИЗМЕНЯЛИСЬ ПО СМЫСЛУ — КАК В ТВОЁМ КОДЕ)

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

    invite = await context.bot.create_chat_invite_link(
        chat_id=int(chat_id),
        expire_date=now + LINK_EXPIRE,
        member_limit=1
    )

    db = get_db()
    try:
        with db.cursor() as cur:
            cur.execute("""
                INSERT INTO active_links VALUES (%s,%s,%s)
                ON CONFLICT (user_id) DO UPDATE
                SET invite_link=EXCLUDED.invite_link, expire=EXCLUDED.expire
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

async def protect_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    member = update.chat_member
    if member.new_chat_member.status not in ("member", "restricted"):
        return

    user_id = str(member.new_chat_member.user.id)
    invite_link = getattr(member.invite_link, "invite_link", None)
    now = int(time.time())

    db = get_db()
    try:
        with db.cursor() as cur:
            cur.execute("SELECT invite_link, expire FROM active_links WHERE user_id=%s", (user_id,))
            row = cur.fetchone()
    finally:
        release_db(db)

    if not row or invite_link != row["invite_link"] or now > row["expire"] + LINK_GRACE:
        try:
            await context.bot.ban_chat_member(member.chat.id, int(user_id))
            await context.bot.unban_chat_member(member.chat.id, int(user_id))
        except:
            pass
        return

    db = get_db()
    try:
        with db.cursor() as cur:
            cur.execute("DELETE FROM active_links WHERE user_id=%s", (user_id,))
        db.commit()
    finally:
        release_db(db)

# ================= ADMIN COMMANDS =================
async def setchat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private" or not is_admin(update.effective_user.id) or not context.args:
        return
    set_setting("private_chat_id", context.args[0])
    await safe_send(update.message.reply_text, "✅ Чат установлен")

async def add_generic(update, context, table):
    if update.effective_chat.type != "private" or not is_admin(update.effective_user.id) or not context.args:
        return
    db = get_db()
    try:
        with db.cursor() as cur:
            cur.execute(
                f"INSERT INTO {table} (url) VALUES (%s) ON CONFLICT DO NOTHING",
                (context.args[0],)
            )
        db.commit()
    finally:
        release_db(db)
    await safe_send(update.message.reply_text, "✅ Добавлено")

async def remove_generic(update, context, table):
    if update.effective_chat.type != "private" or not is_admin(update.effective_user.id) or not context.args:
        return
    db = get_db()
    try:
        with db.cursor() as cur:
            cur.execute(
                f"DELETE FROM {table} WHERE url=%s",
                (context.args[0],)
            )
        db.commit()
    finally:
        release_db(db)
    await safe_send(update.message.reply_text, "🗑 Удалено")

async def settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private" or not is_admin(update.effective_user.id):
        return
    text = (
        f"📋 Чат: {get_setting('private_chat_id')}\n\n"
        f"🤖 Боты:\n{await get_bots_list()}\n\n"
        f"🌐 Сайты:\n{await get_list('sites')}\n\n"
        f"📣 Прайс:\n{await get_list('price_channels')}\n\n"
        f"☎️ Контакты:\n{await get_list('contact_channels')}\n\n"
        f"💼 Работа:\n{await get_list('job_channels')}"
    )
    await safe_send(update.message.reply_text, text)

async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private" or not is_admin(update.effective_user.id) or not context.args:
        return
    text = " ".join(context.args)

    db = get_db()
    try:
        with db.cursor() as cur:
            cur.execute("SELECT user_id FROM users")
            users = [int(r["user_id"]) for r in cur.fetchall()]
    finally:
        release_db(db)

    for uid in users:
        await safe_send(context.bot.send_message, chat_id=uid, text=text)

    await safe_send(update.message.reply_text, "✅ Рассылка завершена")

# ================= MAIN =================
def main():
    global DB_POOL
    DB_POOL = SimpleConnectionPool(1, 10, dsn=DATABASE_URL, cursor_factory=RealDictCursor)
    init_db()

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("info", info))
    app.add_handler(CommandHandler("link", link))

    app.add_handler(CommandHandler("bots", lambda u, c: safe_send(u.message.reply_text, f"🤖 Боты:\n{asyncio.run(get_bots_list())}")))
    app.add_handler(CommandHandler("sites", lambda u, c: safe_send(u.message.reply_text, f"🌐 Сайты:\n{asyncio.run(get_list('sites'))}")))

    app.add_handler(CommandHandler("setchat", setchat))
    app.add_handler(CommandHandler("addprice", lambda u, c: add_generic(u, c, "price_channels")))
    app.add_handler(CommandHandler("removeprice", lambda u, c: remove_generic(u, c, "price_channels")))
    app.add_handler(CommandHandler("addcontact", lambda u, c: add_generic(u, c, "contact_channels")))
    app.add_handler(CommandHandler("removecontact", lambda u, c: remove_generic(u, c, "contact_channels")))
    app.add_handler(CommandHandler("addjob", lambda u, c: add_generic(u, c, "job_channels")))
    app.add_handler(CommandHandler("removejob", lambda u, c: remove_generic(u, c, "job_channels")))
    app.add_handler(CommandHandler("settings", settings))
    app.add_handler(CommandHandler("broadcast", broadcast))

    app.add_handler(ChatMemberHandler(protect_chat, ChatMemberHandler.CHAT_MEMBER))

    print("🚀 Бот запущен (СТАБИЛЬНЫЙ ФИНАЛ)")
    app.run_polling()

if __name__ == "__main__":
    main()

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
        "• /info — список всех ресурсов 🌐"
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
        return "\n".join(f"💰 {r['url']}" for r in rows) if rows else "—"
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
            f"💰 Прайс-канал:\n{price_list}\n\n"
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

async def link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # полностью оригинальный код команды /link
    user = update.effective_user
    user_id = str(user.id)
    log_user(user)
    now = int(time.time())

    if update.effective_chat.type != "private":
        return await safe_send(update.message.reply_text, "❌ Эта команда доступна только в ЛС бота.")

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

async def info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    bots_list = await get_bots_list()
    sites_list = await get_sites_list()
    price_list = await get_price_list()
    contact_list = await get_contact_list()
    job_list = await get_job_list()
    await safe_send(update.message.reply_text,
        f"🤖 Боты:\n{bots_list}\n\n"
        f"🌐 Сайты:\n{sites_list}\n\n"
        f"💰 Прайс-канал:\n{price_list}\n\n"
        f"📞 Контакт-канал:\n{contact_list}\n\n"
        f"💼 Работа-канал:\n{job_list}"
        + user_commands_hint()
    )

# ================= ADMIN =================
# все админ-команды полностью копированы из оригинала и расширены на новые блоки
async def setchat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private" or not is_admin(update.effective_user.id) or not context.args:
        return
    set_setting("private_chat_id", context.args[0])
    await safe_send(update.message.reply_text, "✅ Чат установлен")

async def addbot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private" or not is_admin(update.effective_user.id) or not context.args:
        return
    db = get_db()
    try:
        with db.cursor() as cur:
            cur.execute("INSERT INTO bots (username) VALUES (%s) ON CONFLICT DO NOTHING", (context.args[0],))
        db.commit()
    finally:
        release_db(db)
    await safe_send(update.message.reply_text, "✅ Бот добавлен")

async def removebot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private" or not is_admin(update.effective_user.id) or not context.args:
        return
    db = get_db()
    try:
        with db.cursor() as cur:
            cur.execute("DELETE FROM bots WHERE username=%s", (context.args[0],))
        db.commit()
    finally:
        release_db(db)
    await safe_send(update.message.reply_text, "🗑 Бот удалён")

async def addsite(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private" or not is_admin(update.effective_user.id) or not context.args:
        return
    db = get_db()
    try:
        with db.cursor() as cur:
            cur.execute("INSERT INTO sites (url) VALUES (%s) ON CONFLICT DO NOTHING", (context.args[0],))
        db.commit()
    finally:
        release_db(db)
    await safe_send(update.message.reply_text, "✅ Сайт добавлен")

async def removesite(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private" or not is_admin(update.effective_user.id) or not context.args:
        return
    db = get_db()
    try:
        with db.cursor() as cur:
            cur.execute("DELETE FROM sites WHERE url=%s", (context.args[0],))
        db.commit()
    finally:
        release_db(db)
    await safe_send(update.message.reply_text, "🗑 Сайт удалён")

# ======= новые админ-команды для 3 блоков ======
async def addprice(update: Update, context: ContextTypes.DEFAULT_TYPE):
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

async def removeprice(update: Update, context: ContextTypes.DEFAULT_TYPE):
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

async def addcontact(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private" or not is_admin(update.effective_user.id) or not context.args:
        return
    db = get_db()
    try:
        with db.cursor() as cur:
            cur.execute("INSERT INTO contact_channels (url) VALUES (%s) ON CONFLICT DO NOTHING", (context.args[0],))
        db.commit()
    finally:
        release_db(db)
    await safe_send(update.message.reply_text, "✅ Контакт-канал добавлен")

async def removecontact(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private" or not is_admin(update.effective_user.id) or not context.args:
        return
    db = get_db()
    try:
        with db.cursor() as cur:
            cur.execute("DELETE FROM contact_channels WHERE url=%s", (context.args[0],))
        db.commit()
    finally:
        release_db(db)
    await safe_send(update.message.reply_text, "🗑 Контакт-канал удалён")

async def addjob(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private" or not is_admin(update.effective_user.id) or not context.args:
        return
    db = get_db()
    try:
        with db.cursor() as cur:
            cur.execute("INSERT INTO job_channels (url) VALUES (%s) ON CONFLICT DO NOTHING", (context.args[0],))
        db.commit()
    finally:
        release_db(db)
    await safe_send(update.message.reply_text, "✅ Работа-канал добавлен")

async def removejob(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private" or not is_admin(update.effective_user.id) or not context.args:
        return
    db = get_db()
    try:
        with db.cursor() as cur:
            cur.execute("DELETE FROM job_channels WHERE url=%s", (context.args[0],))
        db.commit()
    finally:
        release_db(db)
    await safe_send(update.message.reply_text, "🗑 Работа-канал удалён")

# ================= ANTI-SLIV =================
async def protect_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    member = update.chat_member
    if member.new_chat_member.status not in ("left", "kicked"):
        return
    # можно добавить защиту от выхода/удаления

# ================= MAIN =================
def main():
    global DB_POOL
    DB_POOL = SimpleConnectionPool(1, 10, dsn=DATABASE_URL, cursor_factory=RealDictCursor)
    init_db()

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # USER
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("link", link))
    app.add_handler(CommandHandler("info", info))

    # ADMIN
    app.add_handler(CommandHandler("setchat", setchat))
    app.add_handler(CommandHandler("addbot", addbot))
    app.add_handler(CommandHandler("removebot", removebot))
    app.add_handler(CommandHandler("addsite", addsite))
    app.add_handler(CommandHandler("removesite", removesite))
    app.add_handler(CommandHandler("addprice", addprice))
    app.add_handler(CommandHandler("removeprice", removeprice))
    app.add_handler(CommandHandler("addcontact", addcontact))
    app.add_handler(CommandHandler("removecontact", removecontact))
    app.add_handler(CommandHandler("addjob", addjob))
    app.add_handler(CommandHandler("removejob", removejob))
    app.add_handler(CommandHandler("settings", settings))
    app.add_handler(CommandHandler("broadcast", broadcast))

    
    app.add_handler(ChatMemberHandler(protect_chat, ChatMemberHandler.MY_CHAT_MEMBER))

    print("✅ Bot started")
    app.run_polling()

if __name__ == "__main__":
    main()

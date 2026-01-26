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
                CREATE TABLE IF NOT EXISTS price_channels (
                    url TEXT PRIMARY KEY
                );
                CREATE TABLE IF NOT EXISTS contact_channels (
                    url TEXT PRIMARY KEY
                );
                CREATE TABLE IF NOT EXISTS job_channels (
                    url TEXT PRIMARY KEY
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
        "• /info — актуальные боты, сайты и каналы 🌐"
    )

# ================= LISTS =================
def fetch_list(table):
    db = get_db()
    try:
        with db.cursor() as cur:
            cur.execute(f"SELECT * FROM {table}")
            rows = cur.fetchall()
        return rows
    finally:
        release_db(db)

async def get_bots_list():
    rows = fetch_list("bots")
    return "\n".join(f"🟢 {r['username']}" for r in rows) if rows else "—"

async def get_sites_list():
    rows = fetch_list("sites")
    return "\n".join(f"🔗 {r['url']}" for r in rows) if rows else "—"

async def get_price_list():
    rows = fetch_list("price_channels")
    return "\n".join(f"💰 {r['url']}" for r in rows) if rows else "—"

async def get_contact_list():
    rows = fetch_list("contact_channels")
    return "\n".join(f"📞 {r['url']}" for r in rows) if rows else "—"

async def get_job_list():
    rows = fetch_list("job_channels")
    return "\n".join(f"💼 {r['url']}" for r in rows) if rows else "—"

# ================= COMMANDS =================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        return  # только ЛС

    user = update.effective_user
    log_user(user)

    bots_list = await get_bots_list()
    sites_list = await get_sites_list()
    price_list = await get_price_list()
    contact_list = await get_contact_list()
    job_list = await get_job_list()

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

    if is_admin(user.id):
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
            "• /broadcast <текст>"
        )
    else:
        caption += user_commands_hint()

    if WELCOME_IMAGE:
        await safe_send(
            context.bot.send_photo,
            chat_id=update.effective_chat.id,
            photo=WELCOME_IMAGE,
            caption=caption
        )
    else:
        await safe_send(update.message.reply_text, caption)

# ========================= /link =========================
async def link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        return await safe_send(update.message.reply_text, "❌ Команда доступна только в ЛС.")

    user = update.effective_user
    user_id = str(user.id)
    log_user(user)
    now = int(time.time())

    db = get_db()
    try:
        with db.cursor() as cur:
            # проверяем, когда последний раз выдавалась ссылка
            cur.execute("SELECT timestamp FROM last_requests WHERE user_id=%s", (user_id,))
            last = cur.fetchone()
            if last and now - last["timestamp"] < LINK_COOLDOWN:
                remaining = LINK_COOLDOWN - (now - last["timestamp"])
                return await safe_send(
                    update.message.reply_text,
                    f"❌ Подождите {remaining // 60} мин {remaining % 60} сек перед повторным запросом."
                )

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

            cur.execute("""
                INSERT INTO last_requests(user_id, timestamp) VALUES (%s,%s)
                ON CONFLICT (user_id) DO UPDATE SET timestamp=EXCLUDED.timestamp
            """, (user_id, now))
            cur.execute("""
                INSERT INTO active_links(user_id, invite_link, expire) VALUES (%s,%s,%s)
                ON CONFLICT (user_id) DO UPDATE
                SET invite_link=EXCLUDED.invite_link, expire=EXCLUDED.expire
            """, (user_id, invite.invite_link, now + LINK_EXPIRE))
        db.commit()
    finally:
        release_db(db)

    await safe_send(
        update.message.reply_text,
        f"✅ Ссылка готова! ⏳ {LINK_EXPIRE} секунд.",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("🚪 Войти", url=invite.invite_link)]]
        )
    )

# ========================= info =========================
async def info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        return

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
    )

# ================= ADMIN ADD/REMOVE =================
def add_remove_handler(command, table, column):
    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_chat.type != "private":
            return
        if not is_admin(update.effective_user.id):
            return
        if not context.args:
            return await safe_send(update.message.reply_text, f"❌ Укажите значение для {command}")

        value = context.args[0]
        db = get_db()
        try:
            with db.cursor() as cur:
                if command.startswith("add"):
                    cur.execute(f"INSERT INTO {table} ({column}) VALUES (%s) ON CONFLICT DO NOTHING", (value,))
                else:
                    cur.execute(f"DELETE FROM {table} WHERE {column}=%s", (value,))
            db.commit()
        finally:
            release_db(db)
        await safe_send(update.message.reply_text, f"✅ {command} выполнен: {value}")
    return handler

# ========================= ИСПРАВЛЕННАЯ РАССЫЛКА =========================
async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        return
    if not is_admin(update.effective_user.id):
        return
    if not context.args:
        return await safe_send(update.message.reply_text, "❌ Укажите текст")

    text = " ".join(context.args)
    await safe_send(update.message.reply_text, "📤 Рассылка запущена...")

    async def _send_messages():
        db = get_db()
        try:
            with db.cursor() as cur:
                cur.execute("SELECT user_id FROM users")
                rows = cur.fetchall()
        finally:
            release_db(db)

        sent, failed = 0, 0
        for r in rows:
            try:
                await safe_send(context.bot.send_message, int(r["user_id"]), text)
                sent += 1
            except:
                failed += 1
            await asyncio.sleep(0.05)

        await safe_send(update.message.reply_text, f"✅ Рассылка завершена. Отправлено: {sent}, Ошибок: {failed}")

    asyncio.create_task(_send_messages())

# ================= CHAT PROTECT =================
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

# ================= MAIN =================
def main():
    global DB_POOL
    DB_POOL = SimpleConnectionPool(1, 20, dsn=DATABASE_URL, cursor_factory=RealDictCursor)
    init_db()

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # USER
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("link", link))
    app.add_handler(CommandHandler("info", info))

    # ADMIN
    app.add_handler(CommandHandler("setchat", lambda u,c: set_setting("private_chat_id", c.args[0]) if is_admin(u.effective_user.id) else None))
    tables = [
        ("addbot","bots","username"), ("removebot","bots","username"),
        ("addsite","sites","url"), ("removesite","sites","url"),
        ("addprice","price_channels","url"), ("removeprice","price_channels","url"),
        ("addcontact","contact_channels","url"), ("removecontact","contact_channels","url"),
        ("addjob","job_channels","url"), ("removejob","job_channels","url"),
    ]
    for cmd, table, col in tables:
        app.add_handler(CommandHandler(cmd, add_remove_handler(cmd, table, col)))

    app.add_handler(CommandHandler("broadcast", broadcast))

    app.add_handler(ChatMemberHandler(protect_chat, ChatMemberHandler.CHAT_MEMBER))

    app.run_polling()

if __name__ == "__main__":
    main()

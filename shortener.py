import os
import re
import logging
import httpx
from datetime import datetime, timezone
from dotenv import load_dotenv
from pymongo import MongoClient

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters
)

from health_check import start_health_server

# ---------------- LOGGING ----------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
log = logging.getLogger(__name__)

# Suppress noisy HTTP / library logs
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)
logging.getLogger("apscheduler").setLevel(logging.WARNING)

# ---------------- ENV ----------------

load_dotenv()

BOT_TOKEN         = os.getenv("BOT_TOKEN")
SHORTENER_DOMAIN  = os.getenv("SHORTENER_DOMAIN")   # e.g. example.com
SHORTENER_API_KEY = os.getenv("SHORTENER_API_KEY")
MONGO_URI         = os.getenv("MONGO_URI")
DEST_CHANNEL_ID   = int(os.getenv("DEST_CHANNEL_ID", "0"))
HOW_TO_OPEN_URL   = os.getenv("HOW_TO_OPEN_URL")    # 👀 HOW TO OPEN button link
JOIN_BACKUP_URL   = os.getenv("JOIN_BACKUP_URL")     # ✅ Join Backup button link

# ---------------- DATABASE ----------------

client    = MongoClient(MONGO_URI)
db        = client["viralbox_db"]
links_col = db["links"]

# ---------------- DB ----------------

def save_link(long_url, short_url, user_id):
    links_col.insert_one({
        "userId":     int(user_id),
        "longURL":    long_url,
        "shortURL":   short_url,
        "created_at": datetime.now(timezone.utc)
    })
    log.info(f"[DB] Saved | {long_url} → {short_url}")

# ---------------- UTILS ----------------

def extract_urls(text: str) -> list[str]:
    urls = re.findall(r"https?://[^\s]+", text)
    log.info(f"[EXTRACT] Found {len(urls)} URL(s): {urls}")
    return urls


async def shorten_url(url: str) -> str | None:
    api_url = f"https://{SHORTENER_DOMAIN}/api"
    params  = {"api": SHORTENER_API_KEY, "url": url}
    log.info(f"[SHORTEN] Shortening: {url}")
    try:
        async with httpx.AsyncClient() as c:
            r    = await c.get(api_url, params=params, timeout=15)
            data = r.json()
            log.info(f"[SHORTEN] Response: {data}")
            if data.get("status") == "success":
                short = data.get("shortenedUrl")
                log.info(f"[SHORTEN] OK → {short}")
                return short
            else:
                log.warning(f"[SHORTEN] Non-success response: {data}")
    except Exception as e:
        log.error(f"[SHORTEN] Exception: {e}")
    return None


def build_caption(short_links: list[str]) -> str:
    lines = ["📥 Download Links/👀Watch Online\n"]

    for i, link in enumerate(short_links, start=1):
        lines.append(f"Video {i}. 👉{link}")

    lines.append(
        "\n▰▱▱▱▱▱▱▱▱▱▱▱▱▱▱▰\n"
        "🚫⛔️ Note: We Don't Leak Anything here, "
        "We Just Collect & Share from All Over the internet\n"
        "Thanks🔎"
    )

    return "\n".join(lines)


def build_keyboard() -> InlineKeyboardMarkup:
    buttons = [
        InlineKeyboardButton("👀 HOW TO OPEN", url=HOW_TO_OPEN_URL),
        InlineKeyboardButton("✅ Join Backup",  url=JOIN_BACKUP_URL),
    ]
    return InlineKeyboardMarkup([buttons])


async def process_and_shorten(text: str, user_id: int) -> list[str] | None:
    urls = extract_urls(text)
    if not urls:
        log.info("[PROCESS] No URLs in text.")
        return None

    short_links = []
    for url in urls:
        short = await shorten_url(url)
        if short:
            save_link(url, short, user_id)
            short_links.append(short)
        else:
            log.warning(f"[PROCESS] Could not shorten: {url}")

    if not short_links:
        log.warning("[PROCESS] No URLs shortened successfully.")
        return None

    return short_links

# ---------------- COMMANDS ----------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = update.effective_user.first_name or "User"
    log.info(f"[CMD] /start | user={update.effective_user.id} ({name})")
    await update.message.reply_text(
        f"👋 Hello *{name}*!\n\n"
        "Send me any link or media with a caption — "
        "I'll shorten the links and post it to the channel.",
        parse_mode="Markdown"
    )

# ---------------- MESSAGE HANDLER ----------------

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if not message:
        log.warning("[MSG] update.message is None, skipping.")
        return

    user_id    = update.effective_user.id
    message_id = message.message_id
    text       = message.text or message.caption

    log.info(f"[MSG] Received | user={user_id} | msg_id={message_id} | text={repr(text)}")

    if not text:
        log.info(f"[MSG] No text/caption. Deleting msg_id={message_id}.")
        await message.delete()
        return

    short_links = await process_and_shorten(text, user_id)

    if not short_links:
        log.info(f"[MSG] No links shortened. Deleting msg_id={message_id}.")
        await message.delete()
        return

    caption  = build_caption(short_links)
    keyboard = build_keyboard()

    # ---- Post to destination channel ----
    try:
        if message.photo:
            log.info(f"[POST] Sending photo → channel {DEST_CHANNEL_ID}")
            await context.bot.send_photo(
                chat_id=DEST_CHANNEL_ID,
                photo=message.photo[-1].file_id,
                caption=caption,
                reply_markup=keyboard
            )
        elif message.video:
            log.info(f"[POST] Sending video → channel {DEST_CHANNEL_ID}")
            await context.bot.send_video(
                chat_id=DEST_CHANNEL_ID,
                video=message.video.file_id,
                caption=caption,
                reply_markup=keyboard
            )
        elif message.document:
            log.info(f"[POST] Sending document → channel {DEST_CHANNEL_ID}")
            await context.bot.send_document(
                chat_id=DEST_CHANNEL_ID,
                document=message.document.file_id,
                caption=caption,
                reply_markup=keyboard
            )
        elif message.animation:
            log.info(f"[POST] Sending animation → channel {DEST_CHANNEL_ID}")
            await context.bot.send_animation(
                chat_id=DEST_CHANNEL_ID,
                animation=message.animation.file_id,
                caption=caption,
                reply_markup=keyboard
            )
        else:
            log.info(f"[POST] Sending text → channel {DEST_CHANNEL_ID}")
            await context.bot.send_message(
                chat_id=DEST_CHANNEL_ID,
                text=caption,
                reply_markup=keyboard
            )

        log.info("[POST] Successfully posted to channel.")

    except Exception as e:
        log.error(f"[POST] Failed to post to channel: {e}")
        return  # Don't delete original if posting failed

    # ---- Delete user's original message ----
    try:
        await message.delete()
        log.info(f"[DELETE] Deleted msg_id={message_id} from user={user_id}")
    except Exception as e:
        log.error(f"[DELETE] Failed to delete msg_id={message_id}: {e}")

# ---------------- MAIN ----------------

def main():
    log.info("[BOOT] Starting health check server...")
    start_health_server()

    log.info("[BOOT] Building Telegram app...")
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(
        MessageHandler(
            filters.TEXT
            | filters.PHOTO
            | filters.VIDEO
            | filters.Document.ALL
            | filters.ANIMATION,
            handle_message
        )
    )

    log.info("[BOOT] Bot started. Polling...")
    app.run_polling()


if __name__ == "__main__":
    main()

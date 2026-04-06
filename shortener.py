import os
import re
import logging
import asyncio
import httpx
from datetime import datetime, timezone
from dotenv import load_dotenv
from pymongo import MongoClient

from telegram import Update
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

logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)
logging.getLogger("apscheduler").setLevel(logging.WARNING)

# ---------------- ENV ----------------

load_dotenv()

BOT_TOKEN         = os.getenv("BOT_TOKEN")
SHORTENER_DOMAIN  = os.getenv("SHORTENER_DOMAIN")
SHORTENER_API_KEY = os.getenv("SHORTENER_API_KEY")
MONGO_URI         = os.getenv("MONGO_URI")
DEST_CHANNEL_ID   = int(os.getenv("DEST_CHANNEL_ID", "0"))

# ---------------- DATABASE ----------------

client    = MongoClient(MONGO_URI)
db        = client["viralbox_db"]
links_col = db["links"]

# ---------------- QUEUE ----------------

# Each item: (message, context)
message_queue: asyncio.Queue = asyncio.Queue()

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
                log.warning(f"[SHORTEN] Non-success: {data}")
    except Exception as e:
        log.error(f"[SHORTEN] Exception: {e}")
    return None

# ---------------- QUEUE WORKER ----------------

async def queue_worker():
    """Process one message every 5 seconds."""
    log.info("[WORKER] Queue worker started.")
    while True:
        message, context = await message_queue.get()
        try:
            await process_message(message, context)
        except Exception as e:
            log.error(f"[WORKER] Unhandled error: {e}")
        finally:
            message_queue.task_done()

        # Wait 5 seconds before processing next
        log.info("[WORKER] Waiting 5 seconds before next message...")
        await asyncio.sleep(5)


async def process_message(message, context):
    user_id    = message.from_user.id
    message_id = message.message_id
    text       = message.text or message.caption

    if not text:
        log.info(f"[PROCESS] No text. Deleting msg_id={message_id}.")
        await message.delete()
        return

    urls = extract_urls(text)
    if not urls:
        log.info(f"[PROCESS] No URLs. Deleting msg_id={message_id}.")
        await message.delete()
        return

    # Shorten first URL found (one link per message expected)
    short_links = []
    for url in urls:
        short = await shorten_url(url)
        if short:
            save_link(url, short, user_id)
            short_links.append(short)
        else:
            log.warning(f"[PROCESS] Could not shorten: {url}")

    if not short_links:
        log.warning(f"[PROCESS] No URLs shortened. Deleting msg_id={message_id}.")
        await message.delete()
        return

    # Build simple caption — just the shortened link(s)
    caption = "\n".join(short_links)

    # Post to channel
    try:
        if message.photo:
            log.info(f"[POST] Sending photo → channel {DEST_CHANNEL_ID}")
            await context.bot.send_photo(
                chat_id=DEST_CHANNEL_ID,
                photo=message.photo[-1].file_id,
                caption=caption
            )
        elif message.video:
            log.info(f"[POST] Sending video → channel {DEST_CHANNEL_ID}")
            await context.bot.send_video(
                chat_id=DEST_CHANNEL_ID,
                video=message.video.file_id,
                caption=caption
            )
        elif message.document:
            log.info(f"[POST] Sending document → channel {DEST_CHANNEL_ID}")
            await context.bot.send_document(
                chat_id=DEST_CHANNEL_ID,
                document=message.document.file_id,
                caption=caption
            )
        elif message.animation:
            log.info(f"[POST] Sending animation → channel {DEST_CHANNEL_ID}")
            await context.bot.send_animation(
                chat_id=DEST_CHANNEL_ID,
                animation=message.animation.file_id,
                caption=caption
            )
        else:
            log.info(f"[POST] Sending text → channel {DEST_CHANNEL_ID}")
            await context.bot.send_message(
                chat_id=DEST_CHANNEL_ID,
                text=caption
            )
        log.info("[POST] Successfully posted to channel.")
    except Exception as e:
        log.error(f"[POST] Failed: {e}")
        return  # Don't delete if post failed

    # Delete original message
    try:
        await message.delete()
        log.info(f"[DELETE] Deleted msg_id={message_id} from user={user_id}")
    except Exception as e:
        log.error(f"[DELETE] Failed to delete msg_id={message_id}: {e}")

# ---------------- HANDLERS ----------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = update.effective_user.first_name or "User"
    log.info(f"[CMD] /start | user={update.effective_user.id} ({name})")
    await update.message.reply_text(
        f"👋 Hello *{name}*!\n\n"
        "Send me any link or media with a caption — "
        "I'll shorten the links and post to the channel.",
        parse_mode="Markdown"
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if not message:
        return

    log.info(f"[QUEUE] Queued msg_id={message.message_id} | queue size={message_queue.qsize() + 1}")
    await message_queue.put((message, context))

# ---------------- MAIN ----------------

async def post_init(app):
    """Start queue worker after bot is initialized."""
    asyncio.create_task(queue_worker())
    log.info("[BOOT] Queue worker task created.")


def main():
    log.info("[BOOT] Starting health check server...")
    start_health_server()

    log.info("[BOOT] Building Telegram app...")
    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

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

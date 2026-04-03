import os
import re
import logging
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

# ---------------- ENV ----------------

load_dotenv()

BOT_TOKEN           = os.getenv("BOT_TOKEN")
SHORTENER_DOMAIN    = os.getenv("SHORTENER_DOMAIN")
SHORTENER_API_KEY   = os.getenv("SHORTENER_API_KEY")
MONGO_URI           = os.getenv("MONGO_URI")
DEST_CHANNEL_ID     = int(os.getenv("DEST_CHANNEL_ID", "0"))

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
    log.info(f"[DB] Saved link | user={user_id} | {long_url} → {short_url}")

# ---------------- UTILS ----------------

def extract_urls(text: str) -> list[str]:
    urls = re.findall(r"https?://[^\s]+", text)
    log.info(f"[EXTRACT] Found {len(urls)} URL(s): {urls}")
    return urls


async def shorten_url(url: str) -> str | None:
    api_url = f"https://{SHORTENER_DOMAIN}/api"
    params  = {"api": SHORTENER_API_KEY, "url": url}
    log.info(f"[SHORTEN] Requesting short URL for: {url}")
    try:
        async with httpx.AsyncClient() as c:
            r    = await c.get(api_url, params=params, timeout=15)
            data = r.json()
            log.info(f"[SHORTEN] API response: {data}")
            if data.get("status") == "success":
                short = data.get("shortenedUrl")
                log.info(f"[SHORTEN] Success: {url} → {short}")
                return short
            else:
                log.warning(f"[SHORTEN] API returned non-success: {data}")
    except Exception as e:
        log.error(f"[SHORTEN] Exception for {url}: {e}")
    return None


async def process_text(text: str, user_id: int) -> str | None:
    urls = extract_urls(text)
    if not urls:
        log.info(f"[PROCESS] No URLs found in text. Skipping.")
        return None

    result        = text
    any_shortened = False

    for url in urls:
        short = await shorten_url(url)
        if short:
            save_link(url, short, user_id)
            result = result.replace(url, short)
            any_shortened = True
        else:
            log.warning(f"[PROCESS] Could not shorten: {url}")

    if not any_shortened:
        log.warning(f"[PROCESS] No URL was shortened successfully.")
        return None

    log.info(f"[PROCESS] Final text ready for posting.")
    return result

# ---------------- COMMANDS ----------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = update.effective_user.first_name or "User"
    log.info(f"[CMD] /start from user={update.effective_user.id} ({name})")
    await update.message.reply_text(
        f"👋 Hello *{name}*!\n\n"
        "Just send me any link or media with a caption — "
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
        log.info(f"[MSG] No text/caption found. Deleting msg_id={message_id}.")
        await message.delete()
        return

    new_text = await process_text(text, user_id)

    if not new_text:
        log.info(f"[MSG] No URL processed. Deleting msg_id={message_id}.")
        await message.delete()
        return

    # ---- Post to destination channel ----
    try:
        if message.photo:
            log.info(f"[POST] Sending photo to channel {DEST_CHANNEL_ID}")
            await context.bot.send_photo(
                chat_id=DEST_CHANNEL_ID,
                photo=message.photo[-1].file_id,
                caption=new_text
            )
        elif message.video:
            log.info(f"[POST] Sending video to channel {DEST_CHANNEL_ID}")
            await context.bot.send_video(
                chat_id=DEST_CHANNEL_ID,
                video=message.video.file_id,
                caption=new_text
            )
        elif message.document:
            log.info(f"[POST] Sending document to channel {DEST_CHANNEL_ID}")
            await context.bot.send_document(
                chat_id=DEST_CHANNEL_ID,
                document=message.document.file_id,
                caption=new_text
            )
        elif message.animation:
            log.info(f"[POST] Sending animation to channel {DEST_CHANNEL_ID}")
            await context.bot.send_animation(
                chat_id=DEST_CHANNEL_ID,
                animation=message.animation.file_id,
                caption=new_text
            )
        else:
            log.info(f"[POST] Sending text message to channel {DEST_CHANNEL_ID}")
            await context.bot.send_message(
                chat_id=DEST_CHANNEL_ID,
                text=new_text
            )
        log.info(f"[POST] Successfully posted to channel.")
    except Exception as e:
        log.error(f"[POST] Failed to post to channel: {e}")
        return  # Don't delete original if posting failed

    # ---- Delete user's original message ----
    try:
        await message.delete()
        log.info(f"[DELETE] Deleted original msg_id={message_id} from user={user_id}")
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

    log.info("[BOOT] Shortener Bot Started. Polling...")
    app.run_polling()


if __name__ == "__main__":
    main()

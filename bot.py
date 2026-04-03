import os
import re
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

# ---------------- ENV ----------------

load_dotenv()

BOT_TOKEN           = os.getenv("BOT_TOKEN")
SHORTENER_DOMAIN    = os.getenv("SHORTENER_DOMAIN")   # e.g. example.com
SHORTENER_API_KEY   = os.getenv("SHORTENER_API_KEY")  # fixed single API key
MONGO_URI           = os.getenv("MONGO_URI")
DEST_CHANNEL_ID     = int(os.getenv("DEST_CHANNEL_ID", "0"))  # e.g. -1001234567890

# ---------------- DATABASE ----------------

client   = MongoClient(MONGO_URI)
db       = client["viralbox_db"]
links_col = db["links"]

# ---------------- DB ----------------

def save_link(long_url, short_url, user_id):
    links_col.insert_one({
        "userId":    int(user_id),
        "longURL":   long_url,
        "shortURL":  short_url,
        "created_at": datetime.now(timezone.utc)
    })

# ---------------- UTILS ----------------

def extract_urls(text: str) -> list[str]:
    return re.findall(r"https?://[^\s]+", text)


async def shorten_url(url: str) -> str | None:
    api_url = f"https://{SHORTENER_DOMAIN}/api"
    params  = {"api": SHORTENER_API_KEY, "url": url}
    try:
        async with httpx.AsyncClient() as c:
            r    = await c.get(api_url, params=params, timeout=15)
            data = r.json()
            if data.get("status") == "success":
                return data.get("shortenedUrl")
    except Exception:
        pass
    return None


async def process_text(text: str, user_id: int) -> str | None:
    """Replace all URLs in text with shortened versions. Returns None if no URL found."""
    urls = extract_urls(text)
    if not urls:
        return None

    result = text
    any_shortened = False

    for url in urls:
        short = await shorten_url(url)
        if short:
            save_link(url, short, user_id)
            result = result.replace(url, short)
            any_shortened = True

    return result if any_shortened else None

# ---------------- COMMANDS ----------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = update.effective_user.first_name or "User"
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
        return

    user_id = update.effective_user.id
    text    = message.text or message.caption

    # No text / no URL — silently delete and return
    if not text:
        await message.delete()
        return

    new_text = await process_text(text, user_id)

    if not new_text:
        # No URL found — delete original and do nothing
        await message.delete()
        return

    # ---- Post to destination channel with shortened links ----
    if message.photo:
        await context.bot.send_photo(
            chat_id=DEST_CHANNEL_ID,
            photo=message.photo[-1].file_id,
            caption=new_text
        )
    elif message.video:
        await context.bot.send_video(
            chat_id=DEST_CHANNEL_ID,
            video=message.video.file_id,
            caption=new_text
        )
    elif message.document:
        await context.bot.send_document(
            chat_id=DEST_CHANNEL_ID,
            document=message.document.file_id,
            caption=new_text
        )
    elif message.animation:
        await context.bot.send_animation(
            chat_id=DEST_CHANNEL_ID,
            animation=message.animation.file_id,
            caption=new_text
        )
    else:
        # Plain text message
        await context.bot.send_message(
            chat_id=DEST_CHANNEL_ID,
            text=new_text
        )

    # ---- Delete user's original message ----
    await message.delete()

# ---------------- MAIN ----------------

def main():
    start_health_server()

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

    print("Shortener Bot Started...")
    app.run_polling()


if __name__ == "__main__":
    main()

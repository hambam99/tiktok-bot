import os
import sys
import logging
import asyncio
import httpx
import random
import string
import time
import gc
from typing import Optional, List, Tuple
from quart import Quart
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
)
from telegram.request import HTTPXRequest
from hypercorn.config import Config as HyperConfig
from hypercorn.asyncio import serve

# ==============================================================================
# 1. LOGGING & CONFIGURATION
# ==============================================================================

logging.basicConfig(
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    level=logging.INFO
)
logger = logging.getLogger("IGUsernameScanner")

class BotConfig:
    """Central configuration management."""
    BOT_TOKEN: str = os.environ.get("BOT_TOKEN", "")
    PORT: int = int(os.environ.get("PORT", 10000))
    HTTP_TIMEOUT: float = 15.0
    
    # Default Browser Headers for Instagram API lookups
    IG_HEADERS = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "X-IG-App-ID": "936619743392459",
    }

if not BotConfig.BOT_TOKEN:
    logger.critical("FATAL: 'BOT_TOKEN' environment variable is missing!")
    sys.exit(1)

# ==============================================================================
# 2. WEB SERVER FOR HEALTH CHECKS (QUART)
# ==============================================================================

quart_app = Quart(__name__)
BOT_START_TIME = time.time()

@quart_app.route("/")
async def health_check():
    uptime = int(time.time() - BOT_START_TIME)
    return (
        f"🤖 Instagram Username Scanner Bot Operational\n"
        f"⏱️ Uptime: {uptime}s\n"
        f"📊 Status: Running",
        200
    )

@quart_app.route("/ping")
async def ping():
    return "PONG", 200

# ==============================================================================
# 3. GLOBAL SCANNER STATE MANAGER
# ==============================================================================

class ScannerState:
    is_scanning: bool = False
    scanner_task: Optional[asyncio.Task] = None
    scanned_count: int = 0
    available_found: int = 0
    current_target_chat: Optional[int] = None

state = ScannerState()

# ==============================================================================
# 4. INSTAGRAM CHECKER CORE ENGINE
# ==============================================================================

async def check_instagram_username(client: httpx.AsyncClient, username: str) -> Tuple[str, str]:
    """
    Checks an Instagram username status.
    Returns: ('AVAILABLE' | 'TAKEN' | 'RATE_LIMITED' | 'ERROR', detail_message)
    """
    clean_username = username.strip().lstrip("@").lower()
    
    # Validate Instagram username format rules
    if len(clean_username) < 3 or len(clean_username) > 30:
        return "INVALID", "Usernames must be between 3 and 30 characters."
    
    url = f"https://www.instagram.com/{clean_username}/"
    
    try:
        response = await client.get(url, headers=BotConfig.IG_HEADERS)
        
        # 1. Username Available (404 Not Found)
        if response.status_code == 404:
            return "AVAILABLE", clean_username
        
        # 2. Username Taken (200 OK)
        elif response.status_code == 200:
            return "TAKEN", clean_username
        
        # 3. Rate Limited / Protected (429 Too Many Requests or 302 Redirect to Login)
        elif response.status_code in (429, 302, 403):
            logger.warning("Instagram rate limit encountered. Auto-pausing silently for 15 minutes...")
            # SILENT PAUSE: Sleep 15 minutes without notifying Telegram chat
            await asyncio.sleep(900)
            return "RATE_LIMITED", clean_username
            
        else:
            return "ERROR", f"HTTP Status {response.status_code}"

    except httpx.RequestError as e:
        logger.error(f"Network error checking '{clean_username}': {e}")
        return "ERROR", str(e)

def generate_random_username(length: int = 5) -> str:
    """Generates a valid random Instagram username format."""
    chars = string.ascii_lowercase + string.digits + "._"
    # Ensure it doesn't start or end with a period
    middle_chars = [random.choice(chars) for _ in range(length - 2)]
    start_char = random.choice(string.ascii_lowercase)
    end_char = random.choice(string.ascii_lowercase + string.digits)
    return f"{start_char}{''.join(middle_chars)}{end_char}"

# ==============================================================================
# 5. TELEGRAM COMMAND HANDLERS
# ==============================================================================

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Provides instructions and bot command usage."""
    welcome_text = (
        "🔍 **Instagram Username Scanner Bot**\n\n"
        "⚡ **Available Commands:**\n"
        "• `/check <username>` — Check a single username instantly.\n"
        "• `/scan <user1> <user2> ...` — Check a list of usernames.\n"
        "• `/auto <length>` — Start automatic background scanner (e.g., `/auto 4`).\n"
        "• `/stop` — Stop active background scanning.\n"
        "• `/status` — View live scanning stats and uptime.\n\n"
        "💡 *Note: Rate limits are handled completely silently without spamming warnings.*"
    )
    await update.message.reply_text(welcome_text, parse_mode=ParseMode.MARKDOWN)

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Displays scanner runtime statistics."""
    uptime = int(time.time() - BOT_START_TIME)
    scanning_status = "🟢 Active Scanning" if state.is_scanning else "🔴 Idle"
    
    status_text = (
        "📊 **Scanner Status & Metrics**\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        f"⚙️ **State:** `{scanning_status}`\n"
        f"⏱️ **Uptime:** `{uptime}s`\n"
        f"🔍 **Total Checked:** `{state.scanned_count}`\n"
        f"✨ **Available Found:** `{state.available_found}`\n"
        f"🤫 **Rate Limit Notifications:** `DISABLED (Silent Auto-Pause Active)`"
    )
    await update.message.reply_text(status_text, parse_mode=ParseMode.MARKDOWN)

async def check_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles single or multiple manual username lookups."""
    if not context.args:
        await update.message.reply_text("❌ Please specify a username!\nExample: `/check cool_name`", parse_mode=ParseMode.MARKDOWN)
        return

    target_username = context.args[0]
    status_msg = await update.message.reply_text(f"🔍 Checking `@{target_username}`...", parse_mode=ParseMode.MARKDOWN)

    async with httpx.AsyncClient(timeout=BotConfig.HTTP_TIMEOUT, follow_redirects=True) as client:
        status, result = await check_instagram_username(client, target_username)

    state.scanned_count += 1

    if status == "AVAILABLE":
        state.available_found += 1
        claim_msg = (
            f"🎉 **AVAILABLE USERNAME FOUND!**\n\n"
            f"👉 **Handle:** `@{result}`\n"
            f"🔗 **Direct Link:** https://instagram.com/{result}\n\n"
            f"📌 **How to Claim:**\n"
            f"1. Open Instagram App.\n"
            f"2. Go to **Edit Profile** -> **Username**.\n"
            f"3. Type `@{result}` and click **Save** immediately."
        )
        await status_msg.edit_text(claim_msg, parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True)

    elif status == "TAKEN":
        await status_msg.edit_text(f"❌ Username `@{result}` is **TAKEN**.", parse_mode=ParseMode.MARKDOWN)

    elif status == "RATE_LIMITED":
        # Silently handled without alarm messages
        await status_msg.edit_text("⚠️ Instagram server check busy. Please try again in a few moments.", parse_mode=ParseMode.MARKDOWN)

    else:
        await status_msg.edit_text(f"⚠️ Could not verify `@{target_username}` ({result}).", parse_mode=ParseMode.MARKDOWN)

async def scan_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Scans a batch list of usernames passed in arguments."""
    if not context.args:
        await update.message.reply_text("❌ Usage: `/scan user1 user2 user3`", parse_mode=ParseMode.MARKDOWN)
        return

    usernames = context.args[:20]  # Limit batch to 20 at a time
    status_msg = await update.message.reply_text(f"🔄 Processing batch scan of {len(usernames)} usernames...", parse_mode=ParseMode.MARKDOWN)

    found_list = []
    async with httpx.AsyncClient(timeout=BotConfig.HTTP_TIMEOUT, follow_redirects=True) as client:
        for u in usernames:
            status, result = await check_instagram_username(client, u)
            state.scanned_count += 1
            
            if status == "AVAILABLE":
                state.available_found += 1
                found_list.append(result)
            
            # Short delay between manual batch items
            await asyncio.sleep(1.5)

    if found_list:
        found_str = "\n".join([f"• `@{name}`" for name in found_list])
        await status_msg.edit_text(
            f"🎉 **Batch Scan Finished!**\n\n**Available Handles Found:**\n{found_str}\n\n"
            f"Claim them inside your Instagram settings!",
            parse_mode=ParseMode.MARKDOWN
        )
    else:
        await status_msg.edit_text("❌ Batch scan complete. None of the provided usernames were available.", parse_mode=ParseMode.MARKDOWN)

# ==============================================================================
# 6. BACKGROUND AUTOMATIC SCANNER LOOP
# ==============================================================================

async def background_scanner_loop(context: ContextTypes.DEFAULT_TYPE, target_length: int, chat_id: int):
    """Runs continuous random username checks in background with silent rate-limiting."""
    logger.info(f"Starting auto-scanner loop for length {target_length}...")
    
    async with httpx.AsyncClient(timeout=BotConfig.HTTP_TIMEOUT, follow_redirects=True) as client:
        while state.is_scanning:
            target_username = generate_random_username(target_length)
            status, result = await check_instagram_username(client, target_username)
            state.scanned_count += 1

            if status == "AVAILABLE":
                state.available_found += 1
                alert_text = (
                    f"🎯 **AUTOMATIC SCANNER MATCH!**\n\n"
                    f"✨ **Handle:** `@{result}`\n"
                    f"🔗 **Link:** https://instagram.com/{result}\n\n"
                    f"📌 **Claim Steps:** Open Instagram -> Edit Profile -> Change Username to `@{result}`."
                )
                try:
                    await context.bot.send_message(chat_id=chat_id, text=alert_text, parse_mode=ParseMode.MARKDOWN)
                except Exception as e:
                    logger.error(f"Failed to deliver Telegram alert: {e}")

            # Memory cleanup and throttle interval
            gc.collect()
            await asyncio.sleep(2.0)  # Safe delay between random checks

async def auto_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Starts background random handle hunting."""
    if state.is_scanning:
        await update.message.reply_text("⚠️ Auto-scanner is already running! Use `/stop` to cancel it first.", parse_mode=ParseMode.MARKDOWN)
        return

    length = 5
    if context.args and context.args[0].isdigit():
        length = max(3, min(30, int(context.args[0])))

    state.is_scanning = True
    state.current_target_chat = update.effective_chat.id
    
    # Launch async background task
    state.scanner_task = asyncio.create_task(
        background_scanner_loop(context, length, update.effective_chat.id)
    )

    await update.message.reply_text(
        f"🚀 **Auto-Scanner Started!**\n"
        f"📏 **Target Length:** `{length}` characters\n"
        f"🤫 **Silent Mode:** Active (No rate-limit alerts will be sent).\n\n"
        f"Send `/stop` anytime to end scanning.",
        parse_mode=ParseMode.MARKDOWN
    )

async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Stops any running background scanner."""
    if not state.is_scanning:
        await update.message.reply_text("ℹ️ No background scanner is currently active.", parse_mode=ParseMode.MARKDOWN)
        return

    state.is_scanning = False
    if state.scanner_task:
        state.scanner_task.cancel()
        state.scanner_task = None

    await update.message.reply_text("🛑 **Auto-scanner stopped successfully.**", parse_mode=ParseMode.MARKDOWN)

# ==============================================================================
# 7. GLOBAL ERROR HANDLER
# ==============================================================================

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Uncaught exception encountered during execution:", exc_info=context.error)

# ==============================================================================
# 8. MAIN ENTRYPOINT
# ==============================================================================

async def main():
    request_kwargs = HTTPXRequest(
        connect_timeout=20.0,
        read_timeout=BotConfig.HTTP_TIMEOUT,
        write_timeout=BotConfig.HTTP_TIMEOUT,
        pool_timeout=20.0
    )

    telegram_app = (
        ApplicationBuilder()
        .token(BotConfig.BOT_TOKEN)
        .request(request_kwargs)
        .build()
    )

    # Register Command Handlers
    telegram_app.add_handler(CommandHandler("start", start_command))
    telegram_app.add_handler(CommandHandler("help", start_command))
    telegram_app.add_handler(CommandHandler("status", status_command))
    telegram_app.add_handler(CommandHandler("check", check_command))
    telegram_app.add_handler(CommandHandler("scan", scan_command))
    telegram_app.add_handler(CommandHandler("auto", auto_command))
    telegram_app.add_handler(CommandHandler("stop", stop_command))
    
    telegram_app.add_error_handler(error_handler)

    # Initialize Telegram Bot
    await telegram_app.initialize()
    await telegram_app.start()
    
    # Drop pending updates to avoid webhook conflict errors on server restart
    await telegram_app.updater.start_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)
    logger.info("Telegram Polling active & listening for scanner commands!")

    # Configure Web Server for Render / Cloud hosting
    hypercorn_config = HyperConfig()
    hypercorn_config.bind = [f"0.0.0.0:{BotConfig.PORT}"]
    hypercorn_config.shutdown_timeout = 5.0

    logger.info(f"Binding Quart web server to port {BotConfig.PORT}...")

    try:
        await serve(quart_app, hypercorn_config)
    finally:
        logger.info("Initiating graceful shutdown sequence...")
        state.is_scanning = False
        if state.scanner_task:
            state.scanner_task.cancel()
        await telegram_app.updater.stop()
        await telegram_app.stop()
        await telegram_app.shutdown()
        logger.info("Shutdown complete.")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot execution terminated by system signal.")

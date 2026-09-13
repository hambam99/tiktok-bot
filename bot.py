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
logger = logging.getLogger("MultiPlatformScanner")

class BotConfig:
    """Central configuration management."""
    BOT_TOKEN: str = os.environ.get("BOT_TOKEN", "")
    PORT: int = int(os.environ.get("PORT", 10000))
    HTTP_TIMEOUT: float = 15.0
    
    # Platform HTTP Headers
    IG_HEADERS = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "X-IG-App-ID": "936619743392459",
    }
    
    TT_HEADERS = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
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
        f"🤖 IG & TikTok Username Scanner Bot Operational\n"
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
    active_platform: str = "ig"
    current_target_chat: Optional[int] = None

state = ScannerState()

# ==============================================================================
# 4. INSTAGRAM & TIKTOK CHECKER ENGINE
# ==============================================================================

async def check_instagram_username(client: httpx.AsyncClient, username: str) -> Tuple[str, str]:
    """Checks an Instagram username status."""
    clean_username = username.strip().lstrip("@").lower()
    
    if len(clean_username) < 3 or len(clean_username) > 30:
        return "INVALID", "Instagram usernames must be between 3 and 30 characters."
    
    url = f"https://www.instagram.com/{clean_username}/"
    
    try:
        response = await client.get(url, headers=BotConfig.IG_HEADERS)
        
        if response.status_code == 404:
            return "AVAILABLE", clean_username
        elif response.status_code == 200:
            return "TAKEN", clean_username
        elif response.status_code in (429, 302, 403):
            logger.warning("Instagram rate limit encountered. Auto-pausing silently for 15 minutes...")
            await asyncio.sleep(900)  # Silent 15-minute wait
            return "RATE_LIMITED", clean_username
        else:
            return "ERROR", f"HTTP Status {response.status_code}"

    except httpx.RequestError as e:
        logger.error(f"Network error checking IG '{clean_username}': {e}")
        return "ERROR", str(e)

async def check_tiktok_username(client: httpx.AsyncClient, username: str) -> Tuple[str, str]:
    """Checks a TikTok username status."""
    clean_username = username.strip().lstrip("@").lower()
    
    if len(clean_username) < 2 or len(clean_username) > 24:
        return "INVALID", "TikTok usernames must be between 2 and 24 characters."
    
    url = f"https://www.tiktok.com/@{clean_username}"
    
    try:
        response = await client.get(url, headers=BotConfig.TT_HEADERS)
        
        if response.status_code == 404:
            return "AVAILABLE", clean_username
        elif response.status_code == 200:
            return "TAKEN", clean_username
        elif response.status_code in (403, 429):
            logger.warning("TikTok rate limit/protection encountered. Auto-pausing silently for 15 minutes...")
            await asyncio.sleep(900)  # Silent 15-minute wait
            return "RATE_LIMITED", clean_username
        else:
            return "ERROR", f"HTTP Status {response.status_code}"

    except httpx.RequestError as e:
        logger.error(f"Network error checking TikTok '{clean_username}': {e}")
        return "ERROR", str(e)

async def check_username(client: httpx.AsyncClient, platform: str, username: str) -> Tuple[str, str]:
    """Router for selecting platform checker."""
    if platform.lower() in ("tt", "tiktok"):
        return await check_tiktok_username(client, username)
    return await check_instagram_username(client, username)

def generate_random_username(platform: str = "ig", length: int = 5) -> str:
    """Generates a valid random username format for the chosen platform."""
    min_len = 2 if platform in ("tt", "tiktok") else 3
    max_len = 24 if platform in ("tt", "tiktok") else 30
    length = max(min_len, min(max_len, length))
    
    chars = string.ascii_lowercase + string.digits + "._"
    start_char = random.choice(string.ascii_lowercase)
    
    if length == 2:
        end_char = random.choice(string.ascii_lowercase + string.digits)
        return f"{start_char}{end_char}"
    
    middle_chars = [random.choice(chars) for _ in range(length - 2)]
    end_char = random.choice(string.ascii_lowercase + string.digits)
    return f"{start_char}{''.join(middle_chars)}{end_char}"

def parse_platform_arg(args: List[str]) -> Tuple[str, List[str]]:
    """Helper to extract optional platform keyword ('ig' or 'tt') from user arguments."""
    if not args:
        return "ig", []
    
    first_arg = args[0].lower()
    if first_arg in ("ig", "instagram"):
        return "ig", args[1:]
    elif first_arg in ("tt", "tiktok"):
        return "tt", args[1:]
    
    return "ig", args

# ==============================================================================
# 5. TELEGRAM COMMAND HANDLERS
# ==============================================================================

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Provides instructions and bot command usage."""
    welcome_text = (
        "🔍 **Instagram & TikTok Username Scanner Bot**\n\n"
        "⚡ **Commands & Platform Selection:**\n"
        "• `/check [ig|tt] <username>` — Check single handle (e.g., `/check tt cool_name`)\n"
        "• `/scan [ig|tt] <u1> <u2> ...` — Batch check handles (e.g., `/scan ig name1 name2`)\n"
        "• `/auto [ig|tt] <length>` — Start background hunting (e.g., `/auto tt 4`)\n"
        "• `/stop` — Stop active background scanner\n"
        "• `/status` — View Live Scanner metrics\n\n"
        "💡 *Default platform is Instagram (ig) if omitted.*"
    )
    await update.message.reply_text(welcome_text, parse_mode=ParseMode.MARKDOWN)

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Displays scanner runtime statistics."""
    uptime = int(time.time() - BOT_START_TIME)
    platform_name = "TikTok" if state.active_platform == "tt" else "Instagram"
    scanning_status = f"🟢 Active ({platform_name})" if state.is_scanning else "🔴 Idle"
    
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
    """Handles single manual username lookup."""
    platform, remaining_args = parse_platform_arg(context.args)
    
    if not remaining_args:
        await update.message.reply_text("❌ Specify a username!\nExample: `/check tt cool_name` or `/check cool_name`", parse_mode=ParseMode.MARKDOWN)
        return

    target_username = remaining_args[0]
    platform_label = "TikTok" if platform == "tt" else "Instagram"
    status_msg = await update.message.reply_text(f"🔍 Checking {platform_label} `@{target_username}`...", parse_mode=ParseMode.MARKDOWN)

    async with httpx.AsyncClient(timeout=BotConfig.HTTP_TIMEOUT, follow_redirects=True) as client:
        status, result = await check_username(client, platform, target_username)

    state.scanned_count += 1

    if status == "AVAILABLE":
        state.available_found += 1
        link = f"https://tiktok.com/@{result}" if platform == "tt" else f"https://instagram.com/{result}"
        claim_msg = (
            f"🎉 **AVAILABLE {platform_label.upper()} HANDLE FOUND!**\n\n"
            f"👉 **Handle:** `@{result}`\n"
            f"🔗 **Direct Link:** {link}\n\n"
            f"📌 **How to Claim:**\n"
            f"1. Open {platform_label} App.\n"
            f"2. Go to **Edit Profile** -> **Username**.\n"
            f"3. Type `@{result}` and tap **Save** immediately."
        )
        await status_msg.edit_text(claim_msg, parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True)

    elif status == "TAKEN":
        await status_msg.edit_text(f"❌ {platform_label} handle `@{result}` is **TAKEN**.", parse_mode=ParseMode.MARKDOWN)

    elif status == "RATE_LIMITED":
        await status_msg.edit_text(f"⚠️ {platform_label} server check busy. Please try again in a few moments.", parse_mode=ParseMode.MARKDOWN)

    else:
        await status_msg.edit_text(f"⚠️ Could not verify `@{target_username}` ({result}).", parse_mode=ParseMode.MARKDOWN)

async def scan_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Scans a batch list of usernames passed in arguments."""
    platform, usernames = parse_platform_arg(context.args)
    
    if not usernames:
        await update.message.reply_text("❌ Usage: `/scan [ig|tt] user1 user2 user3`", parse_mode=ParseMode.MARKDOWN)
        return

    usernames = usernames[:20]
    platform_label = "TikTok" if platform == "tt" else "Instagram"
    status_msg = await update.message.reply_text(f"🔄 Processing batch scan of {len(usernames)} handles on {platform_label}...", parse_mode=ParseMode.MARKDOWN)

    found_list = []
    async with httpx.AsyncClient(timeout=BotConfig.HTTP_TIMEOUT, follow_redirects=True) as client:
        for u in usernames:
            status, result = await check_username(client, platform, u)
            state.scanned_count += 1
            
            if status == "AVAILABLE":
                state.available_found += 1
                found_list.append(result)
            
            await asyncio.sleep(1.5)

    if found_list:
        found_str = "\n".join([f"• `@{name}`" for name in found_list])
        await status_msg.edit_text(
            f"🎉 **Batch Scan Finished ({platform_label})!**\n\n**Available Handles Found:**\n{found_str}\n\n"
            f"Claim them inside your {platform_label} settings!",
            parse_mode=ParseMode.MARKDOWN
        )
    else:
        await status_msg.edit_text(f"❌ Batch scan complete. None of the provided usernames were available on {platform_label}.", parse_mode=ParseMode.MARKDOWN)

# ==============================================================================
# 6. BACKGROUND AUTOMATIC SCANNER LOOP
# ==============================================================================

async def background_scanner_loop(context: ContextTypes.DEFAULT_TYPE, platform: str, target_length: int, chat_id: int):
    """Runs continuous random username checks in background with silent rate-limiting."""
    platform_label = "TikTok" if platform == "tt" else "Instagram"
    logger.info(f"Starting auto-scanner loop for {platform_label} (length {target_length})...")
    
    async with httpx.AsyncClient(timeout=BotConfig.HTTP_TIMEOUT, follow_redirects=True) as client:
        while state.is_scanning:
            target_username = generate_random_username(platform, target_length)
            status, result = await check_username(client, platform, target_username)
            state.scanned_count += 1

            if status == "AVAILABLE":
                state.available_found += 1
                link = f"https://tiktok.com/@{result}" if platform == "tt" else f"https://instagram.com/{result}"
                alert_text = (
                    f"🎯 **AUTOMATIC SCANNER MATCH ({platform_label.upper()})!**\n\n"
                    f"✨ **Handle:** `@{result}`\n"
                    f"🔗 **Link:** {link}\n\n"
                    f"📌 **Claim Steps:** Open {platform_label} -> Edit Profile -> Change Username to `@{result}`."
                )
                try:
                    await context.bot.send_message(chat_id=chat_id, text=alert_text, parse_mode=ParseMode.MARKDOWN)
                except Exception as e:
                    logger.error(f"Failed to deliver Telegram alert: {e}")

            gc.collect()
            await asyncio.sleep(2.0)

async def auto_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Starts background random handle hunting."""
    if state.is_scanning:
        await update.message.reply_text("⚠️ Auto-scanner is already running! Use `/stop` to cancel it first.", parse_mode=ParseMode.MARKDOWN)
        return

    platform, remaining_args = parse_platform_arg(context.args)
    
    length = 4 if platform == "tt" else 5
    if remaining_args and remaining_args[0].isdigit():
        min_len = 2 if platform == "tt" else 3
        max_len = 24 if platform == "tt" else 30
        length = max(min_len, min(max_len, int(remaining_args[0])))

    state.is_scanning = True
    state.active_platform = platform
    state.current_target_chat = update.effective_chat.id
    
    state.scanner_task = asyncio.create_task(
        background_scanner_loop(context, platform, length, update.effective_chat.id)
    )

    platform_label = "TikTok" if platform == "tt" else "Instagram"
    await update.message.reply_text(
        f"🚀 **Auto-Scanner Started!**\n"
        f"📱 **Platform:** `{platform_label}`\n"
        f"📏 **Target Length:** `{length}` characters\n"
        f"🤫 **Silent Mode:** Active (No rate-limit warnings will be sent).\n\n"
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

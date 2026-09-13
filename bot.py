import os
import sys
import logging
import asyncio
import httpx
import random
import string
import time
import gc
from typing import Optional, Dict, Tuple
from quart import Quart
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
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
logger = logging.getLogger("InteractiveScannerBot")

class BotConfig:
    """Central configuration management."""
    BOT_TOKEN: str = os.environ.get("BOT_TOKEN", "")
    PORT: int = int(os.environ.get("PORT", 10000))
    HTTP_TIMEOUT: float = 15.0
    
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
        f"🤖 Interactive Username Scanner Bot Operational\n"
        f"⏱️ Uptime: {uptime}s\n"
        f"📊 Status: Running",
        200
    )

@quart_app.route("/ping")
async def ping():
    return "PONG", 200

# ==============================================================================
# 3. GLOBAL SCANNER & USER STATE MANAGERS
# ==============================================================================

class ScannerState:
    is_scanning: bool = False
    scanner_task: Optional[asyncio.Task] = None
    scanned_count: int = 0
    available_found: int = 0
    active_platform: str = "ig"
    active_length: int = 4
    current_target_chat: Optional[int] = None

state = ScannerState()

# Track user state when waiting for specific text input: { chat_id: "ig" | "tt" }
user_input_wait: Dict[int, str] = {}

# ==============================================================================
# 4. INSTAGRAM & TIKTOK CHECKER ENGINE
# ==============================================================================

async def check_instagram_username(client: httpx.AsyncClient, username: str) -> Tuple[str, str]:
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
            logger.warning("Instagram rate limit hit. Auto-pausing silently for 15 minutes...")
            await asyncio.sleep(900)
            return "RATE_LIMITED", clean_username
        else:
            return "ERROR", f"HTTP Status {response.status_code}"
    except httpx.RequestError as e:
        logger.error(f"Network error checking IG '{clean_username}': {e}")
        return "ERROR", str(e)

async def check_tiktok_username(client: httpx.AsyncClient, username: str) -> Tuple[str, str]:
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
            logger.warning("TikTok rate limit hit. Auto-pausing silently for 15 minutes...")
            await asyncio.sleep(900)
            return "RATE_LIMITED", clean_username
        else:
            return "ERROR", f"HTTP Status {response.status_code}"
    except httpx.RequestError as e:
        logger.error(f"Network error checking TikTok '{clean_username}': {e}")
        return "ERROR", str(e)

async def check_username(client: httpx.AsyncClient, platform: str, username: str) -> Tuple[str, str]:
    if platform == "tt":
        return await check_tiktok_username(client, username)
    return await check_instagram_username(client, username)

def generate_random_username(platform: str = "ig", length: int = 5) -> str:
    min_len = 2 if platform == "tt" else 3
    max_len = 24 if platform == "tt" else 30
    length = max(min_len, min(max_len, length))
    
    chars = string.ascii_lowercase + string.digits + "._"
    start_char = random.choice(string.ascii_lowercase)
    
    if length == 2:
        end_char = random.choice(string.ascii_lowercase + string.digits)
        return f"{start_char}{end_char}"
    
    middle_chars = [random.choice(chars) for _ in range(length - 2)]
    end_char = random.choice(string.ascii_lowercase + string.digits)
    return f"{start_char}{''.join(middle_chars)}{end_char}"

# ==============================================================================
# 5. KEYBOARD MENU BUILDERS
# ==============================================================================

def build_main_menu_keyboard() -> InlineKeyboardMarkup:
    buttons = [
        [
            InlineKeyboardButton("📸 Instagram Scanner", callback_data="menu_platform_ig"),
            InlineKeyboardButton("🎵 TikTok Scanner", callback_data="menu_platform_tt")
        ],
        [
            InlineKeyboardButton("📊 System Status", callback_data="action_status"),
            InlineKeyboardButton("🛑 Stop Auto-Scanner", callback_data="action_stop")
        ]
    ]
    return InlineKeyboardMarkup(buttons)

def build_platform_keyboard(platform: str) -> InlineKeyboardMarkup:
    platform_name = "TikTok" if platform == "tt" else "Instagram"
    buttons = [
        [
            InlineKeyboardButton(f"✍️ Check Specific Handle", callback_data=f"input_req_{platform}")
        ],
        [
            InlineKeyboardButton("🚀 Auto-Hunt (3-Char)", callback_data=f"auto_start_{platform}_3"),
            InlineKeyboardButton("🚀 Auto-Hunt (4-Char)", callback_data=f"auto_start_{platform}_4"),
        ],
        [
            InlineKeyboardButton("🚀 Auto-Hunt (5-Char)", callback_data=f"auto_start_{platform}_5"),
        ],
        [
            InlineKeyboardButton("🔙 Back to Main Menu", callback_data="menu_main")
        ]
    ]
    return InlineKeyboardMarkup(buttons)

def build_after_check_keyboard(platform: str) -> InlineKeyboardMarkup:
    buttons = [
        [
            InlineKeyboardButton("✍️ Check Another Username", callback_data=f"input_req_{platform}"),
            InlineKeyboardButton("🔙 Platform Menu", callback_data=f"menu_platform_{platform}")
        ],
        [
            InlineKeyboardButton("🏠 Main Menu", callback_data="menu_main")
        ]
    ]
    return InlineKeyboardMarkup(buttons)

# ==============================================================================
# 6. TELEGRAM CALLBACK & HANDLER ENGINE
# ==============================================================================

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Sends the interactive main menu."""
    chat_id = update.effective_chat.id
    if chat_id in user_input_wait:
        del user_input_wait[chat_id]
        
    menu_text = (
        "🤖 **Username Scanner Control Panel**\n\n"
        "Tap an option below to scan handles or configure auto-hunting:"
    )
    if update.message:
        await update.message.reply_text(menu_text, reply_markup=build_main_menu_keyboard(), parse_mode=ParseMode.MARKDOWN)
    elif update.callback_query:
        await update.callback_query.edit_message_text(menu_text, reply_markup=build_main_menu_keyboard(), parse_mode=ParseMode.MARKDOWN)

async def button_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Processes all inline button clicks."""
    query = update.callback_query
    await query.answer()
    data = query.data
    chat_id = update.effective_chat.id

    # 1. Main Menu
    if data == "menu_main":
        if chat_id in user_input_wait:
            del user_input_wait[chat_id]
        await query.edit_message_text(
            "🤖 **Username Scanner Control Panel**\n\nTap an option below to proceed:",
            reply_markup=build_main_menu_keyboard(),
            parse_mode=ParseMode.MARKDOWN
        )

    # 2. Select Platform Submenu
    elif data.startswith("menu_platform_"):
        platform = data.split("_")[2]
        platform_name = "TikTok" if platform == "tt" else "Instagram"
        if chat_id in user_input_wait:
            del user_input_wait[chat_id]
        
        await query.edit_message_text(
            f"📱 **{platform_name} Scanner Options**\n\n"
            f"Select what you would like to do for **{platform_name}**:",
            reply_markup=build_platform_keyboard(platform),
            parse_mode=ParseMode.MARKDOWN
        )

    # 3. Request Manual Username Input
    elif data.startswith("input_req_"):
        platform = data.split("_")[2]
        platform_name = "TikTok" if platform == "tt" else "Instagram"
        user_input_wait[chat_id] = platform
        
        await query.edit_message_text(
            f"✍️ **Type the {platform_name} username you want to check:**\n\n"
            f"*(Just send the handle in chat below)*",
            parse_mode=ParseMode.MARKDOWN
        )

    # 4. Start Background Auto-Scanner
    elif data.startswith("auto_start_"):
        _, _, platform, length_str = data.split("_")
        length = int(length_str)
        platform_name = "TikTok" if platform == "tt" else "Instagram"

        if state.is_scanning:
            await query.edit_message_text(
                f"⚠️ **Scanner is already active!**\n"
                f"Currently scanning on `{state.active_platform.upper()}`.\n\n"
                f"Stop the current scanner first before starting a new one.",
                reply_markup=build_main_menu_keyboard(),
                parse_mode=ParseMode.MARKDOWN
            )
            return

        state.is_scanning = True
        state.active_platform = platform
        state.active_length = length
        state.current_target_chat = chat_id

        state.scanner_task = asyncio.create_task(
            background_scanner_loop(context, platform, length, chat_id)
        )

        await query.edit_message_text(
            f"🚀 **Auto-Scanner Launched!**\n\n"
            f"📱 **Platform:** `{platform_name}`\n"
            f"📏 **Length:** `{length}` characters\n"
            f"🤫 **Silent Mode:** Active (No rate-limit alerts will be sent).\n\n"
            f"You will receive a Telegram message immediately when a free handle is found!",
            reply_markup=build_main_menu_keyboard(),
            parse_mode=ParseMode.MARKDOWN
        )

    # 5. Stop Scanner
    elif data == "action_stop":
        if not state.is_scanning:
            await query.edit_message_text(
                "ℹ️ **No background scanner is currently active.**",
                reply_markup=build_main_menu_keyboard(),
                parse_mode=ParseMode.MARKDOWN
            )
            return

        state.is_scanning = False
        if state.scanner_task:
            state.scanner_task.cancel()
            state.scanner_task = None

        await query.edit_message_text(
            "🛑 **Auto-scanner stopped successfully.**",
            reply_markup=build_main_menu_keyboard(),
            parse_mode=ParseMode.MARKDOWN
        )

    # 6. System Status
    elif data == "action_status":
        uptime = int(time.time() - BOT_START_TIME)
        platform_name = "TikTok" if state.active_platform == "tt" else "Instagram"
        scanning_status = f"🟢 Scanning {platform_name} ({state.active_length}-Char)" if state.is_scanning else "🔴 Idle"

        status_text = (
            "📊 **Live Scanner Status & Metrics**\n"
            "━━━━━━━━━━━━━━━━━━━━━\n"
            f"⚙️ **State:** `{scanning_status}`\n"
            f"⏱️ **Uptime:** `{uptime}s`\n"
            f"🔍 **Total Checked:** `{state.scanned_count}`\n"
            f"✨ **Available Found:** `{state.available_found}`\n"
            f"🤫 **Rate Limit Mode:** `Silent Auto-Pause`"
        )
        await query.edit_message_text(status_text, reply_markup=build_main_menu_keyboard(), parse_mode=ParseMode.MARKDOWN)

async def handle_user_text_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Captures and checks usernames sent as text when requested."""
    chat_id = update.effective_chat.id

    # If the user wasn't prompted to type a handle, point them to the interactive menu
    if chat_id not in user_input_wait:
        await update.message.reply_text(
            "💡 Tap a button below to interact with the scanner:",
            reply_markup=build_main_menu_keyboard()
        )
        return

    platform = user_input_wait[chat_id]
    target_username = update.message.text.strip().lstrip("@")
    platform_name = "TikTok" if platform == "tt" else "Instagram"

    # Reset input wait state
    del user_input_wait[chat_id]

    status_msg = await update.message.reply_text(f"🔍 Checking {platform_name} `@{target_username}`...", parse_mode=ParseMode.MARKDOWN)

    async with httpx.AsyncClient(timeout=BotConfig.HTTP_TIMEOUT, follow_redirects=True) as client:
        status, result = await check_username(client, platform, target_username)

    state.scanned_count += 1

    if status == "AVAILABLE":
        state.available_found += 1
        link = f"https://tiktok.com/@{result}" if platform == "tt" else f"https://instagram.com/{result}"
        claim_msg = (
            f"🎉 **AVAILABLE {platform_name.upper()} HANDLE FOUND!**\n\n"
            f"👉 **Handle:** `@{result}`\n"
            f"🔗 **Direct Link:** {link}\n\n"
            f"📌 **How to Claim:**\n"
            f"1. Open {platform_name} App.\n"
            f"2. Go to **Edit Profile** -> **Username**.\n"
            f"3. Type `@{result}` and tap **Save** immediately."
        )
        await status_msg.edit_text(claim_msg, reply_markup=build_after_check_keyboard(platform), parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True)

    elif status == "TAKEN":
        await status_msg.edit_text(
            f"❌ {platform_name} handle `@{result}` is **TAKEN**.",
            reply_markup=build_after_check_keyboard(platform),
            parse_mode=ParseMode.MARKDOWN
        )

    elif status == "RATE_LIMITED":
        await status_msg.edit_text(
            f"⚠️ {platform_name} server busy. Please try again in a few moments.",
            reply_markup=build_after_check_keyboard(platform),
            parse_mode=ParseMode.MARKDOWN
        )

    else:
        await status_msg.edit_text(
            f"⚠️ Could not verify `@{target_username}` ({result}).",
            reply_markup=build_after_check_keyboard(platform),
            parse_mode=ParseMode.MARKDOWN
        )

# ==============================================================================
# 7. BACKGROUND AUTOMATIC SCANNER LOOP
# ==============================================================================

async def background_scanner_loop(context: ContextTypes.DEFAULT_TYPE, platform: str, target_length: int, chat_id: int):
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
                    await context.bot.send_message(
                        chat_id=chat_id,
                        text=alert_text,
                        reply_markup=build_after_check_keyboard(platform),
                        parse_mode=ParseMode.MARKDOWN
                    )
                except Exception as e:
                    logger.error(f"Failed to deliver Telegram alert: {e}")

            gc.collect()
            await asyncio.sleep(2.0)

# ==============================================================================
# 8. ERROR HANDLER & MAIN ENTRYPOINT
# ==============================================================================

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Uncaught exception encountered during execution:", exc_info=context.error)

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

    # Handlers
    telegram_app.add_handler(CommandHandler("start", start_command))
    telegram_app.add_handler(CommandHandler("menu", start_command))
    telegram_app.add_handler(CallbackQueryHandler(button_callback_handler))
    telegram_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_user_text_input))
    
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

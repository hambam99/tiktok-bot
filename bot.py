import logging
import asyncio
import httpx
import random
import string
import os
import sqlite3
from quart import Quart
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes
from telegram.request import HTTPXRequest
from hypercorn.config import Config
from hypercorn.asyncio import serve

# Configure logging
logging.basicConfig(format='%(asctime)s - %(levelname)s - %(message)s', level=logging.INFO)

# ---------------------------------------------------------
# SECURITY LAYER 1: USER ID WHITELISTING
# Add your numeric Telegram User ID here (e.g., [123456789])
# Send /start to @userinfobot on Telegram to get your ID.
# ---------------------------------------------------------
ALLOWED_TELEGRAM_USERS = [
    int(uid) for uid in os.environ.get("ALLOWED_USERS", "").split(",") if uid.strip().isdigit()
]

def is_authorized(user_id: int) -> bool:
    """Verifies if the incoming Telegram User ID is whitelisted."""
    if not ALLOWED_TELEGRAM_USERS:
        # Warning mode if env var is missing, allowing initial setup
        return True
    return user_id in ALLOWED_TELEGRAM_USERS


# ---------------------------------------------------------
# ENVIRONMENT CONFIGURATION & KEYS
# ---------------------------------------------------------
BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("CRITICAL ERROR: 'BOT_TOKEN' environment variable is missing!")

AUTO_CLAIMER_WEBHOOK_URL = os.environ.get("CLAIMER_WEBHOOK", "")
IG_SESSION_COOKIE = os.environ.get("IG_SESSION_COOKIE", "")  # Optional: For authenticated requests

# 1. ASGI Web Server for Health Checks
quart_app = Quart(__name__)

# Global Platform Toggles & Settings
TT_SCANNER_RUNNING = False
IG_SCANNER_RUNNING = False
AUTO_SCANNER_USER_ID = None

# Backoff / Rate-Limit Controls
IG_COOLDOWN_UNTIL = 0
TT_COOLDOWN_UNTIL = 0

@quart_app.route('/')
async def home():
    tt_status = "Active" if TT_SCANNER_RUNNING else "Idle"
    ig_status = "Active" if IG_SCANNER_RUNNING else "Idle"
    return f"Bot Status: TikTok Sweeper [{tt_status}] | Instagram Sweeper [{ig_status}]", 200

# 2. Database Initialization
def init_db():
    conn = sqlite3.connect('tracker.db')
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS found_4l (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE,
            platform TEXT,
            found_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    conn.commit()
    conn.close()

init_db()

def save_found_handle(username: str, platform: str) -> bool:
    try:
        conn = sqlite3.connect('tracker.db')
        cursor = conn.cursor()
        cursor.execute('INSERT INTO found_4l (username, platform) VALUES (?, ?)', (username, platform))
        conn.commit()
        conn.close()
        return True
    except sqlite3.IntegrityError:
        return False

# 3. Random 4-Letter Handle Candidates
def generate_4l_candidate() -> str:
    vowels = 'aeiou'
    consonants = 'bcdfghjklmnpqrstvwxyz'
    
    patterns = [
        lambda: f"{random.choice(consonants)}{random.choice(vowels)}{random.choice(consonants)}{random.choice(vowels)}",
        lambda: f"{random.choice(vowels)}{random.choice(consonants)}{random.choice(vowels)}{random.choice(consonants)}",
        lambda: "".join(random.choices(string.ascii_lowercase, k=4)),
        lambda: "".join(random.choices(string.ascii_lowercase, k=3)) + random.choice("123456789")
    ]
    return random.choice(patterns)()

# Network Availability Checkers with Rate Limit Detection
async def check_tiktok(client: httpx.AsyncClient, username: str) -> tuple[str, bool, int]:
    url = f'https://www.tiktok.com/oembed?url=https://www.tiktok.com/@{username}'
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
        'Accept': 'application/json'
    }
    try:
        res = await client.get(url, headers=headers)
        if res.status_code == 429:
            logging.warning("TikTok Rate Limit (429) hit!")
            return username, False, 429
        if res.status_code in (400, 404):
            return username, True, res.status_code
        return username, False, res.status_code
    except Exception as e:
        logging.debug(f"TikTok API check exception: {e}")
        return username, False, 500

async def check_instagram(client: httpx.AsyncClient, username: str) -> tuple[str, bool, int]:
    url = f'https://www.instagram.com/api/v1/users/web_profile_info/?username={username}'
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
        'X-IG-App-ID': '936619743392459',
        'Accept': '*/*'
    }
    if IG_SESSION_COOKIE:
        headers['Cookie'] = f"sessionid={IG_SESSION_COOKIE};"

    try:
        res = await client.get(url, headers=headers)
        if res.status_code == 429:
            logging.warning("Instagram Rate Limit (429) hit!")
            return username, False, 429
        if res.status_code == 404:
            return username, True, 404
        return username, False, res.status_code
    except Exception as e:
        logging.debug(f"Instagram API check exception: {e}")
        return username, False, 500

async def trigger_auto_claim_webhook(username: str, platform: str):
    if not AUTO_CLAIMER_WEBHOOK_URL:
        return
    async with httpx.AsyncClient(timeout=3.0) as client:
        try:
            payload = {
                "event": "4L_HANDLE_DISCOVERED",
                "username": username,
                "platform": platform,
                "action": "CLAIM_NOW"
            }
            await client.post(AUTO_CLAIMER_WEBHOOK_URL, json=payload)
            logging.info(f"Fired Auto-Claimer Webhook for @{username} on {platform}")
        except Exception as e:
            logging.error(f"Webhook execution failed: {e}")

# 4. TikTok Isolated Sweeper Task with Exponential Backoff
async def tiktok_4l_sweeper(app):
    global TT_SCANNER_RUNNING, AUTO_SCANNER_USER_ID, TT_COOLDOWN_UNTIL
    logging.info("TikTok Sweeper Worker active.")
    
    async with httpx.AsyncClient(timeout=6.0, follow_redirects=True) as client:
        while True:
            if not TT_SCANNER_RUNNING or not AUTO_SCANNER_USER_ID:
                await asyncio.sleep(2)
                continue

            # Check for active rate limit cooldown
            current_time = asyncio.get_event_loop().time()
            if current_time < TT_COOLDOWN_UNTIL:
                await asyncio.sleep(10)
                continue

            try:
                cand = generate_4l_candidate()
                _, tt_free, status_code = await check_tiktok(client, cand)
                
                # SECURITY LAYER 2: EXPONENTIAL BACKOFF ON 429
                if status_code == 429:
                    logging.warning("TikTok rate limited. Cool down for 10 minutes.")
                    TT_COOLDOWN_UNTIL = current_time + 600
                    if AUTO_SCANNER_USER_ID:
                        await app.bot.send_message(
                            chat_id=AUTO_SCANNER_USER_ID,
                            text="⚠️ **TikTok Rate Limit Triggered**: Scanner auto-paused for 10 minutes to protect IP."
                        )
                    continue

                if tt_free and save_found_handle(cand, 'tiktok'):
                    await trigger_auto_claim_webhook(cand, 'tiktok')
                    claim_url = f"https://www.tiktok.com/@{cand}"
                    alert_text = (
                        f"🎵 **AVAILABLE 4L FOUND (TIKTOK)**\n\n"
                        f"Handle: `@{cand}`\n"
                        f"Platform: **TIKTOK**\n\n"
                        f"⚡ [CLICK HERE TO CLAIM DIRECTLY]({claim_url})"
                    )
                    await app.bot.send_message(chat_id=AUTO_SCANNER_USER_ID, text=alert_text, parse_mode='Markdown')

                await asyncio.sleep(0.8)

            except Exception as e:
                logging.error(f"TikTok Sweeper error: {type(e).__name__} - {e}")
                await asyncio.sleep(5)

# 5. Instagram Isolated Sweeper Task with Exponential Backoff
async def instagram_4l_sweeper(app):
    global IG_SCANNER_RUNNING, AUTO_SCANNER_USER_ID, IG_COOLDOWN_UNTIL
    logging.info("Instagram Sweeper Worker active.")
    
    async with httpx.AsyncClient(timeout=6.0, follow_redirects=True) as client:
        while True:
            if not IG_SCANNER_RUNNING or not AUTO_SCANNER_USER_ID:
                await asyncio.sleep(2)
                continue

            # Check for active rate limit cooldown
            current_time = asyncio.get_event_loop().time()
            if current_time < IG_COOLDOWN_UNTIL:
                await asyncio.sleep(10)
                continue

            try:
                cand = generate_4l_candidate()
                _, ig_free, status_code = await check_instagram(client, cand)
                
                # SECURITY LAYER 2: EXPONENTIAL BACKOFF ON 429
                if status_code == 429:
                    logging.warning("Instagram rate limited. Cool down for 15 minutes.")
                    IG_COOLDOWN_UNTIL = current_time + 900
                    if AUTO_SCANNER_USER_ID:
                        await app.bot.send_message(
                            chat_id=AUTO_SCANNER_USER_ID,
                            text="⚠️ **Instagram Rate Limit Triggered**: Scanner auto-paused for 15 minutes to protect IP."
                        )
                    continue

                if ig_free and save_found_handle(cand, 'instagram'):
                    await trigger_auto_claim_webhook(cand, 'instagram')
                    claim_url = f"https://www.instagram.com/{cand}"
                    alert_text = (
                        f"📸 **AVAILABLE 4L FOUND (INSTAGRAM)**\n\n"
                        f"Handle: `@{cand}`\n"
                        f"Platform: **INSTAGRAM**\n\n"
                        f"⚡ [CLICK HERE TO CLAIM DIRECTLY]({claim_url})"
                    )
                    await app.bot.send_message(chat_id=AUTO_SCANNER_USER_ID, text=alert_text, parse_mode='Markdown')

                await asyncio.sleep(1.5)

            except Exception as e:
                logging.error(f"Instagram Sweeper error: {type(e).__name__} - {e}")
                await asyncio.sleep(5)

# UI Helper
def build_autohunt_keyboard() -> InlineKeyboardMarkup:
    tt_btn_text = "🟢 TikTok: RUNNING" if TT_SCANNER_RUNNING else "🔴 TikTok: STOPPED"
    ig_btn_text = "🟢 Instagram: RUNNING" if IG_SCANNER_RUNNING else "🔴 Instagram: STOPPED"
    
    keyboard = [
        [InlineKeyboardButton(tt_btn_text, callback_data="toggle_tt")],
        [InlineKeyboardButton(ig_btn_text, callback_data="toggle_ig")],
        [
            InlineKeyboardButton("⚡ Start All", callback_data="start_all"),
            InlineKeyboardButton("⛔ Stop All", callback_data="stop_all")
        ],
        [InlineKeyboardButton("🔄 Refresh Status", callback_data="refresh")]
    ]
    return InlineKeyboardMarkup(keyboard)

# Telegram Command Handlers
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_user.id):
        return

    msg = (
        "⚡ **Autonomous 4-Letter Handle Sweeper**\n\n"
        "• Use `/autohunt` to open the interactive control menu\n"
        "• Use `/found` to view discovered handles\n"
        "• Send any handle directly to manually test availability"
    )
    await update.message.reply_text(msg, parse_mode='Markdown')

async def autohunt_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_user.id):
        await update.message.reply_text("⛔ **Access Denied**: Unauthorized User ID.")
        return

    global AUTO_SCANNER_USER_ID
    AUTO_SCANNER_USER_ID = update.effective_user.id
    
    status_text = (
        "⚙️ **AUTOHUNT CONTROL PANEL**\n\n"
        "Select a platform below to toggle active background hunting:\n\n"
        f"• **TikTok Worker**: {'`RUNNING`' if TT_SCANNER_RUNNING else '`STOPPED`'}\n"
        f"• **Instagram Worker**: {'`RUNNING`' if IG_SCANNER_RUNNING else '`STOPPED`'}"
    )
    reply_markup = build_autohunt_keyboard()
    await update.message.reply_text(status_text, reply_markup=reply_markup, parse_mode='Markdown')

async def button_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not is_authorized(query.from_user.id):
        await query.answer("Access Denied", show_alert=True)
        return

    global TT_SCANNER_RUNNING, IG_SCANNER_RUNNING, AUTO_SCANNER_USER_ID
    
    await query.answer()
    AUTO_SCANNER_USER_ID = query.from_user.id

    data = query.data
    if data == "toggle_tt":
        TT_SCANNER_RUNNING = not TT_SCANNER_RUNNING
    elif data == "toggle_ig":
        IG_SCANNER_RUNNING = not IG_SCANNER_RUNNING
    elif data == "start_all":
        TT_SCANNER_RUNNING = True
        IG_SCANNER_RUNNING = True
    elif data == "stop_all":
        TT_SCANNER_RUNNING = False
        IG_SCANNER_RUNNING = False

    status_text = (
        "⚙️ **AUTOHUNT CONTROL PANEL**\n\n"
        "Select a platform below to toggle active background hunting:\n\n"
        f"• **TikTok Worker**: {'`RUNNING`' if TT_SCANNER_RUNNING else '`STOPPED`'}\n"
        f"• **Instagram Worker**: {'`RUNNING`' if IG_SCANNER_RUNNING else '`STOPPED`'}"
    )
    reply_markup = build_autohunt_keyboard()
    await query.edit_message_text(status_text, reply_markup=reply_markup, parse_mode='Markdown')

async def found_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_user.id):
        return

    conn = sqlite3.connect('tracker.db')
    cursor = conn.cursor()
    cursor.execute('SELECT username, platform, found_at FROM found_4l ORDER BY id DESC LIMIT 20')
    rows = cursor.fetchall()
    conn.close()

    if not rows:
        await update.message.reply_text("No 4-letter handles discovered yet.", parse_mode='Markdown')
        return

    reply = "📌 **Recently Discovered 4-Letter Handles:**\n\n"
    for uname, platform, found_at in rows:
        icon = "🎵" if platform == 'tiktok' else "📸"
        reply += f"{icon} `@{uname}` — **{platform.upper()}**\n"
    await update.message.reply_text(reply, parse_mode='Markdown')

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_user.id):
        return

    user_input = update.message.text.strip().lstrip('@').lower()
    if len(user_input) < 2 or len(user_input) > 24:
        await update.message.reply_text('Username must be between 2 and 24 characters.')
        return
    
    msg = await update.message.reply_text(f'🔍 Checking `@{user_input}`...')
    
    async with httpx.AsyncClient(timeout=6.0, follow_redirects=True) as client:
        _, tt_free, _ = await check_tiktok(client, user_input)
        await asyncio.sleep(0.5)
        _, ig_free, _ = await check_instagram(client, user_input)
        
    reply_text = f"✨ **Results for `@{user_input}`:**\n\n"
    reply_text += f"🎵 **TikTok:** {'**AVAILABLE**' if tt_free else 'TAKEN'}\n"
    reply_text += f"📸 **Instagram:** {'**AVAILABLE**' if ig_free else 'TAKEN'}"
    await msg.edit_text(reply_text, parse_mode='Markdown')

# Main Entrypoint
async def main():
    req = HTTPXRequest(connect_timeout=30.0, read_timeout=30.0)
    app = ApplicationBuilder().token(BOT_TOKEN).request(req).build()
    
    app.add_handler(CommandHandler('start', start))
    app.add_handler(CommandHandler('autohunt', autohunt_command))
    app.add_handler(CommandHandler('found', found_command))
    app.add_handler(CallbackQueryHandler(button_callback_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    config = Config()
    port = int(os.environ.get("PORT", 10000))
    config.bind = [f"0.0.0.0:{port}"]
    
    logging.info("Starting Telegram Bot application...")
    
    await app.initialize()
    await app.start()
    await app.updater.start_polling(drop_pending_updates=True)
    
    logging.info("Gathering server and background sweeper tasks...")
    
    await asyncio.gather(
        serve(quart_app, config),
        tiktok_4l_sweeper(app),
        instagram_4l_sweeper(app)
    )

if __name__ == '__main__':
    asyncio.run(main())

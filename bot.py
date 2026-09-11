import logging, asyncio, httpx, random, string, os, sqlite3
from quart import Quart
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes
from telegram.request import HTTPXRequest
from hypercorn.config import Config
from hypercorn.asyncio import serve

logging.basicConfig(format='%(asctime)s - %(levelname)s - %(message)s', level=logging.INFO)

# 1. ASGI Web Server for Health Check
quart_app = Quart(__name__)

# Global Auto-Scan Toggle & Settings
AUTO_SCANNER_RUNNING = False
AUTO_SCANNER_USER_ID = None  # Who receives the alerts
AUTO_CLAIMER_WEBHOOK_URL = os.environ.get("CLAIMER_WEBHOOK", "")
BOT_TOKEN = '8735644612:AAEhuQSjH0f9pxlUA5Lgl8Bv9fOxpB1Rh3k'

@quart_app.route('/')
async def home():
    status = "Active (Scanning)" if AUTO_SCANNER_RUNNING else "Idle"
    return f"Bot status: Autonomous 4L Sweeper [{status}]", 200

# 2. Database for Discovered Available 4L Handles
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

# 3. Random 4-Letter Handle Generators
def generate_4l_candidate() -> str:
    """Generates clean 4-character combinations (CVCV, VCVC, pure alpha, or 3L+digit)"""
    vowels = 'aeiou'
    consonants = 'bcdfghjklmnpqrstvwxyz'
    
    patterns = [
        # CVCV (e.g. zimo, vira)
        lambda: f"{random.choice(consonants)}{random.choice(vowels)}{random.choice(consonants)}{random.choice(vowels)}",
        # VCVC (e.g. exor, opix)
        lambda: f"{random.choice(vowels)}{random.choice(consonants)}{random.choice(vowels)}{random.choice(consonants)}",
        # Pure 4-letter
        lambda: "".join(random.choices(string.ascii_lowercase, k=4)),
        # 3-letter + digit (e.g. vix1, zex9)
        lambda: "".join(random.choices(string.ascii_lowercase, k=3)) + random.choice("123456789")
    ]
    return random.choice(patterns)()

# Checkers
async def check_tiktok(client: httpx.AsyncClient, username: str) -> tuple[str, bool]:
    url = f'https://www.tiktok.com/oembed?url=https://www.tiktok.com/@{username}'
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
        'Accept': 'application/json'
    }
    try:
        res = await client.get(url, headers=headers)
        if res.status_code in (400, 404):
            return username, True
        return username, False
    except Exception:
        return username, False

async def check_instagram(client: httpx.AsyncClient, username: str) -> tuple[str, bool]:
    url = f'https://www.instagram.com/api/v1/users/web_profile_info/?username={username}'
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
        'X-IG-App-ID': '936619743392459',
        'Accept': '*/*'
    }
    try:
        res = await client.get(url, headers=headers)
        if res.status_code == 404:
            return username, True
        return username, False
    except Exception:
        return username, False

async def trigger_auto_claim_webhook(username: str, platform: str):
    """Fires immediate webhook payload to auto-claim script or external fast claimer"""
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

# 4. Continuous Autonomous Sweeper Worker
async def autonomous_4l_sweeper(app):
    global AUTO_SCANNER_RUNNING, AUTO_SCANNER_USER_ID
    logging.info("Autonomous 4L Sweeper Worker initialized.")
    
    async with httpx.AsyncClient(timeout=6.0, follow_redirects=True) as client:
        while True:
            if not AUTO_SCANNER_RUNNING or not AUTO_SCANNER_USER_ID:
                await asyncio.sleep(3)
                continue

            try:
                # Generate random candidate
                cand = generate_4l_candidate()
                
                # Check TikTok
                _, tt_free = await check_tiktok(client, cand)
                if tt_free and save_found_handle(cand, 'tiktok'):
                    await trigger_auto_claim_webhook(cand, 'tiktok')
                    claim_url = f"https://www.tiktok.com/@{cand}"
                    alert_text = (
                        f"🚨 **AVAILABLE 4-LETTER FOUND!** 🚨\n\n"
                        f"Handle: `@{cand}`\n"
                        f"Platform: **TIKTOK**\n\n"
                        f"⚡ [CLICK HERE TO CLAIM DIRECTLY]({claim_url})"
                    )
                    await app.bot.send_message(chat_id=AUTO_SCANNER_USER_ID, text=alert_text, parse_mode='Markdown')

                await asyncio.sleep(0.8)  # Delay between platform checks to prevent IP block

                # Check Instagram
                _, ig_free = await check_instagram(client, cand)
                if ig_free and save_found_handle(cand, 'instagram'):
                    await trigger_auto_claim_webhook(cand, 'instagram')
                    claim_url = f"https://www.instagram.com/{cand}"
                    alert_text = (
                        f"🚨 **AVAILABLE 4-LETTER FOUND!** 🚨\n\n"
                        f"Handle: `@{cand}`\n"
                        f"Platform: **INSTAGRAM**\n\n"
                        f"⚡ [CLICK HERE TO CLAIM DIRECTLY]({claim_url})"
                    )
                    await app.bot.send_message(chat_id=AUTO_SCANNER_USER_ID, text=alert_text, parse_mode='Markdown')

                await asyncio.sleep(1.2)  # Delay before generating next candidate

            except Exception as e:
                logging.error(f"Sweeper loop error: {e}")
                await asyncio.sleep(5)

# Telegram Commands
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "⚡ **Autonomous 4-Letter Handle Sweeper & Sniper**\n\n"
        "• `/autohunt start` — Turn on endless 24/7 background scanning for available 4L usernames.\n"
        "• `/autohunt stop` — Pause the continuous scanner.\n"
        "• `/found` — View all available 4L handles discovered so far.\n"
        "• Send any single username to check availability instantly."
    )
    await update.message.reply_text(msg, parse_mode='Markdown')

async def autohunt_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global AUTO_SCANNER_RUNNING, AUTO_SCANNER_USER_ID
    
    if not context.args or context.args[0].lower() not in ['start', 'stop']:
        await update.message.reply_text("Usage: `/autohunt start` or `/autohunt stop`", parse_mode='Markdown')
        return

    action = context.args[0].lower()
    user_id = update.effective_user.id

    if action == 'start':
        AUTO_SCANNER_RUNNING = True
        AUTO_SCANNER_USER_ID = user_id
        await update.message.reply_text(
            "🔥 **AUTONOMOUS 4L HUNTING STARTED**\n\n"
            "The bot is now generating and checking random 4-letter handles continuously in the background.\n"
            "As soon as an uncreated/available handle is detected, you will get an instant alert!",
            parse_mode='Markdown'
        )
    else:
        AUTO_SCANNER_RUNNING = False
        await update.message.reply_text("🛑 **Autonomous hunt paused.**", parse_mode='Markdown')

async def found_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = sqlite3.connect('tracker.db')
    cursor = conn.cursor()
    cursor.execute('SELECT username, platform, found_at FROM found_4l ORDER BY id DESC LIMIT 20')
    rows = cursor.fetchall()
    conn.close()

    if not rows:
        await update.message.reply_text("No 4-letter handles discovered yet. Run `/autohunt start` to start hunting!", parse_mode='Markdown')
        return

    reply = "📌 **Recently Discovered 4-Letter Handles:**\n\n"
    for uname, platform, found_at in rows:
        reply += f"• `@{uname}` — **{platform.upper()}**\n"
    await update.message.reply_text(reply, parse_mode='Markdown')

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_input = update.message.text.strip().lstrip('@').lower()
    if len(user_input) < 2 or len(user_input) > 24:
        await update.message.reply_text('Username must be between 2 and 24 characters.')
        return
    
    msg = await update.message.reply_text(f'🔍 Checking `@{user_input}`...')
    
    async with httpx.AsyncClient(timeout=6.0, follow_redirects=True) as client:
        _, tt_free = await check_tiktok(client, user_input)
        await asyncio.sleep(0.5)
        _, ig_free = await check_instagram(client, user_input)
        
    reply_text = f"✨ **Results for `@{user_input}`:**\n\n"
    reply_text += f"🎵 **TikTok:** {'**AVAILABLE**' if tt_free else 'TAKEN'}\n"
    reply_text += f"📸 **Instagram:** {'**AVAILABLE**' if ig_free else 'TAKEN'}"
    await msg.edit_text(reply_text, parse_mode='Markdown')

async def main():
    req = HTTPXRequest(connect_timeout=30.0, read_timeout=30.0)
    app = ApplicationBuilder().token(BOT_TOKEN).request(req).build()
    
    app.add_handler(CommandHandler('start', start))
    app.add_handler(CommandHandler('autohunt', autohunt_command))
    app.add_handler(CommandHandler('found', found_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    config = Config()
    port = int(os.environ.get("PORT", 10000))
    config.bind = [f"0.0.0.0:{port}"]
    
    logging.info("Starting Autonomous 4L Sweeper...")
    
    await app.initialize()
    await app.start()
    await app.updater.start_polling(drop_pending_updates=True)
    
    # Launch autonomous sweeper in background
    asyncio.create_task(autonomous_4l_sweeper(app))
    
    await serve(quart_app, config)

if __name__ == '__main__':
    asyncio.run(main())

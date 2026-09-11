import logging, re, asyncio, httpx, random, string, os
from flask import Flask
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes
from telegram.request import HTTPXRequest
from hypercorn.config import Config
from hypercorn.asyncio import serve

logging.basicConfig(format='%(asctime)s - %(levelname)s - %(message)s', level=logging.INFO)

# 1. Flask App Setup for Render Health Checks
flask_app = Flask(__name__)

@flask_app.route('/')
def home():
    return "Bot status: Active", 200

# Credentials
BOT_TOKEN = '8735644612:AAEhuQSjH0f9pxlUA5Lgl8Bv9fOxpB1Rh3k'
RAPIDAPI_KEY = '345f1277afmsh8ecaec81b86c0e9p1fa406jsne5cb618427ac'

def clean_input(text: str) -> str:
    return text.strip().lstrip('@').lower()

def generate_random_short_candidates(count: int = 10) -> list[str]:
    chars = string.ascii_lowercase + string.digits
    candidates = set()
    patterns = [
        lambda: f"{random.choice(chars)}{random.choice(chars)}{random.choice(chars)}_{random.choice(chars)}",
        lambda: f"{random.choice(chars)}.{random.choice(chars)}{random.choice(chars)}{random.choice(chars)}",
        lambda: f"{random.choice(chars)}{random.choice(chars)}_{random.choice(chars)}{random.choice(chars)}",
        lambda: "".join(random.choices(chars, k=5)),
        lambda: f"{random.choice(chars)}{random.choice(chars)}.{random.choice(chars)}{random.choice(chars)}"
    ]
    while len(candidates) < count:
        cand = random.choice(patterns)()
        if not cand.endswith('.') and not cand.startswith('.'):
            candidates.add(cand)
    return list(candidates)

def build_short_candidates(base_word: str) -> list[str]:
    clean = re.sub(r'[^a-zA-Z0-9]', '', base_word).lower()
    candidates = set()
    
    for char in 'vxyz179_':
        candidates.add(f'{clean}{char}')
        candidates.add(f'{char}{clean}')
        
    if len(clean) >= 3:
        for i in range(1, len(clean)):
            candidates.add(f'{clean[:i]}.{clean[i:]}')
            
    for p in ['i.', 'v.', 'x.', 'x_']:
        candidates.add(f'{p}{clean}')
        candidates.add(f'{clean}_{p[0]}')
        
    no_vowels = re.sub(r'[aeiou]', '', clean)
    if len(no_vowels) >= 3 and no_vowels != clean:
        candidates.add(no_vowels)

    valid_list = [c for c in candidates if 3 <= len(c) <= 24 and not c.endswith('.') and not c.startswith('.')]
    valid_list.sort(key=lambda x: (len(x), x))
    return valid_list[:8]

async def check_single_username(client: httpx.AsyncClient, username: str) -> tuple[str, bool]:
    if len(username) < 2:
        return username, False
    
    url = f'https://tiktok-all-in-one.p.rapidapi.com/user/info?username={username}'
    headers = {
        'X-RapidAPI-Key': RAPIDAPI_KEY,
        'X-RapidAPI-Host': 'tiktok-all-in-one.p.rapidapi.com'
    }
    try:
        res = await client.get(url, headers=headers)
        if res.status_code == 200:
            data = res.json()
            if data.get('error') or data.get('userInfo') is None:
                return username, True
            return username, False
        elif res.status_code == 404:
            return username, True
        return username, False
    except Exception as e:
        logging.error(f"Error checking {username}: {e}")
        return username, False

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logging.info("Received /start command")
    await update.message.reply_text('⚡ Send any word/username to check TikTok availability, or send /short to scan for available short handles!')

async def short_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logging.info("Received /short command")
    msg = await update.message.reply_text('🔍 Scanning for random available short handles...')
    candidates = generate_random_short_candidates(count=12)
    
    async with httpx.AsyncClient(timeout=10.0) as client:
        tasks = [check_single_username(client, cand) for cand in candidates]
        results = await asyncio.gather(*tasks)
        
    available = [uname for uname, is_free in results if is_free]
    
    if available:
        available.sort(key=len)
        reply = '🔥 **Available Short Handles Found:**\n\n'
        for idx, uname in enumerate(available[:5], 1):
            reply += f'{idx}. `@{uname}` ({len(uname)} chars)\n'
        reply += '\n_Send /short again to perform a new scan!_'
    else:
        reply = 'No free short handles found in this batch. Send /short again to retry!'
        
    await msg.edit_text(reply, parse_mode='Markdown')

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_input = clean_input(update.message.text)
    logging.info(f"Received username query: {user_input}")
    
    if len(user_input) < 2 or len(user_input) > 24:
        await update.message.reply_text('Username must be between 2 and 24 characters.')
        return
    
    msg = await update.message.reply_text(f'🔍 Scanning `@{user_input}` & short variations...')
    candidates = build_short_candidates(user_input)
    targets = [user_input] + candidates
    
    async with httpx.AsyncClient(timeout=10.0) as client:
        tasks = [check_single_username(client, target) for target in targets]
        results = await asyncio.gather(*tasks)
    
    available_matches = [uname for uname, is_free in results if is_free]
    exact_free = user_input in available_matches
    if exact_free:
        available_matches.remove(user_input)
        
    reply_text = f'✨ **Results for:** `{user_input}`\n\n'
    reply_text += f'Exact handle **@{user_input}** is **AVAILABLE**!\n\n' if exact_free else f'Exact handle **@{user_input}** is **TAKEN**.\n\n'
    
    if available_matches:
        available_matches.sort(key=len)
        reply_text += '⚡ **Available Short Alternatives:**\n'
        for idx, alt in enumerate(available_matches[:6], 1):
            reply_text += f'{idx}. `@{alt}` ({len(alt)} chars)\n'
    else:
        reply_text += 'No available alternatives found in this batch.'
    
    await msg.edit_text(reply_text, parse_mode='Markdown')

async def main():
    req = HTTPXRequest(connect_timeout=30.0, read_timeout=30.0)
    app = ApplicationBuilder().token(BOT_TOKEN).request(req).build()
    
    app.add_handler(CommandHandler('start', start))
    app.add_handler(CommandHandler('short', short_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    config = Config()
    port = int(os.environ.get("PORT", 10000))
    config.bind = [f"0.0.0.0:{port}"]
    
    logging.info("Starting Flask health check server and Telegram bot concurrently...")
    
    # Initialize and start bot application cleanly on main loop
    await app.initialize()
    await app.start()
    await app.updater.start_polling(drop_pending_updates=True)
    
    # Run Hypercorn serving Flask asynchronously alongside the bot updater
    await serve(flask_app, config)

if __name__ == '__main__':
    asyncio.run(main())

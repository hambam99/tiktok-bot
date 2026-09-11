import logging, re, asyncio, httpx, random, string, os, sqlite3
from quart import Quart
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes
from telegram.request import HTTPXRequest
from hypercorn.config import Config
from hypercorn.asyncio import serve

logging.basicConfig(format='%(asctime)s - %(levelname)s - %(message)s', level=logging.INFO)

# 1. ASGI Web Server for Render Health Check
quart_app = Quart(__name__)

@quart_app.route('/')
async def home():
    return "Bot status: Active", 200

BOT_TOKEN = '8735644612:AAEhuQSjH0f9pxlUA5Lgl8Bv9fOxpB1Rh3k'

# 2. Database Initialization
def init_db():
    conn = sqlite3.connect('tracker.db')
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS tracked_handles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            username TEXT NOT NULL,
            platform TEXT NOT NULL,
            UNIQUE(user_id, username, platform)
        )
    ''')
    conn.commit()
    conn.close()

init_db()

def db_add_track(user_id: int, username: str, platform: str) -> bool:
    try:
        conn = sqlite3.connect('tracker.db')
        cursor = conn.cursor()
        cursor.execute('INSERT INTO tracked_handles (user_id, username, platform) VALUES (?, ?, ?)', (user_id, username, platform))
        conn.commit()
        conn.close()
        return True
    except sqlite3.IntegrityError:
        return False

def db_remove_track(user_id: int, username: str, platform: str) -> bool:
    conn = sqlite3.connect('tracker.db')
    cursor = conn.cursor()
    cursor.execute('DELETE FROM tracked_handles WHERE user_id = ? AND username = ? AND platform = ?', (user_id, username, platform))
    rows = cursor.rowcount
    conn.commit()
    conn.close()
    return rows > 0

def db_get_user_tracks(user_id: int) -> list[tuple[str, str]]:
    conn = sqlite3.connect('tracker.db')
    cursor = conn.cursor()
    cursor.execute('SELECT username, platform FROM tracked_handles WHERE user_id = ?', (user_id,))
    rows = cursor.fetchall()
    conn.close()
    return rows

def db_get_all_tracks() -> list[tuple[int, str, str]]:
    conn = sqlite3.connect('tracker.db')
    cursor = conn.cursor()
    cursor.execute('SELECT user_id, username, platform FROM tracked_handles')
    rows = cursor.fetchall()
    conn.close()
    return rows

def clean_input(text: str) -> str:
    return text.strip().lstrip('@').lower()

def generate_random_short_candidates(count: int = 4) -> list[str]:
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

# 3. Custom Leetspeak & Similarity Engine
def build_short_candidates(base_word: str) -> list[str]:
    clean = re.sub(r'[^a-zA-Z0-9]', '', base_word).lower()
    candidates = set()
    
    # Common Leetspeak Map (e.g. mosleh -> m0sleh, m0sle7, m0sl3h)
    leet_map = {
        'o': '0',
        'e': '3',
        'a': '4',
        'i': '1',
        's': '5',
        't': '7',
        'h': '7',
        'z': '2'
    }
    
    # 1. Single character leetspeak replacements
    for i, char in enumerate(clean):
        if char in leet_map:
            subbed = clean[:i] + leet_map[char] + clean[i+1:]
            candidates.add(subbed)

    # 2. Multi-character combined leetspeak
    full_leet = clean
    for char, rep in leet_map.items():
        full_leet = full_leet.replace(char, rep)
    if full_leet != clean:
        candidates.add(full_leet)

    # 3. Vowels removal (mosleh -> mslh)
    no_vowels = re.sub(r'[aeiou]', '', clean)
    if len(no_vowels) >= 3 and no_vowels != clean:
        candidates.add(no_vowels)

    # 4. Phonetic / Aesthetic vowel shifts (mosleh -> maslh, misleh)
    for v in ['a', 'i', 'x']:
        candidates.add(re.sub(r'[aeiou]', v, clean, count=1))

    # 5. Clean minimalist prefixes & suffixes
    for char in ['_', 'x', 'v', '1', '7']:
        candidates.add(f"{clean}{char}")
        candidates.add(f"{char}{clean}")

    # Remove invalid handles
    valid_list = [c for c in candidates if 3 <= len(c) <= 24 and not c.endswith('.') and not c.startswith('.')]
    valid_list.sort(key=lambda x: (len(x), x))
    return valid_list[:5]  # Returns top 5 variations

# Handle Checker Engines
async def check_tiktok(client: httpx.AsyncClient, username: str) -> tuple[str, bool]:
    if len(username) < 2:
        return username, False
    url = f'https://www.tiktok.com/oembed?url=https://www.tiktok.com/@{username}'
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
        'Accept': 'application/json'
    }
    try:
        res = await client.get(url, headers=headers)
        if res.status_code == 200:
            return username, False
        elif res.status_code in (400, 404):
            return username, True
        return username, False
    except Exception as e:
        logging.error(f"TikTok check error for @{username}: {e}")
        return username, False

async def check_instagram(client: httpx.AsyncClient, username: str) -> tuple[str, bool]:
    if len(username) < 2:
        return username, False
    url = f'https://www.instagram.com/api/v1/users/web_profile_info/?username={username}'
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
        'X-IG-App-ID': '936619743392459',
        'Accept': '*/*'
    }
    try:
        res = await client.get(url, headers=headers)
        if res.status_code == 200:
            return username, False
        elif res.status_code == 404:
            return username, True
        return username, False
    except Exception as e:
        logging.error(f"Instagram check error for @{username}: {e}")
        return username, False

# Background Tracker
async def tracker_background_worker(app):
    while True:
        try:
            await asyncio.sleep(900)
            targets = db_get_all_tracks()
            if not targets:
                continue
                
            logging.info(f"Running drop scan for {len(targets)} tracked handles...")
            async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
                for user_id, username, platform in targets:
                    if platform == 'tiktok':
                        _, is_free = await check_tiktok(client, username)
                    else:
                        _, is_free = await check_instagram(client, username)
                        
                    if is_free:
                        msg_text = (
                            f"🚨 **HANDLE DROP ALERT** 🚨\n\n"
                            f"The handle `@{username}` on **{platform.capitalize()}** is now **AVAILABLE**!\n"
                            f"Claim it immediately before someone else takes it."
                        )
                        try:
                            await app.bot.send_message(chat_id=user_id, text=msg_text, parse_mode='Markdown')
                            db_remove_track(user_id, username, platform)
                        except Exception as send_err:
                            logging.error(f"Failed to alert user {user_id}: {send_err}")
                    await asyncio.sleep(1.5)
        except Exception as e:
            logging.error(f"Tracker worker error: {e}")

# Command Handlers
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "⚡ **Dual Handle & Variation Checker (TikTok + Instagram)**\n\n"
        "• Send any word (e.g. `mosleh`) to check exact status + Leetspeak/Short variations.\n"
        "• Send `/short` to scan for short available handles.\n"
        "• Send `/track <platform> <username>` to monitor drops.\n"
        "• Send `/untrack <platform> <username>` to stop tracking.\n"
        "• Send `/list` to view active monitors."
    )
    await update.message.reply_text(msg, parse_mode='Markdown')

async def track_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2:
        await update.message.reply_text("Usage: `/track <tiktok|ig> <username>`", parse_mode='Markdown')
        return
    
    platform_input = context.args[0].lower()
    platform = 'tiktok' if platform_input in ['tiktok', 'tt'] else 'instagram' if platform_input in ['ig', 'instagram'] else None
    
    if not platform:
        await update.message.reply_text("Please specify a valid platform: `tiktok` or `ig`", parse_mode='Markdown')
        return

    target = clean_input(context.args[1])
    user_id = update.effective_user.id
    
    async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
        if platform == 'tiktok':
            _, is_free = await check_tiktok(client, target)
        else:
            _, is_free = await check_instagram(client, target)
        
    if is_free:
        await update.message.reply_text(f"✨ `@{target}` is already **AVAILABLE** on {platform.capitalize()}!", parse_mode='Markdown')
        return

    success = db_add_track(user_id, target, platform)
    if success:
        await update.message.reply_text(f"🎯 **Tracking Started:** `@{target}` on **{platform.capitalize()}**\n\nI will check every 15 minutes and send an instant drop alert!", parse_mode='Markdown')
    else:
        await update.message.reply_text(f"You are already tracking `@{target}` on {platform.capitalize()}.", parse_mode='Markdown')

async def untrack_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2:
        await update.message.reply_text("Usage: `/untrack <tiktok|ig> <username>`", parse_mode='Markdown')
        return
    
    platform_input = context.args[0].lower()
    platform = 'tiktok' if platform_input in ['tiktok', 'tt'] else 'instagram' if platform_input in ['ig', 'instagram'] else None
    target = clean_input(context.args[1])
    user_id = update.effective_user.id
    
    if platform and db_remove_track(user_id, target, platform):
        await update.message.reply_text(f"Stopped tracking `@{target}` on {platform.capitalize()}.", parse_mode='Markdown')
    else:
        await update.message.reply_text(f"Not currently tracking `@{target}` on that platform.", parse_mode='Markdown')

async def list_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    tracks = db_get_user_tracks(user_id)
    
    if not tracks:
        await update.message.reply_text("You are not tracking any handles right now. Use `/track <platform> <username>` to add one.", parse_mode='Markdown')
        return
    
    reply = "📌 **Your Monitored Handles:**\n\n"
    for idx, (uname, platform) in enumerate(tracks, 1):
        reply += f"{idx}. `@{uname}` — **{platform.capitalize()}**\n"
    reply += "\n_Checking every 15 minutes for drop availability._"
    await update.message.reply_text(reply, parse_mode='Markdown')

async def short_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text('🔍 Scanning for available short handles across platforms...')
    candidates = generate_random_short_candidates(count=3)
    
    tt_free = []
    ig_free = []
    
    async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
        for cand in candidates:
            _, is_tt_free = await check_tiktok(client, cand)
            if is_tt_free:
                tt_free.append(cand)
            await asyncio.sleep(1.0)
            
            _, is_ig_free = await check_instagram(client, cand)
            if is_ig_free:
                ig_free.append(cand)
            await asyncio.sleep(1.0)
        
    reply = "🔥 **Short Handle Results:**\n\n"
    reply += "**TikTok Available:**\n" + ("\n".join([f"• `@{u}`" for u in tt_free]) if tt_free else "None found in batch.") + "\n\n"
    reply += "**Instagram Available:**\n" + ("\n".join([f"• `@{u}`" for u in ig_free]) if ig_free else "None found in batch.")
    
    await msg.edit_text(reply, parse_mode='Markdown')

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_input = clean_input(update.message.text)
    if len(user_input) < 2 or len(user_input) > 24:
        await update.message.reply_text('Username must be between 2 and 24 characters.')
        return
    
    msg = await update.message.reply_text(f'🔍 Scanning `@{user_input}` & similar variations on TikTok + Instagram...')
    
    variations = build_short_candidates(user_input)
    all_targets = [user_input] + variations
    
    tt_available = []
    ig_available = []
    
    async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
        for target in all_targets:
            _, tt_free = await check_tiktok(client, target)
            if tt_free:
                tt_available.append(target)
            await asyncio.sleep(1.0)
            
            _, ig_free = await check_instagram(client, target)
            if ig_free:
                ig_available.append(target)
            await asyncio.sleep(1.0)
            
    exact_tt_free = user_input in tt_available
    exact_ig_free = user_input in ig_available
    
    if exact_tt_free:
        tt_available.remove(user_input)
    if exact_ig_free:
        ig_available.remove(user_input)

    reply_text = f"✨ **Exact Match (`@{user_input}`):**\n"
    reply_text += f"🎵 **TikTok:** {'**AVAILABLE**' if exact_tt_free else 'TAKEN'}\n"
    reply_text += f"📸 **Instagram:** {'**AVAILABLE**' if exact_ig_free else 'TAKEN'}\n\n"
    
    reply_text += "⚡ **Available Similar TikTok Handles:**\n"
    if tt_available:
        for alt in tt_available:
            reply_text += f"• `@{alt}`\n"
    else:
        reply_text += "No variations available in this batch.\n"
        
    reply_text += "\n⚡ **Available Similar Instagram Handles:**\n"
    if ig_available:
        for alt in ig_available:
            reply_text += f"• `@{alt}`\n"
    else:
        reply_text += "No variations available in this batch."

    await msg.edit_text(reply_text, parse_mode='Markdown')

async def main():
    req = HTTPXRequest(connect_timeout=30.0, read_timeout=30.0)
    app = ApplicationBuilder().token(BOT_TOKEN).request(req).build()
    
    app.add_handler(CommandHandler('start', start))
    app.add_handler(CommandHandler('short', short_command))
    app.add_handler(CommandHandler('track', track_command))
    app.add_handler(CommandHandler('untrack', untrack_command))
    app.add_handler(CommandHandler('list', list_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    config = Config()
    port = int(os.environ.get("PORT", 10000))
    config.bind = [f"0.0.0.0:{port}"]
    
    logging.info("Starting Web server, Telegram bot, and Leetspeak Variation Engine...")
    
    await app.initialize()
    await app.start()
    await app.updater.start_polling(drop_pending_updates=True)
    
    asyncio.create_task(tracker_background_worker(app))
    
    await serve(quart_app, config)

if __name__ == '__main__':
    asyncio.run(main())

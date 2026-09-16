import os
import sys
import logging
import asyncio
import random
import time
import re
import json
import string
from typing import Optional, Tuple, Dict, Any, List

import aiosqlite
import httpx
from quart import Quart, request, Response
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode, ChatAction
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters
)
from telegram.request import HTTPXRequest
from hypercorn.config import Config as HyperConfig
from hypercorn.asyncio import serve

# ==============================================================================
# 1. CONFIGURATION & LOGGING
# ==============================================================================

logging.basicConfig(
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    level=logging.INFO
)
logger = logging.getLogger("UltraScannerBot")

class BotConfig:
    """Central configuration management."""
    BOT_TOKEN: str = os.environ.get("BOT_TOKEN", "")
    PORT: int = int(os.environ.get("PORT", 10000))
    RENDER_EXTERNAL_URL: str = os.environ.get("RENDER_EXTERNAL_URL", "")
    
    # Optional Rotating Residential Proxy: e.g. "http://user:pass@proxy.com:8080"
    PROXY_URL: Optional[str] = os.environ.get("PROXY_URL", None)
    
    # Session Cookies for authenticated requests to bypass strict block limits
    IG_SESSION_ID: Optional[str] = os.environ.get("IG_SESSION_ID", None)
    TT_SESSION_ID: Optional[str] = os.environ.get("TT_SESSION_ID", None)

    # Webhook mode enablement
    USE_WEBHOOK: bool = os.environ.get("USE_WEBHOOK", "false").lower() == "true"
    
    # Anti-Spam Rate Limiter: Max 8 requests per 10 seconds per user
    RATE_LIMIT_COUNT: int = 8
    RATE_LIMIT_WINDOW: float = 10.0
    MAX_BATCH_SIZE: int = 10
    DB_FILE: str = "scanner_studio.db"

if not BotConfig.BOT_TOKEN:
    logger.critical("FATAL: 'BOT_TOKEN' environment variable is missing!")
    sys.exit(1)

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4.1 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
]

GLOBAL_HTTP_CLIENT: Optional[httpx.AsyncClient] = None

# ==============================================================================
# 2. DATABASE ENGINE (AIOSQLITE)
# ==============================================================================

async def init_db():
    """Initializes persistent SQLite database for target monitoring."""
    async with aiosqlite.connect(BotConfig.DB_FILE) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS watchlist (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                platform TEXT NOT NULL,
                username TEXT NOT NULL,
                added_at REAL NOT NULL,
                UNIQUE(user_id, platform, username)
            )
        """)
        await db.commit()
    logger.info("Database schema verified.")

async def add_to_watchlist(user_id: int, platform: str, username: str) -> bool:
    try:
        async with aiosqlite.connect(BotConfig.DB_FILE) as db:
            await db.execute(
                "INSERT INTO watchlist (user_id, platform, username, added_at) VALUES (?, ?, ?, ?)",
                (user_id, platform.lower(), username.lower(), time.time())
            )
            await db.commit()
            return True
    except Exception:
        return False

async def remove_from_watchlist(user_id: int, platform: str, username: str) -> bool:
    async with aiosqlite.connect(BotConfig.DB_FILE) as db:
        cursor = await db.execute(
            "DELETE FROM watchlist WHERE user_id = ? AND platform = ? AND username = ?",
            (user_id, platform.lower(), username.lower())
        )
        await db.commit()
        return cursor.rowcount > 0

async def get_user_watchlist(user_id: int) -> List[Tuple[str, str]]:
    async with aiosqlite.connect(BotConfig.DB_FILE) as db:
        cursor = await db.execute(
            "SELECT platform, username FROM watchlist WHERE user_id = ?",
            (user_id,)
        )
        return await cursor.fetchall()

# ==============================================================================
# 3. RATE LIMITER & IN-MEMORY CACHE
# ==============================================================================

class SecurityRateLimiter:
    """Sliding-window rate limiter preventing IP exhaustion."""
    def __init__(self, max_requests: int, window_seconds: float):
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self.user_history: Dict[int, List[float]] = {}

    def is_allowed(self, user_id: int) -> bool:
        now = time.time()
        timestamps = self.user_history.get(user_id, [])
        valid_timestamps = [t for t in timestamps if now - t < self.window_seconds]
        
        if len(valid_timestamps) >= self.max_requests:
            return False
            
        valid_timestamps.append(now)
        self.user_history[user_id] = valid_timestamps
        return True

class CacheManager:
    """TTL cache to eliminate repetitive network checks."""
    def __init__(self, ttl_seconds: float = 300):
        self.cache: Dict[str, Tuple[Tuple[str, str], float]] = {}
        self.ttl = ttl_seconds

    def get(self, key: str) -> Optional[Tuple[str, str]]:
        if key in self.cache:
            data, timestamp = self.cache[key]
            if time.time() - timestamp < self.ttl:
                return data
            del self.cache[key]
        return None

    def set(self, key: str, value: Tuple[str, str]):
        self.cache[key] = (value, time.time())

rate_limiter = SecurityRateLimiter(BotConfig.RATE_LIMIT_COUNT, BotConfig.RATE_LIMIT_WINDOW)
cache_mgr = CacheManager(ttl_seconds=300)

# ==============================================================================
# 4. MULTI-ENDPOINT INSTAGRAM & TIKTOK ENGINES
# ==============================================================================

async def check_instagram_username(username: str) -> Tuple[str, str]:
    clean_user = username.lstrip("@").strip().lower()
    cached = cache_mgr.get(f"ig:{clean_user}")
    if cached:
        return cached

    headers = {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "X-IG-App-ID": "936619743392459",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": f"https://www.instagram.com/{clean_user}/",
    }
    if BotConfig.IG_SESSION_ID:
        headers["Cookie"] = f"sessionid={BotConfig.IG_SESSION_ID};"

    # --- Primary Endpoint: Web Profile Info API ---
    try:
        url = f"https://www.instagram.com/api/v1/users/web_profile_info/?username={clean_user}"
        res = await GLOBAL_HTTP_CLIENT.get(url, headers=headers)
        if res.status_code == 404:
            result = ("AVAILABLE", "Handle is completely free.")
            cache_mgr.set(f"ig:{clean_user}", result)
            return result
        elif res.status_code == 200:
            data = res.json()
            user = data.get("data", {}).get("user")
            if user is None:
                result = ("AVAILABLE", "Handle is free/unassigned.")
            else:
                full_name = user.get('full_name', clean_user)
                result = ("TAKEN", f"Registered to: {full_name}")
            cache_mgr.set(f"ig:{clean_user}", result)
            return result
    except Exception:
        pass

    # --- Secondary Fallback Endpoint: Search API ---
    try:
        fallback_url = f"https://www.instagram.com/web/search/topsearch/?query={clean_user}"
        res = await GLOBAL_HTTP_CLIENT.get(fallback_url, headers=headers)
        if res.status_code == 200:
            users = res.json().get("users", [])
            exact_match = any(u.get("user", {}).get("username", "").lower() == clean_user for u in users)
            result = ("TAKEN", "Account active (Search Fallback).") if exact_match else ("AVAILABLE", "Handle unlisted in search.")
            cache_mgr.set(f"ig:{clean_user}", result)
            return result
    except Exception:
        pass

    result = ("BLOCKED", "Rate limited across endpoints.")
    cache_mgr.set(f"ig:{clean_user}", result)
    return result

async def check_tiktok_username(username: str) -> Tuple[str, str]:
    clean_user = username.lstrip("@").strip().lower()
    cached = cache_mgr.get(f"tt:{clean_user}")
    if cached:
        return cached

    headers = {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    if BotConfig.TT_SESSION_ID:
        headers["Cookie"] = f"sessionid_ss={BotConfig.TT_SESSION_ID};"

    # --- Primary Endpoint: Profile HTML Script Parsing ---
    try:
        url = f"https://www.tiktok.com/@{clean_user}"
        res = await GLOBAL_HTTP_CLIENT.get(url, headers=headers, follow_redirects=True)
        if res.status_code == 404:
            result = ("AVAILABLE", "Profile page returned 404.")
            cache_mgr.set(f"tt:{clean_user}", result)
            return result
        elif res.status_code == 200:
            match = re.search(r'<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__"[^>]*>(.*?)</script>', res.text, re.DOTALL)
            if match:
                payload = json.loads(match.group(1))
                user_detail = payload.get("__DEFAULT_SCOPE__", {}).get("webapp.user-detail", {})
                status_code = user_detail.get("statusCode")
                if status_code in (10221, 10202, 404):
                    result = ("AVAILABLE", "User ID unassigned.")
                else:
                    result = ("TAKEN", "Account is active.")
                cache_mgr.set(f"tt:{clean_user}", result)
                return result
    except Exception:
        pass

    # --- Secondary Fallback Endpoint: User Detail API ---
    try:
        api_url = f"https://www.tiktok.com/api/user/detail/?uniqueId={clean_user}"
        res = await GLOBAL_HTTP_CLIENT.get(api_url, headers=headers)
        if res.status_code == 200:
            user_info = res.json().get("userInfo")
            result = ("TAKEN", "Profile confirmed (API Fallback).") if user_info else ("AVAILABLE", "User not found (API Fallback).")
            cache_mgr.set(f"tt:{clean_user}", result)
            return result
    except Exception:
        pass

    result = ("BLOCKED", "Rate limited across endpoints.")
    cache_mgr.set(f"tt:{clean_user}", result)
    return result

async def scan_single_handle(username: str) -> Dict[str, Tuple[str, str]]:
    ig_task = check_instagram_username(username)
    tt_task = check_tiktok_username(username)
    ig_res, tt_res = await asyncio.gather(ig_task, tt_task)
    return {"instagram": ig_res, "tiktok": tt_res}

# ==============================================================================
# 5. QUART SERVER, WEBHOOKS & BACKGROUND WORKERS
# ==============================================================================

quart_app = Quart(__name__)
BOT_START_TIME = time.time()
TELEGRAM_APP_REF = None  # Global reference for Telegram Webhook handler

@quart_app.route("/")
async def health_check():
    uptime = int(time.time() - BOT_START_TIME)
    return f"🤖 Production Scanner Active | Uptime: {uptime}s", 200

@quart_app.route("/ping")
async def ping():
    return "PONG", 200

@quart_app.route("/webhook", methods=["POST"])
async def telegram_webhook():
    """Ultra-fast Webhook route replacing continuous polling."""
    if TELEGRAM_APP_REF and request.headers.get("content-type") == "application/json":
        data = await request.get_json()
        update = Update.de_json(data, TELEGRAM_APP_REF.bot)
        await TELEGRAM_APP_REF.process_update(update)
        return Response("ok", status=200)
    return Response("error", status=400)

async def keep_alive_task():
    """Background worker keeping Render instances awake nonstop."""
    await asyncio.sleep(10)
    target_url = BotConfig.RENDER_EXTERNAL_URL.rstrip('/') + "/ping" if BotConfig.RENDER_EXTERNAL_URL else f"http://127.0.0.1:{BotConfig.PORT}/ping"
    
    while True:
        try:
            if GLOBAL_HTTP_CLIENT:
                await GLOBAL_HTTP_CLIENT.get(target_url, timeout=10.0)
        except Exception as e:
            logger.debug(f"Keep-alive ping error: {e}")
        await asyncio.sleep(240)

async def handle_sniper_task(telegram_app):
    """Monitors watched handles in the background and alerts users when available."""
    await asyncio.sleep(15)
    logger.info("Target Sniper service activated.")
    while True:
        try:
            async with aiosqlite.connect(BotConfig.DB_FILE) as db:
                cursor = await db.execute("SELECT user_id, platform, username FROM watchlist")
                rows = await cursor.fetchall()

            for user_id, platform, username in rows:
                if platform == "instagram":
                    status, _ = await check_instagram_username(username)
                else:
                    status, _ = await check_tiktok_username(username)

                if status == "AVAILABLE":
                    alert_msg = (
                        f"🚨 **TARGET SNIPED & AVAILABLE!** 🚨\n\n"
                        f"The handle `@{username}` is now **AVAILABLE** on **{platform.capitalize()}**!\n"
                        f"Claim it immediately!"
                    )
                    try:
                        await telegram_app.bot.send_message(chat_id=user_id, text=alert_msg, parse_mode=ParseMode.MARKDOWN)
                        await remove_from_watchlist(user_id, platform, username)
                    except Exception as err:
                        logger.error(f"Failed to send alert to {user_id}: {err}")
                await asyncio.sleep(2)
        except Exception as e:
            logger.error(f"Sniper background worker error: {e}")
        await asyncio.sleep(900)  # Scan target list every 15 minutes

# ==============================================================================
# 6. COMMAND HANDLERS & PATTERN GENERATOR
# ==============================================================================

def get_status_icon(status: str) -> str:
    return "🟢 AVAILABLE" if status == "AVAILABLE" else ("🔴 TAKEN" if status == "TAKEN" else "⚠️ RATE LIMITED")

def build_scan_keyboard(username: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔄 Re-Scan", callback_data=f"rescan:{username}"),
            InlineKeyboardButton("🎯 Watch IG", callback_data=f"watch:instagram:{username}"),
            InlineKeyboardButton("🎯 Watch TT", callback_data=f"watch:tiktok:{username}")
        ]
    ])

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    welcome_text = (
        "🚀 **Ultra Scanner & Target Sniper Studio**\n\n"
        "⚡ **Commands:**\n"
        "• `/scan <username>` — Check IG & TikTok availability\n"
        "• `/batch <user1, user2>` — Scan up to 10 handles simultaneously\n"
        "• `/generate <3char|4char>` — Auto-generate & scan rare handles\n"
        "• `/watch <ig|tt> <user>` — Target sniper: get alerted when a handle drops\n"
        "• `/watchlist` — View all actively monitored target handles\n"
        "• `/unwatch <ig|tt> <user>` — Stop monitoring a target handle\n\n"
        "💡 *Or simply send any username directly in chat!*"
    )
    await update.message.reply_text(welcome_text, parse_mode=ParseMode.MARKDOWN)

async def scan_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not rate_limiter.is_allowed(update.effective_user.id):
        await update.message.reply_text("⏳ **Rate Limit Exceeded.** Please wait 10 seconds.")
        return

    raw_user = context.args[0] if context.args else ""
    if not raw_user:
        await update.message.reply_text("❌ **Specify a username!**\nExample: `/scan luxury`", parse_mode=ParseMode.MARKDOWN)
        return

    clean_user = raw_user.lstrip("@").strip()
    status_msg = await update.message.reply_text(f"🔍 *Scanning `@{clean_user}`...*", parse_mode=ParseMode.MARKDOWN)
    results = await scan_single_handle(clean_user)

    ig_status, ig_info = results["instagram"]
    tt_status, tt_info = results["tiktok"]

    response = (
        f"📊 **Scan Results for `@${clean_user}`**\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"📸 **Instagram:** {get_status_icon(ig_status)}\n"
        f"└ _{ig_info}_\n\n"
        f"🎵 **TikTok:** {get_status_icon(tt_status)}\n"
        f"└ _{tt_info}_"
    )
    await status_msg.edit_text(response, parse_mode=ParseMode.MARKDOWN, reply_markup=build_scan_keyboard(clean_user))

async def batch_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not rate_limiter.is_allowed(update.effective_user.id):
        await update.message.reply_text("⏳ **Rate Limit Exceeded.**")
        return

    if not context.args:
        await update.message.reply_text("❌ Specify usernames separated by commas! Example: `/batch user1, user2`")
        return

    raw_input = " ".join(context.args)
    handles = [h.strip().lstrip("@") for h in re.split(r'[, \n]+', raw_input) if h.strip()][:BotConfig.MAX_BATCH_SIZE]
    status_msg = await update.message.reply_text(f"⚡ *Batch Scanning {len(handles)} handles...*", parse_mode=ParseMode.MARKDOWN)

    semaphore = asyncio.Semaphore(3)
    async def worker(h):
        async with semaphore:
            res = await scan_single_handle(h)
            await asyncio.sleep(0.3)
            return h, res

    results = await asyncio.gather(*[worker(h) for h in handles])
    report = [f"📋 **Batch Scan Report ({len(handles)} handles)**\n━━━━━━━━━━━━━━━━━━━"]
    for handle, res in results:
        ig_st = "🟢" if res["instagram"][0] == "AVAILABLE" else ("🔴" if res["instagram"][0] == "TAKEN" else "⚠️")
        tt_st = "🟢" if res["tiktok"][0] == "AVAILABLE" else ("🔴" if res["tiktok"][0] == "TAKEN" else "⚠️")
        report.append(f"`@{handle}` $\rightarrow$ IG: {ig_st} | TT: {tt_st}")

    await status_msg.edit_text("\n".join(report), parse_mode=ParseMode.MARKDOWN)

async def generate_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Auto-generates pattern handles (e.g., 3-char, 4-char) and batch scans them."""
    if not rate_limiter.is_allowed(update.effective_user.id):
        await update.message.reply_text("⏳ **Rate Limit Exceeded.**")
        return

    gen_type = context.args[0].lower() if context.args else "4char"
    length = 3 if gen_type == "3char" else 4

    # Generate 5 random candidate handles
    candidates = []
    chars = string.ascii_lowercase + string.digits + "_"
    for _ in range(5):
        candidates.append("".join(random.choices(chars, k=length)))

    status_msg = await update.message.reply_text(f"🎲 *Generating & scanning 5 rare `{length}-character` handles...*", parse_mode=ParseMode.MARKDOWN)

    report = [f"🎲 **Pattern Generator (`{length}-char`)**\n━━━━━━━━━━━━━━━━━━━"]
    for handle in candidates:
        res = await scan_single_handle(handle)
        ig_st = "🟢" if res["instagram"][0] == "AVAILABLE" else ("🔴" if res["instagram"][0] == "TAKEN" else "⚠️")
        tt_st = "🟢" if res["tiktok"][0] == "AVAILABLE" else ("🔴" if res["tiktok"][0] == "TAKEN" else "⚠️")
        report.append(f"`@{handle}` $\rightarrow$ IG: {ig_st} | TT: {tt_st}")

    await status_msg.edit_text("\n".join(report), parse_mode=ParseMode.MARKDOWN)

async def watch_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Adds a handle to the sniper watchlist."""
    if len(context.args) < 2:
        await update.message.reply_text("❌ Usage: `/watch <ig|tt> <username>`", parse_mode=ParseMode.MARKDOWN)
        return

    platform = "instagram" if context.args[0].lower() in ("ig", "instagram") else "tiktok"
    username = context.args[1].lstrip("@").strip()

    success = await add_to_watchlist(update.effective_user.id, platform, username)
    if success:
        await update.message.reply_text(f"🎯 **Target Locked!** Monitoring `@{username}` on **{platform.capitalize()}**. You will receive an instant message when it drops!", parse_mode=ParseMode.MARKDOWN)
    else:
        await update.message.reply_text(f"⚠️ `@{username}` is already in your watchlist.")

async def unwatch_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2:
        await update.message.reply_text("❌ Usage: `/unwatch <ig|tt> <username>`", parse_mode=ParseMode.MARKDOWN)
        return

    platform = "instagram" if context.args[0].lower() in ("ig", "instagram") else "tiktok"
    username = context.args[1].lstrip("@").strip()

    removed = await remove_from_watchlist(update.effective_user.id, platform, username)
    if removed:
        await update.message.reply_text(f"🗑️ Removed `@{username}` ({platform}) from your watchlist.", parse_mode=ParseMode.MARKDOWN)
    else:
        await update.message.reply_text("❌ Target handle not found in your watchlist.")

async def watchlist_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    items = await get_user_watchlist(update.effective_user.id)
    if not items:
        await update.message.reply_text("📋 Your watchlist is empty. Add targets with `/watch <ig|tt> <username>`.")
        return

    lines = ["📋 **Your Active Sniper Watchlist**\n━━━━━━━━━━━━━━━━━━━"]
    for platform, username in items:
        lines.append(f"• **{platform.capitalize()}:** `@{username}`")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)

async def handle_direct_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text.startswith("/") or " " in text or len(text) > 30:
        return

    if not rate_limiter.is_allowed(update.effective_user.id):
        await update.message.reply_text("⏳ Wait a few seconds before scanning again.")
        return

    clean_user = text.lstrip("@").strip()
    status_msg = await update.message.reply_text(f"🔍 *Scanning `@{clean_user}`...*", parse_mode=ParseMode.MARKDOWN)
    results = await scan_single_handle(clean_user)

    response = (
        f"📊 **Scan Results for `@${clean_user}`**\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"📸 **Instagram:** {get_status_icon(results['instagram'][0])}\n"
        f"└ _{results['instagram'][1]}_\n\n"
        f"🎵 **TikTok:** {get_status_icon(results['tiktok'][0])}\n"
        f"└ _{results['tiktok'][1]}_"
    )
    await status_msg.edit_text(response, parse_mode=ParseMode.MARKDOWN, reply_markup=build_scan_keyboard(clean_user))

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    data = query.data
    if data.startswith("rescan:"):
        username = data.split(":")[1]
        results = await scan_single_handle(username)
        response = (
            f"📊 **Scan Results for `@${username}`** *(Refreshed)*\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"📸 **Instagram:** {get_status_icon(results['instagram'][0])}\n"
            f"└ _{results['instagram'][1]}_\n\n"
            f"🎵 **TikTok:** {get_status_icon(results['tiktok'][0])}\n"
            f"└ _{results['tiktok'][1]}_"
        )
        await query.edit_message_text(response, parse_mode=ParseMode.MARKDOWN, reply_markup=build_scan_keyboard(username))
    elif data.startswith("watch:"):
        _, platform, username = data.split(":")
        success = await add_to_watchlist(query.from_user.id, platform, username)
        if success:
            await query.message.reply_text(f"🎯 **Target Locked!** Added `@{username}` ({platform}) to your watchlist.")
        else:
            await query.message.reply_text(f"⚠️ `@{username}` is already in your watchlist.")

# ==============================================================================
# 7. MAIN APPLICATION BOOTSTRAPPER
# ==============================================================================

async def main():
    global GLOBAL_HTTP_CLIENT, TELEGRAM_APP_REF
    logger.info("Initializing Commercial Scanner Runtime...")

    await init_db()

    # Configure HTTPX AsyncClient with Proxy if present
    client_kwargs = {
        "limits": httpx.Limits(max_keepalive_connections=50, max_connections=200),
        "timeout": httpx.Timeout(20.0, connect=10.0),
        "follow_redirects": True,
        "http2": True
    }
    if BotConfig.PROXY_URL:
        client_kwargs["proxy"] = BotConfig.PROXY_URL
        logger.info("Residential Proxy Tunnel Configured.")

    GLOBAL_HTTP_CLIENT = httpx.AsyncClient(**client_kwargs)

    request_kwargs = HTTPXRequest(connect_timeout=15.0, read_timeout=20.0)
    telegram_app = ApplicationBuilder().token(BotConfig.BOT_TOKEN).request(request_kwargs).build()
    TELEGRAM_APP_REF = telegram_app

    # Command Handlers
    telegram_app.add_handler(CommandHandler("start", start_command))
    telegram_app.add_handler(CommandHandler("help", start_command))
    telegram_app.add_handler(CommandHandler("scan", scan_command))
    telegram_app.add_handler(CommandHandler("batch", batch_command))
    telegram_app.add_handler(CommandHandler("generate", generate_command))
    telegram_app.add_handler(CommandHandler("watch", watch_command))
    telegram_app.add_handler(CommandHandler("unwatch", unwatch_command))
    telegram_app.add_handler(CommandHandler("watchlist", watchlist_command))
    telegram_app.add_handler(CallbackQueryHandler(callback_handler))
    telegram_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_direct_text))

    await telegram_app.initialize()
    await telegram_app.start()

    # Webhook or Polling Dispatch
    if BotConfig.USE_WEBHOOK and BotConfig.RENDER_EXTERNAL_URL:
        webhook_url = f"{BotConfig.RENDER_EXTERNAL_URL.rstrip('/')}/webhook"
        await telegram_app.bot.set_webhook(url=webhook_url)
        logger.info(f"Webhook registered at: {webhook_url}")
    else:
        await telegram_app.updater.start_polling(drop_pending_updates=True)
        logger.info("Polling mode active.")

    # Background Tasks
    asyncio.create_task(keep_alive_task())
    asyncio.create_task(handle_sniper_task(telegram_app))

    hyper_config = HyperConfig()
    hyper_config.bind = [f"0.0.0.0:{BotConfig.PORT}"]

    try:
        await serve(quart_app, hyper_config)
    finally:
        logger.info("Shutting down engine cleanly...")
        if telegram_app.updater and telegram_app.updater.running:
            await telegram_app.updater.stop()
        await telegram_app.stop()
        await telegram_app.shutdown()
        if GLOBAL_HTTP_CLIENT:
            await GLOBAL_HTTP_CLIENT.aclose()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Execution stopped.")

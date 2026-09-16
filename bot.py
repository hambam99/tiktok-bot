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

# --- Defensive Import for SQLite Persistence ---
try:
    import aiosqlite
    HAS_AIOSQLITE = True
except ImportError:
    HAS_AIOSQLITE = False

import httpx
from quart import Quart, request, Response
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
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

if not HAS_AIOSQLITE:
    logger.warning("WARNING: 'aiosqlite' not found. Falling back to in-memory watchlist.")

class BotConfig:
    BOT_TOKEN: str = os.environ.get("BOT_TOKEN", "").strip()
    PORT: int = int(os.environ.get("PORT", 10000))
    RENDER_EXTERNAL_URL: str = os.environ.get("RENDER_EXTERNAL_URL", "").strip()
    
    PROXY_URL: Optional[str] = os.environ.get("PROXY_URL", None)
    IG_SESSION_ID: Optional[str] = os.environ.get("IG_SESSION_ID", None)
    TT_SESSION_ID: Optional[str] = os.environ.get("TT_SESSION_ID", None)

    USE_WEBHOOK: bool = os.environ.get("USE_WEBHOOK", "false").lower() == "true"
    
    RATE_LIMIT_COUNT: int = 8
    RATE_LIMIT_WINDOW: float = 10.0
    MAX_BATCH_SIZE: int = 10
    DB_FILE: str = os.environ.get("DB_PATH", "scanner_studio.db")

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
IN_MEMORY_WATCHLIST: Dict[int, List[Tuple[str, str]]] = {}

# ==============================================================================
# 2. PERSISTENCE ENGINE WITH FALLBACK
# ==============================================================================

async def init_db():
    if not HAS_AIOSQLITE:
        return
    try:
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
        logger.info("SQLite database schema initialized.")
    except Exception as e:
        logger.error(f"Failed to initialize SQLite DB: {e}. Falling back to in-memory mode.")

async def add_to_watchlist(user_id: int, platform: str, username: str) -> bool:
    plat = platform.lower()
    user_name = username.lower()
    
    if HAS_AIOSQLITE:
        try:
            async with aiosqlite.connect(BotConfig.DB_FILE) as db:
                await db.execute(
                    "INSERT INTO watchlist (user_id, platform, username, added_at) VALUES (?, ?, ?, ?)",
                    (user_id, plat, user_name, time.time())
                )
                await db.commit()
                return True
        except Exception:
            return False
    else:
        user_list = IN_MEMORY_WATCHLIST.setdefault(user_id, [])
        item = (plat, user_name)
        if item not in user_list:
            user_list.append(item)
            return True
        return False

async def remove_from_watchlist(user_id: int, platform: str, username: str) -> bool:
    plat = platform.lower()
    user_name = username.lower()
    
    if HAS_AIOSQLITE:
        try:
            async with aiosqlite.connect(BotConfig.DB_FILE) as db:
                cursor = await db.execute(
                    "DELETE FROM watchlist WHERE user_id = ? AND platform = ? AND username = ?",
                    (user_id, plat, user_name)
                )
                await db.commit()
                return cursor.rowcount > 0
        except Exception:
            return False
    else:
        user_list = IN_MEMORY_WATCHLIST.get(user_id, [])
        item = (plat, user_name)
        if item in user_list:
            user_list.remove(item)
            return True
        return False

async def get_user_watchlist(user_id: int) -> List[Tuple[str, str]]:
    if HAS_AIOSQLITE:
        try:
            async with aiosqlite.connect(BotConfig.DB_FILE) as db:
                cursor = await db.execute(
                    "SELECT platform, username FROM watchlist WHERE user_id = ?",
                    (user_id,)
                )
                return await cursor.fetchall()
        except Exception:
            return []
    return IN_MEMORY_WATCHLIST.get(user_id, [])

async def get_all_watchlist_items() -> List[Tuple[int, str, str]]:
    if HAS_AIOSQLITE:
        try:
            async with aiosqlite.connect(BotConfig.DB_FILE) as db:
                cursor = await db.execute("SELECT user_id, platform, username FROM watchlist")
                return await cursor.fetchall()
        except Exception:
            return []
    
    all_items = []
    for uid, items in IN_MEMORY_WATCHLIST.items():
        for plat, uname in items:
            all_items.append((uid, plat, uname))
    return all_items

# ==============================================================================
# 3. RATE LIMITER & CACHE
# ==============================================================================

class SecurityRateLimiter:
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
# 4. MULTI-ENDPOINT CHECKERS
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
            result = ("AVAILABLE", "Handle is free/unassigned.") if user is None else ("TAKEN", f"Registered to: {user.get('full_name', clean_user)}")
            cache_mgr.set(f"ig:{clean_user}", result)
            return result
    except Exception:
        pass

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
                result = ("AVAILABLE", "User ID unassigned.") if status_code in (10221, 10202, 404) else ("TAKEN", "Account is active.")
                cache_mgr.set(f"tt:{clean_user}", result)
                return result
    except Exception:
        pass

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
    ig_res, tt_res = await asyncio.gather(
        check_instagram_username(username),
        check_tiktok_username(username)
    )
    return {"instagram": ig_res, "tiktok": tt_res}

# ==============================================================================
# 5. SERVER & BACKGROUND WORKERS
# ==============================================================================

quart_app = Quart(__name__)
BOT_START_TIME = time.time()
TELEGRAM_APP_REF = None

@quart_app.route("/")
async def health_check():
    uptime = int(time.time() - BOT_START_TIME)
    return f"🤖 Production Scanner Active | Uptime: {uptime}s", 200

@quart_app.route("/ping")
async def ping():
    return "PONG", 200

@quart_app.route("/webhook", methods=["POST"])
async def telegram_webhook():
    if TELEGRAM_APP_REF and request.headers.get("content-type") == "application/json":
        data = await request.get_json()
        update = Update.de_json(data, TELEGRAM_APP_REF.bot)
        await TELEGRAM_APP_REF.process_update(update)
        return Response("ok", status=200)
    return Response("error", status=400)

async def keep_alive_task():
    await asyncio.sleep(10)
    target_url = BotConfig.RENDER_EXTERNAL_URL.rstrip('/') + "/ping" if BotConfig.RENDER_EXTERNAL_URL else f"http://127.0.0.1:{BotConfig.PORT}/ping"
    while True:
        try:
            if GLOBAL_HTTP_CLIENT:
                await GLOBAL_HTTP_CLIENT.get(target_url, timeout=10.0)
        except Exception as e:
            logger.debug(f"Keep-alive error: {e}")
        await asyncio.sleep(240)

async def handle_sniper_task(telegram_app):
    await asyncio.sleep(15)
    logger.info("Target Sniper background service running.")
    while True:
        try:
            rows = await get_all_watchlist_items()
            for user_id, platform, username in rows:
                status, _ = await check_instagram_username(username) if platform == "instagram" else await check_tiktok_username(username)

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
                        logger.error(f"Failed alert to {user_id}: {err}")
                await asyncio.sleep(2)
        except Exception as e:
            logger.error(f"Sniper background worker error: {e}")
        await asyncio.sleep(900)

# ==============================================================================
# 6. TELEGRAM COMMAND HANDLERS
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
        "• `/watch <ig|tt> <user>` — Target sniper alert when a handle drops\n"
        "• `/watchlist` — View all actively monitored target handles\n"
        "• `/unwatch <ig|tt> <user>` — Remove target handle\n\n"
        "💡 *Or send any handle directly in chat!*"
    )
    await update.message.reply_text(welcome_text, parse_mode=ParseMode.MARKDOWN)

async def scan_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not rate_limiter.is_allowed(update.effective_user.id):
        await update.message.reply_text("⏳ **Rate Limit Exceeded.** Wait 10 seconds.")
        return

    raw_user = context.args[0] if context.args else ""
    if not raw_user:
        await update.message.reply_text("❌ Specify a username!\nExample: `/scan luxury`", parse_mode=ParseMode.MARKDOWN)
        return

    clean_user = raw_user.lstrip("@").strip()
    status_msg = await update.message.reply_text(f"🔍 *Scanning `@{clean_user}`...*", parse_mode=ParseMode.MARKDOWN)
    results = await scan_single_handle(clean_user)

    response = (
        f"📊 **Scan Results for `@{clean_user}`**\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"📸 **Instagram:** {get_status_icon(results['instagram'][0])}\n"
        f"└ _{results['instagram'][1]}_\n\n"
        f"🎵 **TikTok:** {get_status_icon(results['tiktok'][0])}\n"
        f"└ _{results['tiktok'][1]}_"
    )
    await status_msg.edit_text(response, parse_mode=ParseMode.MARKDOWN, reply_markup=build_scan_keyboard(clean_user))

async def batch_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not rate_limiter.is_allowed(update.effective_user.id):
        await update.message.reply_text("⏳ **Rate Limit Exceeded.**")
        return

    if not context.args:
        await update.message.reply_text("❌ Specify usernames! Example: `/batch user1, user2`", parse_mode=ParseMode.MARKDOWN)
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
        report.append(f"`@{handle}` -> IG: {ig_st} | TT: {tt_st}")

    await status_msg.edit_text("\n".join(report), parse_mode=ParseMode.MARKDOWN)

async def generate_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not rate_limiter.is_allowed(update.effective_user.id):
        await update.message.reply_text("⏳ **Rate Limit Exceeded.**")
        return

    gen_type = context.args[0].lower() if context.args else "4char"
    length = 3 if gen_type == "3char" else 4
    chars = string.ascii_lowercase + string.digits + "_"
    candidates = ["".join(random.choices(chars, k=length)) for _ in range(5)]

    status_msg = await update.message.reply_text(f"🎲 *Scanning 5 random `{length}-char` handles...*", parse_mode=ParseMode.MARKDOWN)

    report = [f"🎲 **Pattern Generator (`{length}-char`)**\n━━━━━━━━━━━━━━━━━━━"]
    for handle in candidates:
        res = await scan_single_handle(handle)
        ig_st = "🟢" if res["instagram"][0] == "AVAILABLE" else ("🔴" if res["instagram"][0] == "TAKEN" else "⚠️")
        tt_st = "🟢" if res["tiktok"][0] == "AVAILABLE" else ("🔴" if res["tiktok"][0] == "TAKEN" else "⚠️")
        report.append(f"`@{handle}` -> IG: {ig_st} | TT: {tt_st}")

    await status_msg.edit_text("\n".join(report), parse_mode=ParseMode.MARKDOWN)

async def watch_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2:
        await update.message.reply_text("❌ Usage: `/watch <ig|tt> <username>`", parse_mode=ParseMode.MARKDOWN)
        return

    platform = "instagram" if context.args[0].lower() in ("ig", "instagram") else "tiktok"
    username = context.args[1].lstrip("@").strip()

    if await add_to_watchlist(update.effective_user.id, platform, username):
        await update.message.reply_text(f"🎯 **Target Locked!** Monitoring `@{username}` on **{platform.capitalize()}**.", parse_mode=ParseMode.MARKDOWN)
    else:
        await update.message.reply_text(f"⚠️ `@{username}` is already in your watchlist.")

async def unwatch_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2:
        await update.message.reply_text("❌ Usage: `/unwatch <ig|tt> <username>`", parse_mode=ParseMode.MARKDOWN)
        return

    platform = "instagram" if context.args[0].lower() in ("ig", "instagram") else "tiktok"
    username = context.args[1].lstrip("@").strip()

    if await remove_from_watchlist(update.effective_user.id, platform, username):
        await update.message.reply_text(f"🗑️ Removed `@{username}` from watchlist.", parse_mode=ParseMode.MARKDOWN)
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
        f"📊 **Scan Results for `@{clean_user}`**\n"
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
            f"📊 **Scan Results for `@{username}`** *(Refreshed)*\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"📸 **Instagram:** {get_status_icon(results['instagram'][0])}\n"
            f"└ _{results['instagram'][1]}_\n\n"
            f"🎵 **TikTok:** {get_status_icon(results['tiktok'][0])}\n"
            f"└ _{results['tiktok'][1]}_"
        )
        await query.edit_message_text(response, parse_mode=ParseMode.MARKDOWN, reply_markup=build_scan_keyboard(username))
    elif data.startswith("watch:"):
        _, platform, username = data.split(":")
        if await add_to_watchlist(query.from_user.id, platform, username):
            await query.message.reply_text(f"🎯 **Target Locked!** Added `@{username}` ({platform}) to your watchlist.")
        else:
            await query.message.reply_text(f"⚠️ `@{username}` is already in your watchlist.")

# ==============================================================================
# 7. MAIN RUNTIME
# ==============================================================================

async def main():
    global GLOBAL_HTTP_CLIENT, TELEGRAM_APP_REF
    logger.info("Starting Commercial Scanner Engine...")

    await init_db()

    client_kwargs = {
        "limits": httpx.Limits(max_keepalive_connections=50, max_connections=200),
        "timeout": httpx.Timeout(20.0, connect=10.0),
        "follow_redirects": True,
    }

    # Safe HTTP/2 Initialization check
    try:
        GLOBAL_HTTP_CLIENT = httpx.AsyncClient(http2=True, **client_kwargs)
    except Exception:
        logger.warning("HTTP/2 library missing. Falling back to HTTP/1.1 client.")
        GLOBAL_HTTP_CLIENT = httpx.AsyncClient(http2=False, **client_kwargs)

    request_kwargs = HTTPXRequest(connect_timeout=15.0, read_timeout=20.0)
    telegram_app = ApplicationBuilder().token(BotConfig.BOT_TOKEN).request(request_kwargs).build()
    TELEGRAM_APP_REF = telegram_app

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

    if BotConfig.USE_WEBHOOK and BotConfig.RENDER_EXTERNAL_URL:
        webhook_url = f"{BotConfig.RENDER_EXTERNAL_URL.rstrip('/')}/webhook"
        await telegram_app.bot.set_webhook(url=webhook_url)
        logger.info(f"Webhook set: {webhook_url}")
    else:
        await telegram_app.updater.start_polling(drop_pending_updates=True)
        logger.info("Polling mode active.")

    asyncio.create_task(keep_alive_task())
    asyncio.create_task(handle_sniper_task(telegram_app))

    hyper_config = HyperConfig()
    hyper_config.bind = [f"0.0.0.0:{BotConfig.PORT}"]

    try:
        await serve(quart_app, hyper_config)
    finally:
        logger.info("Shutting down scanner engine cleanly...")
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
        logger.info("Scanner stopped cleanly.")

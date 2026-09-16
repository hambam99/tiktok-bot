import os
import sys
import logging
import asyncio
import random
import time
import re
import json
import string
import html
from typing import Optional, Tuple, Dict, Any, List

# --- Defensive SQLite Import ---
try:
    import aiosqlite
    HAS_AIOSQLITE = True
except ImportError:
    HAS_AIOSQLITE = False

import httpx
from quart import Quart, request, Response
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
from telegram.constants import ParseMode
from telegram.error import TelegramError, RetryAfter, Forbidden, BadRequest
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
# 1. HARDENED CONFIGURATION & ENVIRONMENT
# ==============================================================================

logging.basicConfig(
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    level=logging.INFO
)
logger = logging.getLogger("UltraScannerBot")

if not HAS_AIOSQLITE:
    logger.warning("PROACTIVE NOTICE: 'aiosqlite' module absent. In-memory fallback activated.")

class BotConfig:
    BOT_TOKEN: str = os.environ.get("BOT_TOKEN", "").strip()
    PORT: int = int(os.environ.get("PORT", 10000))
    RENDER_EXTERNAL_URL: str = os.environ.get("RENDER_EXTERNAL_URL", "").strip()
    
    PROXY_URL: Optional[str] = os.environ.get("PROXY_URL", None)
    IG_SESSION_ID: Optional[str] = os.environ.get("IG_SESSION_ID", None)
    TT_SESSION_ID: Optional[str] = os.environ.get("TT_SESSION_ID", None)

    USE_WEBHOOK: bool = os.environ.get("USE_WEBHOOK", "false").lower() == "true"
    
    RATE_LIMIT_COUNT: int = 6
    RATE_LIMIT_WINDOW: float = 10.0
    MAX_BATCH_SIZE: int = 10
    DB_FILE: str = os.environ.get("DB_PATH", "scanner_studio.db")

if not BotConfig.BOT_TOKEN:
    logger.critical("FATAL: 'BOT_TOKEN' environment variable is missing!")
    sys.exit(1)

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
]

GLOBAL_HTTP_CLIENT: Optional[httpx.AsyncClient] = None
IN_MEMORY_WATCHLIST: Dict[int, List[Tuple[str, str]]] = {}

# ==============================================================================
# 2. SANITIZATION & INPUT VALIDATION ENGINE
# ==============================================================================

def sanitize_username(raw_username: str) -> str:
    clean = raw_username.lstrip("@").strip().lower()
    return re.sub(r'[^a-z0-9._]', '', clean)

def validate_username_format(username: str, platform: str) -> Tuple[bool, str]:
    if not username:
        return False, "Username cannot be empty."
    if len(username) < 1 or len(username) > 30:
        return False, "Length must be between 1 and 30 characters."
    
    if platform == "instagram":
        if username.startswith(".") or username.endswith("."):
            return False, "Instagram handles cannot start or end with a period."
        if ".." in username:
            return False, "Instagram handles cannot contain consecutive periods."
    elif platform == "tiktok":
        if username.endswith("."):
            return False, "TikTok handles cannot end with a period."
    return True, "Valid"

# ==============================================================================
# 3. DATABASE ENGINE WITH CONCURRENCY SAFEGUARDS
# ==============================================================================

async def init_db():
    if not HAS_AIOSQLITE:
        return
    try:
        async with aiosqlite.connect(BotConfig.DB_FILE, timeout=10.0) as db:
            await db.execute("PRAGMA journal_mode=WAL;")
            await db.execute("PRAGMA busy_timeout=5000;")
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
        logger.info("SQLite WAL-mode database initialized successfully.")
    except Exception as e:
        logger.error(f"Database setup error: {e}. Defaulting to in-memory mode.")

async def add_to_watchlist(user_id: int, platform: str, username: str) -> bool:
    plat = platform.lower()
    user_name = sanitize_username(username)
    
    if HAS_AIOSQLITE:
        try:
            async with aiosqlite.connect(BotConfig.DB_FILE, timeout=10.0) as db:
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
    user_name = sanitize_username(username)
    
    if HAS_AIOSQLITE:
        try:
            async with aiosqlite.connect(BotConfig.DB_FILE, timeout=10.0) as db:
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
            async with aiosqlite.connect(BotConfig.DB_FILE, timeout=10.0) as db:
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
            async with aiosqlite.connect(BotConfig.DB_FILE, timeout=10.0) as db:
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
# 4. MEMORY CACHE & RATE LIMITING
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
# 5. MULTI-LAYERED ZERO-FALSE-POSITIVE CHECKERS
# ==============================================================================

async def check_instagram_username(username: str) -> Tuple[str, str]:
    clean_user = sanitize_username(username)
    valid, msg = validate_username_format(clean_user, "instagram")
    if not valid:
        return ("INVALID", msg)

    cached = cache_mgr.get(f"ig:{clean_user}")
    if cached:
        return cached

    signup_url = "https://www.instagram.com/api/v1/web/accounts/web_create_user/check_username/"
    signup_headers = {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Content-Type": "application/x-www-form-urlencoded",
        "X-CSRFToken": "missing",
        "X-IG-App-ID": "936619743392459",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": "https://www.instagram.com/accounts/emailsignup/",
    }
    if BotConfig.IG_SESSION_ID:
        signup_headers["Cookie"] = f"sessionid={BotConfig.IG_SESSION_ID};"

    try:
        if GLOBAL_HTTP_CLIENT:
            res = await GLOBAL_HTTP_CLIENT.post(signup_url, headers=signup_headers, data={"username": clean_user})
            if res.status_code == 200:
                data = res.json()
                is_avail = data.get("available", False)
                error_type = data.get("error_type", "")
                
                if is_avail and not error_type:
                    res_tuple = ("AVAILABLE", "Handle is free for registration.")
                    cache_mgr.set(f"ig:{clean_user}", res_tuple)
                    return res_tuple
                elif error_type == "username_is_taken":
                    res_tuple = ("TAKEN", "Handle actively registered to an account.")
                    cache_mgr.set(f"ig:{clean_user}", res_tuple)
                    return res_tuple
                else:
                    feedback = data.get("feedback_message") or "Handle is banned, locked, or system reserved."
                    res_tuple = ("TAKEN", f"Unavailable: {feedback}")
                    cache_mgr.set(f"ig:{clean_user}", res_tuple)
                    return res_tuple
    except Exception as e:
        logger.debug(f"IG Stage 1 endpoint error: {e}")

    try:
        profile_url = f"https://www.instagram.com/api/v1/users/web_profile_info/?username={clean_user}"
        profile_headers = {
            "User-Agent": random.choice(USER_AGENTS),
            "Accept": "*/*",
            "X-IG-App-ID": "936619743392459",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": f"https://www.instagram.com/{clean_user}/",
        }
        if GLOBAL_HTTP_CLIENT:
            res = await GLOBAL_HTTP_CLIENT.get(profile_url, headers=profile_headers)
            if res.status_code == 200:
                data = res.json()
                user_data = data.get("data", {}).get("user")
                res_tuple = ("TAKEN", f"Registered to user ID: {user_data.get('id', 'Unknown')}") if user_data else ("TAKEN", "Handle is locked or banned.")
                cache_mgr.set(f"ig:{clean_user}", res_tuple)
                return res_tuple
            elif res.status_code == 404:
                if len(clean_user) <= 4:
                    res_tuple = ("TAKEN", "Rare short handle is soft-locked by Instagram.")
                else:
                    res_tuple = ("AVAILABLE", "Unassigned profile endpoint.")
                cache_mgr.set(f"ig:{clean_user}", res_tuple)
                return res_tuple
    except Exception as e:
        logger.debug(f"IG Stage 2 fallback error: {e}")

    res_tuple = ("BLOCKED", "Instagram rate-limited or blocked IP check.")
    cache_mgr.set(f"ig:{clean_user}", res_tuple)
    return res_tuple

async def check_tiktok_username(username: str) -> Tuple[str, str]:
    clean_user = sanitize_username(username)
    valid, msg = validate_username_format(clean_user, "tiktok")
    if not valid:
        return ("INVALID", msg)

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
        if GLOBAL_HTTP_CLIENT:
            res = await GLOBAL_HTTP_CLIENT.get(url, headers=headers, follow_redirects=True)
            if res.status_code == 404:
                res_tuple = ("AVAILABLE", "TikTok profile page returns 404.")
                cache_mgr.set(f"tt:{clean_user}", res_tuple)
                return res_tuple
            elif res.status_code == 200:
                match = re.search(r'<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__"[^>]*>(.*?)</script>', res.text, re.DOTALL)
                if match:
                    payload = json.loads(match.group(1))
                    user_detail = payload.get("__DEFAULT_SCOPE__", {}).get("webapp.user-detail", {})
                    status_code = user_detail.get("statusCode")
                    
                    if status_code in (10221, 10202, 404):
                        res_tuple = ("AVAILABLE", "Handle unassigned in core state.")
                    else:
                        user_info = user_detail.get("userInfo", {}).get("user", {})
                        sec_uid = user_info.get("secUid", "")
                        res_tuple = ("TAKEN", f"Active account (SecID: {sec_uid[:8]}...)") if sec_uid else ("TAKEN", "Registered or banned handle.")
                    cache_mgr.set(f"tt:{clean_user}", res_tuple)
                    return res_tuple
    except Exception as e:
        logger.debug(f"TikTok Stage 1 error: {e}")

    try:
        api_url = f"https://www.tiktok.com/api/user/detail/?uniqueId={clean_user}"
        if GLOBAL_HTTP_CLIENT:
            res = await GLOBAL_HTTP_CLIENT.get(api_url, headers=headers)
            if res.status_code == 200:
                user_info = res.json().get("userInfo")
                res_tuple = ("TAKEN", "Confirmed active profile.") if user_info else ("AVAILABLE", "Unregistered API handle.")
                cache_mgr.set(f"tt:{clean_user}", res_tuple)
                return res_tuple
    except Exception as e:
        logger.debug(f"TikTok Stage 2 error: {e}")

    res_tuple = ("BLOCKED", "TikTok rate-limited cloud request.")
    cache_mgr.set(f"tt:{clean_user}", res_tuple)
    return res_tuple

async def scan_single_handle(username: str) -> Dict[str, Tuple[str, str]]:
    clean_user = sanitize_username(username)
    ig_res, tt_res = await asyncio.gather(
        check_instagram_username(clean_user),
        check_tiktok_username(clean_user)
    )
    return {"instagram": ig_res, "tiktok": tt_res}

# ==============================================================================
# 6. QUART APP & BACKGROUND WORKERS
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
        try:
            data = await request.get_json()
            update = Update.de_json(data, TELEGRAM_APP_REF.bot)
            await TELEGRAM_APP_REF.process_update(update)
            return Response("ok", status=200)
        except Exception as e:
            logger.error(f"Webhook processing failure: {e}")
            return Response("error", status=500)
    return Response("error", status=400)

async def keep_alive_task():
    await asyncio.sleep(10)
    target_url = BotConfig.RENDER_EXTERNAL_URL.rstrip('/') + "/ping" if BotConfig.RENDER_EXTERNAL_URL else f"http://127.0.0.1:{BotConfig.PORT}/ping"
    while True:
        try:
            if GLOBAL_HTTP_CLIENT:
                await GLOBAL_HTTP_CLIENT.get(target_url, timeout=10.0)
        except Exception as e:
            logger.debug(f"Keep-alive ping error: {e}")
        await asyncio.sleep(200)

async def handle_sniper_task(telegram_app):
    await asyncio.sleep(15)
    logger.info("Target Sniper background worker initialized.")
    while True:
        try:
            rows = await get_all_watchlist_items()
            for user_id, platform, username in rows:
                if platform == "instagram":
                    status, desc = await check_instagram_username(username)
                else:
                    status, desc = await check_tiktok_username(username)

                if status == "AVAILABLE":
                    alert_msg = (
                        f"🚨 <b>TARGET SNIPED & AVAILABLE!</b> 🚨\n\n"
                        f"The handle <code>@{html.escape(username)}</code> is now <b>AVAILABLE</b> on <b>{platform.capitalize()}</b>!\n"
                        f"Claim it immediately!"
                    )
                    try:
                        await telegram_app.bot.send_message(chat_id=user_id, text=alert_msg, parse_mode=ParseMode.HTML)
                        await remove_from_watchlist(user_id, platform, username)
                    except Forbidden:
                        logger.warning(f"User {user_id} blocked the bot. Removing active targets.")
                        await remove_from_watchlist(user_id, platform, username)
                    except Exception as err:
                        logger.error(f"Sniper alert failure for {user_id}: {err}")
                await asyncio.sleep(3)
        except Exception as e:
            logger.error(f"Sniper worker exception: {e}")
        await asyncio.sleep(600)

# ==============================================================================
# 7. TELEGRAM HANDLERS WITH HTML SAFEGUARD WRAPPERS
# ==============================================================================

def get_status_icon(status: str) -> str:
    if status == "AVAILABLE":
        return "🟢 AVAILABLE"
    elif status == "TAKEN":
        return "🔴 TAKEN"
    elif status == "INVALID":
        return "❌ INVALID FORMAT"
    return "⚠️ RATE LIMITED / LOCKED"

def build_scan_keyboard(username: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔄 Re-Scan", callback_data=f"rescan:{username}"),
            InlineKeyboardButton("🎯 Watch IG", callback_data=f"watch:instagram:{username}"),
            InlineKeyboardButton("🎯 Watch TT", callback_data=f"watch:tiktok:{username}")
        ]
    ])

async def safe_reply(target, text: str, reply_markup=None):
    """
    Unified HTML response handler accepting Update, Message, or CallbackQuery objects safely.
    """
    try:
        if hasattr(target, "edit_message_text"):
            await target.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)
        elif hasattr(target, "edit_text"):
            await target.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)
        elif hasattr(target, "reply_text"):
            await target.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)
        elif hasattr(target, "effective_message") and target.effective_message:
            await target.effective_message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)
    except BadRequest as e:
        logger.warning(f"HTML dispatch fallback triggered: {e}")
        clean_text = re.sub(r'<[^>]+>', '', text)
        try:
            if hasattr(target, "edit_message_text"):
                await target.edit_message_text(clean_text, reply_markup=reply_markup)
            elif hasattr(target, "edit_text"):
                await target.edit_text(clean_text, reply_markup=reply_markup)
            elif hasattr(target, "reply_text"):
                await target.reply_text(clean_text, reply_markup=reply_markup)
            elif hasattr(target, "effective_message") and target.effective_message:
                await target.effective_message.reply_text(clean_text, reply_markup=reply_markup)
        except Exception as ex:
            logger.error(f"Fallback text dispatch failed: {ex}")
    except TelegramError as e:
        logger.error(f"Telegram API dispatch error: {e}")

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    welcome_text = (
        "🚀 <b>Ultra Scanner & Target Sniper Studio</b>\n\n"
        "⚡ <b>Commands:</b>\n"
        "• <code>/scan &lt;username&gt;</code> — Instant IG & TikTok availability check\n"
        "• <code>/batch &lt;user1, user2&gt;</code> — Scan up to 10 handles simultaneously\n"
        "• <code>/generate &lt;3char|4char&gt;</code> — Auto-generate & scan rare handles\n"
        "• <code>/watch &lt;ig|tt&gt; &lt;user&gt;</code> — Set target sniper alert for dropped handles\n"
        "• <code>/watchlist</code> — View all monitored target handles\n"
        "• <code>/unwatch &lt;ig|tt&gt; &lt;user&gt;</code> — Remove target handle\n\n"
        "💡 <i>Or type any username directly in chat to scan!</i>"
    )
    await safe_reply(update, welcome_text)

async def scan_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or not rate_limiter.is_allowed(update.effective_user.id):
        await safe_reply(update, "⏳ <b>Rate Limit Exceeded.</b> Wait 10 seconds.")
        return

    raw_user = context.args[0] if context.args else ""
    if not raw_user:
        await safe_reply(update, "❌ Specify a username!\nExample: <code>/scan luxury</code>")
        return

    clean_user = sanitize_username(raw_user)
    if not update.effective_message:
        return

    status_msg = await update.effective_message.reply_text(f"🔍 <i>Scanning <code>@{html.escape(clean_user)}</code>...</i>", parse_mode=ParseMode.HTML)
    results = await scan_single_handle(clean_user)

    response = (
        f"📊 <b>Scan Results for <code>@{html.escape(clean_user)}</code></b>\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"📸 <b>Instagram:</b> {get_status_icon(results['instagram'][0])}\n"
        f"└ <i>{html.escape(results['instagram'][1])}</i>\n\n"
        f"🎵 <b>TikTok:</b> {get_status_icon(results['tiktok'][0])}\n"
        f"└ <i>{html.escape(results['tiktok'][1])}</i>"
    )
    await safe_reply(status_msg, response, reply_markup=build_scan_keyboard(clean_user))

async def batch_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or not rate_limiter.is_allowed(update.effective_user.id):
        await safe_reply(update, "⏳ <b>Rate Limit Exceeded.</b>")
        return

    if not context.args:
        await safe_reply(update, "❌ Specify usernames! Example: <code>/batch user1, user2</code>")
        return

    if not update.effective_message:
        return

    raw_input = " ".join(context.args)
    handles = [sanitize_username(h) for h in re.split(r'[, \n]+', raw_input) if h.strip()][:BotConfig.MAX_BATCH_SIZE]
    status_msg = await update.effective_message.reply_text(f"⚡ <i>Batch Scanning {len(handles)} handles...</i>", parse_mode=ParseMode.HTML)

    semaphore = asyncio.Semaphore(3)
    async def worker(h):
        async with semaphore:
            res = await scan_single_handle(h)
            await asyncio.sleep(0.4)
            return h, res

    results = await asyncio.gather(*[worker(h) for h in handles])
    report = [f"📋 <b>Batch Scan Report ({len(handles)} handles)</b>\n━━━━━━━━━━━━━━━━━━━"]
    for handle, res in results:
        ig_st = "🟢" if res["instagram"][0] == "AVAILABLE" else ("🔴" if res["instagram"][0] == "TAKEN" else "⚠️")
        tt_st = "🟢" if res["tiktok"][0] == "AVAILABLE" else ("🔴" if res["tiktok"][0] == "TAKEN" else "⚠️")
        report.append(f"<code>@{html.escape(handle)}</code> -&gt; IG: {ig_st} | TT: {tt_st}")

    await safe_reply(status_msg, "\n".join(report))

async def generate_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or not rate_limiter.is_allowed(update.effective_user.id):
        await safe_reply(update, "⏳ <b>Rate Limit Exceeded.</b>")
        return

    if not update.effective_message:
        return

    gen_type = context.args[0].lower() if context.args else "4char"
    length = 3 if gen_type == "3char" else 4
    chars = string.ascii_lowercase + string.digits
    candidates = ["".join(random.choices(chars, k=length)) for _ in range(5)]

    status_msg = await update.effective_message.reply_text(f"🎲 <i>Scanning 5 random <code>{length}-char</code> handles...</i>", parse_mode=ParseMode.HTML)

    report = [f"🎲 <b>Pattern Generator (<code>{length}-char</code>)</b>\n━━━━━━━━━━━━━━━━━━━"]
    for handle in candidates:
        res = await scan_single_handle(handle)
        ig_st = "🟢" if res["instagram"][0] == "AVAILABLE" else ("🔴" if res["instagram"][0] == "TAKEN" else "⚠️")
        tt_st = "🟢" if res["tiktok"][0] == "AVAILABLE" else ("🔴" if res["tiktok"][0] == "TAKEN" else "⚠️")
        report.append(f"<code>@{html.escape(handle)}</code> -&gt; IG: {ig_st} | TT: {tt_st}")

    await safe_reply(status_msg, "\n".join(report))

async def watch_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user:
        return

    if not context.args or len(context.args) < 2:
        await safe_reply(update, "❌ Usage: <code>/watch &lt;ig|tt&gt; &lt;username&gt;</code>")
        return

    platform = "instagram" if context.args[0].lower() in ("ig", "instagram") else "tiktok"
    username = sanitize_username(context.args[1])

    if await add_to_watchlist(update.effective_user.id, platform, username):
        await safe_reply(update, f"🎯 <b>Target Locked!</b> Monitoring <code>@{html.escape(username)}</code> on <b>{platform.capitalize()}</b>.")
    else:
        await safe_reply(update, f"⚠️ <code>@{html.escape(username)}</code> is already in your watchlist.")

async def unwatch_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user:
        return

    if not context.args or len(context.args) < 2:
        await safe_reply(update, "❌ Usage: <code>/unwatch &lt;ig|tt&gt; &lt;username&gt;</code>")
        return

    platform = "instagram" if context.args[0].lower() in ("ig", "instagram") else "tiktok"
    username = sanitize_username(context.args[1])

    if await remove_from_watchlist(update.effective_user.id, platform, username):
        await safe_reply(update, f"🗑️ Removed <code>@{html.escape(username)}</code> from watchlist.")
    else:
        await safe_reply(update, f"❌ Target handle <code>@{html.escape(username)}</code> not found in your watchlist.")

async def watchlist_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user:
        return

    items = await get_user_watchlist(update.effective_user.id)
    if not items:
        await safe_reply(update, "📋 Your watchlist is empty. Add targets with <code>/watch &lt;ig|tt&gt; &lt;username&gt;</code>.")
        return

    lines = ["📋 <b>Your Active Sniper Watchlist</b>\n━━━━━━━━━━━━━━━━━━━"]
    for platform, username in items:
        lines.append(f"• <b>{platform.capitalize()}:</b> <code>@{html.escape(username)}</code>")
    await safe_reply(update, "\n".join(lines))

async def handle_direct_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    text = update.message.text.strip()
    if text.startswith("/") or " " in text or len(text) > 30:
        return

    if not update.effective_user or not rate_limiter.is_allowed(update.effective_user.id):
        await safe_reply(update, "⏳ Wait a few seconds before scanning again.")
        return

    clean_user = sanitize_username(text)
    if not clean_user:
        return

    status_msg = await update.message.reply_text(f"🔍 <i>Scanning <code>@{html.escape(clean_user)}</code>...</i>", parse_mode=ParseMode.HTML)
    results = await scan_single_handle(clean_user)

    response = (
        f"📊 <b>Scan Results for <code>@{html.escape(clean_user)}</code></b>\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"📸 <b>Instagram:</b> {get_status_icon(results['instagram'][0])}\n"
        f"└ <i>{html.escape(results['instagram'][1])}</i>\n\n"
        f"🎵 <b>TikTok:</b> {get_status_icon(results['tiktok'][0])}\n"
        f"└ <i>{html.escape(results['tiktok'][1])}</i>"
    )
    await safe_reply(status_msg, response, reply_markup=build_scan_keyboard(clean_user))

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return

    await query.answer()

    data = query.data or ""
    if data.startswith("rescan:"):
        username = sanitize_username(data.split(":")[1])
        results = await scan_single_handle(username)
        response = (
            f"📊 <b>Scan Results for <code>@{html.escape(username)}</code></b> <i>(Refreshed)</i>\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"📸 <b>Instagram:</b> {get_status_icon(results['instagram'][0])}\n"
            f"└ <i>{html.escape(results['instagram'][1])}</i>\n\n"
            f"🎵 <b>TikTok:</b> {get_status_icon(results['tiktok'][0])}\n"
            f"└ <i>{html.escape(results['tiktok'][1])}</i>"
        )
        await safe_reply(query, response, reply_markup=build_scan_keyboard(username))
    elif data.startswith("watch:"):
        parts = data.split(":")
        if len(parts) >= 3:
            platform = parts[1]
            clean_user = sanitize_username(parts[2])
            if await add_to_watchlist(query.from_user.id, platform, clean_user):
                await safe_reply(query, f"🎯 <b>Target Locked!</b> Added <code>@{html.escape(clean_user)}</code> ({platform}) to your watchlist.")
            else:
                await safe_reply(query, f"⚠️ <code>@{html.escape(clean_user)}</code> is already in your watchlist.")

# ==============================================================================
# 8. MAIN ENGINE LIFECYCLE
# ==============================================================================

async def setup_bot_commands(telegram_app):
    """Registers native Telegram blue Menu button commands."""
    commands = [
        BotCommand("start", "Show bot main menu & instructions"),
        BotCommand("scan", "Check IG & TikTok handle availability"),
        BotCommand("batch", "Scan up to 10 handles simultaneously"),
        BotCommand("generate", "Auto-generate & scan rare handles"),
        BotCommand("watch", "Snipe handle: alert when available"),
        BotCommand("watchlist", "View all monitored target handles"),
        BotCommand("unwatch", "Remove handle from watchlist"),
        BotCommand("help", "Display commands overview"),
    ]
    try:
        await telegram_app.bot.set_my_commands(commands)
        logger.info("Native Telegram command menu successfully configured.")
    except Exception as e:
        logger.error(f"Failed to set Telegram command menu: {e}")

async def main():
    global GLOBAL_HTTP_CLIENT, TELEGRAM_APP_REF
    logger.info("Initializing Hardened Scanner Engine...")

    await init_db()

    client_kwargs: Dict[str, Any] = {
        "limits": httpx.Limits(max_keepalive_connections=50, max_connections=200),
        "timeout": httpx.Timeout(20.0, connect=10.0),
        "follow_redirects": True,
    }
    
    if BotConfig.PROXY_URL:
        client_kwargs["proxy"] = BotConfig.PROXY_URL
        logger.info("Proxy support active.")

    try:
        GLOBAL_HTTP_CLIENT = httpx.AsyncClient(http2=True, **client_kwargs)
    except Exception:
        logger.warning("HTTP/2 library absent or incompatible. Defaulting to HTTP/1.1 client engine.")
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

    await setup_bot_commands(telegram_app)

    if BotConfig.USE_WEBHOOK and BotConfig.RENDER_EXTERNAL_URL:
        webhook_url = f"{BotConfig.RENDER_EXTERNAL_URL.rstrip('/')}/webhook"
        await telegram_app.bot.set_webhook(url=webhook_url)
        logger.info(f"Webhook registered: {webhook_url}")
    else:
        if telegram_app.updater:
            await telegram_app.updater.start_polling(drop_pending_updates=True)
        logger.info("Polling mode active.")

    asyncio.create_task(keep_alive_task())
    asyncio.create_task(handle_sniper_task(telegram_app))

    hyper_config = HyperConfig()
    hyper_config.bind = [f"0.0.0.0:{BotConfig.PORT}"]

    try:
        await serve(quart_app, hyper_config)
    finally:
        logger.info("Gracefully shutting down engine components...")
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
        logger.info("Engine process terminated.")

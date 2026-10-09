import asyncio
import json
import logging
import os
import random
import string
import sys
from typing import Optional, Set
from curl_cffi.requests import AsyncSession

# ==================== CONFIGURATION ====================
PLATFORM = "tiktok"            # Target platform: "tiktok" or "instagram"
GEN_MODE = "pronounceable"     # "pronounceable" or "random"
MAX_LENGTH = 6                 # Max username length (3 to 6)
CONCURRENCY_LIMIT = 3          # Keep low (3-5) without residential proxies to avoid bans
REQUEST_DELAY = 1.2            # Base delay between requests (seconds)

# Telegram Notifications
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "YOUR_CHAT_ID_HERE")

# Proxies (Leave empty if testing without proxies)
# Format: "http://user:pass@ip:port" or "socks5://ip:port"
PROXIES = [
    # "http://proxy1.com:8080",
    # "http://proxy2.com:8080",
]

# File Paths
CHECKED_FILE = "checked_usernames.json"
HITS_FILE = f"available_{PLATFORM}.txt"
# =======================================================

# Logging Setup
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)

CONSONANTS = "bcdfghjklmnpqrstvwxyz"
VOWELS = "aeiou"

class TelegramAlert:
    """Sends instant alerts to Telegram when a username is found."""
    def __init__(self, token: str, chat_id: str):
        self.token = token
        self.chat_id = chat_id
        self.api_url = f"https://api.telegram.org/bot{self.token}/sendMessage"

    async def send(self, session: AsyncSession, text: str):
        if not self.token or self.token == "YOUR_BOT_TOKEN_HERE":
            return
        try:
            payload = {
                "chat_id": self.chat_id,
                "text": text,
                "parse_mode": "Markdown"
            }
            await session.post(self.api_url, json=payload, timeout=10)
        except Exception as e:
            logging.error(f"Failed to send Telegram alert: {e}")


class UsernameGenerator:
    """Generates pronounceable or random usernames."""
    @staticmethod
    def pronounceable(length: int) -> str:
        pattern = []
        use_consonant = random.choice([True, False])
        for _ in range(length):
            pattern.append(random.choice(CONSONANTS) if use_consonant else random.choice(VOWELS))
            use_consonant = not use_consonant
        return "".join(pattern)

    @staticmethod
    def random_string(length: int) -> str:
        return "".join(random.choice(string.ascii_lowercase + string.digits if length > 4 else string.ascii_lowercase) for _ in range(length))


class InfiniteScanner:
    def __init__(self):
        self.platform = PLATFORM.lower()
        self.checked: Set[str] = self.load_checked()
        self.notifier = TelegramAlert(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)
        self.consecutive_rate_limits = 0

    def load_checked(self) -> Set[str]:
        if os.path.exists(CHECKED_FILE):
            try:
                with open(CHECKED_FILE, "r") as f:
                    return set(json.load(f))
            except Exception:
                return set()
        return set()

    def save_checked(self):
        try:
            with open(CHECKED_FILE, "w") as f:
                json.dump(list(self.checked), f)
        except Exception as e:
            logging.error(f"Error saving state: {e}")

    def save_hit(self, username: str):
        with open(HITS_FILE, "a") as f:
            f.write(f"{username}\n")

    def get_proxy(self) -> Optional[dict]:
        if not PROXIES:
            return None
        proxy = random.choice(PROXIES)
        return {"http": proxy, "https": proxy}

    def get_headers(self) -> dict:
        return {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
        }

    async def check_tiktok(self, session: AsyncSession, username: str) -> str:
        url = f"https://www.tiktok.com/@{username}"
        try:
            resp = await session.get(
                url,
                headers=self.get_headers(),
                impersonate="chrome120",
                proxies=self.get_proxy(),
                timeout=12
            )
            if resp.status_code == 404:
                return "AVAILABLE"
            elif resp.status_code == 200:
                if "Couldn't find this account" in resp.text or '"statusCode":10221' in resp.text:
                    return "AVAILABLE"
                return "TAKEN"
            elif resp.status_code in (429, 403):
                return "RATE_LIMITED"
            return "UNKNOWN"
        except Exception:
            return "ERROR"

    async def check_instagram(self, session: AsyncSession, username: str) -> str:
        url = f"https://www.instagram.com/api/v1/users/web_profile_info/?username={username}"
        headers = self.get_headers()
        headers["X-IG-App-ID"] = "936619743392459"
        
        try:
            resp = await session.get(
                url,
                headers=headers,
                impersonate="chrome120",
                proxies=self.get_proxy(),
                timeout=12
            )
            if resp.status_code == 404:
                return "AVAILABLE"
            elif resp.status_code == 200:
                return "TAKEN"
            elif resp.status_code in (429, 403, 302):
                return "RATE_LIMITED"
            return "UNKNOWN"
        except Exception:
            return "ERROR"

    async def worker(self, semaphore: asyncio.Semaphore, session: AsyncSession, username: str):
        async with semaphore:
            if username in self.checked:
                return

            if self.platform == "tiktok":
                status = await self.check_tiktok(session, username)
            else:
                status = await self.check_instagram(session, username)

            self.checked.add(username)

            if status == "AVAILABLE":
                self.consecutive_rate_limits = 0
                logging.info(f"🟢 [FOUND AVAILABLE] -> {username}")
                self.save_hit(username)
                
                # Send alert to Telegram
                msg = f"🎯 *Username Claimable!*\n\n*Platform:* {self.platform.upper()}\n*Username:* `{username}`"
                await self.notifier.send(session, msg)

            elif status == "TAKEN":
                self.consecutive_rate_limits = max(0, self.consecutive_rate_limits - 1)
                logging.info(f"🔴 Taken: {username}")

            elif status == "RATE_LIMITED":
                self.consecutive_rate_limits += 1
                logging.warning(f"⚠️ Rate limited on '{username}'. Cooling down...")
                # Exponential backoff on rate limits
                cooldown = min(60, 5 * self.consecutive_rate_limits)
                await asyncio.sleep(cooldown)

            else:
                logging.debug(f"⚪ Skipped/Error: {username}")

            # Save progress periodically
            if len(self.checked) % 20 == 0:
                self.save_checked()


    async def run(self):
        logging.info(f"🚀 Infinite {self.platform.upper()} Scanner Started.")
        logging.info(f"⚙️ Config: Mode={GEN_MODE}, Max Length={MAX_LENGTH}, Concurrency={CONCURRENCY_LIMIT}")

        semaphore = asyncio.Semaphore(CONCURRENCY_LIMIT)

        async with AsyncSession() as session:
            # Send startup alert
            await self.notifier.send(session, f"🤖 *Username Scanner Online*\nTarget: {self.platform.upper()} | Length <= {MAX_LENGTH}")

            while True:
                try:
                    # Generate username candidate
                    length = random.choice(range(3, MAX_LENGTH + 1))
                    if GEN_MODE == "pronounceable":
                        username = UsernameGenerator.pronounceable(length)
                    else:
                        username = UsernameGenerator.random_string(length)

                    if username in self.checked:
                        continue

                    # Spawn worker
                    asyncio.create_task(self.worker(semaphore, session, username))

                    # Pace requests
                    await asyncio.sleep(REQUEST_DELAY)

                except KeyboardInterrupt:
                    logging.info("🛑 Stopping scanner...")
                    self.save_checked()
                    break
                except Exception as e:
                    logging.error(f"Unexpected error in main loop: {e}")
                    await asyncio.sleep(5)

if __name__ == "__main__":
    scanner = InfiniteScanner()
    try:
        asyncio.run(scanner.run())
    except (KeyboardInterrupt, SystemExit):
        logging.info("Scanner shut down gracefully.")

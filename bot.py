import os
import sys
import logging
import urllib.parse
import asyncio
import io
import httpx
import random
import time
import gc
from typing import Optional, Tuple
from PIL import Image, ImageEnhance
from quart import Quart
from telegram import Update
from telegram.constants import ParseMode, ChatAction
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
logger = logging.getLogger("FreeMediaBot")

class BotConfig:
    BOT_TOKEN: str = os.environ.get("BOT_TOKEN", "")
    REPLICATE_API_TOKEN: Optional[str] = os.environ.get("REPLICATE_API_TOKEN", None)
    POLLINATIONS_API_KEY: Optional[str] = os.environ.get("POLLINATIONS_API_KEY", None)
    PORT: int = int(os.environ.get("PORT", 10000))
    
    HTTP_TIMEOUT: float = 120.0
    
    ASPECT_RATIOS = {
        "1:1": (1024, 1024),
        "16:9": (1280, 720),
        "9:16": (720, 1280),
        "4:3": (1024, 768),
        "3:4": (768, 1024)
    }

if not BotConfig.BOT_TOKEN:
    logger.critical("FATAL: 'BOT_TOKEN' environment variable is missing!")
    sys.exit(1)

# ==============================================================================
# 2. RENDER HEALTH CHECK WEBSERVER (QUART)
# ==============================================================================

quart_app = Quart(__name__)
BOT_START_TIME = time.time()

@quart_app.route("/")
async def health_check():
    uptime = int(time.time() - BOT_START_TIME)
    return (
        f"🤖 AI Photo & Video Bot Operational\n"
        f"⏱️ Uptime: {uptime}s\n"
        f"🔑 Replicate Integration: {'ACTIVE' if BotConfig.REPLICATE_API_TOKEN else 'INACTIVE'}",
        200
    )

@quart_app.route("/ping")
async def ping():
    return "PONG", 200

# ==============================================================================
# 3. HELPER UTILITIES & SILENT RATE-LIMIT HTTP HANDLER
# ==============================================================================

async def silent_http_get(client: httpx.AsyncClient, url: str, headers: dict = None) -> Optional[httpx.Response]:
    """
    Executes HTTP GET with silent handling for 429 Rate Limits.
    Pauses execution quietly without sending user notifications.
    """
    try:
        response = await client.get(url, headers=headers)
        if response.status_code == 429:
            logger.warning("Rate limit hit (429). Auto-pausing silently for 15 minutes...")
            await asyncio.sleep(900)  # 15 minutes silent wait
            return await client.get(url, headers=headers)
        return response
    except Exception as e:
        logger.error(f"HTTP request exception: {e}")
        return None

def parse_prompt_flags(raw_prompt: str) -> Tuple[str, int, int, str]:
    words = raw_prompt.split()
    clean_words = []
    width, height = 1024, 1024
    ar_key = "1:1"
    
    i = 0
    while i < len(words):
        if words[i].lower() == "--ar" and i + 1 < len(words):
            target_ar = words[i + 1]
            if target_ar in BotConfig.ASPECT_RATIOS:
                width, height = BotConfig.ASPECT_RATIOS[target_ar]
                ar_key = target_ar
            i += 2
        else:
            clean_words.append(words[i])
            i += 1
            
    clean_prompt = " ".join(clean_words).strip()
    return clean_prompt, width, height, ar_key

def build_smooth_motion_gif(image_bytes: bytes, num_frames: int = 24) -> io.BytesIO:
    base_image = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    orig_w, orig_h = base_image.size

    frames = []
    for i in range(num_frames):
        progress = i / float(num_frames)
        scale = 1.0 + 0.15 * (0.5 - 0.5 * (2 * 3.14159 * progress))

        nw, nh = int(orig_w * scale), int(orig_h * scale)
        resized = base_image.resize((nw, nh), Image.Resampling.LANCZOS)

        offset_x = max(0, min(int((nw - orig_w) * (0.5 + 0.3 * (progress - 0.5))), nw - orig_w))
        offset_y = max(0, min(int((nh - orig_h) * (0.5 - 0.3 * (progress - 0.5))), nh - orig_h))

        cropped = resized.crop((offset_x, offset_y, offset_x + orig_w, offset_y + orig_h))
        
        enhancer = ImageEnhance.Contrast(cropped)
        contrast_factor = 1.0 + 0.08 * (0.5 - 0.5 * (4 * 3.14159 * progress))
        frame = enhancer.enhance(contrast_factor)
        
        frames.append(frame)

    output_buffer = io.BytesIO()
    frames[0].save(
        output_buffer,
        format="GIF",
        save_all=True,
        append_images=frames[1:],
        duration=50,
        loop=0,
        optimize=True
    )
    output_buffer.seek(0)
    output_buffer.name = "ai_motion.gif"
    
    del frames, base_image
    gc.collect()
    
    return output_buffer

# ==============================================================================
# 4. PHOTO ENGINE PIPELINE
# ==============================================================================

async def generate_photo_bytes(prompt: str, width: int, height: int, seed: int) -> Tuple[Optional[bytes], str]:
    encoded_prompt = urllib.parse.quote(prompt)
    
    endpoints = [
        ("FLUX Model", f"https://image.pollinations.ai/prompt/{encoded_prompt}?width={width}&height={height}&seed={seed}&model=flux&nologo=true"),
        ("TURBO Model", f"https://image.pollinations.ai/prompt/{encoded_prompt}?width={width}&height={height}&seed={seed}&model=turbo&nologo=true"),
        ("SDXL Model", f"https://image.pollinations.ai/prompt/{encoded_prompt}?width={width}&height={height}&seed={seed}&model=sdxl&nologo=true"),
    ]

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8"
    }

    if BotConfig.POLLINATIONS_API_KEY:
        headers["Authorization"] = f"Bearer {BotConfig.POLLINATIONS_API_KEY}"

    async with httpx.AsyncClient(timeout=BotConfig.HTTP_TIMEOUT, follow_redirects=True) as client:
        for model_name, url in endpoints:
            response = await silent_http_get(client, url, headers=headers)
            if response and response.status_code == 200 and len(response.content) > 5000:
                return response.content, model_name
                
    return None, "Failed"

# ==============================================================================
# 5. VIDEO ENGINE PIPELINE
# ==============================================================================

async def generate_video_file(prompt: str, seed: int) -> Tuple[io.BytesIO, str, bool]:
    # Tier 1: Replicate API
    if BotConfig.REPLICATE_API_TOKEN:
        try:
            import replicate
            def call_replicate():
                return replicate.run(
                    "lucataco/animate-diff:beecf59c4aee8099616d7a468d6ff0b115ebfb56cf6d4217112c3f80c65ba8f8",
                    input={
                        "prompt": f"{prompt}, 8k resolution, cinematic smooth motion",
                        "n_prompt": "blurry, low quality, static",
                        "guidance_scale": 7.5,
                        "num_inference_steps": 25
                    }
                )

            output_url = await asyncio.to_thread(call_replicate)
            video_url_str = str(output_url) if isinstance(output_url, str) else str(output_url[0])

            async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
                res = await silent_http_get(client, video_url_str)
                if res and res.status_code == 200 and len(res.content) > 20000:
                    buf = io.BytesIO(res.content)
                    buf.name = f"video_{seed}.mp4"
                    return buf, "Replicate AnimateDiff (Native MP4)", True
        except Exception as e:
            logger.error(f"Replicate engine bypass: {e}")

    # Tier 2: Native Video Stream Endpoint
    try:
        encoded = urllib.parse.quote(f"{prompt}, 4k video motion, fluid movement")
        video_api_url = f"https://gen.pollinations.ai/video/{encoded}?seed={seed}"
        headers = {"User-Agent": "Mozilla/5.0", "Accept": "video/mp4,video/*;q=0.9,*/*;q=0.8"}
        if BotConfig.POLLINATIONS_API_KEY:
            headers["Authorization"] = f"Bearer {BotConfig.POLLINATIONS_API_KEY}"

        async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
            res = await silent_http_get(client, video_api_url, headers=headers)
            if res and res.status_code == 200 and len(res.content) > 50000:
                buf = io.BytesIO(res.content)
                buf.name = f"video_{seed}.mp4"
                return buf, "Pollinations Video Stream (Native MP4)", True
    except Exception as e:
        logger.warning(f"Native stream endpoint bypass: {e}")

    # Tier 3: Motion Loop Engine
    base_image_bytes, _ = await generate_photo_bytes(
        prompt=f"{prompt}, hyper-realistic, volumetric light, motion blur",
        width=1024,
        height=1024,
        seed=seed
    )

    if not base_image_bytes:
        raise RuntimeError("Image/Video render servers are currently unresponsive.")

    motion_gif_buf = await asyncio.to_thread(build_smooth_motion_gif, base_image_bytes)
    return motion_gif_buf, "Cinematic Motion Engine", False

# ==============================================================================
# 6. TELEGRAM COMMAND HANDLERS
# ==============================================================================

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    welcome_text = (
        "✨ **AI Photo & Video Generator**\n\n"
        "📸 **Commands:**\n"
        "• `/photo <prompt>` — Generate HD photo\n"
        "• `/photo <prompt> --ar 16:9` — Landscape\n"
        "• `/photo <prompt> --ar 9:16` — Portrait\n"
        "• `/video <prompt>` — Generate AI motion video\n"
        "• `/status` — Server health check"
    )
    await update.message.reply_text(welcome_text, parse_mode=ParseMode.MARKDOWN)

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uptime = int(time.time() - BOT_START_TIME)
    replicate_status = "🟢 Active" if BotConfig.REPLICATE_API_TOKEN else "🟡 Fallback Engine Active"
    
    status_msg = (
        "⚙️ **System Diagnostics**\n"
        "━━━━━━━━━━━━━━━━━━━\n"
        f"⏱️ **Uptime:** `{uptime}s`\n"
        f"📡 **Status:** `Healthy`\n"
        f"🎥 **Video Provider:** {replicate_status}\n"
        f"🖼️ **Image Provider:** `Pollinations Multi-Tier`\n"
        f"🤫 **Rate Limit Handler:** `Silent Auto-Pause`"
    )
    await update.message.reply_text(status_msg, parse_mode=ParseMode.MARKDOWN)

async def photo_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw_args = " ".join(context.args)
    if not raw_args:
        await update.message.reply_text("❌ Provide a prompt!\nExample: `/photo cute astronaut cat --ar 16:9`", parse_mode=ParseMode.MARKDOWN)
        return

    prompt, width, height, ar_key = parse_prompt_flags(raw_args)
    status_msg = await update.message.reply_text(f"🎨 *Rendering photo ({ar_key})...*", parse_mode=ParseMode.MARKDOWN)

    seed = random.randint(100000, 999999)
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.UPLOAD_PHOTO)

    image_bytes, engine_used = await generate_photo_bytes(prompt, width, height, seed)

    if image_bytes:
        try:
            await update.message.reply_photo(
                photo=image_bytes,
                caption=f"✨ *Prompt:* `{prompt}`\n⚙️ *Engine:* `{engine_used}`",
                parse_mode=ParseMode.MARKDOWN
            )
            doc_file = io.BytesIO(image_bytes)
            doc_file.name = f"Render_{seed}.png"
            await update.message.reply_document(
                document=doc_file,
                filename=doc_file.name,
                caption="📁 *Full Quality Uncompressed PNG*",
                parse_mode=ParseMode.MARKDOWN
            )
            await status_msg.delete()
        except Exception as e:
            logger.error(f"Telegram photo dispatch error: {e}")
            await status_msg.edit_text("❌ Failed to send photo payload.")
    else:
        await status_msg.edit_text("❌ Image servers are busy. Please try again in a moment!")

async def video_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw_prompt = " ".join(context.args)
    if not raw_prompt:
        await update.message.reply_text("❌ Provide a prompt!\nExample: `/video airplane flying through sunset`", parse_mode=ParseMode.MARKDOWN)
        return

    clean_prompt, _, _, _ = parse_prompt_flags(raw_prompt)
    status_msg = await update.message.reply_text("🎬 *Rendering video (~30-60s)...*", parse_mode=ParseMode.MARKDOWN)

    seed = random.randint(100000, 999999)
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.RECORD_VIDEO)

    try:
        file_buffer, engine_name, is_native_mp4 = await generate_video_file(clean_prompt, seed)
        
        if is_native_mp4:
            await update.message.reply_video(
                video=file_buffer,
                caption=f"🎬 *AI Video:* `{clean_prompt}`\n⚙️ *Engine:* `{engine_name}`",
                parse_mode=ParseMode.MARKDOWN,
                supports_streaming=True
            )
        else:
            await update.message.reply_animation(
                animation=file_buffer,
                caption=f"🎬 *AI Motion Clip:* `{clean_prompt}`\n⚙️ *Engine:* `{engine_name}`",
                parse_mode=ParseMode.MARKDOWN
            )

        await status_msg.delete()

    except Exception as e:
        logger.error(f"Video pipeline failure: {e}")
        await status_msg.edit_text("❌ Video generation timed out. Please try a simpler prompt!")

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Uncaught exception encountered:", exc_info=context.error)

# ==============================================================================
# 7. MAIN ORCHESTRATION
# ==============================================================================

async def main():
    request_kwargs = HTTPXRequest(
        connect_timeout=30.0,
        read_timeout=BotConfig.HTTP_TIMEOUT,
        write_timeout=BotConfig.HTTP_TIMEOUT,
        pool_timeout=30.0
    )

    telegram_app = (
        ApplicationBuilder()
        .token(BotConfig.BOT_TOKEN)
        .request(request_kwargs)
        .build()
    )

    telegram_app.add_handler(CommandHandler("start", start_command))
    telegram_app.add_handler(CommandHandler("help", start_command))
    telegram_app.add_handler(CommandHandler("status", status_command))
    telegram_app.add_handler(CommandHandler("photo", photo_command))
    telegram_app.add_handler(CommandHandler("video", video_command))
    telegram_app.add_error_handler(error_handler)

    await telegram_app.initialize()
    await telegram_app.start()
    await telegram_app.updater.start_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)

    hypercorn_config = HyperConfig()
    hypercorn_config.bind = [f"0.0.0.0:{BotConfig.PORT}"]
    hypercorn_config.shutdown_timeout = 5.0

    try:
        await serve(quart_app, hypercorn_config)
    finally:
        await telegram_app.updater.stop()
        await telegram_app.stop()
        await telegram_app.shutdown()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot execution terminated cleanly.")

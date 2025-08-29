import os
import re
import shutil
import asyncio
import logging
import tempfile
from pathlib import Path
from typing import Optional

from aiogram import Bot, Dispatcher, executor, types
from aiogram.utils.exceptions import MessageNotModified
from dotenv import load_dotenv

try:
    import redis  # type: ignore
except Exception:
    redis = None  # optional

import yt_dlp

# ---------- Config & Setup ----------
load_dotenv()

TELEGRAM_TOKEN = os.getenv("TOKEN")
OWNER = os.getenv("OWNER")
REDIS_URL = os.getenv("REDIS_URL")
DOWNLOAD_DIR = Path(os.getenv("DOWNLOAD_DIR", "/tmp"))
MAX_FILE_MB = int(os.getenv("MAX_FILE_MB", "200"))  # keep modest default

if not TELEGRAM_TOKEN:
    raise RuntimeError("TOKEN is not set. Please provide Telegram bot token via env.")

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("ytdlbot")

bot = Bot(token=TELEGRAM_TOKEN, parse_mode=types.ParseMode.HTML)
dp = Dispatcher(bot)

# Optional Redis client for simple rate limit
rcli = None
if REDIS_URL and redis:
    try:
        rcli = redis.Redis.from_url(REDIS_URL, decode_responses=True)
        rcli.ping()
        log.info("Connected to Redis")
    except Exception as e:
        log.warning("Redis not available: %s", e)
        rcli = None

URL_RE = re.compile(r"https?://\S+")

# ---------- Helpers ----------
def human_size(num_bytes: int) -> str:
    step = 1024.0
    units = ["B", "KB", "MB", "GB"]
    size = float(num_bytes)
    for u in units:
        if size < step or u == units[-1]:
            return f"{size:.2f} {u}"
        size /= step
    return f"{size:.2f} GB"

def sanitize(text: str) -> str:
    # Keep it filesystem-safe and short
    text = re.sub(r"[^\w\s.-]", "", text).strip()
    return text[:80] if text else "file"

# ---------- yt-dlp Download ----------
def build_ydl_opts(tmpdir: Path) -> dict:
    outtmpl = str(tmpdir / "%(title).80s.%(ext)s")
    return {
        "outtmpl": outtmpl,
        "restrictfilenames": True,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "ignoreerrors": True,
        "format": "bv*+ba/b",
        "postprocessors": [
            # Leave as-is by default; ffmpeg will be used implicitly if needed
        ],
    }

def pick_file_from_dir(d: Path) -> Optional[Path]:
    cand = None
    for p in d.iterdir():
        if p.is_file() and not p.name.endswith(".part"):
            cand = p
            break
    return cand

async def rate_limited(user_id: int) -> bool:
    # Simple rate limit: 1 task per 30 seconds per user if Redis is available.
    if not rcli:
        return False
    key = f"ytdlbot:busy:{user_id}"
    if rcli.get(key):
        return True
    rcli.setex(key, 30, "1")
    return False

# ---------- Handlers ----------
@dp.message_handler(commands=["start", "help"])
async def cmd_help(message: types.Message):
    text = (
        "Привет! Я помогу скачать видео/аудио по ссылке с поддерживаемых сайтов (yt-dlp).\n\n"
        "<b>Как пользоваться:</b>\n"
        "• Пришлите ссылку на видео\n"
        "• Я скачаю файл и отправлю его вам\n\n"
        "<b>Ограничения:</b>\n"
        f"• Максимальный размер отправки: ~{MAX_FILE_MB} MB\n"
        "• Плейлисты не поддерживаются (только одна ссылка)\n"
    )
    await message.reply(text)

@dp.message_handler(regexp=URL_RE.pattern)
async def handle_url(message: types.Message):
    url_match = URL_RE.search(message.text or "")
    if not url_match:
        return
    url = url_match.group(0)

    if await rate_limited(message.from_user.id):
        await message.reply("Слишком часто. Подождите 30 секунд и попробуйте снова.")
        return

    status = await message.reply("⏳ Загружаю… это может занять время.")

    tmpdir = Path(tempfile.mkdtemp(prefix="ytdl_", dir=str(DOWNLOAD_DIR)))
    try:
        ydl_opts = build_ydl_opts(tmpdir)
        log.info("Downloading %s -> %s", url, tmpdir)
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)

        file_path = pick_file_from_dir(tmpdir)
        if not file_path or not file_path.exists():
            raise RuntimeError("Не удалось получить файл после загрузки.")

        size_mb = file_path.stat().st_size / (1024 * 1024)
        if size_mb > MAX_FILE_MB:
            await status.edit_text(
                f"Файл слишком большой: {human_size(file_path.stat().st_size)}.\n"
                f"Лимит установлен на {MAX_FILE_MB} MB."
            )
            return

        # Try to send as video if extension looks like video, else as document
        ext = file_path.suffix.lower().lstrip(".")
        caption = f"{sanitize(file_path.stem)} — {human_size(file_path.stat().st_size)}"

        try:
            if ext in {"mp4", "mkv", "webm", "mov"}:
                await message.reply_video(open(file_path, "rb"), caption=caption)
            elif ext in {"mp3", "m4a", "aac", "flac", "wav", "ogg"}:
                await message.reply_audio(open(file_path, "rb"), caption=caption)
            else:
                await message.reply_document(open(file_path, "rb"), caption=caption)
            await status.delete()
        except Exception as e:
            log.exception("Send failed: %s", e)
            await status.edit_text("Не удалось отправить файл. Попробуйте другой формат или ссылку.")
    except yt_dlp.utils.DownloadError as e:
        log.warning("yt-dlp error: %s", e)
        try:
            await status.edit_text("Ошибка загрузки. Проверьте ссылку или доступность видео.")
        except MessageNotModified:
            pass
    except Exception as e:
        log.exception("Unexpected error: %s", e)
        try:
            await status.edit_text("Произошла непредвиденная ошибка. Попробуйте ещё раз позже.")
        except MessageNotModified:
            pass
    finally:
        # Cleanup
        try:
            shutil.rmtree(tmpdir, ignore_errors=True)
        except Exception:
            pass

if __name__ == "__main__":
    # Start long polling
    executor.start_polling(dp, skip_updates=True)

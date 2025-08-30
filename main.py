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
import yt_dlp

# -------------------- CONFIG --------------------
load_dotenv()

TELEGRAM_TOKEN = os.getenv("TOKEN")
OWNER = os.getenv("OWNER")
DOWNLOAD_DIR = Path(os.getenv("DOWNLOAD_DIR", "/tmp"))
MAX_FILE_MB = int(os.getenv("MAX_FILE_MB", "200"))

if not TELEGRAM_TOKEN:
    raise RuntimeError("TOKEN is not set. Please provide Telegram bot token via env.")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
log = logging.getLogger("ytdlbot")

bot = Bot(token=TELEGRAM_TOKEN, parse_mode=types.ParseMode.HTML)
dp = Dispatcher(bot)

URL_RE = re.compile(r"https?://\S+")

# -------------------- HELPERS --------------------
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
    text = re.sub(r"[^\w\s.-]", "", text).strip()
    return text[:80] if text else "file"

def build_ydl_opts(tmpdir: Path, mode: str) -> dict:
    outtmpl = str(tmpdir / "%(title).80s.%(ext)s")
    opts = {
        "outtmpl": outtmpl,
        "restrictfilenames": True,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "ignoreerrors": True,
        "format": "bv*+ba/b" if mode == "video" else "bestaudio/best",
        "postprocessors": [],
    }
    # если выбрано аудио — конвертируем в mp3
    if mode == "audio":
        opts["postprocessors"].append({
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        })
    return opts

def pick_file_from_dir(d: Path) -> Optional[Path]:
    for p in d.iterdir():
        if p.is_file() and not p.name.endswith(".part"):
            return p
    return None

# -------------------- COMMANDS --------------------
@dp.message_handler(commands=["start", "help"])
async def cmd_help(message: types.Message):
    text = (
        "👋 Привет! Я помогу скачать видео или аудио по ссылке с YouTube и других сайтов.\n\n"
        "<b>Как пользоваться:</b>\n"
        "1. Пришлите ссылку на видео\n"
        "2. Выберите, что скачать — 🎥 Видео или 🎵 Аудио\n"
        "3. Получите файл\n\n"
        f"⚡ Ограничения:\n• Максимальный размер: ~{MAX_FILE_MB} MB\n"
        "• Плейлисты не поддерживаются\n"
    )
    await message.reply(text)

# -------------------- URL HANDLER --------------------
@dp.message_handler(regexp=URL_RE.pattern)
async def handle_url(message: types.Message):
    url_match = URL_RE.search(message.text or "")
    if not url_match:
        return
    url = url_match.group(0)

    # Спрашиваем формат
    keyboard = types.InlineKeyboardMarkup()
    keyboard.add(
        types.InlineKeyboardButton("🎥 Видео", callback_data=f"download_video|{url}"),
        types.InlineKeyboardButton("🎵 Аудио", callback_data=f"download_audio|{url}")
    )
    await message.reply("Что скачать?", reply_markup=keyboard)

# -------------------- CALLBACK HANDLER --------------------
@dp.callback_query_handler(lambda c: c.data.startswith("download_"))
async def process_download(call: types.CallbackQuery):
    await call.answer()
    mode, url = call.data.split("|", 1)
    mode = mode.replace("download_", "")

    status = await call.message.reply("⏳ Загружаю… это может занять немного времени.")
    tmpdir = Path(tempfile.mkdtemp(prefix="ytdl_", dir=str(DOWNLOAD_DIR)))

    try:
        ydl_opts = build_ydl_opts(tmpdir, mode)
        log.info("Downloading %s -> %s", url, tmpdir)
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.extract_info(url, download=True)

        file_path = pick_file_from_dir(tmpdir)
        if not file_path or not file_path.exists():
            raise RuntimeError("Не удалось получить файл после загрузки.")

        size_mb = file_path.stat().st_size / (1024 * 1024)
        caption = f"{sanitize(file_path.stem)} — {human_size(file_path.stat().st_size)}"

        if size_mb > MAX_FILE_MB:
            await status.edit_text(
                f"⚠️ Файл слишком большой ({human_size(file_path.stat().st_size)}).\n"
                "Попробуйте скачать по ссылке напрямую."
            )
            return

        ext = file_path.suffix.lower().lstrip(".")
        try:
            if ext in {"mp4", "mkv", "webm", "mov"}:
                await call.message.reply_video(open(file_path, "rb"), caption=caption)
            elif ext in {"mp3", "m4a", "aac", "flac", "wav", "ogg"}:
                await call.message.reply_audio(open(file_path, "rb"), caption=caption)
            else:
                await call.message.reply_document(open(file_path, "rb"), caption=caption)
            await status.delete()
        except Exception as e:
            log.exception("Send failed: %s", e)
            await status.edit_text("❌ Не удалось отправить файл. Попробуйте другой формат или ссылку.")
    except yt_dlp.utils.DownloadError:
        try:
            await status.edit_text("❌ Ошибка загрузки. Проверьте ссылку или доступность видео.")
        except MessageNotModified:
            pass
    except Exception as e:
        log.exception("Unexpected error: %s", e)
        try:
            await status.edit_text("❌ Произошла непредвиденная ошибка. Попробуйте ещё раз позже.")
        except MessageNotModified:
            pass
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

# -------------------- RUN --------------------
if __name__ == "__main__":
    executor.start_polling(dp, skip_updates=True)

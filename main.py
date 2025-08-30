import os
import re
import shutil
import asyncio
import logging
import tempfile
from pathlib import Path
from typing import Optional

from aiogram import Bot, Dispatcher, types
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils import executor
from aiogram.utils.exceptions import MessageNotModified
from dotenv import load_dotenv

import yt_dlp

# ---------- Настройки ----------
load_dotenv()

TELEGRAM_TOKEN = os.getenv("TOKEN")
OWNER = os.getenv("OWNER")
DOWNLOAD_DIR = Path(os.getenv("DOWNLOAD_DIR", "/tmp"))
MAX_FILE_MB = int(os.getenv("MAX_FILE_MB", "200"))

if not TELEGRAM_TOKEN:
    raise RuntimeError("TOKEN не установлен! Добавь его в переменные окружения.")

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("ytdlbot")

bot = Bot(token=TELEGRAM_TOKEN, parse_mode=types.ParseMode.HTML)
dp = Dispatcher(bot)

URL_RE = re.compile(r"https?://\S+")

# ---------- Хелперы ----------
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
    return re.sub(r"[^\w\s.-]", "", text).strip()[:80] or "file"

def build_ydl_opts(tmpdir: Path, audio_only=False) -> dict:
    outtmpl = str(tmpdir / "%(title).80s.%(ext)s")
    opts = {
        "outtmpl": outtmpl,
        "restrictfilenames": True,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "ignoreerrors": True,
    }
    if audio_only:
        opts["format"] = "bestaudio/best"
        opts["postprocessors"] = [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }]
    else:
        opts["format"] = "bv*+ba/b"
    return opts

def pick_file_from_dir(d: Path) -> Optional[Path]:
    for p in d.iterdir():
        if p.is_file() and not p.name.endswith(".part"):
            return p
    return None

# ---------- Обработчики ----------
@dp.message_handler(commands=["start", "help"])
async def cmd_help(message: types.Message):
    text = (
        "👋 Привет! Я помогу скачать <b>видео</b> или <b>аудио</b> с YouTube и других сайтов.\n\n"
        "<b>Как пользоваться:</b>\n"
        "1️⃣ Пришли ссылку на видео\n"
        "2️⃣ Выбери формат: 🎥 Видео или 🎵 Аудио\n"
        "3️⃣ Дождись, пока я скачаю и отправлю файл\n\n"
        f"<b>Ограничение:</b> до ~{MAX_FILE_MB} МБ"
    )
    await message.reply(text)

@dp.message_handler(regexp=URL_RE.pattern)
async def handle_url(message: types.Message):
    url = URL_RE.search(message.text or "").group(0)

    keyboard = InlineKeyboardMarkup(row_width=2)
    keyboard.add(
        InlineKeyboardButton("🎥 Видео", callback_data=f"video|{url}"),
        InlineKeyboardButton("🎵 Аудио", callback_data=f"audio|{url}")
    )
    await message.reply("Выбери формат загрузки:", reply_markup=keyboard)

@dp.callback_query_handler(lambda c: c.data.startswith(("video", "audio")))
async def process_download(call: types.CallbackQuery):
    mode, url = call.data.split("|", 1)
    audio_only = mode == "audio"

    status = await call.message.reply("⏳ Загружаю… Подождите немного.")

    tmpdir = Path(tempfile.mkdtemp(prefix="ytdl_", dir=str(DOWNLOAD_DIR)))
    try:
        ydl_opts = build_ydl_opts(tmpdir, audio_only=audio_only)
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)

        file_path = pick_file_from_dir(tmpdir)
        if not file_path or not file_path.exists():
            raise RuntimeError("Не удалось получить файл после загрузки.")

        size_mb = file_path.stat().st_size / (1024 * 1024)
        caption = f"{sanitize(file_path.stem)} — {human_size(file_path.stat().st_size)}"

        if size_mb > MAX_FILE_MB:
            await status.edit_text(
                f"⚠️ Файл слишком большой: {human_size(file_path.stat().st_size)}.\n"
                f"Лимит — {MAX_FILE_MB} MB."
            )
            return

        try:
            if audio_only:
                await call.message.reply_audio(open(file_path, "rb"), caption=caption)
            elif file_path.suffix.lower().lstrip(".") in {"mp4", "mkv", "webm", "mov"}:
                await call.message.reply_video(open(file_path, "rb"), caption=caption)
            else:
                await call.message.reply_document(open(file_path, "rb"), caption=caption)
            await status.delete()
        except Exception as e:
            log.exception("Ошибка при отправке файла: %s", e)
            await status.edit_text("Не удалось отправить файл. Попробуйте другой формат или ссылку.")
    except yt_dlp.utils.DownloadError:
        await status.edit_text("❌ Ошибка загрузки. Проверь ссылку или доступность видео.")
    except Exception as e:
        log.exception("Непредвиденная ошибка: %s", e)
        await status.edit_text("⚠️ Произошла ошибка. Попробуй ещё раз позже.")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

if __name__ == "__main__":
    log.info("Бот запущен...")
    executor.start_polling(dp, skip_updates=True)

import os
import re
import asyncio
import tempfile
import subprocess
import json
import urllib.request
from pathlib import Path

import imageio_ffmpeg as _ffmpeg_pkg

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)
from telegram.constants import ParseMode

# ── Config ────────────────────────────────────────────────────────────────────
BOT_TOKEN       = os.getenv("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
RAPIDAPI_KEY    = os.getenv("RAPIDAPI_KEY", "")   # isteğe bağlı, fallback için
MAX_FILE_MB     = 50
DOWNLOAD_DIR    = Path(tempfile.gettempdir()) / "tg_downloader"
DOWNLOAD_DIR.mkdir(exist_ok=True)

# ffmpeg binary — önce sistemde ara, yoksa imageio-ffmpeg'inkini kullan
FFMPEG = "ffmpeg"
try:
    subprocess.run([FFMPEG, "-version"], capture_output=True, check=True)
except (FileNotFoundError, subprocess.CalledProcessError):
    FFMPEG = _ffmpeg_pkg.get_ffmpeg_exe()

# ── URL patterns ──────────────────────────────────────────────────────────────
TWITTER_RE = re.compile(
    r"https?://(www\.)?(twitter\.com|x\.com)/\S+/status/\d+", re.IGNORECASE
)
YOUTUBE_RE = re.compile(
    r"https?://(www\.)?(youtube\.com/watch\?v=|youtu\.be/)[\w\-]+", re.IGNORECASE
)

def detect_platform(url: str) -> str | None:
    if TWITTER_RE.search(url):
        return "twitter"
    if YOUTUBE_RE.search(url):
        return "youtube"
    return None

# ── Twitter download — 2 yöntem ───────────────────────────────────────────────
def twitter_download_ytdlp(url: str, output_path: str) -> tuple:
    """
    yt-dlp syndication API — login gerektirmez, herkese açık.
    Railway gibi gerçek sunucularda sorunsuz çalışır.
    """
    cmd = [
        "yt-dlp",
        "--no-playlist",
        "--no-warnings",
        "--extractor-args", "twitter:api=syndication",
        "-f", "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
        "--merge-output-format", "mp4",
        "-o", output_path,
        url,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if result.returncode != 0:
        return False, result.stderr or result.stdout

    parent = Path(output_path).parent
    stem   = Path(output_path).stem
    matches = [f for f in parent.glob(f"{stem}*")
               if not f.suffix in (".html", ".htm")]
    if not matches:
        return False, "Dosya bulunamadı."
    return True, str(matches[0])


def twitter_download_rapidapi(url: str, output_path: str) -> tuple:
    """
    RapidAPI twitter-downloader45 — RAPIDAPI_KEY gerekir (ücretsiz plan var).
    """
    if not RAPIDAPI_KEY:
        return False, "RapidAPI key yok."

    api_url = "https://twitter-downloader45.p.rapidapi.com/twitter/download"
    payload = json.dumps({"url": url}).encode()
    req = urllib.request.Request(api_url, data=payload, headers={
        "Content-Type": "application/json",
        "x-rapidapi-host": "twitter-downloader45.p.rapidapi.com",
        "x-rapidapi-key": RAPIDAPI_KEY,
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read())
    except Exception as e:
        return False, str(e)

    # API'den video URL'si al
    video_url = None
    if isinstance(data, dict):
        video_url = (data.get("url") or
                     data.get("video_url") or
                     (data.get("videos") or [{}])[0].get("url"))
    if not video_url:
        return False, f"API'den URL alınamadı: {str(data)[:200]}"

    # İndir
    try:
        req2 = urllib.request.Request(video_url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req2, timeout=60) as r:
            with open(output_path, "wb") as f:
                f.write(r.read())
        return True, output_path
    except Exception as e:
        return False, str(e)


def twitter_download(url: str, base: str) -> tuple:
    """Önce yt-dlp dene, başarısız olursa RapidAPI'ye geç."""
    output = base + ".%(ext)s"
    ok, path = twitter_download_ytdlp(url, output)
    if ok:
        return True, path

    # Fallback
    ok2, path2 = twitter_download_rapidapi(url, base + ".mp4")
    if ok2:
        return True, path2

    return False, path  # ilk hatayı döndür


# ── yt-dlp genel helper (YouTube için) ───────────────────────────────────────
def ytdlp_download(url: str, output_path: str, extra_args: list) -> tuple:
    cmd = [
        "yt-dlp",
        "--no-playlist",
        "--no-warnings",
        "-o", output_path,
        *extra_args,
        url,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if result.returncode != 0:
        return False, result.stderr or result.stdout

    parent  = Path(output_path).parent
    stem    = Path(output_path).stem
    matches = [f for f in parent.glob(f"{stem}*")
               if f.suffix not in (".html", ".htm")]
    if not matches:
        return False, "Dosya bulunamadı."
    return True, str(matches[0])


# ── GIF dönüştürme ────────────────────────────────────────────────────────────
def video_to_gif(video_path: str, gif_path: str) -> tuple:
    palette = gif_path.replace(".gif", "_palette.png")
    p1 = subprocess.run([
        FFMPEG, "-y", "-i", video_path,
        "-vf", "fps=12,scale=480:-1:flags=lanczos,palettegen",
        palette
    ], capture_output=True)
    if p1.returncode != 0:
        return False, p1.stderr.decode()
    p2 = subprocess.run([
        FFMPEG, "-y", "-i", video_path, "-i", palette,
        "-lavfi", "fps=12,scale=480:-1:flags=lanczos[x];[x][1:v]paletteuse",
        gif_path
    ], capture_output=True)
    Path(palette).unlink(missing_ok=True)
    if p2.returncode != 0:
        return False, p2.stderr.decode()
    return True, gif_path


# ── Handlers ──────────────────────────────────────────────────────────────────
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 *Media Downloader Bot*\n\n"
        "Twitter/X veya YouTube linki at, ben hallederim!\n\n"
        "📥 *Desteklenen formatlar:*\n"
        "• Twitter → MP4 · GIF · MP3\n"
        "• YouTube → MP4 · MP3\n\n"
        "Linki direkt yaz, seçenekler gelsin ✅",
        parse_mode=ParseMode.MARKDOWN,
    )


async def handle_link(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    platform = detect_platform(text)
    if not platform:
        return

    ctx.user_data["url"] = text
    ctx.user_data["platform"] = platform

    if platform == "twitter":
        keyboard = [[
            InlineKeyboardButton("🎬 MP4",      callback_data="dl:twitter:mp4"),
            InlineKeyboardButton("🎞️ GIF",      callback_data="dl:twitter:gif"),
            InlineKeyboardButton("🎵 MP3",      callback_data="dl:twitter:mp3"),
        ]]
        label = "Twitter/X"
    else:
        keyboard = [[
            InlineKeyboardButton("🎬 MP4 (video)", callback_data="dl:youtube:mp4"),
            InlineKeyboardButton("🎵 MP3 (ses)",   callback_data="dl:youtube:mp3"),
        ]]
        label = "YouTube"

    await update.message.reply_text(
        f"🔗 *{label}* linki algılandı.\nHangi formatta indireyim?",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def handle_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    _, platform, fmt = query.data.split(":")
    url = ctx.user_data.get("url")
    if not url:
        await query.edit_message_text("⚠️ Oturum süresi doldu. Linki tekrar gönder.")
        return

    msg = await query.edit_message_text(f"⏳ İndiriliyor… `{fmt.upper()}` bekleniyor.")
    uid  = query.from_user.id
    base = str(DOWNLOAD_DIR / f"{uid}_media")

    try:
        if platform == "twitter":
            await _dl_twitter(update, ctx, url, fmt, base, msg)
        else:
            await _dl_youtube(update, ctx, url, fmt, base, msg)
    except asyncio.TimeoutError:
        await msg.edit_text("⏱️ Zaman aşımı. Tekrar dene.")
    except Exception as e:
        await msg.edit_text(f"❌ Hata:\n`{e}`", parse_mode=ParseMode.MARKDOWN)
    finally:
        for f in DOWNLOAD_DIR.glob(f"{uid}_media*"):
            f.unlink(missing_ok=True)


async def _dl_twitter(update, ctx, url, fmt, base, msg):
    chat_id = update.effective_chat.id

    # Video önce indir (mp4, gif, mp3 hepsi için lazım)
    ok, path = twitter_download(url, base)
    if not ok:
        await msg.edit_text(
            f"❌ Video indirilemedi:\n`{path}`\n\n"
            "_İpucu: x.com/kullanici/status/ID formatında olmalı._",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    if fmt == "mp4":
        await msg.edit_text("📤 Yükleniyor…")
        with open(path, "rb") as f:
            await ctx.bot.send_video(chat_id, f, caption="Twitter/X · MP4")

    elif fmt == "gif":
        await msg.edit_text("🎞️ GIF'e dönüştürülüyor…")
        gif_path = base + ".gif"
        ok2, gif = video_to_gif(path, gif_path)
        if not ok2:
            await msg.edit_text(f"❌ GIF dönüşümü başarısız:\n`{gif}`", parse_mode=ParseMode.MARKDOWN)
            return
        size_mb = Path(gif).stat().st_size / 1_048_576
        if size_mb > MAX_FILE_MB:
            await msg.edit_text(f"⚠️ GIF çok büyük ({size_mb:.1f} MB), MP4 gönderiyorum…")
            with open(path, "rb") as f:
                await ctx.bot.send_video(chat_id, f, caption="Twitter/X · (GIF yerine MP4)")
        else:
            await msg.edit_text("📤 Yükleniyor…")
            with open(gif, "rb") as f:
                await ctx.bot.send_animation(chat_id, f, caption="Twitter/X · GIF")

    elif fmt == "mp3":
        await msg.edit_text("🎵 Ses çıkarılıyor…")
        mp3_path = base + "_audio.mp3"
        p = subprocess.run([
            FFMPEG, "-y", "-i", path, "-vn",
            "-acodec", "libmp3lame", "-q:a", "2", mp3_path
        ], capture_output=True)
        if p.returncode != 0:
            await msg.edit_text("❌ Ses çıkarılamadı.")
            return
        await msg.edit_text("📤 Yükleniyor…")
        with open(mp3_path, "rb") as f:
            await ctx.bot.send_audio(chat_id, f, caption="Twitter/X · MP3")

    await msg.delete()


async def _dl_youtube(update, ctx, url, fmt, base, msg):
    chat_id = update.effective_chat.id

    if fmt == "mp4":
        ok, path = ytdlp_download(url, base + ".%(ext)s", [
            "-f", "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
            "--merge-output-format", "mp4",
        ])
        if not ok:
            await msg.edit_text(f"❌ İndirilemedi:\n`{path}`", parse_mode=ParseMode.MARKDOWN)
            return
        size_mb = Path(path).stat().st_size / 1_048_576
        if size_mb > MAX_FILE_MB:
            await msg.edit_text(f"⚠️ {size_mb:.1f} MB — limit aşıldı, 480p deneniyor…")
            Path(path).unlink(missing_ok=True)
            ok, path = ytdlp_download(url, base + "_low.%(ext)s", [
                "-f", "bestvideo[height<=480][ext=mp4]+bestaudio[ext=m4a]/worst[ext=mp4]",
                "--merge-output-format", "mp4",
            ])
            if not ok:
                await msg.edit_text("❌ Düşük kalite de başarısız.")
                return
        await msg.edit_text("📤 Yükleniyor…")
        with open(path, "rb") as f:
            await ctx.bot.send_video(chat_id, f, caption="YouTube · MP4", supports_streaming=True)

    elif fmt == "mp3":
        ok, path = ytdlp_download(url, base + ".%(ext)s", [
            "-x", "--audio-format", "mp3", "--audio-quality", "192K",
        ])
        if not ok:
            await msg.edit_text(f"❌ Ses indirilemedi:\n`{path}`", parse_mode=ParseMode.MARKDOWN)
            return
        await msg.edit_text("📤 Yükleniyor…")
        with open(path, "rb") as f:
            await ctx.bot.send_audio(chat_id, f, caption="YouTube · MP3")

    await msg.delete()


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_link))
    app.add_handler(CallbackQueryHandler(handle_callback, pattern=r"^dl:"))
    print("🤖 Bot başlatıldı…")
    app.run_polling()

if __name__ == "__main__":
    main()

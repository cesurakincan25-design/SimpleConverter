import os
import re
import asyncio
import tempfile
import subprocess
from pathlib import Path

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)
from telegram.constants import ParseMode

# ── Config ──────────────────────────────────────────────────────────────────
BOT_TOKEN = os.getenv("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
MAX_FILE_MB = 50          # Telegram bot limit (free tier)
DOWNLOAD_DIR = Path(tempfile.gettempdir()) / "tg_downloader"
DOWNLOAD_DIR.mkdir(exist_ok=True)

# ── URL patterns ─────────────────────────────────────────────────────────────
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


# ── yt-dlp helpers ────────────────────────────────────────────────────────────
def ytdlp_download(url: str, output_path: str, extra_args: list[str]) -> tuple[bool, str]:
    """Run yt-dlp and return (success, filepath_or_error)."""
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
    # yt-dlp may add extension; find what it created
    parent = Path(output_path).parent
    stem = Path(output_path).stem
    matches = list(parent.glob(f"{stem}*"))
    if matches:
        return True, str(matches[0])
    return False, "Dosya bulunamadı."


def video_to_gif(video_path: str, gif_path: str) -> tuple[bool, str]:
    """Convert video → GIF using ffmpeg (palette trick for quality)."""
    palette = gif_path.replace(".gif", "_palette.png")
    # Step 1: generate palette
    p1 = subprocess.run([
        "ffmpeg", "-y", "-i", video_path,
        "-vf", "fps=12,scale=480:-1:flags=lanczos,palettegen",
        palette
    ], capture_output=True)
    if p1.returncode != 0:
        return False, p1.stderr.decode()
    # Step 2: render GIF
    p2 = subprocess.run([
        "ffmpeg", "-y", "-i", video_path, "-i", palette,
        "-lavfi", "fps=12,scale=480:-1:flags=lanczos[x];[x][1:v]paletteuse",
        gif_path
    ], capture_output=True)
    Path(palette).unlink(missing_ok=True)
    if p2.returncode != 0:
        return False, p2.stderr.decode()
    return True, gif_path


# ── Telegram handlers ─────────────────────────────────────────────────────────
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
        return  # mesajı ignore et, komut değil

    ctx.user_data["url"] = text
    ctx.user_data["platform"] = platform

    if platform == "twitter":
        keyboard = [
            [
                InlineKeyboardButton("🎬 MP4", callback_data="dl:twitter:mp4"),
                InlineKeyboardButton("🎞️ GIF", callback_data="dl:twitter:gif"),
                InlineKeyboardButton("🎵 MP3", callback_data="dl:twitter:mp3"),
            ]
        ]
        label = "Twitter/X"
    else:
        keyboard = [
            [
                InlineKeyboardButton("🎬 MP4 (video)", callback_data="dl:youtube:mp4"),
                InlineKeyboardButton("🎵 MP3 (ses)", callback_data="dl:youtube:mp3"),
            ]
        ]
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

    uid = query.from_user.id
    base = str(DOWNLOAD_DIR / f"{uid}_media")

    try:
        if platform == "twitter":
            await _download_twitter(update, ctx, url, fmt, base, msg)
        else:
            await _download_youtube(update, ctx, url, fmt, base, msg)
    except asyncio.TimeoutError:
        await msg.edit_text("⏱️ İndirme zaman aşımına uğradı. Tekrar dene.")
    except Exception as e:
        await msg.edit_text(f"❌ Hata oluştu:\n`{e}`", parse_mode=ParseMode.MARKDOWN)
    finally:
        # Temizlik
        for f in DOWNLOAD_DIR.glob(f"{uid}_media*"):
            f.unlink(missing_ok=True)


async def _download_twitter(update, ctx, url, fmt, base, msg):
    chat_id = update.effective_chat.id

    if fmt == "mp4":
        ok, path = ytdlp_download(url, base + ".%(ext)s", [
            "-f", "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
            "--merge-output-format", "mp4",
        ])
        if not ok:
            await msg.edit_text(f"❌ MP4 indirilemedi:\n`{path}`", parse_mode=ParseMode.MARKDOWN)
            return
        await msg.edit_text("📤 Yükleniyor…")
        with open(path, "rb") as f:
            await ctx.bot.send_video(chat_id, f, caption="Twitter/X · MP4")

    elif fmt == "gif":
        # Önce video indir, sonra GIF'e çevir
        ok, path = ytdlp_download(url, base + ".%(ext)s", [
            "-f", "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
            "--merge-output-format", "mp4",
        ])
        if not ok:
            await msg.edit_text(f"❌ Video indirilemedi:\n`{path}`", parse_mode=ParseMode.MARKDOWN)
            return
        await msg.edit_text("🎞️ GIF'e dönüştürülüyor…")
        gif_path = base + ".gif"
        ok2, gif = video_to_gif(path, gif_path)
        if not ok2:
            await msg.edit_text(f"❌ GIF dönüşümü başarısız:\n`{gif}`", parse_mode=ParseMode.MARKDOWN)
            return
        size_mb = Path(gif).stat().st_size / 1_048_576
        if size_mb > MAX_FILE_MB:
            await msg.edit_text(f"⚠️ GIF boyutu çok büyük ({size_mb:.1f} MB > {MAX_FILE_MB} MB).\nMP4 olarak gönderiyorum…")
            with open(path, "rb") as f:
                await ctx.bot.send_video(chat_id, f, caption="Twitter/X · (GIF yerine MP4)")
        else:
            await msg.edit_text("📤 Yükleniyor…")
            with open(gif, "rb") as f:
                await ctx.bot.send_animation(chat_id, f, caption="Twitter/X · GIF")

    elif fmt == "mp3":
        ok, path = ytdlp_download(url, base + ".%(ext)s", [
            "-x", "--audio-format", "mp3", "--audio-quality", "192K",
        ])
        if not ok:
            await msg.edit_text(f"❌ Ses indirilemedi:\n`{path}`", parse_mode=ParseMode.MARKDOWN)
            return
        await msg.edit_text("📤 Yükleniyor…")
        with open(path, "rb") as f:
            await ctx.bot.send_audio(chat_id, f, caption="Twitter/X · MP3")

    await msg.delete()


async def _download_youtube(update, ctx, url, fmt, base, msg):
    chat_id = update.effective_chat.id

    if fmt == "mp4":
        ok, path = ytdlp_download(url, base + ".%(ext)s", [
            "-f", "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
            "--merge-output-format", "mp4",
        ])
        if not ok:
            await msg.edit_text(f"❌ Video indirilemedi:\n`{path}`", parse_mode=ParseMode.MARKDOWN)
            return
        size_mb = Path(path).stat().st_size / 1_048_576
        if size_mb > MAX_FILE_MB:
            await msg.edit_text(
                f"⚠️ Dosya boyutu {size_mb:.1f} MB — Telegram limiti {MAX_FILE_MB} MB.\n"
                "Daha düşük kalitede tekrar deneniyor…"
            )
            Path(path).unlink(missing_ok=True)
            ok, path = ytdlp_download(url, base + "_low.%(ext)s", [
                "-f", "bestvideo[height<=480][ext=mp4]+bestaudio[ext=m4a]/worst[ext=mp4]",
                "--merge-output-format", "mp4",
            ])
            if not ok:
                await msg.edit_text("❌ Düşük kaliteli indirme de başarısız.")
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

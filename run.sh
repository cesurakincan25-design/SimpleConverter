#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────
#  Telegram Media Downloader Bot — Kurulum & Çalıştırma
#  Gereksinimler: Python 3.11+, ffmpeg, yt-dlp
# ─────────────────────────────────────────────────────────────────

set -e

# 1. Bağımlılıkları yükle (Debian/Ubuntu)
echo "📦 Sistem bağımlılıkları kuruluyor…"
sudo apt-get update -qq
sudo apt-get install -y ffmpeg python3-pip python3-venv

# 2. Sanal ortam
echo "🐍 Sanal ortam oluşturuluyor…"
python3 -m venv .venv
source .venv/bin/activate

# 3. Python paketleri
echo "📥 Python paketleri yükleniyor…"
pip install --upgrade pip -q
pip install -r requirements.txt -q

# 4. Token kontrolü
if [ -z "$BOT_TOKEN" ]; then
  echo ""
  echo "⚠️  BOT_TOKEN ortam değişkeni ayarlı değil!"
  echo "   Şu şekilde ayarla:"
  echo "   export BOT_TOKEN='123456:ABC-DEF...'"
  echo ""
  read -rp "Token'ı şimdi gir (ya da Enter ile atla): " token
  if [ -n "$token" ]; then
    export BOT_TOKEN="$token"
  fi
fi

# 5. Botu başlat
echo ""
echo "🤖 Bot başlatılıyor…"
python bot.py

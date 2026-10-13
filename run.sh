#!/usr/bin/env bash
# Botni ishga tushirish (macOS / Linux). Birinchi marta virtual muhit yaratadi.
set -e
cd "$(dirname "$0")"
if [ ! -f .env ]; then
  echo "Birinchi ishga tushirish: .env yaratilmoqda."
  cp .env.example .env
  read -r -p "Telegram bot token (BotFather): " TG
  read -r -p "Groq API key (gsk_...): " KEY
  TG="$TG" KEY="$KEY" perl -pi -e 's/^TELEGRAM_BOT_TOKEN=.*/TELEGRAM_BOT_TOKEN=$ENV{TG}/; s/^LLM_API_KEY=.*/LLM_API_KEY=$ENV{KEY}/' .env
  echo ".env saqlandi. Keyin o'zgartirish uchun: open -e .env"
fi
PY=$(command -v python3.12 || command -v python3.11 || command -v python3.13 || command -v python3.10 || command -v python3)
if [ ! -d .venv ]; then
  echo "Virtual muhit yaratilmoqda ($PY)..."
  "$PY" -m venv .venv
fi
source .venv/bin/activate
pip install -q --upgrade pip >/dev/null
pip install -q -r requirements.txt
echo "Bot ishga tushmoqda... (to'xtatish: Ctrl+C)"
python main.py

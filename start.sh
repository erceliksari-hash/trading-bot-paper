#!/usr/bin/env bash
set -euo pipefail

# start.sh - create/activate venv, install requirements, load .env, run bot
PROJECT_DIR="$HOME/trading-bot-paper"
cd "$PROJECT_DIR"

# Create venv if it does not exist
if [ ! -d "venv" ]; then
  python3 -m venv venv
fi

# Activate virtualenv
# shellcheck source=/dev/null
source venv/bin/activate

# Export variables from .env if present (ignore commented lines)
if [ -f .env ]; then
  # shellcheck disable=SC2046
  export $(grep -v '^#' .env | xargs)
fi

# Upgrade pip and install requirements
python -m pip install --upgrade pip setuptools wheel
if [ -f requirements.txt ]; then
  pip install -r requirements.txt
fi

# Ensure log directory
OUT_LOG="/tmp/tradingbot.out.log"
ERR_LOG="/tmp/tradingbot.err.log"
mkdir -p "$(dirname "$OUT_LOG")"

# Run the trading bot (replace with python3 if needed)
exec python trading_bot.py >>"$OUT_LOG" 2>>"$ERR_LOG"

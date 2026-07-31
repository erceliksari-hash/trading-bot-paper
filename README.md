# Trading bot paper-trade repository

This repository contains a minimal paper-trading trading bot that:
- Fetches data from Binance (via ccxt) for crypto and Yahoo Finance for stocks
- Computes simple indicators (EMA, RSI, ATR)
- Generates basic buy/sell signals (EMA crossover + RSI filter)
- Executes paper trades (updates wallet.json and logs trades)
- Sends Telegram notifications for trades and hourly summaries
- Runs every 15 minutes (scheduler)

Getting started

1. Clone the repo

   git clone https://github.com/erceliksari-hash/trading-bot-paper.git
   cd trading-bot-paper

2. Create a Python virtual environment and install dependencies

   python3 -m venv venv
   source venv/bin/activate
   pip install -r requirements.txt

3. Configure environment

Set Telegram credentials as environment variables (recommended):

   export TELEGRAM_TOKEN="your_token"
   export TELEGRAM_CHAT_ID="your_chat_id"

Or fill config.json (not recommended for tokens).

4. Create wallet.json (example)

   {
     "USD": 10000.0
   }

5. Run the bot

   python trading_bot.py

Run as daemon (linux): use systemd or Docker. See systemd/ and Dockerfile in repo.

Notes
- This is a paper-trading template for testing and development. Do not use with real API keys without adding safety checks and proper error handling.
- Add or remove assets in config.json. For crypto use symbols like "BTC/USDT". For stocks use Yahoo tickers like "AAPL" or BIST with ".IS" suffix if available on yfinance.

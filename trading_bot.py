# Basit, genişletilebilir trading bot iskeleti (paper trading + Telegram bildirimleri)
# Gereksinimler: ccxt, pandas, numpy, yfinance, python-telegram-bot, apscheduler, pandas_ta

import os
import time
import json
import logging
from datetime import datetime
import ccxt
import yfinance as yf
import pandas as pd
import numpy as np
import pandas_ta as ta
from apscheduler.schedulers.background import BackgroundScheduler
import requests

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Config yükle (assets, params) ---
with open("config.json", "r") as f:
    CONFIG = json.load(f)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN") or CONFIG.get("telegram_token")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID") or CONFIG.get("telegram_chat_id")

# Paper wallet (basit)
if os.path.exists("wallet.json"):
    with open("wallet.json", "r") as f:
        WALLET = json.load(f)  # { "USD": 10000.0 }
else:
    WALLET = {"USD": 10000.0}

# Basit trade history
TRADES_LOG = "trades_log.csv"
if not os.path.exists(TRADES_LOG):
    pd.DataFrame(columns=["time","symbol","side","price","amount","reason"]).to_csv(TRADES_LOG,index=False)

# CCXT örneği (sadece data için)
exchange = ccxt.binance({"enableRateLimit": True})

# --- Helper functions ---
def send_telegram(text, image_path=None):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        logger.warning("Telegram token/chat_id not set, skipping notification")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"}
    try:
        requests.post(url, data=payload, timeout=10)
    except Exception as e:
        logger.exception("Telegram message failed: %s", e)
    if image_path:
        url2 = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto"
        try:
            with open(image_path, "rb") as img:
                requests.post(url2, data={"chat_id": TELEGRAM_CHAT_ID}, files={"photo": img})
        except Exception as e:
            logger.exception("Telegram photo failed: %s", e)

def fetch_ohlcv_ccxt(symbol, timeframe="15m", limit=200):
    data = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
    df = pd.DataFrame(data, columns=["ts","open","high","low","close","volume"])
    df["datetime"] = pd.to_datetime(df["ts"], unit="ms")
    df.set_index("datetime", inplace=True)
    return df

def fetch_ohlcv_yfinance(ticker, period="7d", interval="15m"):
    df = yf.download(tickers=ticker, period=period, interval=interval, progress=False)
    if df.empty:
        return df
    df = df.rename(columns={"Open":"open","High":"high","Low":"low","Close":"close","Volume":"volume"})
    df.index = pd.to_datetime(df.index)
    return df[["open","high","low","close","volume"]]

def compute_indicators(df):
    df["ema_short"] = ta.ema(df["close"], length=9)
    df["ema_long"] = ta.ema(df["close"], length=21)
    df["rsi"] = ta.rsi(df["close"], length=14)
    df["atr"] = ta.atr(df["high"], df["low"], df["close"], length=14)
    return df

def detect_support_resistance(df, window=5):
    highs = df["high"].rolling(window*2+1, center=True).apply(
        lambda x: 1 if np.argmax(x)==window else 0, raw=True
    )
    lows = df["low"].rolling(window*2+1, center=True).apply(
        lambda x: 1 if np.argmin(x)==window else 0, raw=True
    )
    sr = {"resistances": df[highs==1]["high"].tolist(), "supports": df[lows==1]["low"].tolist()}
    return sr

def fibonacci_levels(df):
    high = df["high"].max()
    low = df["low"].min()
    diff = high - low
    levels = {
        "0.0": high,
        "0.236": high - 0.236*diff,
        "0.382": high - 0.382*diff,
        "0.5": high - 0.5*diff,
        "0.618": high - 0.618*diff,
        "1.0": low
    }
    return levels

def position_size(balance_usd, risk_pct, price, atr):
    risk_amount = balance_usd * risk_pct
    qty = risk_amount / (atr if atr>0 else price*0.01)
    return qty

def log_trade(time, symbol, side, price, amount, reason):
    row = {"time": time, "symbol": symbol, "side": side, "price": price, "amount": amount, "reason": reason}
    df = pd.DataFrame([row])
    df.to_csv(TRADES_LOG, mode="a", header=False, index=False)

# --- Strategy / Signal generation ---
def generate_signal(symbol, source="crypto"):
    try:
        if source=="crypto":
            df = fetch_ohlcv_ccxt(symbol, timeframe=CONFIG["timeframe"], limit=300)
        else:
            df = fetch_ohlcv_yfinance(symbol, period="7d", interval=CONFIG["timeframe"])
        if df.empty:
            return None, "no_data"
        df = compute_indicators(df)
        sr = detect_support_resistance(df)
        fib = fibonacci_levels(df.tail(100))
        last = df.iloc[-1]
        prev = df.iloc[-2]

        signal = None
        reason = ""
        if (prev["ema_short"] < prev["ema_long"]) and (last["ema_short"] > last["ema_long"]):
            if last["rsi"] < CONFIG["rsi_overbought"]:
                signal = "buy"
                reason = "ema_cross + rsi_ok"
            else:
                signal = None
                reason = "rsi_overbought_block"
        elif (prev["ema_short"] > prev["ema_long"]) and (last["ema_short"] < last["ema_long"]):
            if last["rsi"] > CONFIG["rsi_oversold"]:
                signal = "sell"
                reason = "ema_cross_down + rsi_ok"
            else:
                signal = None
                reason = "rsi_oversold_block"

        vol_ok = last["volume"] > df["volume"].rolling(20).mean().iloc[-1] * 0.7
        if signal and not vol_ok:
            reason += " | low_volume_flag"

        atr = last["atr"] if not np.isnan(last["atr"]) else (last["high"]-last["low"])
        price = last["close"]
        sl = price - 1.5*atr if signal=="buy" else price + 1.5*atr if signal=="sell" else None
        tp = price + 3*atr if signal=="buy" else price - 3*atr if signal=="sell" else None

        meta = {
            "signal": signal,
            "reason": reason,
            "price": float(price),
            "sl": float(sl) if sl is not None else None,
            "tp": float(tp) if tp is not None else None,
            "atr": float(atr),
            "supports": sr["supports"][-3:],
            "resistances": sr["resistances"][-3:],
            "fibonacci": fib
        }
        return meta, "ok"
    except Exception as e:
        logger.exception("generate_signal error for %s: %s", symbol, e)
        return None, "error"

# --- Paper trade executor ---
def paper_execute(symbol, meta, side):
    price = meta["price"]
    atr = meta["atr"]
    balance = WALLET.get("USD", 10000.0)
    risk_pct = CONFIG["daily_risk_pct"]
    qty = position_size(balance, risk_pct, price, atr)
    cost = qty * price
    base_symbol = symbol.split("/")[0] if "/" in symbol else symbol
    WALLET["USD"] = WALLET.get("USD", 0) - cost if side=="buy" else WALLET.get("USD",0) + cost
    WALLET[base_symbol] = WALLET.get(base_symbol, 0) + qty if side=="buy" else WALLET.get(base_symbol,0) - qty
    log_trade(datetime.utcnow().isoformat(), symbol, side, price, qty, meta.get("reason",""))
    with open("wallet.json","w") as f:
        json.dump(WALLET, f, indent=2)
    send_telegram(f"Paper trade executed: {side} {symbol} price={price:.4f} qty={qty:.6f}\nSL={meta['sl']}\nTP={meta['tp']}\nReason: {meta.get('reason')}")
    return True

# --- Main periodic job ---
def run_analysis_cycle():
    logger.info("Run analysis: %s", datetime.utcnow().isoformat())
    for item in CONFIG["assets"]:
        symbol = item["symbol"]
        source = item.get("source","crypto")
        meta, status = generate_signal(symbol, source=source)
        if status!="ok":
            logger.info("No data/status for %s: %s", symbol, status)
            continue
        if meta["signal"]:
            if CONFIG.get("paper_trade", True):
                paper_execute(symbol, meta, meta["signal"])
            else:
                send_telegram(f"Signal for {symbol}: {meta['signal']} @ {meta['price']:.4f}\nSL:{meta['sl']}\nTP:{meta['tp']}\nReason:{meta['reason']}")
        else:
            logger.info("No signal for %s (%s)", symbol, meta["reason"])
    logger.info("Cycle finished.")

# --- Hourly summary job ---
def hourly_summary():
    wallet_text = "<b>Hourly summary</b>\n\n"
    for k,v in WALLET.items():
        wallet_text += f"{k}: {v}\n"
    if os.path.exists(TRADES_LOG):
        trades = pd.read_csv(TRADES_LOG)
        last = trades.tail(5).to_string(index=False)
        wallet_text += "\nLast trades:\n" + last
    send_telegram(wallet_text)

# --- Scheduler setup ---
def start_scheduler():
    scheduler = BackgroundScheduler()
    scheduler.add_job(run_analysis_cycle, "interval", minutes=15, next_run_time=datetime.utcnow())
    scheduler.add_job(hourly_summary, "cron", minute=0)
    scheduler.start()
    logger.info("Scheduler started.")
    try:
        while True:
            time.sleep(1)
    except (KeyboardInterrupt, SystemExit):
        scheduler.shutdown()

if __name__=="__main__":
    start_scheduler()

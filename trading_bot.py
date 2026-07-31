# trading_bot.py
# Basit, genişletilebilir trading bot iskeleti (paper trading + Telegram bildirimleri)
# Gereksinimler: ccxt, pandas, numpy, yfinance, requests, apscheduler, pandas_ta

import os
import time
import json
import logging
from datetime import datetime, timezone
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

# TELEGRAM token/chat fallback-okuma (çeşitli env anahtarlarını destekler)
TELEGRAM_TOKEN = (
    os.getenv("TELEGRAM_BOT_TOKEN")
    or os.getenv("TELEGRAM_TOKEN")
    or CONFIG.get("telegram_token")
)
TELEGRAM_CHAT_ID = (
    os.getenv("TELEGRAM_CHAT_ID")
    or os.getenv("TELEGRAM_CHATID")
    or os.getenv("TELEGRAM_CHAT")
    or CONFIG.get("telegram_chat_id")
)

# Ensure chat id representation (store as string for requests payload)
if TELEGRAM_CHAT_ID is not None:
    TELEGRAM_CHAT_ID = str(TELEGRAM_CHAT_ID)

# Paper wallet (basit)
if os.path.exists("wallet.json"):
    with open("wallet.json", "r") as f:
        WALLET = json.load(f)  # { "USD": 10000.0 }
else:
    WALLET = {"USD": 10000.0}

# Basit trade history
TRADES_LOG = "trades_log.csv"
if not os.path.exists(TRADES_LOG):
    pd.DataFrame(columns=["time", "symbol", "side", "price", "amount", "reason"]).to_csv(TRADES_LOG, index=False)

# CCXT örneği (sadece data için) - yaratılırken hata olursa None bırakılır
try:
    exchange = ccxt.binance({"enableRateLimit": True})
except Exception as e:
    logger.exception("CCXT initialization failed: %s", e)
    exchange = None

# --- Helper functions ---
def send_telegram(text, image_path=None, retries=3, backoff=2):
    """Basit Telegram notifier; token/chat id yoksa sadece loglar.
    - retries: deneme sayısı
    - backoff: saniye cinsinden bekleme (her retry artar)
    """
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        logger.warning("Telegram token/chat_id not set, skipping notification")
        return False

    for attempt in range(1, retries + 1):
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
            payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"}
            r = requests.post(url, data=payload, timeout=10)
            logger.debug("Telegram send attempt %s, status=%s, body=%s", attempt, r.status_code, r.text)
            r.raise_for_status()

            if image_path:
                url2 = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto"
                with open(image_path, "rb") as img:
                    r2 = requests.post(url2, data={"chat_id": TELEGRAM_CHAT_ID}, files={"photo": img}, timeout=20)
                    logger.debug("Telegram photo send status=%s body=%s", r2.status_code, r2.text)
                    r2.raise_for_status()
            return True
        except Exception as e:
            logger.warning("Telegram send attempt %s failed: %s", attempt, e)
            if attempt < retries:
                time.sleep(backoff * attempt)
            else:
                logger.exception("All Telegram send attempts failed.")
                return False

def get_telegram_me():
    """Helper: getMe ile token doğrulaması yapar ve kullanıcı bilgisini döner."""
    if not TELEGRAM_TOKEN:
        return None
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getMe"
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        logger.exception("getMe failed: %s", e)
        return None

def fetch_ohlcv_ccxt(symbol, timeframe="15m", limit=200):
    """CCXT ile veri çekme, hata ve boş veri koruması."""
    if not exchange:
        logger.error("CCXT exchange client not available")
        return pd.DataFrame()
    try:
        data = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
        if not data:
            return pd.DataFrame()
        df = pd.DataFrame(data, columns=["ts", "open", "high", "low", "close", "volume"])
        df["datetime"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
        df.set_index("datetime", inplace=True)
        df = df[["open", "high", "low", "close", "volume"]].apply(pd.to_numeric, errors="coerce")
        df = df.dropna()
        return df
    except Exception as e:
        logger.exception("fetch_ohlcv_ccxt failed for %s: %s", symbol, e)
        return pd.DataFrame()

def fetch_ohlcv_yfinance(ticker, period="7d", interval="15m"):
    """yfinance wrapper; normalize kolon isimleri ve boş veri guard'ı."""
    try:
        df = yf.download(tickers=ticker, period=period, interval=interval, progress=False)
        if df is None or df.empty:
            return pd.DataFrame()
        
        # yfinance MultiIndex (çok katmanlı sütun) düzeltmesi
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)

        df.columns = [str(c).lower() for c in df.columns]
        if "adj close" in df.columns and "close" not in df.columns:
            df["close"] = df["adj close"]
        for col in ["open", "high", "low", "close", "volume"]:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")
        df = df.dropna()
        df.index = pd.to_datetime(df.index, utc=True)
        return df[["open", "high", "low", "close", "volume"]]
    except Exception as e:
        logger.exception("fetch_ohlcv_yfinance failed for %s: %s", ticker, e)
        return pd.DataFrame()

def compute_indicators(df):
    """Basit göstergeler; eksik veri durumunda exception fırlatır."""
    df = df.copy()
    required = {"open", "high", "low", "close", "volume"}
    if not required.issubset(df.columns):
        raise ValueError(f"DataFrame missing required cols: {set(df.columns)}")
    try:
        df["ema_short"] = ta.ema(df["close"], length=9)
        df["ema_long"] = ta.ema(df["close"], length=21)
        df["rsi"] = ta.rsi(df["close"], length=14)
        df["atr"] = ta.atr(df["high"], df["low"], df["close"], length=14)
    except Exception as e:
        logger.exception("compute_indicators failed: %s", e)
        raise
    return df

def detect_support_resistance(df, window=5):
    """Support/resistance detection using centered rolling; returns lists."""
    if df is None or df.empty:
        return {"resistances": [], "supports": []}
    needed = window * 2 + 1
    if len(df) < needed:
        return {"resistances": [], "supports": []}
    try:
        highs = df["high"].rolling(needed, center=True).apply(
            lambda x: 1 if np.argmax(x) == window else 0, raw=True
        ).fillna(0)
        lows = df["low"].rolling(needed, center=True).apply(
            lambda x: 1 if np.argmin(x) == window else 0, raw=True
        ).fillna(0)
        res_mask = highs == 1
        sup_mask = lows == 1
        resistances = df.loc[res_mask, "high"].tolist()
        supports = df.loc[sup_mask, "low"].tolist()
        return {"resistances": resistances, "supports": supports}
    except Exception as e:
        logger.exception("detect_support_resistance failed: %s", e)
        return {"resistances": [], "supports": []}

def fibonacci_levels(df):
    if df is None or df.empty:
        return {}
    try:
        high = df["high"].max()
        low = df["low"].min()
        diff = max(high - low, 0)
        return {
            "0.0": high,
            "0.236": high - 0.236 * diff,
            "0.382": high - 0.382 * diff,
            "0.5": high - 0.5 * diff,
            "0.618": high - 0.618 * diff,
            "1.0": low,
        }
    except Exception as e:
        logger.exception("fibonacci_levels failed: %s", e)
        return {}

def position_size(balance_usd, risk_pct, price, atr):
    try:
        base_risk = risk_pct if (risk_pct and risk_pct > 0) else CONFIG.get("daily_risk_pct", 0.01)
        risk_amount = balance_usd * base_risk
        effective_atr = atr if (atr and not np.isnan(atr) and atr > 0) else max(price * 0.01, 1e-8)
        qty = risk_amount / effective_atr
        return qty
    except Exception as e:
        logger.exception("position_size failed: %s", e)
        return 0.0

def log_trade(time_str, symbol, side, price, amount, reason):
    row = {"time": time_str, "symbol": symbol, "side": side, "price": price, "amount": amount, "reason": reason}
    df = pd.DataFrame([row])
    df.to_csv(TRADES_LOG, mode="a", header=False, index=False)

# --- Strategy / Signal generation ---
def generate_signal(symbol, source="crypto"):
    try:
        if source == "crypto":
            df = fetch_ohlcv_ccxt(symbol, timeframe=CONFIG.get("timeframe", "15m"), limit=300)
        else:
            df = fetch_ohlcv_yfinance(symbol, period="7d", interval=CONFIG.get("timeframe", "15m"))
        if df is None or df.empty:
            return None, "no_data"
        df = compute_indicators(df)
        sr = detect_support_resistance(df)
        fib = fibonacci_levels(df.tail(100))
        if len(df) < 2:
            return None, "no_data"
        last = df.iloc[-1]
        prev = df.iloc[-2]

        signal = None
        reason = ""

        if pd.notna(prev.get("ema_short")) and pd.notna(prev.get("ema_long")) and pd.notna(last.get("ema_short")) and pd.notna(last.get("ema_long")):
            if (prev["ema_short"] < prev["ema_long"]) and (last["ema_short"] > last["ema_long"]):
                if last.get("rsi", 100) < CONFIG.get("rsi_overbought", 70):
                    signal = "buy"
                    reason = "ema_cross + rsi_ok"
                else:
                    reason = "rsi_overbought_block"
            elif (prev["ema_short"] > prev["ema_long"]) and (last["ema_short"] < last["ema_long"]):
                if last.get("rsi", 0) > CONFIG.get("rsi_oversold", 30):
                    signal = "sell"
                    reason = "ema_cross_down + rsi_ok"
                else:
                    reason = "rsi_oversold_block"
        else:
            reason = "indicator_nan"

        vol_ok = True
        try:
            vol_mean = df["volume"].rolling(20).mean().iloc[-1]
            if pd.notna(vol_mean):
                vol_ok = last["volume"] > vol_mean * 0.7
        except Exception:
            vol_ok = True
        if signal and not vol_ok:
            reason += " | low_volume_flag"

        atr = last.get("atr") if pd.notna(last.get("atr")) else (last["high"] - last["low"])
        price = last["close"]
        sl = price - 1.5 * atr if signal == "buy" else (price + 1.5 * atr if signal == "sell" else None)
        tp = price + 3 * atr if signal == "buy" else (price - 3 * atr if signal == "sell" else None)

        meta = {
            "signal": signal,
            "reason": reason,
            "price": float(price) if price is not None else None,
            "sl": float(sl) if sl is not None else None,
            "tp": float(tp) if tp is not None else None,
            "atr": float(atr) if atr is not None else None,
            "supports": sr.get("supports", [])[-3:],
            "resistances": sr.get("resistances", [])[-3:],
            "fibonacci": fib
        }
        return meta, "ok"
    except Exception as e:
        logger.exception("generate_signal error for %s: %s", symbol, e)
        return None, "error"

# --- Paper trade executor ---
def paper_execute(symbol, meta, side):
    price = meta.get("price")
    atr = meta.get("atr")
    balance = WALLET.get("USD", 10000.0)
    risk_pct = CONFIG.get("daily_risk_pct", 0.01)
    qty = position_size(balance, risk_pct, price, atr)
    cost = qty * price
    base_symbol = symbol.split("/")[0] if "/" in symbol else symbol
    if side == "buy":
        WALLET["USD"] = WALLET.get("USD", 0) - cost
        WALLET[base_symbol] = WALLET.get(base_symbol, 0) + qty
    else:
        WALLET["USD"] = WALLET.get("USD", 0) + cost
        WALLET[base_symbol] = max(WALLET.get(base_symbol, 0) - qty, 0)
    log_trade(datetime.now(timezone.utc).isoformat(), symbol, side, price, qty, meta.get("reason", ""))
    with open("wallet.json", "w") as f:
        json.dump(WALLET, f, indent=2)
    try:
        send_telegram(f"Paper trade executed: {side} {symbol} price={price:.4f} qty={qty:.6f}\nSL={meta.get('sl')}\nTP={meta.get('tp')}\nReason: {meta.get('reason')}")
    except Exception:
        logger.exception("Failed to send telegram after paper trade")
    return True

# --- Main periodic job ---
def run_analysis_cycle():
    logger.info("Run analysis: %s", datetime.now(timezone.utc).isoformat())
    for item in CONFIG.get("assets", []):
        symbol = item.get("symbol")
        source = item.get("source", "crypto")
        if not symbol:
            continue
        meta, status = generate_signal(symbol, source=source)
        if status != "ok":
            logger.info("No data/status for %s: %s", symbol, status)
            continue
        if meta and meta.get("signal"):
            if CONFIG.get("paper_trade", True):
                try:
                    paper_execute(symbol, meta, meta.get("signal"))
                except Exception as e:
                    logger.exception("paper_execute failed: %s", e)
            else:
                send_telegram(f"Signal for {symbol}: {meta.get('signal')} @ {meta.get('price'):.4f}\nSL:{meta.get('sl')}\nTP:{meta.get('tp')}\nReason:{meta.get('reason')}")
        else:
            logger.info("No signal for %s (%s)", symbol, meta.get("reason") if meta else "no_meta")
    logger.info("Cycle finished.")

# --- Hourly summary job ---
def hourly_summary():
    wallet_text = "<b>Hourly summary</b>\n\n"
    for k, v in WALLET.items():
        wallet_text += f"{k}: {v}\n"
    if os.path.exists(TRADES_LOG):
        try:
            trades = pd.read_csv(TRADES_LOG)
            last = trades.tail(5).to_string(index=False)
            wallet_text += "\nLast trades:\n" + last
        except Exception as e:
            logger.exception("hourly_summary read trades failed: %s", e)
    send_telegram(wallet_text)

# --- Scheduler setup ---
def start_scheduler():
    # Startup telegram check
    me = get_telegram_me()
    if me is None:
        logger.warning("Telegram getMe failed or TELEGRAM_TOKEN not set. Telegram notifications may not work.")
    else:
        try:
            ok = send_telegram(f"<b>Bot started</b>\nTime: {datetime.now(timezone.utc).isoformat()}")
            if ok:
                logger.info("Startup telegram notification sent.")
            else:
                logger.warning("Startup telegram notification failed.")
        except Exception:
            logger.exception("Startup telegram notification exception")

    scheduler = BackgroundScheduler()
    scheduler.add_job(run_analysis_cycle, "interval", minutes=int(CONFIG.get("interval_minutes", 15)), next_run_time=datetime.now(timezone.utc))
    scheduler.add_job(hourly_summary, "cron", minute=0)
    scheduler.start()
    logger.info("Scheduler started.")
    try:
        while True:
            time.sleep(1)
    except (KeyboardInterrupt, SystemExit):
        scheduler.shutdown()

if __name__ == "__main__":
    start_scheduler()

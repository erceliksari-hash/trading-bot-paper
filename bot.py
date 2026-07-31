import os
import asyncio
import logging
import io
from datetime import datetime, timedelta

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

import pandas as pd
import numpy as np

from telegram import Bot, InputFile, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ApplicationBuilder, CallbackQueryHandler, CommandHandler, ContextTypes
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

import config
from assets import ASSETS
from strategy import SignalStrategy
from data_provider import AsyncDataProvider

config.setup_logging()
logger = logging.getLogger(__name__)


class VirtualPortfolio:
    """Sanal portföy (paper trading) yönetimi — kademeli kar alma, trailing stop, breakeven desteği."""

    def __init__(self, initial_balance: float = 10000.0, notify_callback=None):
        self.initial_balance = initial_balance
        self.balance = initial_balance
        self.positions = {}
        self.closed_trades = []
        self.daily_target = 0.015
        self.daily_pnl = 0.0
        self.total_pnl = 0.0
        self.trade_count = 0
        self.win_count = 0
        self.loss_count = 0
        self.last_prices = {}
        self.notify_callback = notify_callback

    def _notify(self, msg: str):
        """Telegram callback'ı varsa async olarak mesaj gönderir (fail-safe)."""
        if self.notify_callback:
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(self.notify_callback(msg))
            except RuntimeError:
                logger.debug("Bildirim için event loop yok.")
                pass

    def _portfolio_summary_html(self) -> str:
        """Anlık kasa durumunu HTML formatında döndürür."""
        s = self.get_summary()
        unrealized, _ = self.get_unrealized_pnl()
        return (
            f"\n💰 <b>KASA:</b> {s['balance']:.2f} USDT | "
            f"Varlık: {s['total_equity']:.2f} USDT | "
            f"Unrealized: {unrealized:+.2f} USDT\n"
            f"📈 Getiri: %{s['total_return_pct']} | "
            f"Bugün: {s['daily_pnl']:+.2f} USDT | "
            f"WinRate: %{s['win_rate']} ({s['win_count']}/{s['trade_count']}) | "
            f"Açık: {s['open_positions']}"
        )

    def open_position(self, symbol, direction, entry, size, sl, tp, tp1=None, tp2=None, tp3=None, market_condition='YATAY'):
        if symbol in self.positions:
            logger.info(f"Pozisyon zaten açık: {symbol}")
            return False
        amount = self.balance * size
        self.positions[symbol] = {
            'direction': direction, 'entry': entry, 'size': size,
            'amount': amount, 'sl': sl, 'tp': tp,
            'tp1': tp1, 'tp2': tp2, 'tp3': tp3,
            'open_time': datetime.now(),
            'scale_out_done': {'tp1': False, 'tp2': False, 'tp3': False},
            'remaining_size': 1.0,
            'highest_price': entry,
            'lowest_price': entry,
            'add_count': 0,
            'breakeven_done': False,
            'market_condition': market_condition
        }
        self.last_prices[symbol] = entry

        notify_msg = (
            f"📥 <b>POZİSYON AÇILDI</b>\n"
            f"🔹 {symbol} {direction} @ {entry}\n"
            f"🔹 Miktar: {amount:.2f} USDT\n"
            f"{self._portfolio_summary_html()}"
        )
        self._notify(notify_msg)
        logger.info(f"[PORTFOY] AÇILDI {symbol} {direction} @ {entry} | Miktar: {amount:.2f} USDT")
        return True

    def partial_close(self, symbol, exit_price, close_pct, reason):
        pos = self.positions.get(symbol)
        if not pos:
            logger.debug(f"partial_close: pozisyon yok {symbol}")
            return None
        close_amount = pos['amount'] * pos['remaining_size'] * close_pct
        if pos['direction'] == 'LONG':
            pnl_pct = (exit_price - pos['entry']) / pos['entry']
        else:
            pnl_pct = (pos['entry'] - exit_price) / pos['entry']
        pnl_amount = close_amount * pnl_pct
        self.balance += pnl_amount
        self.daily_pnl += pnl_amount
        self.total_pnl += pnl_amount
        pos['remaining_size'] -= close_pct
        if pos['remaining_size'] <= 0.01:
            return self.close_position(symbol, exit_price, reason)

        notify_msg = (
            f"🎯 <b>KISMI KAPAMA</b>\n"
            f"🔹 {symbol} %{close_pct*100:.0f} @ {exit_price}\n"
            f"🔹 P&L: {pnl_amount:+.2f} USDT | Kalan: %{pos['remaining_size']*100:.0f}\n"
            f"🔹 Sebep: {reason}\n"
            f"{self._portfolio_summary_html()}"
        )
        self._notify(notify_msg)
        logger.info(f"[PORTFOY] KISMI KAPAMA {symbol} %{close_pct*100:.0f} @ {exit_price}")
        return {
            'symbol': symbol, 'direction': pos['direction'],
            'entry': pos['entry'], 'exit': exit_price,
            'pnl_pct': round(pnl_pct * 100, 2),
            'pnl_amount': round(pnl_amount, 2),
            'reason': reason, 'time': datetime.now(),
            'partial': True, 'remaining': pos['remaining_size']
        }

    def close_position(self, symbol, exit_price, reason):
        pos = self.positions.pop(symbol, None)
        if not pos:
            logger.debug(f"close_position: pozisyon yok {symbol}")
            return None
        if pos['direction'] == 'LONG':
            pnl_pct = (exit_price - pos['entry']) / pos['entry']
        else:
            pnl_pct = (pos['entry'] - exit_price) / pos['entry']
        pnl_amount = pos['amount'] * pos['remaining_size'] * pnl_pct
        self.balance += pnl_amount
        self.daily_pnl += pnl_amount
        self.total_pnl += pnl_amount
        self.trade_count += 1
        if pnl_amount > 0:
            self.win_count += 1
            result_emoji = "✅ KAR"
        else:
            self.loss_count += 1
            result_emoji = "❌ ZARAR"
        trade_record = {
            'symbol': symbol, 'direction': pos['direction'],
            'entry': pos['entry'], 'exit': exit_price,
            'pnl_pct': round(pnl_pct * 100, 2),
            'pnl_amount': round(pnl_amount, 2),
            'reason': reason, 'time': datetime.now(), 'partial': False
        }
        self.closed_trades.append(trade_record)
        self.last_prices.pop(symbol, None)

        notify_msg = (
            f"{result_emoji} <b>POZİSYON KAPANDI</b>\n"
            f"🔹 {symbol} {pos['direction']} @ {exit_price}\n"
            f"🔹 P&L: {pnl_amount:+.2f} USDT (%{pnl_pct*100:.2f})\n"
            f"🔹 Sebep: {reason}\n"
            f"{self._portfolio_summary_html()}"
        )
        self._notify(notify_msg)
        logger.info(f"[PORTFOY] KAPANDI {symbol} {result_emoji} %{pnl_pct*100:.2f} | Bakiye: {self.balance:.2f}")
        return trade_record

    def update_price(self, symbol, price):
        self.last_prices[symbol] = price
        if symbol in self.positions:
            pos = self.positions[symbol]
            if pos['direction'] == 'LONG':
                pos['highest_price'] = max(pos.get('highest_price', pos['entry']), price)
            else:
                pos['lowest_price'] = min(pos.get('lowest_price', pos['entry']), price)

    def check_scale_out(self, symbol, current):
        if symbol not in self.positions:
            return
        pos = self.positions[symbol]
        direction = pos['direction']
        try:
            if pos.get('tp1') and not pos['scale_out_done'].get('tp1'):
                if (direction == 'LONG' and current >= pos['tp1']) or (direction == 'SHORT' and current <= pos['tp1']):
                    self.partial_close(symbol, current, 0.50, "TP1 Kademeli Kar Alma")
                    if symbol in self.positions:
                        self.positions[symbol]['scale_out_done']['tp1'] = True

            if symbol in self.positions and pos.get('tp2') and not pos['scale_out_done'].get('tp2'):
                if (direction == 'LONG' and current >= pos['tp2']) or (direction == 'SHORT' and current <= pos['tp2']):
                    self.partial_close(symbol, current, 0.30, "TP2 Kademeli Kar Alma")
                    if symbol in self.positions:
                        self.positions[symbol]['scale_out_done']['tp2'] = True

            if symbol in self.positions and pos.get('tp3') and not pos['scale_out_done'].get('tp3'):
                if (direction == 'LONG' and current >= pos['tp3']) or (direction == 'SHORT' and current <= pos['tp3']):
                    self.partial_close(symbol, current, 0.20, "TP3 Kademeli Kar Alma")
                    if symbol in self.positions:
                        self.positions[symbol]['scale_out_done']['tp3'] = True
        except Exception as e:
            logger.exception(f"check_scale_out hata: {e}")

    def check_trailing_stop(self, symbol, current, atr):
        if symbol not in self.positions:
            return False
        pos = self.positions[symbol]
        entry = pos['entry']
        direction = pos['direction']
        try:
            if direction == 'LONG':
                profit_pct = (current - entry) / entry
                if profit_pct >= 0.02:
                    recent_low = float(self.last_prices.get(symbol, current)) * 0.98
                    new_sl = max(recent_low, entry)
                    if new_sl > pos['sl']:
                        old_sl = pos['sl']
                        pos['sl'] = new_sl
                        self._notify(f"🔄 <b>Trailing Stop</b>\n{symbol}: SL {old_sl:.4f} → {new_sl:.4f}")
                        return True
            else:
                profit_pct = (entry - current) / entry
                if profit_pct >= 0.02:
                    recent_high = float(self.last_prices.get(symbol, current)) * 1.02
                    new_sl = min(recent_high, entry)
                    if new_sl < pos['sl']:
                        old_sl = pos['sl']
                        pos['sl'] = new_sl
                        self._notify(f"🔄 <b>Trailing Stop</b>\n{symbol}: SL {old_sl:.4f} → {new_sl:.4f}")
                        return True
        except Exception as e:
            logger.exception(f"check_trailing_stop hata: {e}")
        return False

    def check_breakeven(self, symbol, current):
        if symbol not in self.positions:
            return
        pos = self.positions[symbol]
        if pos.get('breakeven_done'):
            return
        try:
            entry = pos['entry']
            direction = pos['direction']
            if direction == 'LONG':
                if (current - entry) / entry >= 0.01:
                    pos['sl'] = entry * 1.001
                    pos['breakeven_done'] = True
                    self._notify(f"🛡️ <b>Breakeven</b>\n{symbol}: SL girişe çekildi @ {pos['sl']:.4f}")
            else:
                if (entry - current) / entry >= 0.01:
                    pos['sl'] = entry * 0.999
                    pos['breakeven_done'] = True
                    self._notify(f"🛡️ <b>Breakeven</b>\n{symbol}: SL girişe çekildi @ {pos['sl']:.4f}")
        except Exception as e:
            logger.exception(f"check_breakeven hata: {e}")

    def check_time_exit(self, symbol):
        if symbol not in self.positions:
            return False
        pos = self.positions[symbol]
        open_time = pos.get('open_time')
        if not open_time:
            return False
        try:
            hours_open = (datetime.now() - open_time).total_seconds() / 3600
            current = self.last_prices.get(symbol, pos['entry'])
            if hours_open > 24:
                self.close_position(symbol, current, f"⌛ 24 saat zaman çıkışı ({round(hours_open, 1)} saat)")
                return True
            if hours_open > 12:
                pnl_pct = (current - pos['entry']) / pos['entry'] if pos['direction'] == 'LONG' else (pos['entry'] - current) / pos['entry']
                if pnl_pct < 0:
                    self.close_position(symbol, current, f"⌛ 12 saat zararda, zaman çıkışı")
                    return True
        except Exception as e:
            logger.exception(f"check_time_exit hata: {e}")
        return False

    def check_sl(self, symbol, current):
        if symbol not in self.positions:
            return False
        pos = self.positions[symbol]
        try:
            if pos['direction'] == 'LONG' and current <= pos['sl']:
                self.close_position(symbol, current, "Stop Loss")
                return True
            elif pos['direction'] == 'SHORT' and current >= pos['sl']:
                self.close_position(symbol, current, "Stop Loss")
                return True
        except Exception as e:
            logger.exception(f"check_sl hata: {e}")
        return False

    def check_trend_reversal(self, symbol, df):
        if symbol not in self.positions or df is None or len(df) < 3:
            return False, ""
        pos = self.positions[symbol]
        try:
            last = df.iloc[-1]
            prev = df.iloc[-2]
            current = float(last['close'])
            direction = pos['direction']
            reversal_score = 0
            signals = []
            hist = float(last['macd_hist']) if 'macd_hist' in last and not pd.isna(last['macd_hist']) else 0
            hist_prev = float(prev['macd_hist']) if 'macd_hist' in prev and not pd.isna(prev['macd_hist']) else 0

            if direction == "LONG":
                if 0 < hist < hist_prev:
                    reversal_score += 1; signals.append("MACD hist daralıyor")
                if hist < 0:
                    reversal_score += 2; signals.append("MACD negatif")
            else:
                if hist_prev < hist < 0:
                    reversal_score += 1; signals.append("MACD hist daralıyor")
                if hist > 0:
                    reversal_score += 2; signals.append("MACD pozitif")

            rsi = float(last['rsi']) if 'rsi' in last and not pd.isna(last['rsi']) else 50
            if direction == "LONG" and rsi > 70:
                reversal_score += 1; signals.append("RSI aşırı alım (>70)")
            if direction == "SHORT" and rsi < 30:
                reversal_score += 1; signals.append("RSI aşırı satım (<30)")

            if reversal_score >= 3:
                self.close_position(symbol, current, "Trend dönüşü: " + ", ".join(signals[:2]))
                return True, ", ".join(signals[:2])
        except Exception as e:
            logger.exception(f"check_trend_reversal hata: {e}")
        return False, ""

    def get_unrealized_pnl(self):
        unrealized = 0.0
        details = []
        for symbol, pos in self.positions.items():
            price = self.last_prices.get(symbol, pos['entry'])
            try:
                if pos['direction'] == 'LONG':
                    pnl_pct = (price - pos['entry']) / pos['entry'] * 100
                    pnl_amount = (price - pos['entry']) / pos['entry'] * pos['amount'] * pos['remaining_size']
                else:
                    pnl_pct = (pos['entry'] - price) / pos['entry'] * 100
                    pnl_amount = (pos['entry'] - price) / pos['entry'] * pos['amount'] * pos['remaining_size']
                unrealized += pnl_amount
                details.append({
                    'symbol': symbol, 'direction': pos['direction'],
                    'entry': pos['entry'], 'current': price,
                    'pnl_pct': round(pnl_pct, 2), 'pnl_amount': round(pnl_amount, 2),
                    'remaining': pos['remaining_size']
                })
            except Exception as e:
                logger.exception(f"get_unrealized_pnl hata {symbol}: {e}")
        return round(unrealized, 2), details

    def get_summary(self):
        win_rate = (self.win_count / self.trade_count * 100) if self.trade_count > 0 else 0.0
        total_return = (self.balance - self.initial_balance) / self.initial_balance * 100
        unrealized, _ = self.get_unrealized_pnl()
        total_equity = self.balance + unrealized
        return {
            'balance': round(self.balance, 2), 'initial': self.initial_balance,
            'total_pnl': round(self.total_pnl, 2), 'daily_pnl': round(self.daily_pnl, 2),
            'unrealized_pnl': unrealized,
            'total_equity': round(total_equity, 2),
            'total_return_pct': round(total_return, 2),
            'trade_count': self.trade_count, 'win_count': self.win_count,
            'loss_count': self.loss_count, 'win_rate': round(win_rate, 1),
            'open_positions': len(self.positions),
            'daily_target_met': total_return >= self.daily_target * 100
        }

    def reset_daily(self):
        self.daily_pnl = 0.0


class TradingBot:
    def __init__(self):
        self._telegram_token = config.TELEGRAM_TOKEN
        self.chat_id = config.CHAT_ID
        if self._telegram_token:
            self.bot = Bot(token=self._telegram_token)
            logger.info("Telegram bot başlatıldı.")
        else:
            self.bot = None
            logger.warning("TELEGRAM_BOT_TOKEN bulunamadı; bildirimler devre dışı.")

        self.data_provider = AsyncDataProvider()
        self.strategy = SignalStrategy()
        self.portfolio = VirtualPortfolio(initial_balance=config.CAPITAL, notify_callback=self.send_msg)
        self.analysis_history = []
        self.running = True

    async def send_msg(self, text: str):
        if not self.bot or not self.chat_id:
            logger.info("Telegram mesaj (gönderilmedi): " + text[:200])
            return
        try:
            await self.bot.send_message(chat_id=self.chat_id, text=text, parse_mode='HTML')
        except Exception as e:
            logger.exception("Telegram hatası: " + str(e))

    async def fetch_data(self, symbol, is_crypto, period, interval):
        for attempt in range(3):
            try:
                if is_crypto:
                    df = await self.data_provider.fetch_crypto(symbol, timeframe=interval, limit=200)
                else:
                    df = await self.data_provider.fetch_stock(symbol, period=period, interval=interval)
                if df is None or len(df) < 60 or 'close' not in df.columns:
                    return None
                return df
            except Exception as e:
                logger.warning(f"{symbol} deneme {attempt+1}/3: {e}")
                if attempt < 2:
                    await asyncio.sleep(5)
        return None

    async def send_chart(self, df, symbol, result, category):
        try:
            plt.figure(figsize=(12, 8))
            sr = result.get('sr', {}) if isinstance(result, dict) else {}
            plt.subplot(2, 1, 1)
            plt.plot(df.index, df['close'], label='Kapanış', color='black', linewidth=1.2)
            if 'ema9' in df.columns: plt.plot(df.index, df['ema9'], label='EMA9', alpha=0.7, color='blue', linewidth=0.8)
            if 'ema21' in df.columns: plt.plot(df.index, df['ema21'], label='EMA21', alpha=0.7, color='orange', linewidth=0.8)
            if 'vwap' in df.columns: plt.plot(df.index, df['vwap'], label='VWAP', alpha=0.5, color='cyan', linewidth=0.8)
            
            if sr.get('nearest_resistance'): plt.axhline(y=sr['nearest_resistance'], color='darkred', linestyle='--', alpha=0.6, label='D1: '+str(sr['nearest_resistance']))
            if sr.get('nearest_support'): plt.axhline(y=sr['nearest_support'], color='darkgreen', linestyle='--', alpha=0.6, label='S1: '+str(sr['nearest_support']))
            if result.get('tp1'): plt.axhline(y=result['tp1'], color='gold', linestyle='--', alpha=0.5, label='TP1: '+str(result['tp1']))
            if result.get('sl') is not None and result.get('current') is not None:
                plt.axhline(y=result['sl'], color='red', linestyle='-', alpha=0.9, label='SL: '+str(result['sl']))
                plt.axhline(y=result['current'], color='green', linestyle='-', alpha=0.7, label='Giriş: '+str(result['current']))
            
            plt.title(f"{category} | {symbol} | {result.get('signal')} (Güç: {result.get('strength', 0)}/5)", fontsize=11)
            plt.legend(loc='upper left', fontsize=7, ncol=2)
            plt.grid(True, alpha=0.3)
            
            plt.subplot(2, 1, 2)
            if 'rsi' in df.columns:
                plt.plot(df.index, df['rsi'], color='purple', linewidth=1)
                plt.axhline(y=70, color='red', linestyle='--', alpha=0.5)
                plt.axhline(y=30, color='green', linestyle='--', alpha=0.5)
                plt.ylim(0, 100)
                plt.title(f"RSI: {result.get('rsi', '')}")
                plt.grid(True, alpha=0.3)
            
            plt.tight_layout()
            buf = io.BytesIO()
            plt.savefig(buf, format='png', dpi=80)
            buf.seek(0)
            plt.close()

            if self.bot and self.chat_id:
                try:
                    photo = InputFile(buf, filename=f"{symbol}_chart.png")
                    await self.bot.send_photo(chat_id=self.chat_id, photo=photo)
                except Exception as e:
                    logger.exception(f"Grafik gönderme hatası {symbol}: {e}")
            buf.close()
        except Exception as e:
            logger.exception("Grafik hatası " + symbol + ": " + str(e))

    async def analyze_category(self, category_name, cfg):
        msg_parts = []
        strong_signals = []
        for symbol in cfg['symbols']:
            df = await self.fetch_data(symbol, cfg['is_crypto'], cfg['period'], cfg['timeframe'])
            if df is None: continue
            try:
                result = self.strategy.analyze(df, symbol)
            except Exception as e:
                logger.exception("Strateji hatası " + symbol + ": " + str(e))
                continue
            if result is None or 'current' not in result: continue
            current = result['current']

            try:
                self.portfolio.update_price(symbol, current)
            except Exception:
                pass

            if symbol in self.portfolio.positions:
                self.portfolio.check_scale_out(symbol, current)
                self.portfolio.check_trailing_stop(symbol, current, result.get('atr', current * 0.01))
                self.portfolio.check_breakeven(symbol, current)
                if not self.portfolio.check_time_exit(symbol):
                    if not self.portfolio.check_sl(symbol, current):
                        self.portfolio.check_trend_reversal(symbol, result.get('df'))

            if result.get('signal') in ['AL', 'SAT'] and symbol not in self.portfolio.positions:
                direction = result.get('direction')
                size = float(result.get('risk_percent') or 0)
                sl, tp = result.get('sl'), result.get('tp')
                tp1, tp2, tp3 = result.get('tp1'), result.get('tp2'), result.get('tp3')
                market_condition = result.get('market_condition', 'YATAY')
                if sl and tp and size > 0:
                    self.portfolio.open_position(symbol, direction, current, size, sl, tp, tp1, tp2, tp3, market_condition)

            signal = result.get('signal', "NÖTR")
            emoji = {"AL": "🟢", "SAT": "🔴", "EKLE": "📥", "KAPAT": "📤", "KISMI KAPAT": "📉"}.get(signal, "⚪")
            trend_emoji = "📈" if result.get('trend') == "YUKSELIS" else "📉" if result.get('trend') == "DUSUS" else "➡️"

            part = f"{emoji} <b>{symbol}</b> | Trend: {trend_emoji} {result.get('trend')}\n"
            part += f"   Sinyal: <b>{signal}</b> (Güç: {'⭐' * int(result.get('strength',0))})\n"
            part += f"   Fiyat: {current} | RSI: {result.get('rsi')} ({result.get('rsi_zone')})\n"
            msg_parts.append(part)

            if result.get('strength', 0) >= 3 and signal != "NÖTR":
                strong_signals.append({'symbol': symbol, 'result': result, 'df': df, 'category': category_name})
                if signal in ["AL", "SAT", "EKLE", "KAPAT", "KISMI KAPAT"]:
                    await self.send_chart(df, symbol, result, category_name)
                    await asyncio.sleep(1)

        if msg_parts:
            header = f"📊 <b>{category_name} ANALİZİ</b> ({datetime.now().strftime('%H:%M')})\n\n"
            await self.send_msg(header + "\n".join(msg_parts))
        return strong_signals

    async def run_analysis(self):
        logger.info("=== 15dk ANALİZİ BAŞLIYOR ===")
        s = self.portfolio.get_summary()
        if s['total_return_pct'] >= self.portfolio.daily_target * 100:
            if not self.strategy.daily_target_met:
                self.strategy.daily_target_met = True
                await self.send_msg("🎉 <b>GÜNLÜK HEDEF TAMAMLANDI!</b>\nGetiri: %" + str(s['total_return_pct']))
        else:
            self.strategy.daily_target_met = False

        for category, cfg in ASSETS.items():
            try:
                await self.analyze_category(category, cfg)
                await asyncio.sleep(2)
            except Exception as e:
                logger.exception(category + " hatası: " + str(e))
        logger.info("=== ANALİZ TAMAMLANDI ===")

    async def send_hourly_portfolio_status(self):
        s = self.portfolio.get_summary()
        await self.send_msg(f"💼 <b>KASA DURUMU</b>\n💰 Bakiye: {s['balance']} USDT\n📈 Toplam Varlık: {s['total_equity']} USDT")

    async def send_daily_portfolio_report(self):
        s = self.portfolio.get_summary()
        await self.send_msg(f"📈 <b>GÜNLÜK RAPOR</b>\n💰 Bakiye: {s['balance']} USDT\nGetiri: %{s['total_return_pct']}")


# Global bot instance referansı (Menü içinde anlık özet basabilmek için)
global_bot_instance = None


# --- TELEGRAM INLINE MENÜ HANDLER FONKSİYONLARI ---

async def menu_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Kullanıcı /start veya /menu yazdığında ana menüyü anlık özetle birlikte gösterir."""
    global global_bot_instance

    # Anlık kasa ve piyasa özet metni oluşturalım
    summary_text = "🤖 <b>AKILLI PİYASA & KASA PANELİ</b>\n\n"
    if global_bot_instance and global_bot_instance.portfolio:
        s = global_bot_instance.portfolio.get_summary()
        unrealized, _ = global_bot_instance.portfolio.get_unrealized_pnl()
        summary_text += (
            f"💰 <b>Kasa:</b> {s['balance']} USDT | Varlık: {s['total_equity']} USDT\n"
            f"📈 <b>Toplam Getiri:</b> %{s['total_return_pct']} | Günlük: {s['daily_pnl']:+.2f} USDT\n"
            f"📊 <b>Açık Pozisyon:</b> {s['open_positions']} | WinRate: %{s['win_rate']}\n"
            f"-----------------------------------------\n"
        )
    else:
        summary_text += "ℹ️ Sistem aktif, veriler yükleniyor...\n-----------------------------------------\n"

    summary_text += "Güncel kategori seçiminizi aşağıdan yapabilirsiniz:"

    keyboard = [
        [InlineKeyboardButton("📊 Piyasa / Kripto", callback_data="market")],
        [InlineKeyboardButton("📈 BIST / Hisse", callback_data="bist")],
        [InlineKeyboardButton("💱 Döviz Kurları", callback_data="forex")],
        [InlineKeyboardButton("💰 Kasa Yönetimi", callback_data="cash_management")],
        [InlineKeyboardButton("🏦 Sanal Kasa Yönetimi", callback_data="virtual_cash")],
        [InlineKeyboardButton("🪙 Emtia", callback_data="commodities")],
        [InlineKeyboardButton("📰 Haber ve Analiz", callback_data="news_analysis")],
        [InlineKeyboardButton("⚙️ Sistem Durumu", callback_data="status")],
        [InlineKeyboardButton("🔄 Paneli Yenile", callback_data="main_menu")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    if update.message:
        await update.message.reply_text(summary_text, parse_mode="HTML", reply_markup=reply_markup)
    elif update.callback_query:
        query = update.callback_query
        await query.answer("Panel güncellendi.")
        try:
            await query.edit_message_text(text=summary_text, parse_mode="HTML", reply_markup=reply_markup)
        except Exception:
            pass

async def button_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    back_button = [[InlineKeyboardButton("🔙 Ana Menüye Dön", callback_data="main_menu")]]
    reply_markup = InlineKeyboardMarkup(back_button)

    if query.data == "market":
        content = "📊 <b>Güncel Piyasa / Kripto Verileri:</b>\n\nArka plandaki 15 dakikalık otomatik tarama sonuçları ana akışa düşmektedir."
    elif query.data == "bist":
        content = "📈 <b>BIST / Hisse Senetleri:</b>\n\nBIST tarama modülü aktif."
    elif query.data == "forex":
        content = "💱 <b>Döviz Kurları:</b>\n\nMajör parite takip durumları güncel."
    elif query.data == "cash_management":
        content = "💰 <b>Kasa Yönetimi:</b>\n\nRisk parametreleri ve bakiye dağılımı kontrol altında."
    elif query.data == "virtual_cash":
        content = "🏦 <b>Sanal Kasa Yönetimi (Paper Trading):</b>\n\nAktif pozisyonlar ve kademeli kar al / stop loss seviyeleri izleniyor."
    elif query.data == "commodities":
        content = "🪙 <b>Emtia Verileri:</b>\n\nAltın, gümüş ve emtia sinyalleri devrede."
    elif query.data == "news_analysis":
        content = "📰 <b>Haber ve Analiz:</b>\n\nPiyasa akışı ve teknik özetler."
    elif query.data == "status":
        global global_bot_instance
        op_count = len(global_bot_instance.portfolio.positions) if global_bot_instance else 0
        content = f"⚙️ <b>Sistem Durumu:</b>\n\n- Bot Durumu: Çalışıyor ✅\n- Açık Pozisyon Sayısı: {op_count}\n- Zamanlayıcılar: Aktif"
    elif query.data == "main_menu":
        await menu_start(update, context)
        return
    else:
        content = "İşlem gerçekleştiriliyor..."

    try:
        await query.edit_message_text(text=content, parse_mode="HTML", reply_markup=reply_markup)
    except Exception:
        # bazı durumlarda edit_message_text hata verebilir; sessizce geç
        pass


async def main():
    global global_bot_instance
    bot_instance = TradingBot()
    global_bot_instance = bot_instance
    
    if not bot_instance._telegram_token:
        logger.error("TELEGRAM_TOKEN tanımlı değil! Lütfen config.py dosyasını kontrol edin.")
        return

    scheduler = AsyncIOScheduler()
    scheduler.add_job(bot_instance.run_analysis, IntervalTrigger(minutes=config.INTERVAL_MINUTES), id='analysis')
    scheduler.add_job(bot_instance.send_hourly_portfolio_status, IntervalTrigger(hours=config.SUMMARY_INTERVAL_HOURS), id='portfolio_status')
    scheduler.add_job(bot_instance.send_daily_portfolio_report, IntervalTrigger(hours=24), id='daily')
    scheduler.start()

    application = ApplicationBuilder().token(bot_instance._telegram_token).build()
    
    application.add_handler(CommandHandler("start", menu_start))
    application.add_handler(CommandHandler("menu", menu_start))
    application.add_handler(CallbackQueryHandler(button_callback_handler))

    logger.info("Bot ve Anlık Özet Menü Sistemi Başlatıldı...")
    
    await application.initialize()
    await application.start()
    await application.updater.start_polling()

    try:
        while bot_instance.running:
            await asyncio.sleep(3600)
    except (KeyboardInterrupt, asyncio.CancelledError):
        logger.info("Bot durduruluyor...")
    finally:
        try:
            await application.updater.stop()
        except Exception:
            pass
        await application.stop()
        await application.shutdown()
        await bot_instance.data_provider.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Kullanıcı tarafından sonlandırıldı.")

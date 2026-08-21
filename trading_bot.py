import os
import logging
import asyncio
import aiohttp
import hmac
import hashlib
from datetime import datetime
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
from typing import Optional, Dict, List, Tuple

# ─── Environment ─────────────────────────────────────────────────────────────
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
BINANCE_API_KEY = os.getenv("BINANCE_API_KEY")
BINANCE_SECRET_KEY = os.getenv("BINANCE_SECRET_KEY")

if not all([BOT_TOKEN, BINANCE_API_KEY, BINANCE_SECRET_KEY]):
    raise ValueError("Missing required environment variables: BOT_TOKEN, BINANCE_API_KEY, BINANCE_SECRET_KEY")

# ─── Scalping Configuration ───────────────────────────────────────────────────
BINANCE_BASE_URL = "https://api.binance.com"

TRADING_PAIRS = {
    "BTC": "BTCUSDT",
    "ETH": "ETHUSDT",
    "BNB": "BNBUSDT",
    "SOL": "SOLUSDT",
    "XRP": "XRPUSDT",
}

SCALPING_TIMEFRAMES = ["1m", "5m", "15m"]   # Ultra-fast scalping timeframes
TAKE_PROFIT_PERCENT = 2.5                    # Quick profit capture
STOP_LOSS_PERCENT = 1.5                      # Fast loss cutting
RISK_PERCENT = 4.0                           # % of capital risked per trade
SCAN_INTERVAL_SECONDS = 30                   # Market scan interval (seconds)
KLINES_LIMIT = 100                           # Candles per request

# ─── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ─── Global state ─────────────────────────────────────────────────────────────
# Unlimited concurrent positions: symbol → trade info
active_trades: Dict[str, Dict] = {}
is_running = False          # continuous scan loop flag
scan_task: Optional[asyncio.Task] = None
total_trades_today = 0
total_pnl_today = 0.0


# ─────────────────────────────────────────────────────────────────────────────
# Binance API Client
# ─────────────────────────────────────────────────────────────────────────────
class BinanceClient:
    """Binance REST API client with HMAC-signed requests."""

    def __init__(self, api_key: str, secret_key: str):
        self.api_key = api_key
        self.secret_key = secret_key
        self.base_url = BINANCE_BASE_URL

    def _sign(self, query: str) -> str:
        return hmac.new(
            self.secret_key.encode(),
            query.encode(),
            hashlib.sha256,
        ).hexdigest()

    async def _get(self, path: str, params: str = "", signed: bool = False) -> Optional[dict]:
        try:
            if signed:
                ts = int(datetime.now().timestamp() * 1000)
                query = f"{params}&timestamp={ts}" if params else f"timestamp={ts}"
                sig = self._sign(query)
                url = f"{self.base_url}{path}?{query}&signature={sig}"
            else:
                url = f"{self.base_url}{path}?{params}" if params else f"{self.base_url}{path}"

            headers = {"X-MBX-APIKEY": self.api_key}
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as r:
                    if r.status == 200:
                        return await r.json()
                    logger.error(f"GET {path} → HTTP {r.status}: {await r.text()}")
        except Exception as e:
            logger.error(f"GET {path} error: {e}")
        return None

    async def _post(self, path: str, params: str) -> Optional[dict]:
        try:
            ts = int(datetime.now().timestamp() * 1000)
            query = f"{params}&timestamp={ts}"
            sig = self._sign(query)
            url = f"{self.base_url}{path}?{query}&signature={sig}"
            headers = {"X-MBX-APIKEY": self.api_key}
            async with aiohttp.ClientSession() as session:
                async with session.post(url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as r:
                    if r.status == 200:
                        return await r.json()
                    logger.error(f"POST {path} → HTTP {r.status}: {await r.text()}")
        except Exception as e:
            logger.error(f"POST {path} error: {e}")
        return None

    async def get_account_balance(self) -> Optional[float]:
        """Return free USDT balance."""
        data = await self._get("/api/v3/account", signed=True)
        if data:
            for b in data.get("balances", []):
                if b["asset"] == "USDT":
                    return float(b["free"])
        return None

    async def get_current_price(self, symbol: str) -> Optional[float]:
        data = await self._get("/api/v3/ticker/price", f"symbol={symbol}")
        return float(data["price"]) if data else None

    async def get_klines(self, symbol: str, interval: str, limit: int = KLINES_LIMIT) -> Optional[List[List]]:
        """Return raw kline rows: [open_time, open, high, low, close, volume, ...]."""
        data = await self._get("/api/v3/klines", f"symbol={symbol}&interval={interval}&limit={limit}")
        return data if isinstance(data, list) else None

    async def place_order(self, symbol: str, side: str, quantity: float) -> Optional[dict]:
        qty_str = f"{quantity:.8f}".rstrip("0").rstrip(".")
        params = f"symbol={symbol}&side={side}&type=MARKET&quantity={qty_str}"
        return await self._post("/api/v3/order", params)


# ─────────────────────────────────────────────────────────────────────────────
# Technical Indicators
# ─────────────────────────────────────────────────────────────────────────────
def calc_ema(prices: List[float], period: int) -> Optional[float]:
    if len(prices) < period:
        return None
    k = 2 / (period + 1)
    val = sum(prices[:period]) / period
    for p in prices[period:]:
        val = p * k + val * (1 - k)
    return val


def calc_ema_series(prices: List[float], period: int) -> List[float]:
    """Return the full EMA series (same length as prices, NaN-padded at start)."""
    if len(prices) < period:
        return []
    k = 2 / (period + 1)
    series: List[float] = []
    val = sum(prices[:period]) / period
    series.append(val)
    for p in prices[period:]:
        val = p * k + val * (1 - k)
        series.append(val)
    return series


def calc_rsi(prices: List[float], period: int = 14) -> Optional[float]:
    """Wilder's Smoothed RSI using all available price data."""
    if len(prices) < period + 1:
        return None
    # Seed with simple averages over the first `period` changes
    changes = [prices[i] - prices[i - 1] for i in range(1, len(prices))]
    avg_g = sum(max(d, 0) for d in changes[:period]) / period
    avg_l = sum(abs(min(d, 0)) for d in changes[:period]) / period
    # Wilder smoothing over remaining candles
    for d in changes[period:]:
        gain = max(d, 0)
        loss = abs(min(d, 0))
        avg_g = (avg_g * (period - 1) + gain) / period
        avg_l = (avg_l * (period - 1) + loss) / period
    if avg_l == 0:
        return 100.0
    return 100.0 - 100.0 / (1 + avg_g / avg_l)


def calc_macd(prices: List[float]) -> Tuple[Optional[float], Optional[float]]:
    """Return (macd_line, signal_line) using standard 12/26/9 settings."""
    ema12 = calc_ema_series(prices, 12)
    ema26 = calc_ema_series(prices, 26)
    if not ema12 or not ema26:
        return None, None
    # Align both series on the longer EMA
    offset = len(ema12) - len(ema26)
    macd_series = [ema12[offset + i] - ema26[i] for i in range(len(ema26))]
    signal_series = calc_ema_series(macd_series, 9)
    if not signal_series:
        return None, None
    return macd_series[-1], signal_series[-1]


def calc_support_resistance(closes: List[float], window: int = 20) -> Tuple[float, float]:
    """Simple S/R using rolling min/max over last `window` candles."""
    recent = closes[-window:]
    return min(recent), max(recent)


def calc_volume_ratio(volumes: List[float], period: int = 20) -> Optional[float]:
    """Current volume vs average of previous `period` candles."""
    if len(volumes) < period + 1:
        return None
    avg_vol = sum(volumes[-period - 1:-1]) / period
    if avg_vol == 0:
        return None
    return volumes[-1] / avg_vol


# ─────────────────────────────────────────────────────────────────────────────
# Signal Analysis  (ALL indicators must align for a confirmed entry)
# ─────────────────────────────────────────────────────────────────────────────
async def analyze_symbol(client: BinanceClient, symbol: str, tf: str) -> Optional[Dict]:
    """
    Returns a signal dict or None.
    A BUY/SELL signal requires ALL of:
      1. EMA alignment  (5 > 10 > 20 for BUY, reversed for SELL)
      2. RSI in healthy zone (40–60 for BUY entry, same window avoids over-extended moves)
      3. MACD histogram positive (BUY) / negative (SELL)
      4. Volume spike ≥ 1.2× average
      5. Price near support (BUY) or resistance (SELL)
    """
    rows = await client.get_klines(symbol, tf, limit=KLINES_LIMIT)
    if not rows or len(rows) < 50:
        return None

    try:
        closes = [float(r[4]) for r in rows]
        volumes = [float(r[5]) for r in rows]

        price = closes[-1]
        e5 = calc_ema(closes, 5)
        e10 = calc_ema(closes, 10)
        e20 = calc_ema(closes, 20)
        rsi_val = calc_rsi(closes, 14)
        macd_line, signal_line = calc_macd(closes)
        support, resistance = calc_support_resistance(closes, 20)
        vol_ratio = calc_volume_ratio(volumes, 20)

        if None in (e5, e10, e20, rsi_val, macd_line, signal_line, vol_ratio):
            return None

        macd_hist = macd_line - signal_line
        proximity_pct = 0.5 / 100  # within 0.5% of S/R level

        buy_signals = [
            e5 > e10 > e20,                                     # EMA bullish alignment
            40 <= rsi_val <= 65,                                 # RSI healthy, not over-bought
            macd_hist > 0,                                       # MACD positive momentum
            vol_ratio >= 1.2,                                    # Volume confirmation
            price <= support * (1 + proximity_pct),             # Near support level
        ]

        sell_signals = [
            e5 < e10 < e20,                                     # EMA bearish alignment
            35 <= rsi_val <= 60,                                 # RSI healthy, not over-sold
            macd_hist < 0,                                       # MACD negative momentum
            vol_ratio >= 1.2,                                    # Volume confirmation
            price >= resistance * (1 - proximity_pct),          # Near resistance level
        ]

        if all(buy_signals):
            signal = "BUY"
        elif all(sell_signals):
            signal = "SELL"
        else:
            signal = "WAIT"

        return {
            "signal": signal,
            "price": price,
            "ema5": round(e5, 4),
            "ema10": round(e10, 4),
            "ema20": round(e20, 4),
            "rsi": round(rsi_val, 2),
            "macd_hist": round(macd_hist, 6),
            "vol_ratio": round(vol_ratio, 2),
            "support": round(support, 4),
            "resistance": round(resistance, 4),
        }
    except Exception as e:
        logger.error(f"analyze_symbol({symbol},{tf}): {e}")
        return None


def multi_tf_signal(results: List[Optional[Dict]]) -> str:
    """
    Require ALL available timeframes to agree on the same BUY/SELL signal.
    If any timeframe says WAIT or returns no data, signal is WAIT.
    """
    valid = [r for r in results if r is not None]
    if not valid:
        return "WAIT"
    signals = [r["signal"] for r in valid]
    if all(s == "BUY" for s in signals):
        return "BUY"
    if all(s == "SELL" for s in signals):
        return "SELL"
    return "WAIT"


# ─────────────────────────────────────────────────────────────────────────────
# Position Sizing  (dynamic, risk-based)
# ─────────────────────────────────────────────────────────────────────────────
def compute_position_size(capital: float, price: float) -> float:
    """
    Risk RISK_PERCENT of current capital per trade.
    With SL at STOP_LOSS_PERCENT below entry, the max loss per unit = price * SL%.
    quantity = (capital * risk%) / (price * SL%)
    """
    risk_amount = capital * (RISK_PERCENT / 100)
    loss_per_unit = price * (STOP_LOSS_PERCENT / 100)
    if loss_per_unit == 0:
        return 0.0
    return risk_amount / loss_per_unit


# ─────────────────────────────────────────────────────────────────────────────
# Trade Execution
# ─────────────────────────────────────────────────────────────────────────────
async def open_trade(client: BinanceClient, symbol: str, signal: str, notify_chat_id: int, app) -> bool:
    """Open a new position. Returns True on success."""
    global total_trades_today

    try:
        capital = await client.get_account_balance()
        if not capital or capital < 5:
            logger.warning(f"Insufficient balance (${capital}) to open {symbol}")
            return False

        price = await client.get_current_price(symbol)
        if not price:
            return False

        quantity = compute_position_size(capital, price)
        if quantity <= 0:
            return False

        order = await client.place_order(symbol, signal, quantity)
        if not order:
            return False

        tp_price = price * (1 + TAKE_PROFIT_PERCENT / 100) if signal == "BUY" else price * (1 - TAKE_PROFIT_PERCENT / 100)
        sl_price = price * (1 - STOP_LOSS_PERCENT / 100) if signal == "BUY" else price * (1 + STOP_LOSS_PERCENT / 100)

        active_trades[symbol] = {
            "side": signal,
            "entry_price": price,
            "quantity": quantity,
            "tp_price": tp_price,
            "sl_price": sl_price,
            "capital_at_open": capital,
            "opened_at": datetime.now(),
            "order_id": order.get("orderId"),
        }
        total_trades_today += 1

        msg = (
            f"✅ *{signal} Ouvert* | `{symbol}`\n"
            f"💵 Prix: `${price:.4f}`\n"
            f"📦 Quantité: `{quantity:.6f}`\n"
            f"🎯 TP: `${tp_price:.4f}` (+{TAKE_PROFIT_PERCENT}%)\n"
            f"⛔ SL: `${sl_price:.4f}` (-{STOP_LOSS_PERCENT}%)\n"
            f"💰 Capital utilisé: `${capital * RISK_PERCENT / 100:.2f}` ({RISK_PERCENT}%)\n"
            f"📊 Capital total: `${capital:.2f}`"
        )
        await app.bot.send_message(chat_id=notify_chat_id, text=msg, parse_mode="Markdown")
        logger.info(f"OPENED {signal} {symbol} @ ${price:.4f} qty={quantity:.6f}")
        return True

    except Exception as e:
        logger.error(f"open_trade({symbol}): {e}")
        return False


async def close_trade(client: BinanceClient, symbol: str, reason: str, notify_chat_id: int, app):
    """Close an open position and report PnL."""
    global total_pnl_today

    trade = active_trades.get(symbol)
    if not trade:
        return

    try:
        close_side = "SELL" if trade["side"] == "BUY" else "BUY"
        price = await client.get_current_price(symbol)
        if not price:
            price = trade["entry_price"]

        order = await client.place_order(symbol, close_side, trade["quantity"])
        if not order:
            logger.error(f"close_trade({symbol}): order placement failed, position tracking retained")
            return

        if trade["side"] == "BUY":
            pnl = (price - trade["entry_price"]) * trade["quantity"]
        else:
            pnl = (trade["entry_price"] - price) * trade["quantity"]

        total_pnl_today += pnl
        pnl_pct = (pnl / (trade["entry_price"] * trade["quantity"])) * 100
        emoji = "🟢" if pnl >= 0 else "🔴"

        msg = (
            f"{emoji} *{trade['side']} Fermé* | `{symbol}` | _{reason}_\n"
            f"📥 Entrée: `${trade['entry_price']:.4f}`\n"
            f"📤 Sortie: `${price:.4f}`\n"
            f"💹 PnL: `{'+' if pnl >= 0 else ''}{pnl:.2f} USDT` ({pnl_pct:+.2f}%)\n"
            f"📈 PnL jour total: `{'+' if total_pnl_today >= 0 else ''}{total_pnl_today:.2f} USDT`"
        )
        await app.bot.send_message(chat_id=notify_chat_id, text=msg, parse_mode="Markdown")
        logger.info(f"CLOSED {symbol} reason={reason} pnl={pnl:.2f}")

        del active_trades[symbol]
    except Exception as e:
        logger.error(f"close_trade({symbol}): {e}")


# ─────────────────────────────────────────────────────────────────────────────
# TP / SL monitor  (called inside scan loop for open positions)
# ─────────────────────────────────────────────────────────────────────────────
async def check_open_positions(client: BinanceClient, notify_chat_id: int, app):
    """Check TP/SL for all open positions and close if triggered."""
    for symbol in list(active_trades.keys()):
        trade = active_trades.get(symbol)
        if not trade:
            continue
        price = await client.get_current_price(symbol)
        if not price:
            continue
        if trade["side"] == "BUY":
            if price >= trade["tp_price"]:
                await close_trade(client, symbol, "TP atteint ✅", notify_chat_id, app)
            elif price <= trade["sl_price"]:
                await close_trade(client, symbol, "SL déclenché ⛔", notify_chat_id, app)
        else:
            if price <= trade["tp_price"]:
                await close_trade(client, symbol, "TP atteint ✅", notify_chat_id, app)
            elif price >= trade["sl_price"]:
                await close_trade(client, symbol, "SL déclenché ⛔", notify_chat_id, app)


# ─────────────────────────────────────────────────────────────────────────────
# Continuous Market Scan Loop
# ─────────────────────────────────────────────────────────────────────────────
async def market_scan_loop(client: BinanceClient, notify_chat_id: int, app):
    """
    24/7 continuous scan:
      1. Check TP/SL for existing positions.
      2. Scan all pairs on all timeframes.
      3. Open new positions when ALL indicators align.
      4. No limit on concurrent trades or daily trades.
    """
    global is_running

    logger.info("⚡ Scan loop started")
    while is_running:
        try:
            # 1. Monitor open positions
            await check_open_positions(client, notify_chat_id, app)

            # 2. Scan for new signals
            for name, symbol in TRADING_PAIRS.items():
                # Already have a position in this symbol — skip (avoid doubling)
                if symbol in active_trades:
                    continue

                tf_results = await asyncio.gather(
                    *[analyze_symbol(client, symbol, tf) for tf in SCALPING_TIMEFRAMES]
                )
                signal = multi_tf_signal(list(tf_results))

                if signal in ("BUY", "SELL"):
                    logger.info(f"🎯 Signal {signal} confirmé sur {symbol} (tous TF)")
                    await open_trade(client, symbol, signal, notify_chat_id, app)

        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"scan loop error: {e}")

        await asyncio.sleep(SCAN_INTERVAL_SECONDS)

    logger.info("⏹️ Scan loop stopped")


# ─────────────────────────────────────────────────────────────────────────────
# Telegram Command Handlers
# ─────────────────────────────────────────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "🤖 *Scalping Bot HF – Binance*\n\n"
        "Commandes:\n"
        "/trade — Démarrer le scan continu\n"
        "/stop  — Arrêter le scan\n"
        "/status — Solde & positions\n"
        "/positions — Détail des positions ouvertes\n\n"
        f"⚙️ Config:\n"
        f"• TP: `{TAKE_PROFIT_PERCENT}%` | SL: `{STOP_LOSS_PERCENT}%`\n"
        f"• Risque/trade: `{RISK_PERCENT}%` du capital\n"
        f"• Timeframes: `{', '.join(SCALPING_TIMEFRAMES)}`\n"
        f"• Scan: toutes les `{SCAN_INTERVAL_SECONDS}s`\n"
        f"• Trades simultanés: illimités\n\n"
        "⚠️ *Trading réel – argent véritable!*"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")


async def cmd_trade(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global is_running, scan_task, total_trades_today, total_pnl_today

    if is_running:
        await update.message.reply_text("⚡ Le scan est déjà actif!")
        return

    client = BinanceClient(BINANCE_API_KEY, BINANCE_SECRET_KEY)
    balance = await client.get_account_balance()
    if balance is None:
        await update.message.reply_text("❌ Impossible de vérifier le solde Binance. Vérifiez vos clés API.")
        return

    total_trades_today = 0
    total_pnl_today = 0.0
    is_running = True

    msg = (
        f"🚀 *Scalping Bot démarré!*\n\n"
        f"💰 Capital: `${balance:.2f}` USDT\n"
        f"📊 Risque/trade: `${balance * RISK_PERCENT / 100:.2f}` ({RISK_PERCENT}%)\n"
        f"🎯 TP: `{TAKE_PROFIT_PERCENT}%` | SL: `{STOP_LOSS_PERCENT}%`\n"
        f"⏱️ Scan toutes les `{SCAN_INTERVAL_SECONDS}s`\n"
        f"🔍 Paires: `{', '.join(TRADING_PAIRS.keys())}`\n\n"
        "Le bot envoie une notification à chaque trade."
    )
    await update.message.reply_text(msg, parse_mode="Markdown")

    chat_id = update.effective_chat.id
    scan_task = asyncio.create_task(
        market_scan_loop(client, chat_id, context.application)
    )


async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global is_running, scan_task

    if not is_running:
        await update.message.reply_text("⏹️ Le bot n'est pas en cours d'exécution.")
        return

    is_running = False
    if scan_task:
        scan_task.cancel()
        scan_task = None

    msg = (
        f"⛔ *Bot arrêté*\n\n"
        f"📊 Trades aujourd'hui: `{total_trades_today}`\n"
        f"💹 PnL jour: `{'+' if total_pnl_today >= 0 else ''}{total_pnl_today:.2f} USDT`\n"
    )
    if active_trades:
        msg += f"\n⚠️ {len(active_trades)} position(s) encore ouverte(s) – à gérer manuellement."
    await update.message.reply_text(msg, parse_mode="Markdown")


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    client = BinanceClient(BINANCE_API_KEY, BINANCE_SECRET_KEY)
    balance = await client.get_account_balance()

    status_icon = "🟢 ACTIF" if is_running else "🔴 INACTIF"
    balance_line = f"💰 Solde USDT: `${balance:.2f}`\n" if balance else "💰 Solde USDT: _indisponible_\n"
    msg = (
        f"📊 *Statut du Bot*\n\n"
        f"🤖 État: {status_icon}\n"
        + balance_line +
        f"📋 Positions ouvertes: `{len(active_trades)}`\n"
        f"📈 Trades aujourd'hui: `{total_trades_today}`\n"
        f"💹 PnL jour: `{'+' if total_pnl_today >= 0 else ''}{total_pnl_today:.2f} USDT`\n"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")


async def cmd_positions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not active_trades:
        await update.message.reply_text("✅ Aucune position ouverte.")
        return

    client = BinanceClient(BINANCE_API_KEY, BINANCE_SECRET_KEY)
    msg = f"📋 *Positions ouvertes ({len(active_trades)})*\n\n"

    for symbol, t in active_trades.items():
        price = await client.get_current_price(symbol)
        if price and t["side"] == "BUY":
            unrealized = (price - t["entry_price"]) * t["quantity"]
        elif price:
            unrealized = (t["entry_price"] - price) * t["quantity"]
        else:
            unrealized = 0.0
        pct = (unrealized / (t["entry_price"] * t["quantity"])) * 100 if t["entry_price"] else 0
        emoji = "🟢" if unrealized >= 0 else "🔴"
        duration = datetime.now() - t["opened_at"]
        mins = int(duration.total_seconds() / 60)

        price_line = f"  Entrée: `${t['entry_price']:.4f}` | Actuel: `${price:.4f}`\n" if price else f"  Entrée: `${t['entry_price']:.4f}`\n"
        msg += (
            f"{emoji} *{symbol}* ({t['side']})\n"
            + price_line +
            f"  PnL latent: `{'+' if unrealized >= 0 else ''}{unrealized:.2f} USDT` ({pct:+.2f}%)\n"
            f"  TP: `${t['tp_price']:.4f}` | SL: `${t['sl_price']:.4f}`\n"
            f"  Ouvert depuis: `{mins}min`\n\n"
        )

    await update.message.reply_text(msg, parse_mode="Markdown")


# ─────────────────────────────────────────────────────────────────────────────
# Entry Point
# ─────────────────────────────────────────────────────────────────────────────
def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("trade", cmd_trade))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("positions", cmd_positions))

    logger.info("✅ Scalping HF Bot actif – en attente de commandes Telegram...")
    app.run_polling()


if __name__ == "__main__":
    main()

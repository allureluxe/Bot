import os
import logging
import asyncio
import aiohttp
import math
from datetime import datetime
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
import hmac
import hashlib
import json
from typing import Optional, Dict, List, Tuple, Union

# Load environment variables
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
BINANCE_API_KEY = os.getenv("BINANCE_API_KEY")
BINANCE_SECRET_KEY = os.getenv("BINANCE_SECRET_KEY")

BINANCE_BASE_URL = "https://api.binance.com"

# Quote asset
QUOTE_ASSET = "USDC"

STOP_LOSS_PERCENT = 5   # 5% stop loss
TAKE_PROFIT_PERCENT = 5  # 5% take profit

# --- Money management ---
RISK_PER_TRADE = 0.01       # 1 % du solde par position
MIN_TRADE_AMOUNT = 5.0      # taille minimale en USDC
MAX_TRADE_AMOUNT = 25.0     # plafond par position en USDC

# --- Scalping scanner ---
SCALPING_TIMEFRAMES = ["1m", "5m", "15m"]
MIN_QUOTE_VOLUME = float(os.getenv("MIN_QUOTE_VOLUME", "1000000"))  # volume 24 h minimum en USDC
MAX_SPREAD_PCT = float(os.getenv("MAX_SPREAD_PCT", "0.35"))         # spread bid/ask max toléré (%)
MAX_OPEN_POSITIONS = 5        # nombre max de positions simultanées
MIN_SIGNAL_CONFIRMATIONS = int(os.getenv("MIN_SIGNAL_CONFIRMATIONS", "2"))
NOTIONAL_TOLERANCE = 0.9999   # tolérance pour éviter les faux rejets de flottants

if MIN_QUOTE_VOLUME < 100_000:
    MIN_QUOTE_VOLUME = 100_000
if MAX_SPREAD_PCT <= 0 or MAX_SPREAD_PCT > 1.0:
    MAX_SPREAD_PCT = 0.35
if MIN_SIGNAL_CONFIRMATIONS < 1 or MIN_SIGNAL_CONFIRMATIONS > len(SCALPING_TIMEFRAMES):
    MIN_SIGNAL_CONFIRMATIONS = 2

# Setup logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Active trades tracking
active_trades: Dict[str, Dict] = {}


class BinanceClient:
    """Binance API Client for trading"""
    
    def __init__(self, api_key: str, secret_key: str):
        self.api_key = api_key
        self.secret_key = secret_key
        self.base_url = BINANCE_BASE_URL
    
    def _generate_signature(self, data: str) -> str:
        """Generate HMAC SHA256 signature"""
        return hmac.new(
            self.secret_key.encode(),
            data.encode(),
            hashlib.sha256
        ).hexdigest()
    
    async def get_account_balance(self) -> Optional[float]:
        """Get USDC balance"""
        try:
            timestamp = int(datetime.now().timestamp() * 1000)
            params = f"timestamp={timestamp}"
            signature = self._generate_signature(params)
            
            headers = {"X-MBX-APIKEY": self.api_key}
            url = f"{self.base_url}/api/v3/account?{params}&signature={signature}"
            
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as response:
                    if response.status == 200:
                        data = await response.json()
                        for balance in data.get("balances", []):
                            if balance["asset"] == QUOTE_ASSET:
                                return float(balance["free"])
            return None
        except Exception as e:
            logger.error(f"Error getting account balance: {e}")
            return None
    
    async def get_current_price(self, symbol: str) -> Optional[float]:
        """Get current symbol price"""
        try:
            url = f"{self.base_url}/api/v3/ticker/price?symbol={symbol}"
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as response:
                    if response.status == 200:
                        data = await response.json()
                        return float(data["price"])
            return None
        except Exception as e:
            logger.error(f"Error getting price for {symbol}: {e}")
            return None
    
    async def get_klines(self, symbol: str, interval: str, limit: int = 100) -> Optional[List[float]]:
        """Get candlestick data from Binance"""
        try:
            url = f"{self.base_url}/api/v3/klines?symbol={symbol}&interval={interval}&limit={limit}"
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as response:
                    if response.status == 200:
                        data = await response.json()
                        closes = [float(candle[4]) for candle in data]  # Close price is index 4
                        return closes
            return None
        except Exception as e:
            logger.error(f"Error getting klines for {symbol}: {e}")
            return None
    
    async def place_order(self, symbol: str, side: str, quantity: Union[float, str]) -> Optional[Dict]:
        """Place market order on Binance"""
        try:
            timestamp = int(datetime.now().timestamp() * 1000)
            params = f"symbol={symbol}&side={side}&type=MARKET&quantity={quantity}&timestamp={timestamp}"
            signature = self._generate_signature(params)
            
            headers = {"X-MBX-APIKEY": self.api_key}
            url = f"{self.base_url}/api/v3/order?{params}&signature={signature}"
            
            async with aiohttp.ClientSession() as session:
                async with session.post(url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as response:
                    if response.status == 200:
                        return await response.json()
                    else:
                        error = await response.text()
                        logger.error(f"Binance order error: {error}")
            return None
        except Exception as e:
            logger.error(f"Error placing order for {symbol}: {e}")
            return None

    async def get_exchange_info(self) -> Optional[Dict]:
        """Get Binance exchange metadata"""
        try:
            url = f"{self.base_url}/api/v3/exchangeInfo"
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as response:
                    if response.status == 200:
                        return await response.json()
            return None
        except Exception as e:
            logger.error(f"Error getting exchange info: {e}")
            return None

    async def get_24h_tickers(self) -> Optional[List[Dict]]:
        """Get 24h stats for all symbols"""
        try:
            url = f"{self.base_url}/api/v3/ticker/24hr"
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as response:
                    if response.status == 200:
                        return await response.json()
            return None
        except Exception as e:
            logger.error(f"Error getting 24h tickers: {e}")
            return None

    async def get_book_tickers(self) -> Optional[List[Dict]]:
        """Get best bid/ask for all symbols"""
        try:
            url = f"{self.base_url}/api/v3/ticker/bookTicker"
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as response:
                    if response.status == 200:
                        return await response.json()
            return None
        except Exception as e:
            logger.error(f"Error getting book tickers: {e}")
            return None


def ema(prices: List[float], period: int) -> Optional[float]:
    """Calculate Exponential Moving Average"""
    if len(prices) < period:
        return None
    
    multiplier = 2 / (period + 1)
    ema_value = sum(prices[:period]) / period
    
    for price in prices[period:]:
        ema_value = (price - ema_value) * multiplier + ema_value
    
    return ema_value


def rsi(prices: List[float], period: int = 14) -> Optional[float]:
    """Calculate Relative Strength Index"""
    if len(prices) < period + 1:
        return None
    
    gains = []
    losses = []
    
    for i in range(1, period + 1):
        change = prices[i] - prices[i - 1]
        if change > 0:
            gains.append(change)
            losses.append(0)
        else:
            gains.append(0)
            losses.append(abs(change))
    
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    
    if avg_loss == 0:
        return 100
    
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def calculate_trade_amount(balance: float) -> float:
    """Compute position size: RISK_PER_TRADE % of balance, clamped to [MIN, MAX]."""
    amount = balance * RISK_PER_TRADE
    amount = max(MIN_TRADE_AMOUNT, amount)
    amount = min(MAX_TRADE_AMOUNT, amount)
    return round(amount, 2)


def is_symbol_tradeable(volume: float, spread_pct: float) -> bool:
    """Return True if the symbol passes liquidity and spread filters."""
    return volume >= MIN_QUOTE_VOLUME and spread_pct <= MAX_SPREAD_PCT


def clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(value, maximum))


def floor_to_step(value: float, step: float) -> float:
    if step <= 0:
        return value
    return math.floor(value / step) * step


def format_quantity(quantity: float, step_size: float) -> str:
    if step_size <= 0:
        return f"{quantity:.8f}"
    decimals = max(0, int(round(-math.log10(step_size)))) if step_size < 1 else 0
    return f"{quantity:.{decimals}f}"


def parse_symbol_filters(exchange_info: Dict, symbol: str) -> Optional[Dict[str, float]]:
    for symbol_info in exchange_info.get("symbols", []):
        if symbol_info.get("symbol") != symbol:
            continue
        lot_size = next((f for f in symbol_info.get("filters", []) if f.get("filterType") == "LOT_SIZE"), None)
        notional = next((f for f in symbol_info.get("filters", []) if f.get("filterType") in {"NOTIONAL", "MIN_NOTIONAL"}), None)
        if not lot_size:
            return None
        return {
            "min_qty": float(lot_size.get("minQty", "0")),
            "step_size": float(lot_size.get("stepSize", "0")),
            "min_notional": float((notional or {}).get("minNotional", "0")),
        }
    return None


def compute_order_quantity(balance: float, price: float, filters: Dict[str, float]) -> Tuple[Optional[float], str]:
    if filters["step_size"] <= 0:
        return None, "stepSize Binance invalide"

    max_available_notional = balance * 0.95
    desired_notional = clamp(balance * RISK_PER_TRADE, MIN_TRADE_AMOUNT, MAX_TRADE_AMOUNT)
    notional = min(desired_notional, max_available_notional)
    if notional <= 0:
        return None, "solde insuffisant"

    quantity = floor_to_step(notional / price, filters["step_size"])
    if quantity < filters["min_qty"]:
        quantity = floor_to_step(filters["min_qty"], filters["step_size"])

    final_notional = quantity * price
    min_notional = filters["min_notional"]
    if min_notional > 0 and final_notional < min_notional:
        needed_steps = math.ceil((min_notional / price) / filters["step_size"])
        needed_qty = needed_steps * filters["step_size"]
        quantity = needed_qty
        final_notional = quantity * price

    if quantity <= 0:
        return None, "quantité calculée invalide"
    if quantity < filters["min_qty"]:
        return None, f"quantité {quantity:.8f} < minQty {filters['min_qty']}"
    if min_notional > 0 and final_notional < (min_notional * NOTIONAL_TOLERANCE):
        return None, f"notional {final_notional:.4f} < minNotional {min_notional}"
    if final_notional > max_available_notional:
        return None, f"notional requis {final_notional:.4f} > disponible {max_available_notional:.4f}"
    return quantity, ""


async def analyze_symbol(binance_client: BinanceClient, symbol: str, tf: str) -> Optional[Dict]:
    """Analyze symbol with technical indicators"""
    prices = await binance_client.get_klines(symbol, tf, limit=100)
    
    if not prices or len(prices) < 50:
        logger.warning(f"Insufficient data for {symbol} on {tf}")
        return None
    
    try:
        current_price = prices[-1]
        ema20 = ema(prices, 20)
        ema50 = ema(prices, 50)
        current_rsi = rsi(prices, 14)
        
        if not all([ema20, ema50, current_rsi is not None]):
            return None
        
        if current_price > ema20 > ema50 and current_rsi > 55:
            signal = "BUY"
        elif current_price < ema20 < ema50 and current_rsi < 45:
            signal = "SELL"
        else:
            signal = "WAIT"
        
        return {
            "price": round(current_price, 2),
            "ema20": round(ema20, 2),
            "ema50": round(ema50, 2),
            "rsi": round(current_rsi, 2),
            "signal": signal
        }
    except Exception as e:
        logger.error(f"Error analyzing {symbol} on {tf}: {e}")
        return None


async def execute_trade(
    binance_client: BinanceClient,
    symbol: str,
    signal: str,
    update: Update,
    balance: float,
    exchange_info: Dict,
) -> Tuple[bool, float]:
    """Execute trade based on signal"""
    try:
        # Max open positions safety check
        if signal == "BUY" and len(active_trades) >= MAX_OPEN_POSITIONS:
            reason = f"max positions atteint ({MAX_OPEN_POSITIONS})"
            logger.info(f"Skip {symbol} BUY: {reason}")
            await update.message.reply_text(f"⚠️ {symbol}: BUY ignoré ({reason})")
            return False, 0.0

        # Check if already have open trade
        if signal == "BUY" and symbol in active_trades:
            logger.info(f"Already have open trade for {symbol}")
            await update.message.reply_text(f"⚠️ {symbol}: BUY ignoré (position déjà ouverte)")
            return False, 0.0

        # Get current price
        current_price = await binance_client.get_current_price(symbol)
        if not current_price:
            reason = "prix introuvable"
            logger.error(f"Could not get price for {symbol}")
            await update.message.reply_text(f"⚠️ {symbol}: trade ignoré ({reason})")
            return False, 0.0

        # Execute order
        if signal == "BUY":
            filters = parse_symbol_filters(exchange_info, symbol)
            if not filters:
                await update.message.reply_text(f"⚠️ {symbol}: BUY ignoré (filtres Binance introuvables)")
                return False, 0.0

            quantity, reason = compute_order_quantity(balance, current_price, filters)
            if quantity is None:
                logger.info(f"Skip {symbol} BUY: {reason}")
                await update.message.reply_text(f"⚠️ {symbol}: BUY ignoré ({reason})")
                return False, 0.0

            order_qty = format_quantity(quantity, filters["step_size"])
            order = await binance_client.place_order(symbol, "BUY", order_qty)
            if order:
                active_trades[symbol] = {
                    "side": "BUY",
                    "entry_price": current_price,
                    "quantity": quantity,
                    "timestamp": datetime.now(),
                    "order_id": order.get("orderId")
                }
                message = f"✅ *BUY Exécuté*\n{symbol}\nPrix: ${current_price}\nQuantité: {round(quantity, 8)}"
                await update.message.reply_text(message, parse_mode="Markdown")
                logger.info(f"BUY order placed for {symbol} at ${current_price}")
                return True, (quantity * current_price)
            await update.message.reply_text(f"⚠️ {symbol}: BUY rejeté par Binance")
            return False, 0.0

        elif signal == "SELL" and symbol in active_trades:
            quantity = active_trades[symbol]["quantity"]
            filters = parse_symbol_filters(exchange_info, symbol)
            if not filters:
                await update.message.reply_text(f"⚠️ {symbol}: SELL ignoré (filtres Binance introuvables)")
                return False, 0.0
            order_qty = format_quantity(quantity, filters["step_size"])
            order = await binance_client.place_order(symbol, "SELL", order_qty)
            if order:
                message = f"✅ *SELL Exécuté*\n{symbol}\nPrix: ${current_price}\nQuantité: {round(quantity, 8)}"
                await update.message.reply_text(message, parse_mode="Markdown")
                if symbol in active_trades:
                    del active_trades[symbol]
                logger.info(f"SELL order placed for {symbol} at ${current_price}")
                return True, 0.0
            await update.message.reply_text(f"⚠️ {symbol}: SELL rejeté par Binance")
            return False, 0.0
        return False, 0.0

    except Exception as e:
        logger.error(f"Error executing trade for {symbol}: {e}")
        return False, 0.0


async def auto_trade(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start scalping scanner across all liquid USDT pairs"""
    try:
        binance_client = BinanceClient(BINANCE_API_KEY, BINANCE_SECRET_KEY)

        # Get account balance
        balance = await binance_client.get_account_balance()
        if not balance:
            await update.message.reply_text("❌ Erreur: Impossible de vérifier le solde")
            return

        exchange_info = await binance_client.get_exchange_info()
        tickers_24h = await binance_client.get_24h_tickers()
        book_tickers = await binance_client.get_book_tickers()
        if not all([exchange_info, tickers_24h, book_tickers]):
            await update.message.reply_text("❌ Erreur: Données marché indisponibles pour le scan")
            return

        usdc_symbols = [
            s["symbol"]
            for s in exchange_info.get("symbols", [])
            if s.get("quoteAsset") == QUOTE_ASSET and s.get("status") == "TRADING" and s.get("isSpotTradingAllowed")
        ]
        volume_map = {item.get("symbol"): float(item.get("quoteVolume", "0") or 0) for item in tickers_24h}
        book_map = {
            item.get("symbol"): (
                float(item.get("bidPrice", "0") or 0),
                float(item.get("askPrice", "0") or 0),
            )
            for item in book_tickers
        }

        message = f"🤖 *Mode Trading Autonome ({QUOTE_ASSET})*\n\n💰 Solde {QUOTE_ASSET}: {balance:.2f} {QUOTE_ASSET}\n"
        message += f"📊 Taille dynamique: {RISK_PER_TRADE * 100:.2f}% (min {MIN_TRADE_AMOUNT} / max {MAX_TRADE_AMOUNT} {QUOTE_ASSET})\n"
        message += f"⛔ Stop Loss: {STOP_LOSS_PERCENT}%\n"
        message += f"🎯 Take Profit: {TAKE_PROFIT_PERCENT}%\n\n"
        message += "🚀 Analyse en cours...\n"
        await update.message.reply_text(message, parse_mode="Markdown")

        scanned = len(usdc_symbols)
        filtered_liquidity = 0
        filtered_no_book = 0
        filtered_spread = 0
        analyzed_symbols: List[str] = []
        decisions: List[str] = []
        buy_signals = 0
        sell_signals = 0
        wait_signals = 0
        trades_done = 0

        for symbol in usdc_symbols:
            quote_volume = volume_map.get(symbol, 0)
            if quote_volume < MIN_QUOTE_VOLUME:
                filtered_liquidity += 1
                continue

            bid_ask = book_map.get(symbol)
            if not bid_ask:
                filtered_no_book += 1
                continue
            bid, ask = bid_ask
            if bid <= 0 or ask <= 0:
                filtered_spread += 1
                continue
            mid = (bid + ask) / 2
            spread_pct = ((ask - bid) / mid) * 100
            if spread_pct > MAX_SPREAD_PCT:
                filtered_spread += 1
                continue

            buy_count = 0
            sell_count = 0
            for tf in SCALPING_TIMEFRAMES:
                result = await analyze_symbol(binance_client, symbol, tf)
                if result:
                    if result["signal"] == "BUY":
                        buy_count += 1
                    elif result["signal"] == "SELL":
                        sell_count += 1

            analyzed_symbols.append(symbol)
            decision = "WAIT"
            if buy_count >= MIN_SIGNAL_CONFIRMATIONS:
                decision = "BUY"
                buy_signals += 1
                executed, spent_notional = await execute_trade(
                    binance_client, symbol, "BUY", update, balance, exchange_info
                )
                if executed:
                    trades_done += 1
                    refreshed_balance = await binance_client.get_account_balance()
                    if refreshed_balance is not None:
                        balance = refreshed_balance
                    else:
                        balance = max(0.0, balance - spent_notional)
            elif sell_count >= MIN_SIGNAL_CONFIRMATIONS:
                decision = "SELL"
                sell_signals += 1
                executed, _ = await execute_trade(
                    binance_client, symbol, "SELL", update, balance, exchange_info
                )
                if executed:
                    trades_done += 1
            else:
                wait_signals += 1
            decisions.append(f"{symbol}: {decision} (BUY={buy_count} SELL={sell_count})")

        total_decisions = len(decisions)
        if total_decisions > 30:
            decisions = decisions[:30] + [f"... {total_decisions - 30} décisions supplémentaires"]

        scan_summary = (
            f"📡 *Scan {QUOTE_ASSET} terminé*\n"
            f"• Scannés: {scanned}\n"
            f"• Filtrés liquidité: {filtered_liquidity} (< {MIN_QUOTE_VOLUME:,.0f} {QUOTE_ASSET})\n"
            f"• Filtrés sans bid/ask: {filtered_no_book}\n"
            f"• Filtrés spread: {filtered_spread} (> {MAX_SPREAD_PCT:.2f}%)\n"
            f"• Analysés: {len(analyzed_symbols)}\n"
            f"• Signaux BUY/SELL/WAIT: {buy_signals}/{sell_signals}/{wait_signals}\n"
            f"• Trades exécutés: {trades_done}"
        )
        await update.message.reply_text(scan_summary, parse_mode="Markdown")

        if decisions:
            await update.message.reply_text("🧭 *Décisions*\n" + "\n".join(decisions), parse_mode="Markdown")

        if active_trades:
            positions_msg = f"📋 *Positions Ouvertes ({len(active_trades)}):*\n"
            for sym, trade in active_trades.items():
                positions_msg += f"\n• {sym}: {trade['side']} @ ${trade['entry_price']:.4f}"
            await update.message.reply_text(positions_msg, parse_mode="Markdown")

    except Exception as e:
        logger.error(f"Error in auto_trade: {e}")
        await update.message.reply_text(f"❌ Erreur: {str(e)}")


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show trading status"""
    try:
        binance_client = BinanceClient(BINANCE_API_KEY, BINANCE_SECRET_KEY)
        balance = await binance_client.get_account_balance()
        
        message = "📊 *Statut du Bot*\n\n"
        if balance:
            message += f"💰 Solde {QUOTE_ASSET}: {balance:.2f} {QUOTE_ASSET}\n"
        
        if active_trades:
            message += f"\n📋 Positions Ouvertes: {len(active_trades)}\n"
            for symbol, trade in active_trades.items():
                message += f"\n{symbol}:\n"
                message += f"  • Side: {trade['side']}\n"
                message += f"  • Entry: ${trade['entry_price']:.2f}\n"
                message += f"  • Qty: {round(trade['quantity'], 8)}"
        else:
            message += "\n✅ Aucune position ouverte"
        
        await update.message.reply_text(message, parse_mode="Markdown")
    
    except Exception as e:
        logger.error(f"Error in status: {e}")
        await update.message.reply_text(f"❌ Erreur: {str(e)}")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start command"""
    message = (
        "🤖 *Bot Scalping Binance*\n\n"
        "Commandes disponibles:\n"
        "/trade - Démarrer le scanner scalping\n"
        "/status - Voir le statut des positions\n"
        "/stop - Arrêter le trading\n\n"
        f"⚙️ Configuration:\n"
        f"• Risque par trade: {RISK_PER_TRADE * 100:.0f}% du solde\n"
        f"• Taille min/max: {MIN_TRADE_AMOUNT}/{MAX_TRADE_AMOUNT} {QUOTE_ASSET}\n"
        f"• Stop Loss: {STOP_LOSS_PERCENT}%\n"
        f"• Take Profit: {TAKE_PROFIT_PERCENT}%\n"
        f"• Timeframes: {', '.join(SCALPING_TIMEFRAMES)}\n"
        f"• Positions max: {MAX_OPEN_POSITIONS}\n\n"
        "⚠️ ATTENTION: Trading réel avec argent véritable!"
    )
    await update.message.reply_text(message, parse_mode="Markdown")


async def stop_trading(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Stop trading"""
    message = "⛔ Trading automatique arrêté\n\n"
    if active_trades:
        message += f"Positions ouvertes: {len(active_trades)}\n"
        for symbol in active_trades.keys():
            message += f"• {symbol}\n"
        message += "\n⚠️ Les positions restent ouvertes!"
    else:
        message += "✅ Aucune position ouverte"
    
    await update.message.reply_text(message, parse_mode="Markdown")


def main():
    """Start the bot"""
    missing = [
        name for name, val in [
            ("BOT_TOKEN", BOT_TOKEN),
            ("BINANCE_API_KEY", BINANCE_API_KEY),
            ("BINANCE_SECRET_KEY", BINANCE_SECRET_KEY),
        ]
        if not val
    ]
    if missing:
        logger.critical(f"❌ Variables d'environnement manquantes : {', '.join(missing)}")
        raise SystemExit(1)

    app = ApplicationBuilder().token(BOT_TOKEN).build()
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("trade", auto_trade))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("stop", stop_trading))
    
    logger.info("✅ Bot trading autonome Binance actif...")
    app.run_polling()


if __name__ == "__main__":
    main()

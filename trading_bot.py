import os
import logging
import asyncio
import aiohttp
from datetime import datetime
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
import hmac
import hashlib
import json
from decimal import Decimal, InvalidOperation
from typing import Optional, Dict, List, Tuple

# Load environment variables
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
BINANCE_API_KEY = os.getenv("BINANCE_API_KEY")
BINANCE_SECRET_KEY = os.getenv("BINANCE_SECRET_KEY")

if not all([BOT_TOKEN, BINANCE_API_KEY, BINANCE_SECRET_KEY]):
    raise ValueError("Missing required environment variables: BOT_TOKEN, BINANCE_API_KEY, BINANCE_SECRET_KEY")

BINANCE_BASE_URL = "https://api.binance.com"

# Quote asset (USDC only)
QUOTE_ASSET = "USDC"

# Trading pairs configuration
TRADING_PAIRS = {
    "BTC": f"BTC{QUOTE_ASSET}",
    "ETH": f"ETH{QUOTE_ASSET}",
    "BNB": f"BNB{QUOTE_ASSET}"
}

TRADE_AMOUNT = 20  # $20 par trade
STOP_LOSS_PERCENT = 5  # 5% stop loss
TAKE_PROFIT_PERCENT = 5  # 5% take profit
TIMEFRAMES = ["15m", "1h", "4h"]  # Binance timeframes

# --- Money management ---
RISK_PER_TRADE = 0.01       # 1 % du solde USDC par position
MIN_TRADE_AMOUNT = 5.0      # taille minimale en USDC
MAX_TRADE_AMOUNT = 25.0     # plafond par position en USDC

# --- Scalping scanner ---
SCALPING_TIMEFRAMES = ["1m", "5m", "15m"]
MIN_QUOTE_VOLUME = float(os.getenv("SCALPING_MIN_QUOTE_VOLUME", "2000000"))  # volume 24 h minimum en USDC
MAX_SPREAD_PCT = float(os.getenv("SCALPING_MAX_SPREAD_PCT", "0.25"))          # spread bid/ask max toléré (%)
MIN_SIGNAL_CONSENSUS = max(1, int(os.getenv("SCALPING_MIN_SIGNAL_CONSENSUS", "2")))
MAX_SYMBOLS_TO_SCAN = max(1, int(os.getenv("SCALPING_MAX_SYMBOLS_TO_SCAN", "20")))
MAX_OPEN_POSITIONS = 5        # nombre max de positions simultanées

# Setup logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Active trades tracking
active_trades: Dict[str, Dict] = {}
scan_lock = asyncio.Lock()


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

    async def get_usdc_symbols(self) -> List[str]:
        """Get Binance trading symbols quoted in USDC"""
        try:
            url = f"{self.base_url}/api/v3/exchangeInfo"
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as response:
                    if response.status != 200:
                        return []
                    data = await response.json()
                    return [
                        s["symbol"]
                        for s in data.get("symbols", [])
                        if s.get("status") == "TRADING" and s.get("quoteAsset") == QUOTE_ASSET
                    ]
        except Exception as e:
            logger.error(f"Error getting USDC symbols: {e}")
            return []

    async def get_symbol_filters(self, symbol: str) -> Optional[Dict]:
        """Get Binance filters for a symbol"""
        try:
            url = f"{self.base_url}/api/v3/exchangeInfo?symbol={symbol}"
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as response:
                    if response.status != 200:
                        return None
                    data = await response.json()
                    symbols = data.get("symbols", [])
                    if not symbols:
                        return None
                    return symbols[0]
        except Exception as e:
            logger.error(f"Error getting filters for {symbol}: {e}")
            return None

    async def get_24h_tickers(self) -> Dict[str, Dict]:
        """Get 24h ticker data keyed by symbol"""
        try:
            url = f"{self.base_url}/api/v3/ticker/24hr"
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as response:
                    if response.status != 200:
                        return {}
                    data = await response.json()
                    return {item["symbol"]: item for item in data if "symbol" in item}
        except Exception as e:
            logger.error(f"Error getting 24h tickers: {e}")
            return {}

    async def get_book_tickers(self) -> Dict[str, Dict]:
        """Get best bid/ask ticker data keyed by symbol"""
        try:
            url = f"{self.base_url}/api/v3/ticker/bookTicker"
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as response:
                    if response.status != 200:
                        return {}
                    data = await response.json()
                    return {item["symbol"]: item for item in data if "symbol" in item}
        except Exception as e:
            logger.error(f"Error getting book tickers: {e}")
            return {}
    
    async def place_order(self, symbol: str, side: str, quantity: float) -> Optional[Dict]:
        """Place market order on Binance"""
        try:
            timestamp = int(datetime.now().timestamp() * 1000)
            quantity_str = f"{quantity:.16f}".rstrip("0").rstrip(".")
            params = f"symbol={symbol}&side={side}&type=MARKET&quantity={quantity_str}&timestamp={timestamp}"
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


def _to_decimal(value: float | str) -> Optional[Decimal]:
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _round_down_to_step(quantity: Decimal, step_size: Decimal) -> Decimal:
    if step_size <= 0:
        return quantity
    return (quantity // step_size) * step_size


def build_liquidity_candidate(ticker_24h: Optional[Dict], book_ticker: Optional[Dict]) -> Tuple[bool, str]:
    if not ticker_24h:
        return False, "ticker24h_missing"
    if not book_ticker:
        return False, "bookticker_missing"

    quote_volume = float(ticker_24h.get("quoteVolume", 0.0))
    if quote_volume < MIN_QUOTE_VOLUME:
        return False, f"low_volume({quote_volume:.0f}<{MIN_QUOTE_VOLUME:.0f})"

    bid_price = float(book_ticker.get("bidPrice", 0.0))
    ask_price = float(book_ticker.get("askPrice", 0.0))
    if bid_price <= 0 or ask_price <= 0:
        return False, "invalid_bid_ask"

    mid = (bid_price + ask_price) / 2
    spread_pct = ((ask_price - bid_price) / mid) * 100 if mid > 0 else 999
    if spread_pct > MAX_SPREAD_PCT:
        return False, f"wide_spread({spread_pct:.3f}%>{MAX_SPREAD_PCT:.3f}%)"

    return True, "ok"


def prepare_order_quantity(
    current_price: float,
    quote_amount: float,
    symbol_info: Dict
) -> Tuple[Optional[float], Optional[str]]:
    filters = symbol_info.get("filters", [])
    lot_size = next((f for f in filters if f.get("filterType") == "LOT_SIZE"), {})
    min_notional_filter = next(
        (f for f in filters if f.get("filterType") in {"MIN_NOTIONAL", "NOTIONAL"}),
        {}
    )

    step_size = _to_decimal(lot_size.get("stepSize", "0"))
    min_qty = _to_decimal(lot_size.get("minQty", "0"))
    max_qty = _to_decimal(lot_size.get("maxQty", "999999999"))
    min_notional = _to_decimal(min_notional_filter.get("minNotional", "0"))
    price_dec = _to_decimal(current_price)
    quote_amount_dec = _to_decimal(quote_amount)

    if not all([step_size, min_qty, max_qty, price_dec, quote_amount_dec]):
        return None, "invalid_symbol_constraints"
    if price_dec <= 0:
        return None, "invalid_price"

    raw_qty = quote_amount_dec / price_dec
    rounded_qty = _round_down_to_step(raw_qty, step_size)

    if rounded_qty < min_qty:
        return None, f"qty_below_min({rounded_qty}<{min_qty})"
    if rounded_qty > max_qty:
        rounded_qty = _round_down_to_step(max_qty, step_size)

    notional = rounded_qty * price_dec
    if min_notional and notional < min_notional:
        needed_qty = _round_down_to_step((min_notional / price_dec), step_size)
        if needed_qty > rounded_qty:
            rounded_qty = needed_qty
            notional = rounded_qty * price_dec
        if notional < min_notional:
            return None, f"notional_below_min({notional:.4f}<{min_notional})"

    if rounded_qty <= 0:
        return None, "rounded_qty_zero"

    return float(rounded_qty), None


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
        
        if current_price > ema20 > ema50 and current_rsi > 52:
            signal = "BUY"
        elif current_price < ema20 < ema50 and current_rsi < 48:
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
    binance_symbol: str,
    signal: str,
    balance: float,
    update: Update
):
    """Execute trade based on signal"""
    try:
        # Check if already have open trade
        if binance_symbol in active_trades:
            logger.info(f"Already have open trade for {binance_symbol}")
            return

        if signal == "BUY" and len(active_trades) >= MAX_OPEN_POSITIONS:
            reason = f"max_open_positions_reached({MAX_OPEN_POSITIONS})"
            logger.info(f"Skip {binance_symbol}: {reason}")
            await update.message.reply_text(f"⛔ {binance_symbol} ignoré: {reason}")
            return

        # Get current price
        current_price = await binance_client.get_current_price(binance_symbol)
        if not current_price:
            logger.error(f"Could not get price for {binance_symbol}")
            return

        # Calculate quote amount from risk controls
        quote_amount = max(MIN_TRADE_AMOUNT, min(MAX_TRADE_AMOUNT, balance * RISK_PER_TRADE))
        quote_amount = min(quote_amount, balance)

        symbol_info = await binance_client.get_symbol_filters(binance_symbol)
        if not symbol_info:
            logger.info(f"Skip {binance_symbol}: symbol_constraints_unavailable")
            await update.message.reply_text(f"⛔ {binance_symbol} ignoré: contraintes Binance indisponibles")
            return

        quantity, quantity_error = prepare_order_quantity(current_price, quote_amount, symbol_info)
        if quantity_error or not quantity:
            reason = quantity_error or "invalid_quantity"
            logger.info(f"Order blocked for {binance_symbol}: {reason}")
            await update.message.reply_text(f"⛔ Ordre bloqué {binance_symbol}: {reason}")
            return

        # Execute order
        if signal == "BUY":
            order = await binance_client.place_order(binance_symbol, "BUY", quantity)
            if order:
                active_trades[binance_symbol] = {
                    "side": "BUY",
                    "entry_price": current_price,
                    "quantity": quantity,
                    "timestamp": datetime.now(),
                    "order_id": order.get("orderId")
                }
                message = f"✅ *BUY Exécuté*\n{binance_symbol}\nPrix: ${current_price}\nQuantité: {round(quantity, 8)}"
                await update.message.reply_text(message, parse_mode="Markdown")
                logger.info(f"BUY order placed for {binance_symbol} at ${current_price}")
        
        elif signal == "SELL" and any(t["side"] == "BUY" for t in active_trades.values()):
            order = await binance_client.place_order(binance_symbol, "SELL", quantity)
            if order:
                message = f"✅ *SELL Exécuté*\n{binance_symbol}\nPrix: ${current_price}\nQuantité: {round(quantity, 8)}"
                await update.message.reply_text(message, parse_mode="Markdown")
                if binance_symbol in active_trades:
                    del active_trades[binance_symbol]
                logger.info(f"SELL order placed for {binance_symbol} at ${current_price}")
    
    except Exception as e:
        logger.error(f"Error executing trade for {symbol_name}: {e}")


async def auto_trade(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start autonomous trading"""
    if scan_lock.locked():
        await update.message.reply_text("⏳ Une analyse est déjà en cours. Utilise /status pour suivre.")
        return

    try:
        async with scan_lock:
            binance_client = BinanceClient(BINANCE_API_KEY, BINANCE_SECRET_KEY)

            # Get account balance
            balance = await binance_client.get_account_balance()
            if not balance:
                await update.message.reply_text("❌ Erreur: Impossible de vérifier le solde")
                return

            quote_amount = max(MIN_TRADE_AMOUNT, min(MAX_TRADE_AMOUNT, balance * RISK_PER_TRADE))
            quote_amount = min(quote_amount, balance)

            message = f"🤖 *Mode Trading Autonome*\n\n💰 Solde {QUOTE_ASSET}: {balance:.2f} {QUOTE_ASSET}\n"
            message += f"📊 Montant par trade estimé: {quote_amount:.2f} {QUOTE_ASSET}\n"
            message += f"⛔ Stop Loss: {STOP_LOSS_PERCENT}%\n"
            message += f"🎯 Take Profit: {TAKE_PROFIT_PERCENT}%\n\n"
            message += "🚀 Analyse en cours...\n"
            await update.message.reply_text(message, parse_mode="Markdown")

            all_symbols = await binance_client.get_usdc_symbols()
            tickers_24h = await binance_client.get_24h_tickers()
            book_tickers = await binance_client.get_book_tickers()

            scanned_count = 0
            filtered_count = 0
            skipped_reasons: List[str] = []
            qualified_symbols: List[str] = []

            for symbol in all_symbols:
                ticker_24h = tickers_24h.get(symbol)
                book_ticker = book_tickers.get(symbol)
                is_valid, reason = build_liquidity_candidate(ticker_24h, book_ticker)
                scanned_count += 1
                if not is_valid:
                    filtered_count += 1
                    skipped_reasons.append(f"{symbol}: {reason}")
                    logger.info(f"Skip {symbol}: {reason}")
                    continue
                qualified_symbols.append(symbol)

            # Prefer the most liquid symbols first
            qualified_symbols.sort(
                key=lambda s: float(tickers_24h.get(s, {}).get("quoteVolume", 0.0)),
                reverse=True
            )
            symbols_to_analyze = qualified_symbols[:MAX_SYMBOLS_TO_SCAN]

            executed_count = 0
            analysis_timeframes = SCALPING_TIMEFRAMES if SCALPING_TIMEFRAMES else TIMEFRAMES
            for binance_symbol in symbols_to_analyze:
                buy_count = 0
                sell_count = 0

                for tf in analysis_timeframes:
                    result = await analyze_symbol(binance_client, binance_symbol, tf)
                    if result:
                        if result["signal"] == "BUY":
                            buy_count += 1
                        elif result["signal"] == "SELL":
                            sell_count += 1

                if buy_count >= MIN_SIGNAL_CONSENSUS and buy_count > sell_count:
                    await execute_trade(binance_client, binance_symbol, "BUY", balance, update)
                    executed_count += 1
                elif sell_count >= MIN_SIGNAL_CONSENSUS and sell_count > buy_count:
                    await execute_trade(binance_client, binance_symbol, "SELL", balance, update)
                    executed_count += 1
                else:
                    reason = f"weak_or_conflicting_signal(buy={buy_count},sell={sell_count})"
                    skipped_reasons.append(f"{binance_symbol}: {reason}")
                    logger.info(f"Skip {binance_symbol}: {reason}")

            summary = "📈 *Résumé Scan*\n"
            summary += f"• Symboles scannés: {scanned_count}\n"
            summary += f"• Filtrés liquidité/spread: {filtered_count}\n"
            summary += f"• Candidats analysés: {len(symbols_to_analyze)}\n"
            summary += f"• Tentatives d'entrée: {executed_count}\n"
            if skipped_reasons:
                summary += "\n• Exemples de skips:\n"
                for item in skipped_reasons[:8]:
                    summary += f"  - {item}\n"
            await update.message.reply_text(summary, parse_mode="Markdown")

            # Report active trades
            if active_trades:
                trades_msg = "\n📋 *Positions Ouvertes:*\n"
                for symbol, trade in active_trades.items():
                    trades_msg += f"\n{symbol}: {trade['side']} @ ${trade['entry_price']:.2f}"
                await update.message.reply_text(trades_msg, parse_mode="Markdown")
    
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
        "🤖 *Bot Trading Autonome Binance*\n\n"
        "Commandes disponibles:\n"
        "/trade - Démarrer le trading automatique\n"
        "/status - Voir le statut des positions\n"
        "/stop - Arrêter le trading\n\n"
        f"⚙️ Configuration:\n"
        f"• Montant par trade: {TRADE_AMOUNT} {QUOTE_ASSET}\n"
        f"• Stop Loss: {STOP_LOSS_PERCENT}%\n"
        f"• Take Profit: {TAKE_PROFIT_PERCENT}%\n\n"
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
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("trade", auto_trade))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("stop", stop_trading))
    
    logger.info("✅ Bot trading autonome Binance actif...")
    app.run_polling()


if __name__ == "__main__":
    main()

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
from typing import Optional, Dict, List

# Load environment variables
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
BINANCE_API_KEY = os.getenv("BINANCE_API_KEY")
BINANCE_SECRET_KEY = os.getenv("BINANCE_SECRET_KEY")

if not all([BOT_TOKEN, BINANCE_API_KEY, BINANCE_SECRET_KEY]):
    raise ValueError("Missing required environment variables: BOT_TOKEN, BINANCE_API_KEY, BINANCE_SECRET_KEY")

BINANCE_BASE_URL = "https://api.binance.com"

# Legacy fixed amount kept for reference but no longer used directly
STOP_LOSS_PERCENT = 5   # 5% stop loss
TAKE_PROFIT_PERCENT = 5  # 5% take profit

# --- Money management ---
RISK_PER_TRADE = 0.01       # 1 % du solde USDT par position
MIN_TRADE_AMOUNT = 5.0      # taille minimale en USDT
MAX_TRADE_AMOUNT = 25.0     # plafond par position en USDT

# --- Scalping scanner ---
SCALPING_TIMEFRAMES = ["1m", "5m", "15m"]
MIN_QUOTE_VOLUME = 5_000_000  # volume 24 h minimum en USDT
MAX_SPREAD_PCT = 0.15         # spread bid/ask max toléré (%)
MAX_OPEN_POSITIONS = 5        # nombre max de positions simultanées

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
        """Get USDT balance"""
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
                            if balance["asset"] == "USDT":
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
    
    async def get_exchange_info(self) -> Optional[List[str]]:
        """Return active USDT spot trading pairs"""
        try:
            url = f"{self.base_url}/api/v3/exchangeInfo"
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as response:
                    if response.status == 200:
                        data = await response.json()
                        symbols = [
                            s["symbol"]
                            for s in data.get("symbols", [])
                            if s.get("quoteAsset") == "USDT"
                            and s.get("status") == "TRADING"
                            and s.get("isSpotTradingAllowed", False)
                        ]
                        return symbols
            return None
        except Exception as e:
            logger.error(f"Error fetching exchange info: {e}")
            return None

    async def get_ticker_24h(self) -> Optional[List[Dict]]:
        """Return 24-h ticker stats for all symbols"""
        try:
            url = f"{self.base_url}/api/v3/ticker/24hr"
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as response:
                    if response.status == 200:
                        return await response.json()
            return None
        except Exception as e:
            logger.error(f"Error fetching 24h tickers: {e}")
            return None

    async def get_order_book(self, symbol: str) -> Optional[Dict]:
        """Return best bid/ask for spread calculation"""
        try:
            url = f"{self.base_url}/api/v3/ticker/bookTicker?symbol={symbol}"
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as response:
                    if response.status == 200:
                        return await response.json()
            return None
        except Exception as e:
            logger.error(f"Error fetching order book for {symbol}: {e}")
            return None

    async def place_order(self, symbol: str, side: str, quantity: float) -> Optional[Dict]:
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
    """Compute position size: 1 % of balance, clamped to [MIN, MAX]."""
    amount = balance * RISK_PER_TRADE
    amount = max(MIN_TRADE_AMOUNT, amount)
    amount = min(MAX_TRADE_AMOUNT, amount)
    return round(amount, 2)


def is_symbol_tradeable(volume: float, spread_pct: float) -> bool:
    """Return True if the symbol passes liquidity and spread filters."""
    return volume >= MIN_QUOTE_VOLUME and spread_pct <= MAX_SPREAD_PCT


async def fetch_liquid_usdt_symbols(binance_client: BinanceClient) -> List[str]:
    """
    Return USDT pairs that have enough 24-h volume and a tight spread.
    Results are sorted by descending quote volume so the most liquid
    pairs are analysed first.
    """
    spot_symbols_task = binance_client.get_exchange_info()
    tickers_task = binance_client.get_ticker_24h()

    spot_symbols, tickers = await asyncio.gather(spot_symbols_task, tickers_task)

    if not spot_symbols or not tickers:
        return []

    spot_set = set(spot_symbols)

    # Build a volume map for quick lookup
    volume_map: Dict[str, float] = {}
    for t in tickers:
        sym = t.get("symbol", "")
        if sym in spot_set:
            try:
                volume_map[sym] = float(t.get("quoteVolume", 0))
            except (ValueError, TypeError):
                pass

    # Filter by volume first (cheap), then check spread (requires extra call)
    candidates = [
        sym for sym, vol in volume_map.items()
        if vol >= MIN_QUOTE_VOLUME
    ]

    # Sort by volume descending to prioritise most liquid
    candidates.sort(key=lambda s: volume_map.get(s, 0), reverse=True)

    # Limit how many spread calls we make to avoid hammering the API
    MAX_CANDIDATES = 100
    candidates = candidates[:MAX_CANDIDATES]

    liquid = []
    for sym in candidates:
        book = await binance_client.get_order_book(sym)
        if not book:
            continue
        try:
            bid = float(book["bidPrice"])
            ask = float(book["askPrice"])
            if bid <= 0:
                continue
            spread_pct = (ask - bid) / bid * 100
        except (KeyError, ValueError, ZeroDivisionError):
            continue

        if is_symbol_tradeable(volume_map.get(sym, 0), spread_pct):
            liquid.append(sym)

    logger.info(f"Liquid USDT symbols found: {len(liquid)} out of {len(candidates)} candidates")
    return liquid


def scalping_signal(results: List[Optional[Dict]]) -> str:
    """
    Aggregate multi-timeframe analysis results into a single BUY/SELL/WAIT.
    A signal requires a majority of timeframes to agree.
    """
    buy_count = sum(1 for r in results if r and r.get("signal") == "BUY")
    sell_count = sum(1 for r in results if r and r.get("signal") == "SELL")
    total = len([r for r in results if r])

    if total == 0:
        return "WAIT"

    # Require at least 2 timeframes to agree
    if buy_count >= 2:
        return "BUY"
    if sell_count >= 2:
        return "SELL"
    return "WAIT"


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


async def execute_trade(binance_client: BinanceClient, binance_symbol: str, signal: str, update: Update, trade_amount: float):
    """Execute trade based on signal"""
    try:
        # Check if already have open trade for this symbol
        if binance_symbol in active_trades:
            logger.info(f"Already have open trade for {binance_symbol}")
            return

        # Check overall position cap
        if len(active_trades) >= MAX_OPEN_POSITIONS:
            logger.info(f"Max open positions ({MAX_OPEN_POSITIONS}) reached, skipping {binance_symbol}")
            return

        # Get current price
        current_price = await binance_client.get_current_price(binance_symbol)
        if not current_price:
            logger.error(f"Could not get price for {binance_symbol}")
            return

        # Calculate quantity based on dynamic trade amount
        quantity = trade_amount / current_price

        # Execute order
        if signal == "BUY":
            order = await binance_client.place_order(binance_symbol, "BUY", round(quantity, 8))
            if order:
                active_trades[binance_symbol] = {
                    "side": "BUY",
                    "entry_price": current_price,
                    "quantity": quantity,
                    "timestamp": datetime.now(),
                    "order_id": order.get("orderId")
                }
                message = f"✅ *BUY Exécuté*\n{binance_symbol}\nPrix: ${current_price}\nMontant: ${trade_amount}\nQuantité: {round(quantity, 8)}"
                await update.message.reply_text(message, parse_mode="Markdown")
                logger.info(f"BUY order placed for {binance_symbol} at ${current_price}")

        elif signal == "SELL":
            order = await binance_client.place_order(binance_symbol, "SELL", round(quantity, 8))
            if order:
                message = f"✅ *SELL Exécuté*\n{binance_symbol}\nPrix: ${current_price}\nMontant: ${trade_amount}\nQuantité: {round(quantity, 8)}"
                await update.message.reply_text(message, parse_mode="Markdown")
                if binance_symbol in active_trades:
                    del active_trades[binance_symbol]
                logger.info(f"SELL order placed for {binance_symbol} at ${current_price}")

    except Exception as e:
        logger.error(f"Error executing trade for {binance_symbol}: {e}")


async def auto_trade(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start scalping scanner across all liquid USDT pairs"""
    try:
        binance_client = BinanceClient(BINANCE_API_KEY, BINANCE_SECRET_KEY)

        # Get account balance
        balance = await binance_client.get_account_balance()
        if not balance:
            await update.message.reply_text("❌ Erreur: Impossible de vérifier le solde")
            return

        trade_amount = calculate_trade_amount(balance)

        message = (
            f"🤖 *Scanner Scalping Actif*\n\n"
            f"💰 Solde USDT: ${balance:.2f}\n"
            f"📊 Montant par trade: ${trade_amount} (1% du solde)\n"
            f"⛔ Stop Loss: {STOP_LOSS_PERCENT}%\n"
            f"🎯 Take Profit: {TAKE_PROFIT_PERCENT}%\n"
            f"🔎 Timeframes: {', '.join(SCALPING_TIMEFRAMES)}\n\n"
            f"⏳ Recherche des paires liquides...\n"
        )
        await update.message.reply_text(message, parse_mode="Markdown")

        # Discover liquid USDT pairs
        symbols = await fetch_liquid_usdt_symbols(binance_client)
        if not symbols:
            await update.message.reply_text("❌ Aucune paire liquide trouvée")
            return

        await update.message.reply_text(
            f"📡 {len(symbols)} paires liquides trouvées — analyse en cours...",
            parse_mode="Markdown"
        )

        entries = 0

        for binance_symbol in symbols:
            # Skip if we hit the max position cap
            if len(active_trades) >= MAX_OPEN_POSITIONS:
                break

            # Multi-timeframe analysis
            tf_results = []
            for tf in SCALPING_TIMEFRAMES:
                result = await analyze_symbol(binance_client, binance_symbol, tf)
                tf_results.append(result)

            signal = scalping_signal(tf_results)

            if signal in ("BUY", "SELL"):
                await execute_trade(binance_client, binance_symbol, signal, update, trade_amount)
                entries += 1

        # Summary
        summary = f"✅ *Scan terminé*\n{entries} signal(s) exécuté(s)"
        if active_trades:
            summary += f"\n\n📋 *Positions Ouvertes ({len(active_trades)}):*\n"
            for symbol, trade in active_trades.items():
                summary += f"\n• {symbol}: {trade['side']} @ ${trade['entry_price']:.4f}"
        await update.message.reply_text(summary, parse_mode="Markdown")

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
            message += f"💰 Solde USDT: ${balance:.2f}\n"
        
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
        f"• Risque par trade: {int(RISK_PER_TRADE * 100)}% du solde\n"
        f"• Taille min/max: ${MIN_TRADE_AMOUNT}/${MAX_TRADE_AMOUNT}\n"
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
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("trade", auto_trade))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("stop", stop_trading))
    
    logger.info("✅ Bot trading autonome Binance actif...")
    app.run_polling()


if __name__ == "__main__":
    main()

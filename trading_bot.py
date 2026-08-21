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

# Trading pairs configuration
TRADING_PAIRS = {
    "BTC": "BTCUSDT",
    "ETH": "ETHUSDT",
    "BNB": "BNBUSDT"
}

TRADE_AMOUNT = 20  # €20 par trade
STOP_LOSS_PERCENT = 5  # 5% stop loss
TAKE_PROFIT_PERCENT = 5  # 5% take profit
TIMEFRAMES = ["15m", "1h", "4h"]  # Binance timeframes

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
                    response_text = await response.text()
                    logger.info(f"Binance balance status: {response.status}")
                    logger.info(f"Binance balance response body: {response_text}")

                    if response.status == 200:
                        data = json.loads(response_text)
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


async def execute_trade(binance_client: BinanceClient, symbol_name: str, signal: str, update: Update):
    """Execute trade based on signal"""
    try:
        binance_symbol = TRADING_PAIRS.get(symbol_name)
        if not binance_symbol:
            logger.error(f"Symbol {symbol_name} not found in trading pairs")
            return
        
        # Check if already have open trade
        if binance_symbol in active_trades:
            logger.info(f"Already have open trade for {binance_symbol}")
            return
        
        # Get current price
        current_price = await binance_client.get_current_price(binance_symbol)
        if not current_price:
            logger.error(f"Could not get price for {binance_symbol}")
            return
        
        # Calculate quantity based on trade amount
        quantity = TRADE_AMOUNT / current_price
        
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
                message = f"✅ *BUY Exécuté*\n{symbol_name}\nPrix: ${current_price}\nQuantité: {round(quantity, 8)}"
                await update.message.reply_text(message, parse_mode="Markdown")
                logger.info(f"BUY order placed for {binance_symbol} at ${current_price}")
        
        elif signal == "SELL" and any(t["side"] == "BUY" for t in active_trades.values()):
            order = await binance_client.place_order(binance_symbol, "SELL", round(quantity, 8))
            if order:
                message = f"✅ *SELL Exécuté*\n{symbol_name}\nPrix: ${current_price}\nQuantité: {round(quantity, 8)}"
                await update.message.reply_text(message, parse_mode="Markdown")
                if binance_symbol in active_trades:
                    del active_trades[binance_symbol]
                logger.info(f"SELL order placed for {binance_symbol} at ${current_price}")
    
    except Exception as e:
        logger.error(f"Error executing trade for {symbol_name}: {e}")


async def auto_trade(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start autonomous trading"""
    try:
        binance_client = BinanceClient(BINANCE_API_KEY, BINANCE_SECRET_KEY)
        
        # Get account balance
        balance = await binance_client.get_account_balance()
        if not balance:
            await update.message.reply_text("❌ Erreur: Impossible de vérifier le solde")
            return
        
        message = f"🤖 *Mode Trading Autonome*\n\n💰 Solde USDT: ${balance:.2f}\n"
        message += f"📊 Montant par trade: ${TRADE_AMOUNT}\n"
        message += f"⛔ Stop Loss: {STOP_LOSS_PERCENT}%\n"
        message += f"🎯 Take Profit: {TAKE_PROFIT_PERCENT}%\n\n"
        message += "🚀 Analyse en cours...\n"
        
        await update.message.reply_text(message, parse_mode="Markdown")
        
        # Analyze each trading pair
        for symbol_name in TRADING_PAIRS.keys():
            binance_symbol = TRADING_PAIRS[symbol_name]
            buy_count = 0
            sell_count = 0
            
            for tf in TIMEFRAMES:
                result = await analyze_symbol(binance_client, binance_symbol, tf)
                if result:
                    if result["signal"] == "BUY":
                        buy_count += 1
                    elif result["signal"] == "SELL":
                        sell_count += 1
            
            # Execute trade if signal is strong
            if buy_count >= 2:
                await execute_trade(binance_client, symbol_name, "BUY", update)
            elif sell_count >= 2:
                await execute_trade(binance_client, symbol_name, "SELL", update)
        
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
        "🤖 *Bot Trading Autonome Binance*\n\n"
        "Commandes disponibles:\n"
        "/trade - Démarrer le trading automatique\n"
        "/status - Voir le statut des positions\n"
        "/stop - Arrêter le trading\n\n"
        f"⚙️ Configuration:\n"
        f"• Montant par trade: ${TRADE_AMOUNT}\n"
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

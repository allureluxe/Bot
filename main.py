import os
import logging
import aiohttp
import asyncio
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

# Load environment variables
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
TWELVE_API_KEY = os.getenv("TWELVE_API_KEY")


BASE_URL = "https://api.twelvedata.com/time_series"

PAIRS = {
    "gold": "XAU/USD",
    "eurusd": "EUR/USD",
    "btc": "BTC/USD"
}

TIMEFRAMES = ["1min", "5min", "15min", "30min", "1h"]

# Setup logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)


async def get_candles(symbol, interval, outputsize=100):
    """Fetch candle data from Twelve Data API"""
    params = {
        "symbol": symbol,
        "interval": interval,
        "apikey": TWELVE_API_KEY,
        "outputsize": outputsize,
        "format": "JSON"
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(BASE_URL, params=params, timeout=aiohttp.ClientTimeout(total=10)) as response:
                if response.status != 200:
                    logger.error(f"API error for {symbol}: Status {response.status}")
                    return None
                
                data = await response.json()

                if "values" not in data:
                    logger.warning(f"No values in response for {symbol}")
                    return None

                closes = [float(candle["close"]) for candle in reversed(data["values"])]
                return closes
    except asyncio.TimeoutError:
        logger.error(f"Timeout fetching data for {symbol}")
        return None
    except Exception as e:
        logger.error(f"Error fetching candles for {symbol}: {e}")
        return None


def ema(prices, period):
    """Calculate Exponential Moving Average"""
    if len(prices) < period:
        return None
    
    multiplier = 2 / (period + 1)
    ema_value = sum(prices[:period]) / period

    for price in prices[period:]:
        ema_value = (price - ema_value) * multiplier + ema_value

    return ema_value


def rsi(prices, period=14):
    """Calculate Relative Strength Index"""
    if len(prices) < period + 1:
        logger.warning(f"Insufficient data for RSI calculation: {len(prices)} < {period + 1}")
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


async def analyze(symbol, tf):
    """Analyze symbol with technical indicators"""
    prices = await get_candles(symbol, tf)

    if not prices or len(prices) < 50:
        logger.warning(f"Insufficient data for {symbol} on {tf}")
        return None

    try:
        current_price = prices[-1]
        ema20 = ema(prices, 20)
        ema50 = ema(prices, 50)
        current_rsi = rsi(prices, 14)

        if not all([ema20, ema50, current_rsi]):
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


async def asset_report(update: Update, asset_key):
    """Generate trading report for an asset"""
    try:
        symbol = PAIRS.get(asset_key)
        if not symbol:
            await update.message.reply_text(f"Asset '{asset_key}' not found. Available: {', '.join(PAIRS.keys())}")
            return

        message = f"📊 *{symbol}*\n\n"

        buy_count = 0
        sell_count = 0

        for tf in TIMEFRAMES:
            result = await analyze(symbol, tf)

            if not result:
                message += f"⚠️ {tf} | Erreur\n"
                continue

            emoji = "🟢" if result["signal"] == "BUY" else "🔴" if result["signal"] == "SELL" else "⚪"
            message += (
                f"{emoji} {tf} | {result['signal']} | "
                f"P:{result['price']} | "
                f"RSI:{result['rsi']}\n"
            )

            if result["signal"] == "BUY":
                buy_count += 1
            elif result["signal"] == "SELL":
                sell_count += 1

        message += "\n" + "="*40 + "\n"

        if buy_count >= 3:
            message += "🚀 *SIGNAL GLOBAL : BUY CONFIRMÉ*"
        elif sell_count >= 3:
            message += "⬇️ *SIGNAL GLOBAL : SELL CONFIRMÉ*"
        else:
            message += "⏸️ *SIGNAL GLOBAL : WAIT*"

        await update.message.reply_text(message, parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Error in asset_report: {e}")
        await update.message.reply_text("❌ Erreur lors de l'analyse. Veuillez réessayer.")


async def gold(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /gold command"""
    await asset_report(update, "gold")


async def eurusd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /eurusd command"""
    await asset_report(update, "eurusd")


async def btc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /btc command"""
    await asset_report(update, "btc")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start command"""
    message = (
        "🤖 *Bot Trading Avancé*\n\n"
        "Commandes disponibles:\n"
        "/gold - Analyse XAU/USD\n"
        "/eurusd - Analyse EUR/USD\n"
        "/btc - Analyse BTC/USD\n\n"
        "Les analyses utilisent EMA(20, 50) et RSI(14)"
    )
    await update.message.reply_text(message, parse_mode="Markdown")


def main():
    """Start the bot"""
    missing = [name for name, val in [
        ("BOT_TOKEN", BOT_TOKEN),
        ("TWELVE_API_KEY", TWELVE_API_KEY),
    ] if not val]
    if missing:
        logging.critical("❌ Missing required environment variables: %s", ", ".join(missing))
        return

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("gold", gold))
    app.add_handler(CommandHandler("eurusd", eurusd))
    app.add_handler(CommandHandler("btc", btc))

    logger.info("✅ Bot trading avancé actif...")
    app.run_polling()


if __name__ == "__main__":
    main()

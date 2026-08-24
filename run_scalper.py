#!/usr/bin/env python3
"""Point d'entree du bot de scalping.

    python run_scalper.py --check       diagnostic seul, ne trade pas
    python run_scalper.py --backtest    teste la strategie sur l'historique
    python run_scalper.py               lance le bot (DRY_RUN=true par defaut)
"""
import argparse
import asyncio
import logging
import signal
import sys

from scalper.backtest import render_backtest, run_backtest
from scalper.config import CONFIG
from scalper.engine import ScalpingEngine
from scalper.exchange import BinanceClient
from scalper.preflight import render_report, run_preflight


def setup_logging(verbose: bool = False) -> None:
    logging.basicConfig(
        format="%(asctime)s | %(levelname)-8s | %(name)-18s | %(message)s",
        level=logging.DEBUG if verbose else logging.INFO,
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
    )
    logging.getLogger("aiohttp").setLevel(logging.WARNING)


async def cmd_check() -> int:
    async with BinanceClient(
        CONFIG.binance_api_key, CONFIG.binance_secret_key,
        CONFIG.base_url, CONFIG.max_concurrent_requests,
    ) as client:
        ok, checks = await run_preflight(CONFIG, client)
        print(render_report(checks))
        return 0 if ok else 2


async def cmd_backtest(symbols_arg: str, limit: int) -> int:
    async with BinanceClient(
        CONFIG.binance_api_key, CONFIG.binance_secret_key,
        CONFIG.base_url, CONFIG.max_concurrent_requests,
    ) as client:
        if symbols_arg:
            symbols = [s.strip().upper() for s in symbols_arg.split(",") if s.strip()]
        else:
            info = await client.get_exchange_info()
            tickers = await client.get_24h_tickers()
            volumes = {t["symbol"]: float(t.get("quoteVolume", 0) or 0) for t in tickers}
            eligible = [
                s["symbol"] for s in info.get("symbols", [])
                if s.get("quoteAsset") == CONFIG.quote_asset
                and s.get("status") == "TRADING"
                and s.get("isSpotTradingAllowed")
            ]
            symbols = sorted(eligible, key=lambda s: -volumes.get(s, 0))[:8]

        print(f"Backtest sur : {', '.join(symbols)}\n")
        results = await run_backtest(CONFIG, client, symbols, limit)
        print(render_backtest(results, CONFIG))
        return 0


async def cmd_run() -> int:
    engine = ScalpingEngine(CONFIG)
    loop = asyncio.get_running_loop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, engine.stop)
        except (NotImplementedError, RuntimeError):
            pass  # Windows

    try:
        return await engine.run()
    except asyncio.CancelledError:
        return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Bot de scalping Binance Spot")
    parser.add_argument("--check", action="store_true",
                        help="lance le diagnostic puis quitte")
    parser.add_argument("--backtest", action="store_true",
                        help="teste la strategie sur l'historique puis quitte")
    parser.add_argument("--symbols", default="",
                        help="paires du backtest, separees par des virgules")
    parser.add_argument("--limit", type=int, default=1000,
                        help="nombre de bougies pour le backtest (max 1000)")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    setup_logging(args.verbose)

    if args.check:
        return asyncio.run(cmd_check())
    if args.backtest:
        return asyncio.run(cmd_backtest(args.symbols, min(args.limit, 1000)))

    if CONFIG.live and not CONFIG.testnet:
        print("=" * 66)
        print("  MODE REEL : des ordres seront passes avec de l'argent reel.")
        print("  Pour simuler sans risque : DRY_RUN=true dans le fichier .env")
        print("=" * 66, flush=True)

    return asyncio.run(cmd_run())


if __name__ == "__main__":
    sys.exit(main())

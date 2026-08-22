"""Moteur de scalping autonome.

Ce que l'ancien bot n'avait pas et qui explique l'absence totale de trades :
- aucune boucle : `/trade` faisait UN passage puis s'arretait,
- le stop loss et le take profit etaient affiches mais jamais executes,
- aucune surveillance des positions ouvertes.

Ici deux boucles tournent en permanence :
- `_scan_loop`   : cherche des entrees toutes les SCAN_INTERVAL_SECONDS,
- `_monitor_loop`: surveille SL / TP / trailing toutes les
                   MONITOR_INTERVAL_SECONDS et ferme reellement les positions.
"""
import asyncio
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .config import Config
from .exchange import BinanceClient, BinanceError
from .indicators import Candle
from .notifier import TelegramNotifier
from .preflight import render_report, run_preflight
from .risk import RiskManager, floor_to_step, format_quantity
from .state import Position, StateStore
from .strategy import Rejection, ScalpingStrategy, Signal

logger = logging.getLogger(__name__)

LEVERAGED_SUFFIXES = ("UPUSDT", "DOWNUSDT", "BULLUSDT", "BEARUSDT")


class ScalpingEngine:
    def __init__(self, config: Config):
        self.config = config
        self.store = StateStore(config.state_file, config.trade_log_file)
        self.state = self.store.load()
        self.risk = RiskManager(config, self.state)
        self.strategy = ScalpingStrategy(config)
        self.client = BinanceClient(
            config.binance_api_key,
            config.binance_secret_key,
            config.base_url,
            config.max_concurrent_requests,
        )
        self.notifier = TelegramNotifier(config.telegram_token, config.telegram_chat_id)

        self.universe: List[str] = []
        self._universe_ts = 0.0
        self._trend_cache: Dict[str, tuple] = {}
        self._last_prices: Dict[str, float] = {}
        self._running = False
        self._paper_balance = float(os.getenv("DRY_RUN_EQUITY", "1000"))
        self._scans = 0
        self._signals_seen = 0
        self._last_rejections: Dict[str, str] = {}
        self._last_error_notice: Dict[str, float] = {}

    # ------------------------------------------------------------ cycle de vie

    async def run(self) -> int:
        await self.client.start()
        try:
            ok, checks = await run_preflight(self.config, self.client)
            report = render_report(checks)
            print(report, flush=True)
            for line in report.splitlines():
                logger.info(line)

            if not ok:
                logger.critical("Diagnostic bloquant : le bot ne demarre pas.")
                return 2

            await self.notifier.start()
            self.notifier.send(self._startup_message())

            self._running = True
            await asyncio.gather(
                self._scan_loop(),
                self._monitor_loop(),
                self._heartbeat_loop(),
            )
            return 0
        finally:
            self._running = False
            self.store.save(self.state)
            await self.notifier.close()
            await self.client.close()

    def stop(self) -> None:
        logger.info("Arret demande, fermeture propre...")
        self._running = False

    def _startup_message(self) -> str:
        cfg = self.config
        mode = "SIMULATION" if cfg.dry_run else ("TESTNET" if cfg.testnet else "REEL")
        return (
            f"<b>Bot scalping demarre</b>\n"
            f"Mode : <b>{mode}</b>\n"
            f"Actif de cotation : {cfg.quote_asset}\n"
            f"Risque de base : {cfg.base_risk_pct}% (min {cfg.min_risk_pct}% / max {cfg.max_risk_pct}%)\n"
            f"Positions max : {cfg.max_open_positions}\n"
            f"SL {cfg.sl_atr_mult}xATR / TP {cfg.tp_atr_mult}xATR "
            f"(R:R {cfg.tp_atr_mult / cfg.sl_atr_mult:.2f})\n"
            f"Perte journaliere max : {cfg.daily_max_loss_pct}%\n"
            f"Positions rechargees : {len(self.state.positions)}"
        )

    # ---------------------------------------------------------------- capital

    async def quote_balance(self) -> float:
        if self.config.dry_run:
            return max(0.0, self._paper_balance)
        try:
            return await self.client.get_free_balance(self.config.quote_asset)
        except BinanceError as exc:
            logger.error("Lecture du solde impossible : %s", exc)
            return 0.0

    async def equity(self) -> float:
        """Capital total = liquidites + valeur de marche des positions."""
        balance = await self.quote_balance()
        for symbol, position in self.state.positions.items():
            price = self._last_prices.get(symbol, position.entry_price)
            balance += position.quantity * price
        return balance

    # --------------------------------------------------------------- univers

    async def _refresh_universe(self, force: bool = False) -> None:
        if not force and self.universe and (
            time.time() - self._universe_ts < self.config.universe_refresh_seconds
        ):
            return

        cfg = self.config
        info = await self.client.get_exchange_info(force=force)
        tickers = await self.client.get_24h_tickers()
        volumes = {t["symbol"]: float(t.get("quoteVolume", 0) or 0) for t in tickers}

        blacklist = set(cfg.symbol_blacklist)
        candidates: List[tuple] = []
        for symbol_info in info.get("symbols", []):
            symbol = symbol_info.get("symbol", "")
            if symbol_info.get("quoteAsset") != cfg.quote_asset:
                continue
            if symbol_info.get("status") != "TRADING":
                continue
            if not symbol_info.get("isSpotTradingAllowed"):
                continue
            if symbol in blacklist:
                continue
            # Les tokens a effet de levier sont impropres au scalping.
            if any(symbol.endswith(suffix) for suffix in LEVERAGED_SUFFIXES):
                continue
            if cfg.symbol_whitelist and symbol not in cfg.symbol_whitelist:
                continue

            volume = volumes.get(symbol, 0.0)
            if not cfg.symbol_whitelist and volume < cfg.min_quote_volume:
                continue
            candidates.append((volume, symbol))

        candidates.sort(reverse=True)
        self.universe = [symbol for _, symbol in candidates[: cfg.max_universe]]
        self._universe_ts = time.time()
        logger.info(
            "Univers rafraichi : %d paire(s) surveillee(s) -> %s",
            len(self.universe),
            ", ".join(self.universe[:10]) + ("..." if len(self.universe) > 10 else ""),
        )

    async def _trend_candles(self, symbol: str) -> List[Candle]:
        cached = self._trend_cache.get(symbol)
        if cached and time.time() - cached[0] < 60:
            return cached[1]
        candles = await self.client.get_candles(
            symbol, self.config.trend_timeframe, self.config.klines_limit
        )
        self._trend_cache[symbol] = (time.time(), candles)
        return candles

    # ------------------------------------------------------------ boucle scan

    async def _scan_loop(self) -> None:
        while self._running:
            started = time.time()
            try:
                await self._scan_once()
            except BinanceError as exc:
                if exc.is_geo_blocked:
                    logger.critical(
                        "Binance bloque cette machine (HTTP 451). Arret : "
                        "changez d'hebergement (VPS hors zone restreinte)."
                    )
                    self.notifier.send("<b>ARRET</b> : Binance bloque cette IP (HTTP 451).")
                    self.stop()
                    return
                logger.error("Erreur Binance pendant le scan : %s", exc)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception("Erreur inattendue pendant le scan : %s", exc)

            elapsed = time.time() - started
            await asyncio.sleep(max(1.0, self.config.scan_interval_seconds - elapsed))

    async def _scan_once(self) -> None:
        self._scans += 1
        equity = await self.equity()
        self.risk.roll_day_if_needed(equity)

        if self.state.paused:
            return

        halt_reason = self.risk.check_daily_circuit_breakers(equity)
        if halt_reason and not self.state.halted_until_next_day:
            self.state.halted_until_next_day = True
            self.state.halt_reason = halt_reason
            self.store.save(self.state)
            logger.warning("COUPE-CIRCUIT : %s", halt_reason)
            self.notifier.send(f"<b>Coupe-circuit journalier</b>\n{halt_reason}\nPlus aucune entree aujourd'hui.")
        if self.state.halted_until_next_day:
            return

        slots = self.config.max_open_positions - len(self.state.positions)
        if slots <= 0:
            return

        await self._refresh_universe()
        if not self.universe:
            logger.warning("Univers vide : aucun symbole a analyser.")
            return

        spreads = await self._spread_map()

        candidates: List[str] = []
        for symbol in self.universe:
            if symbol in self.state.positions:
                continue
            allowed, _ = self.risk.can_open(symbol, equity)
            if not allowed:
                continue
            spread = spreads.get(symbol)
            if spread is None or spread > self.config.max_spread_pct:
                continue
            candidates.append(symbol)

        if not candidates:
            logger.info("Scan #%d : aucun candidat apres filtres de spread/cooldown", self._scans)
            return

        results = await asyncio.gather(
            *(self._evaluate(symbol) for symbol in candidates), return_exceptions=True
        )

        signals: List[Signal] = []
        rejections: Dict[str, str] = {}
        for result in results:
            if isinstance(result, Signal):
                signals.append(result)
            elif isinstance(result, Rejection):
                rejections[result.symbol] = result.reason
            elif isinstance(result, BinanceError) and result.is_geo_blocked:
                raise result
            elif isinstance(result, Exception):
                logger.debug("Analyse en echec : %s", result)

        self._last_rejections = rejections
        self._signals_seen += len(signals)
        signals.sort(key=lambda s: (-s.score, -s.risk_reward, -s.volume_ratio))

        logger.info(
            "Scan #%d : %d analyses, %d signal(aux), %d place(s) libre(s)",
            self._scans, len(candidates), len(signals), slots,
        )

        for signal in signals:
            if slots <= 0:
                break
            allowed, reason = self.risk.can_open(signal.symbol, equity)
            if not allowed:
                logger.info("%s ignore : %s", signal.symbol, reason)
                continue
            if await self._open_position(signal, equity):
                slots -= 1
                equity = await self.equity()

    async def _spread_map(self) -> Dict[str, float]:
        books = await self.client.get_book_tickers()
        spreads: Dict[str, float] = {}
        for item in books:
            try:
                bid = float(item.get("bidPrice", 0) or 0)
                ask = float(item.get("askPrice", 0) or 0)
            except (TypeError, ValueError):
                continue
            if bid <= 0 or ask <= 0:
                continue
            mid = (bid + ask) / 2
            spreads[item["symbol"]] = ((ask - bid) / mid) * 100.0
        return spreads

    async def _evaluate(self, symbol: str):
        entry_candles = await self.client.get_candles(
            symbol, self.config.entry_timeframe, self.config.klines_limit
        )
        trend_candles = (
            await self._trend_candles(symbol) if self.config.require_trend_filter else []
        )
        if entry_candles:
            self._last_prices[symbol] = entry_candles[-1].close
        return self.strategy.evaluate(symbol, entry_candles, trend_candles)

    # ------------------------------------------------------------- ouverture

    async def _open_position(self, signal: Signal, equity: float) -> bool:
        symbol = signal.symbol
        filters = self.client.symbol_filters(symbol)
        if not filters:
            logger.warning("%s : filtres Binance introuvables", symbol)
            return False

        balance = await self.quote_balance()
        sizing = self.risk.size_position(
            equity, balance, signal.price, signal.stop_price, filters
        )
        if not sizing.ok:
            logger.info("%s : entree annulee (%s)", symbol, sizing.reason)
            return False

        step = filters["step_size"]
        quantity_str = format_quantity(sizing.quantity, step)

        if self.config.dry_run:
            fill_price = signal.price
            filled_qty = sizing.quantity
            order_id = f"PAPER-{int(time.time() * 1000)}"
            self._paper_balance -= filled_qty * fill_price * (1 + self.config.fee_rate)
        else:
            try:
                order = await self.client.place_market_order(symbol, "BUY", quantity_str)
            except BinanceError as exc:
                logger.error("%s : ordre BUY rejete (%s)", symbol, exc.message)
                self._notify_error(symbol, f"BUY {symbol} rejete : {exc.message}")
                return False

            fill_price, executed_qty, base_fee = self._parse_fills(order, filters)
            if executed_qty <= 0 or fill_price <= 0:
                logger.error("%s : ordre BUY sans execution exploitable", symbol)
                return False
            # Binance preleve la commission dans l'actif recu : la quantite
            # revendable est inferieure a la quantite achetee.
            filled_qty = floor_to_step(executed_qty - base_fee, step)
            if filled_qty <= 0:
                logger.error("%s : quantite nette nulle apres frais", symbol)
                return False
            order_id = str(order.get("orderId", ""))

        stop_price = fill_price * (1 - signal.stop_pct / 100.0)
        take_profit = fill_price * (1 + signal.tp_pct / 100.0)

        position = Position(
            symbol=symbol,
            side="LONG",
            quantity=filled_qty,
            entry_price=fill_price,
            stop_price=stop_price,
            take_profit=take_profit,
            initial_stop=stop_price,
            atr=signal.atr_value,
            opened_at=time.time(),
            risk_amount=filled_qty * (fill_price - stop_price),
            notional=filled_qty * fill_price,
            order_id=order_id,
            highest_price=fill_price,
            dry_run=self.config.dry_run,
        )
        self.state.positions[symbol] = position
        self._last_prices[symbol] = fill_price
        self.store.save(self.state)

        logger.info(
            "ENTREE %s | score %d/6 | %.8f @ %.8f | SL %.8f (-%.2f%%) | TP %.8f (+%.2f%%) | risque %.2f%%",
            symbol, signal.score, filled_qty, fill_price, stop_price, signal.stop_pct,
            take_profit, signal.tp_pct, sizing.risk_pct,
        )
        self.notifier.send(
            f"<b>ACHAT {symbol}</b> {'(simule)' if self.config.dry_run else ''}\n"
            f"Score : {signal.score}/6 — {', '.join(signal.reasons[:3])}\n"
            f"Prix : {fill_price:.8f}\n"
            f"Quantite : {filled_qty:.8f} ({position.notional:.2f} {self.config.quote_asset})\n"
            f"SL : {stop_price:.8f} (-{signal.stop_pct:.2f}%)\n"
            f"TP : {take_profit:.8f} (+{signal.tp_pct:.2f}%)\n"
            f"Risque : {sizing.risk_pct:.2f}% du capital "
            f"({sizing.risk_amount:.2f} {self.config.quote_asset})"
        )
        return True

    def _notify_error(self, key: str, message: str, min_interval: float = 60.0) -> None:
        """Envoie une notification d'erreur au plus une fois par minute et par cle."""
        now = time.time()
        if now - self._last_error_notice.get(key, 0.0) < min_interval:
            return
        self._last_error_notice[key] = now
        self.notifier.send(message)

    @staticmethod
    def _parse_fills(order: Dict[str, Any], filters: Dict[str, Any]) -> tuple:
        """Retourne (prix moyen, quantite executee, commission en actif de base)."""
        fills = order.get("fills") or []
        base_asset = filters.get("base_asset", "")
        total_qty = 0.0
        total_quote = 0.0
        base_fee = 0.0
        for fill in fills:
            try:
                qty = float(fill.get("qty", 0))
                price = float(fill.get("price", 0))
            except (TypeError, ValueError):
                continue
            total_qty += qty
            total_quote += qty * price
            if fill.get("commissionAsset") == base_asset:
                try:
                    base_fee += float(fill.get("commission", 0))
                except (TypeError, ValueError):
                    pass

        if total_qty > 0:
            return total_quote / total_qty, total_qty, base_fee

        # Repli si Binance n'a pas renvoye les fills.
        try:
            executed = float(order.get("executedQty", 0))
            cummulative = float(order.get("cummulativeQuoteQty", 0))
        except (TypeError, ValueError):
            return 0.0, 0.0, 0.0
        if executed <= 0:
            return 0.0, 0.0, 0.0
        return cummulative / executed, executed, 0.0

    # -------------------------------------------------------- boucle monitor

    async def _monitor_loop(self) -> None:
        while self._running:
            try:
                await self._monitor_once()
            except BinanceError as exc:
                if exc.is_geo_blocked:
                    self.stop()
                    return
                logger.error("Erreur Binance pendant la surveillance : %s", exc)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception("Erreur inattendue pendant la surveillance : %s", exc)
            await asyncio.sleep(self.config.monitor_interval_seconds)

    async def _monitor_once(self) -> None:
        if not self.state.positions:
            return

        symbols = list(self.state.positions.keys())
        prices = await self.client.get_prices(symbols)
        dirty = False

        for symbol in symbols:
            position = self.state.positions.get(symbol)
            if position is None:
                continue
            price = prices.get(symbol)
            if not price:
                continue
            self._last_prices[symbol] = price

            if self.strategy.update_trailing(position, price):
                dirty = True

            reason = self.strategy.exit_decision(position, price)
            if reason:
                await self._close_position(position, price, reason)
                dirty = True

        if dirty:
            self.store.save(self.state)

    # -------------------------------------------------------------- cloture

    async def _close_position(self, position: Position, price: float, reason: str) -> None:
        symbol = position.symbol
        filters = self.client.symbol_filters(symbol) or {}
        step = filters.get("step_size", 0.0)
        sold_qty = position.quantity
        exit_price = price

        if self.config.dry_run:
            self._paper_balance += sold_qty * exit_price * (1 - self.config.fee_rate)
        else:
            base_asset = filters.get("base_asset", "")
            try:
                free_base = await self.client.get_free_balance(base_asset) if base_asset else sold_qty
            except BinanceError:
                free_base = sold_qty
            sellable = floor_to_step(min(sold_qty, free_base), step) if step > 0 else min(sold_qty, free_base)

            if sellable <= 0:
                logger.error(
                    "%s : rien a vendre (solde %s = %.8f). Position retiree du suivi.",
                    symbol, base_asset, free_base,
                )
                self.state.positions.pop(symbol, None)
                self.store.save(self.state)
                self.notifier.send(
                    f"<b>{symbol}</b> : cloture impossible (solde {base_asset} nul). "
                    "Position retiree du suivi — verifiez manuellement sur Binance."
                )
                return

            try:
                order = await self.client.place_market_order(
                    symbol, "SELL", format_quantity(sellable, step)
                )
            except BinanceError as exc:
                logger.error("%s : ordre SELL rejete (%s) — nouvelle tentative au prochain cycle",
                             symbol, exc.message)
                # La surveillance repasse toutes les 2 s : sans limitation, un
                # rejet persistant inonderait Telegram.
                self._notify_error(symbol, f"SELL {symbol} rejete : {exc.message}")
                return

            fill_price, executed_qty, _ = self._parse_fills(order, filters)
            exit_price = fill_price if fill_price > 0 else price
            sold_qty = executed_qty if executed_qty > 0 else sellable

        entry_notional = position.entry_price * sold_qty
        exit_notional = exit_price * sold_qty
        fees = (entry_notional + exit_notional) * self.config.fee_rate
        pnl = exit_notional - entry_notional - fees
        pnl_pct = (pnl / entry_notional * 100.0) if entry_notional else 0.0
        r_multiple = (pnl / position.risk_amount) if position.risk_amount > 0 else 0.0
        hold = position.age_seconds()

        self.state.positions.pop(symbol, None)
        risk_pct_used = (
            position.risk_amount / max(1e-9, entry_notional) * 100.0
        )
        self.risk.register_result(symbol, pnl, fees)
        self.store.save(self.state)

        self.store.log_trade({
            "closed_at": datetime.now(timezone.utc).isoformat(),
            "symbol": symbol,
            "side": position.side,
            "quantity": f"{sold_qty:.8f}",
            "entry_price": f"{position.entry_price:.8f}",
            "exit_price": f"{exit_price:.8f}",
            "stop_price": f"{position.stop_price:.8f}",
            "take_profit": f"{position.take_profit:.8f}",
            "pnl": f"{pnl:.4f}",
            "pnl_pct": f"{pnl_pct:.4f}",
            "r_multiple": f"{r_multiple:.3f}",
            "fees": f"{fees:.4f}",
            "reason": reason,
            "hold_seconds": f"{hold:.0f}",
            "risk_pct": f"{risk_pct_used:.3f}",
            "dry_run": position.dry_run,
        })

        outcome = "GAIN" if pnl > 0 else "PERTE"
        logger.info(
            "SORTIE %s | %s | %s | entree %.8f -> sortie %.8f | PnL %.4f %s (%.2f%%, %.2fR) | %.0fs",
            symbol, reason, outcome, position.entry_price, exit_price, pnl,
            self.config.quote_asset, pnl_pct, r_multiple, hold,
        )
        self.notifier.send(
            f"<b>VENTE {symbol}</b> — {reason}\n"
            f"Entree {position.entry_price:.8f} → Sortie {exit_price:.8f}\n"
            f"Resultat : <b>{pnl:+.4f} {self.config.quote_asset}</b> "
            f"({pnl_pct:+.2f}%, {r_multiple:+.2f}R)\n"
            f"Frais : {fees:.4f} | Duree : {hold:.0f}s\n"
            f"Journee : {self.state.daily.realized_pnl:+.4f} {self.config.quote_asset} "
            f"({self.state.daily.pnl_pct():+.2f}%) sur {self.state.daily.trades} trade(s)\n"
            f"Serie : {self.state.win_streak} gain(s) / {self.state.loss_streak} perte(s)"
        )

        equity = await self.equity()
        halt_reason = self.risk.check_daily_circuit_breakers(equity)
        if halt_reason and not self.state.halted_until_next_day:
            self.state.halted_until_next_day = True
            self.state.halt_reason = halt_reason
            self.store.save(self.state)
            logger.warning("COUPE-CIRCUIT : %s", halt_reason)
            self.notifier.send(f"<b>Coupe-circuit journalier</b>\n{halt_reason}")

    # ------------------------------------------------------------ heartbeat

    async def _heartbeat_loop(self) -> None:
        interval = float(os.getenv("HEARTBEAT_SECONDS", "300"))
        while self._running:
            await asyncio.sleep(interval)
            if not self._running:
                return
            try:
                equity = await self.equity()
                daily = self.state.daily
                logger.info(
                    "ETAT | capital %.2f %s | positions %d | scans %d | signaux %d | "
                    "jour %+.4f (%.2f%%) sur %d trade(s) | reussite %.1f%% | risque courant %.2f%%",
                    equity, self.config.quote_asset, len(self.state.positions), self._scans,
                    self._signals_seen, daily.realized_pnl, daily.pnl_pct(), daily.trades,
                    daily.win_rate(), self.risk.current_risk_pct(equity),
                )
                if self._scans > 0 and self._signals_seen == 0 and self._last_rejections:
                    sample = list(self._last_rejections.items())[:5]
                    logger.info(
                        "Aucun signal jusqu'ici. Exemples de refus : %s",
                        " | ".join(f"{s}: {r}" for s, r in sample),
                    )
            except Exception as exc:
                logger.debug("Heartbeat en echec : %s", exc)

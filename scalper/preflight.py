"""Diagnostic de demarrage.

Objectif : ne plus jamais avoir un bot qui « tourne » sans jamais trader sans
qu'on sache pourquoi. Chaque cause connue de silence est testee et expliquee.
"""
import logging
from dataclasses import dataclass
from typing import List, Tuple

from .config import Config
from .exchange import BinanceClient, BinanceError

logger = logging.getLogger(__name__)

OK = "OK"
WARN = "ATTENTION"
FAIL = "BLOQUANT"


@dataclass
class Check:
    level: str
    title: str
    detail: str = ""

    def render(self) -> str:
        icon = {OK: "[OK]  ", WARN: "[!]   ", FAIL: "[STOP]"}[self.level]
        line = f"{icon} {self.title}"
        if self.detail:
            line += f"\n         {self.detail}"
        return line


async def run_preflight(config: Config, client: BinanceClient) -> Tuple[bool, List[Check]]:
    checks: List[Check] = []

    # 1. Coherence de la configuration -------------------------------------
    errors = config.validate()
    if errors:
        for error in errors:
            checks.append(Check(FAIL, "Configuration invalide", error))
    else:
        checks.append(Check(OK, "Configuration coherente"))

    for warning in config.warnings():
        checks.append(Check(WARN, "Avertissement de configuration", warning))

    mode = "SIMULATION (aucun ordre reel)" if config.dry_run else (
        "TESTNET" if config.testnet else "REEL - ARGENT VERITABLE"
    )
    checks.append(Check(OK, f"Mode : {mode}", f"Endpoint : {config.base_url}"))

    # 2. Joignabilite de Binance -------------------------------------------
    try:
        await client.ping()
        checks.append(Check(OK, "API Binance joignable"))
    except BinanceError as exc:
        if exc.is_geo_blocked:
            checks.append(Check(
                FAIL,
                "Binance bloque cette machine (HTTP 451)",
                "L'adresse IP est dans une zone restreinte. C'est le cas de la "
                "quasi-totalite des runners GitHub Actions (bases aux Etats-Unis). "
                "Il faut heberger le bot sur un VPS dans une region autorisee.",
            ))
        else:
            checks.append(Check(FAIL, "API Binance injoignable", exc.message))
        return False, checks
    except Exception as exc:
        checks.append(Check(FAIL, "API Binance injoignable", str(exc)))
        return False, checks

    # 3. Synchronisation d'horloge -----------------------------------------
    try:
        offset = await client.sync_time()
        level = OK if abs(offset) < 2000 else WARN
        checks.append(Check(
            level, "Horloge synchronisee avec Binance", f"decalage {offset} ms"
        ))
    except Exception as exc:
        checks.append(Check(WARN, "Synchronisation d'horloge impossible", str(exc)))

    # 4. Univers de trading -------------------------------------------------
    try:
        info = await client.get_exchange_info()
        tickers = await client.get_24h_tickers()
    except BinanceError as exc:
        checks.append(Check(FAIL, "Donnees de marche indisponibles", exc.message))
        return False, checks

    tradable = [
        s["symbol"] for s in info.get("symbols", [])
        if s.get("quoteAsset") == config.quote_asset
        and s.get("status") == "TRADING"
        and s.get("isSpotTradingAllowed")
    ]
    if not tradable:
        checks.append(Check(
            FAIL,
            f"Aucune paire *{config.quote_asset} negociable",
            f"QUOTE_ASSET={config.quote_asset} n'existe pas ou n'est pas cote sur ce compte. "
            "Utilisez USDT (le plus liquide).",
        ))
        return False, checks

    volumes = {t["symbol"]: float(t.get("quoteVolume", 0) or 0) for t in tickers}
    liquid = [s for s in tradable if volumes.get(s, 0) >= config.min_quote_volume]

    if not liquid:
        best = sorted(
            ((volumes.get(s, 0), s) for s in tradable), reverse=True
        )[:3]
        detail = (
            f"{len(tradable)} paires {config.quote_asset} existent, mais aucune n'atteint "
            f"MIN_QUOTE_VOLUME={config.min_quote_volume:,.0f}. "
            f"Meilleurs volumes 24h : "
            + ", ".join(f"{sym} {vol:,.0f}" for vol, sym in best)
            + ". Baissez MIN_QUOTE_VOLUME ou changez de QUOTE_ASSET."
        )
        checks.append(Check(FAIL, "Univers de trading vide", detail))
        return False, checks

    checks.append(Check(
        OK,
        f"{len(liquid)} paire(s) {config.quote_asset} assez liquides",
        f"sur {len(tradable)} negociables (seuil {config.min_quote_volume:,.0f})",
    ))

    # 5. Compte -------------------------------------------------------------
    if config.dry_run:
        checks.append(Check(
            OK, "DRY_RUN actif", "Aucune cle API n'est necessaire, rien ne sera execute."
        ))
        return not any(c.level == FAIL for c in checks), checks

    try:
        account = await client.get_account()
    except BinanceError as exc:
        if exc.is_auth_error:
            checks.append(Check(
                FAIL,
                "Cles API refusees par Binance",
                f"{exc.message}. Verifiez la cle, le secret, et surtout la "
                "restriction d'adresse IP dans les parametres de l'API Binance.",
            ))
        elif exc.is_timestamp_error:
            checks.append(Check(FAIL, "Horloge desynchronisee", exc.message))
        else:
            checks.append(Check(FAIL, "Acces au compte impossible", exc.message))
        return False, checks

    if not account.get("canTrade", False):
        checks.append(Check(
            FAIL,
            "La cle API n'a pas la permission de trader",
            "Activez « Enable Spot & Margin Trading » sur la cle dans Binance.",
        ))
        return False, checks
    checks.append(Check(OK, "Cle API valide avec permission de trading"))

    balance = 0.0
    for item in account.get("balances", []):
        if item["asset"] == config.quote_asset:
            balance = float(item["free"])
            break

    if balance <= 0:
        checks.append(Check(
            FAIL,
            f"Solde {config.quote_asset} nul",
            f"Le compte n'a aucun {config.quote_asset} disponible : aucune position "
            "ne peut etre ouverte.",
        ))
        return False, checks

    # Verifie qu'au moins une paire est finançable avec ce solde.
    affordable = []
    for symbol in liquid[:40]:
        filters = client.symbol_filters(symbol)
        if not filters:
            continue
        min_notional = filters.get("min_notional", 0)
        cap = balance * config.max_position_pct / 100.0
        if min_notional <= 0 or min_notional <= cap:
            affordable.append(symbol)

    if not affordable:
        example_cap = balance * config.max_position_pct / 100.0
        checks.append(Check(
            FAIL,
            "Capital insuffisant pour le minimum impose par Binance",
            f"Solde {balance:.2f} {config.quote_asset}, plafond par position "
            f"{example_cap:.2f} {config.quote_asset}, mais le minNotional Binance est "
            "superieur. Augmentez MAX_POSITION_PCT ou approvisionnez le compte "
            f"(comptez au moins 60-100 {config.quote_asset} pour scalper serieusement).",
        ))
        return False, checks

    checks.append(Check(
        OK,
        f"Solde disponible : {balance:.2f} {config.quote_asset}",
        f"{len(affordable)} paire(s) finançables avec les plafonds actuels",
    ))

    return not any(c.level == FAIL for c in checks), checks


def render_report(checks: List[Check]) -> str:
    header = "=" * 66 + "\n  DIAGNOSTIC DE DEMARRAGE\n" + "=" * 66
    body = "\n".join(check.render() for check in checks)
    return f"{header}\n{body}\n" + "=" * 66

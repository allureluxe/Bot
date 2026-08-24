"""Configuration centrale du bot de scalping.

Tout est pilote par variables d'environnement pour qu'aucun secret ne soit
present dans le code. Les valeurs par defaut sont pensees pour du scalping
agressif mais survivable sur Binance Spot.
"""
import os
from dataclasses import dataclass, field
from typing import List

from dotenv import load_dotenv

load_dotenv()


def _f(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except (TypeError, ValueError):
        return float(default)


def _i(name: str, default: int) -> int:
    try:
        return int(float(os.getenv(name, default)))
    except (TypeError, ValueError):
        return int(default)


def _b(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on", "oui"}


def _list(name: str, default: List[str]) -> List[str]:
    raw = os.getenv(name)
    if not raw:
        return list(default)
    return [item.strip().upper() for item in raw.split(",") if item.strip()]


@dataclass
class Config:
    # --- Identifiants ---
    binance_api_key: str = field(default_factory=lambda: os.getenv("BINANCE_API_KEY", ""))
    binance_secret_key: str = field(default_factory=lambda: os.getenv("BINANCE_SECRET_KEY", ""))
    telegram_token: str = field(default_factory=lambda: os.getenv("BOT_TOKEN", ""))
    telegram_chat_id: str = field(default_factory=lambda: os.getenv("TELEGRAM_CHAT_ID", ""))

    # --- Mode d'execution ---
    # dry_run: aucune commande n'est envoyee a Binance, tout est simule.
    dry_run: bool = field(default_factory=lambda: _b("DRY_RUN", True))
    testnet: bool = field(default_factory=lambda: _b("BINANCE_TESTNET", False))

    # --- Marche ---
    quote_asset: str = field(default_factory=lambda: os.getenv("QUOTE_ASSET", "USDT").upper())
    symbol_whitelist: List[str] = field(default_factory=lambda: _list("SYMBOL_WHITELIST", []))
    symbol_blacklist: List[str] = field(default_factory=lambda: _list("SYMBOL_BLACKLIST", []))
    max_universe: int = field(default_factory=lambda: _i("MAX_UNIVERSE", 40))
    min_quote_volume: float = field(default_factory=lambda: _f("MIN_QUOTE_VOLUME", 20_000_000))
    max_spread_pct: float = field(default_factory=lambda: _f("MAX_SPREAD_PCT", 0.06))

    # --- Timeframes ---
    entry_timeframe: str = field(default_factory=lambda: os.getenv("ENTRY_TIMEFRAME", "1m"))
    trend_timeframe: str = field(default_factory=lambda: os.getenv("TREND_TIMEFRAME", "5m"))
    klines_limit: int = field(default_factory=lambda: _i("KLINES_LIMIT", 120))

    # --- Strategie ---
    ema_fast: int = field(default_factory=lambda: _i("EMA_FAST", 9))
    ema_slow: int = field(default_factory=lambda: _i("EMA_SLOW", 21))
    ema_trend: int = field(default_factory=lambda: _i("EMA_TREND", 50))
    rsi_period: int = field(default_factory=lambda: _i("RSI_PERIOD", 14))
    atr_period: int = field(default_factory=lambda: _i("ATR_PERIOD", 14))
    rsi_long_min: float = field(default_factory=lambda: _f("RSI_LONG_MIN", 50.0))
    rsi_long_max: float = field(default_factory=lambda: _f("RSI_LONG_MAX", 74.0))
    volume_spike_mult: float = field(default_factory=lambda: _f("VOLUME_SPIKE_MULT", 1.15))
    # Score minimum (sur 6) pour declencher une entree. Bas = agressif.
    min_entry_score: int = field(default_factory=lambda: _i("MIN_ENTRY_SCORE", 4))
    require_trend_filter: bool = field(default_factory=lambda: _b("REQUIRE_TREND_FILTER", True))
    # Volatilite exigee: en dessous, le scalp ne paie pas les frais.
    min_atr_pct: float = field(default_factory=lambda: _f("MIN_ATR_PCT", 0.08))
    max_atr_pct: float = field(default_factory=lambda: _f("MAX_ATR_PCT", 2.50))

    # --- Sortie / risque par trade ---
    sl_atr_mult: float = field(default_factory=lambda: _f("SL_ATR_MULT", 1.1))
    tp_atr_mult: float = field(default_factory=lambda: _f("TP_ATR_MULT", 1.8))
    min_sl_pct: float = field(default_factory=lambda: _f("MIN_SL_PCT", 0.25))
    max_sl_pct: float = field(default_factory=lambda: _f("MAX_SL_PCT", 1.50))
    trail_activate_r: float = field(default_factory=lambda: _f("TRAIL_ACTIVATE_R", 1.0))
    trail_atr_mult: float = field(default_factory=lambda: _f("TRAIL_ATR_MULT", 0.9))
    breakeven_at_r: float = field(default_factory=lambda: _f("BREAKEVEN_AT_R", 0.6))
    max_hold_seconds: int = field(default_factory=lambda: _i("MAX_HOLD_SECONDS", 900))

    # --- Frais ---
    fee_rate: float = field(default_factory=lambda: _f("FEE_RATE", 0.001))
    fee_safety_mult: float = field(default_factory=lambda: _f("FEE_SAFETY_MULT", 1.8))

    # --- Money management ---
    base_risk_pct: float = field(default_factory=lambda: _f("BASE_RISK_PCT", 0.75))
    min_risk_pct: float = field(default_factory=lambda: _f("MIN_RISK_PCT", 0.20))
    max_risk_pct: float = field(default_factory=lambda: _f("MAX_RISK_PCT", 2.00))
    # Progressif: on augmente apres une serie de gains.
    win_streak_step: float = field(default_factory=lambda: _f("WIN_STREAK_STEP", 1.15))
    max_streak_mult: float = field(default_factory=lambda: _f("MAX_STREAK_MULT", 1.80))
    # Regressif: on reduit apres une serie de pertes (jamais de martingale).
    loss_streak_step: float = field(default_factory=lambda: _f("LOSS_STREAK_STEP", 0.70))
    min_streak_mult: float = field(default_factory=lambda: _f("MIN_STREAK_MULT", 0.35))
    # Echelle de capital: le risque % diminue quand le capital grossit.
    equity_ref: float = field(default_factory=lambda: _f("EQUITY_REF", 500.0))
    equity_decay: float = field(default_factory=lambda: _f("EQUITY_DECAY", 0.15))
    # Part maximale du capital immobilisee dans une seule position.
    max_position_pct: float = field(default_factory=lambda: _f("MAX_POSITION_PCT", 25.0))
    max_total_exposure_pct: float = field(default_factory=lambda: _f("MAX_TOTAL_EXPOSURE_PCT", 80.0))

    # --- Garde-fous journaliers ---
    max_open_positions: int = field(default_factory=lambda: _i("MAX_OPEN_POSITIONS", 4))
    max_trades_per_day: int = field(default_factory=lambda: _i("MAX_TRADES_PER_DAY", 0))  # 0 = illimite
    daily_max_loss_pct: float = field(default_factory=lambda: _f("DAILY_MAX_LOSS_PCT", 6.0))
    daily_soft_loss_pct: float = field(default_factory=lambda: _f("DAILY_SOFT_LOSS_PCT", 3.0))
    daily_profit_target_pct: float = field(default_factory=lambda: _f("DAILY_PROFIT_TARGET_PCT", 0.0))
    symbol_cooldown_seconds: int = field(default_factory=lambda: _i("SYMBOL_COOLDOWN_SECONDS", 180))
    max_consecutive_losses: int = field(default_factory=lambda: _i("MAX_CONSECUTIVE_LOSSES", 6))

    # --- Boucles ---
    scan_interval_seconds: float = field(default_factory=lambda: _f("SCAN_INTERVAL_SECONDS", 20))
    monitor_interval_seconds: float = field(default_factory=lambda: _f("MONITOR_INTERVAL_SECONDS", 2))
    universe_refresh_seconds: float = field(default_factory=lambda: _f("UNIVERSE_REFRESH_SECONDS", 900))
    max_concurrent_requests: int = field(default_factory=lambda: _i("MAX_CONCURRENT_REQUESTS", 8))

    # --- Persistance ---
    state_file: str = field(default_factory=lambda: os.getenv("STATE_FILE", "state/scalper_state.json"))
    trade_log_file: str = field(default_factory=lambda: os.getenv("TRADE_LOG_FILE", "state/trades.csv"))

    @property
    def base_url(self) -> str:
        override = os.getenv("BINANCE_BASE_URL")
        if override:
            return override.rstrip("/")
        if self.testnet:
            return "https://testnet.binance.vision"
        return "https://api.binance.com"

    @property
    def live(self) -> bool:
        """True seulement si on envoie de vrais ordres."""
        return not self.dry_run

    def round_trip_fee_pct(self) -> float:
        """Cout aller-retour en pourcentage du prix."""
        return self.fee_rate * 2 * 100

    def min_profitable_tp_pct(self) -> float:
        """TP minimum pour que le trade ait un sens apres frais."""
        return self.round_trip_fee_pct() * self.fee_safety_mult

    def validate(self) -> List[str]:
        """Retourne la liste des problemes bloquants ou dangereux."""
        errors: List[str] = []
        if self.live and not self.binance_api_key:
            errors.append("BINANCE_API_KEY manquante (obligatoire hors DRY_RUN)")
        if self.live and not self.binance_secret_key:
            errors.append("BINANCE_SECRET_KEY manquante (obligatoire hors DRY_RUN)")
        if self.ema_fast >= self.ema_slow:
            errors.append("EMA_FAST doit etre < EMA_SLOW")
        if self.min_risk_pct > self.max_risk_pct:
            errors.append("MIN_RISK_PCT > MAX_RISK_PCT")
        if not 0 < self.base_risk_pct <= 10:
            errors.append("BASE_RISK_PCT doit etre dans ]0, 10]")
        if self.sl_atr_mult <= 0 or self.tp_atr_mult <= 0:
            errors.append("SL_ATR_MULT et TP_ATR_MULT doivent etre > 0")
        if self.min_sl_pct >= self.max_sl_pct:
            errors.append("MIN_SL_PCT doit etre < MAX_SL_PCT")
        if self.daily_max_loss_pct <= 0:
            errors.append("DAILY_MAX_LOSS_PCT doit etre > 0 (coupe-circuit obligatoire)")
        if self.max_open_positions < 1:
            errors.append("MAX_OPEN_POSITIONS doit etre >= 1")
        if self.klines_limit <= max(self.ema_trend, self.rsi_period, self.atr_period) + 5:
            errors.append("KLINES_LIMIT trop petit pour les indicateurs demandes")
        if self.loss_streak_step >= 1:
            errors.append("LOSS_STREAK_STEP doit etre < 1 (interdiction de martingale)")
        return errors

    def warnings(self) -> List[str]:
        """Problemes non bloquants mais qui meritent d'etre signales."""
        warns: List[str] = []
        expected_tp = self.tp_atr_mult / self.sl_atr_mult
        if expected_tp < 1.2:
            warns.append(
                f"Ratio TP/SL = {expected_tp:.2f} : avec 0.2% de frais aller-retour, "
                "l'esperance est probablement negative."
            )
        if self.min_sl_pct * self.tp_atr_mult / self.sl_atr_mult < self.min_profitable_tp_pct():
            warns.append(
                f"Le TP minimum ({self.min_sl_pct * self.tp_atr_mult / self.sl_atr_mult:.3f}%) "
                f"est sous le seuil de rentabilite apres frais ({self.min_profitable_tp_pct():.3f}%)."
            )
        if self.live and not self.testnet:
            warns.append("MODE REEL : les ordres seront executes avec de l'argent reel.")
        if self.max_trades_per_day == 0:
            warns.append("MAX_TRADES_PER_DAY=0 : nombre de trades illimite sur la journee.")
        return warns


CONFIG = Config()

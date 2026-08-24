"""Client Binance Spot asynchrone.

Corrections par rapport a l'ancien client :
- une seule session aiohttp reutilisee (au lieu d'une par requete),
- horodatage base sur l'heure serveur Binance (l'ancien utilisait
  datetime.now() = heure locale -> erreur -1021 hors UTC),
- recvWindow explicite,
- limitation de concurrence pour ne pas se faire bannir (-1003),
- detection explicite du blocage geographique HTTP 451.
"""
import asyncio
import hashlib
import hmac
import logging
import time
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode

import aiohttp

from .indicators import Candle

logger = logging.getLogger(__name__)

RECV_WINDOW = 10_000


class BinanceError(RuntimeError):
    def __init__(self, status: int, code: Optional[int], message: str):
        super().__init__(f"HTTP {status} / code {code} : {message}")
        self.status = status
        self.code = code
        self.message = message

    @property
    def is_geo_blocked(self) -> bool:
        return self.status == 451

    @property
    def is_rate_limited(self) -> bool:
        return self.status in (418, 429) or self.code == -1003

    @property
    def is_auth_error(self) -> bool:
        return self.status == 401 or self.code in (-2014, -2015, -1022)

    @property
    def is_timestamp_error(self) -> bool:
        return self.code == -1021


class BinanceClient:
    def __init__(self, api_key: str, secret_key: str, base_url: str, max_concurrency: int = 8):
        self.api_key = api_key
        self.secret_key = secret_key
        self.base_url = base_url.rstrip("/")
        self._session: Optional[aiohttp.ClientSession] = None
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._time_offset_ms = 0
        self._exchange_info: Optional[Dict[str, Any]] = None
        self._filters_cache: Dict[str, Dict[str, Any]] = {}

    # ------------------------------------------------------------------ infra

    async def __aenter__(self) -> "BinanceClient":
        await self.start()
        return self

    async def __aexit__(self, *_exc) -> None:
        await self.close()

    async def start(self) -> None:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=15),
                headers={"X-MBX-APIKEY": self.api_key} if self.api_key else {},
            )

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

    def _timestamp(self) -> int:
        return int(time.time() * 1000) + self._time_offset_ms

    def _sign(self, query: str) -> str:
        return hmac.new(self.secret_key.encode(), query.encode(), hashlib.sha256).hexdigest()

    async def _request(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        signed: bool = False,
        retries: int = 2,
    ) -> Any:
        await self.start()
        params = dict(params or {})

        for attempt in range(retries + 1):
            if signed:
                params["timestamp"] = self._timestamp()
                params["recvWindow"] = RECV_WINDOW
                query = urlencode(params)
                url = f"{self.base_url}{path}?{query}&signature={self._sign(query)}"
            else:
                query = urlencode(params)
                url = f"{self.base_url}{path}" + (f"?{query}" if query else "")

            try:
                async with self._semaphore:
                    assert self._session is not None
                    async with self._session.request(method, url) as response:
                        text = await response.text()
                        if response.status == 200:
                            return await response.json(content_type=None)

                        code, message = None, text
                        try:
                            payload = await response.json(content_type=None)
                            code = payload.get("code")
                            message = payload.get("msg", text)
                        except Exception:
                            pass

                        error = BinanceError(response.status, code, message)

                        if error.is_timestamp_error and attempt < retries:
                            await self.sync_time()
                            continue
                        if error.is_rate_limited and attempt < retries:
                            await asyncio.sleep(2 ** (attempt + 1))
                            continue
                        raise error

            except aiohttp.ClientError as exc:
                if attempt < retries:
                    await asyncio.sleep(1.5 * (attempt + 1))
                    continue
                raise BinanceError(0, None, f"erreur reseau : {exc}") from exc
            except asyncio.TimeoutError as exc:
                if attempt < retries:
                    continue
                raise BinanceError(0, None, "timeout requete Binance") from exc

        raise BinanceError(0, None, "requete Binance echouee apres retries")

    async def sync_time(self) -> int:
        """Aligne l'horloge locale sur celle de Binance."""
        local_before = int(time.time() * 1000)
        data = await self._request("GET", "/api/v3/time", retries=1)
        server_time = int(data["serverTime"])
        local_after = int(time.time() * 1000)
        latency = (local_after - local_before) // 2
        self._time_offset_ms = server_time - (local_before + latency)
        logger.info("Horloge synchronisee avec Binance (offset %d ms)", self._time_offset_ms)
        return self._time_offset_ms

    # ------------------------------------------------------------ market data

    async def ping(self) -> bool:
        await self._request("GET", "/api/v3/ping", retries=1)
        return True

    async def get_exchange_info(self, force: bool = False) -> Dict[str, Any]:
        if self._exchange_info is None or force:
            self._exchange_info = await self._request("GET", "/api/v3/exchangeInfo")
            self._filters_cache.clear()
        return self._exchange_info

    async def get_24h_tickers(self) -> List[Dict[str, Any]]:
        return await self._request("GET", "/api/v3/ticker/24hr")

    async def get_book_tickers(self) -> List[Dict[str, Any]]:
        return await self._request("GET", "/api/v3/ticker/bookTicker")

    async def get_prices(self, symbols: Optional[List[str]] = None) -> Dict[str, float]:
        """Prix de tous les symboles en une seule requete (poids 2 ou 4)."""
        data = await self._request("GET", "/api/v3/ticker/price")
        wanted = set(symbols) if symbols else None
        out: Dict[str, float] = {}
        for item in data:
            symbol = item.get("symbol")
            if wanted is not None and symbol not in wanted:
                continue
            try:
                out[symbol] = float(item["price"])
            except (KeyError, TypeError, ValueError):
                continue
        return out

    async def get_candles(self, symbol: str, interval: str, limit: int = 120) -> List[Candle]:
        rows = await self._request(
            "GET", "/api/v3/klines", {"symbol": symbol, "interval": interval, "limit": limit}
        )
        return [Candle.from_binance(row) for row in rows]

    # --------------------------------------------------------------- account

    async def get_account(self) -> Dict[str, Any]:
        """Informations de compte signees (soldes, permissions)."""
        return await self._request("GET", "/api/v3/account", signed=True)

    async def get_balances(self) -> Dict[str, float]:
        data = await self._request("GET", "/api/v3/account", signed=True)
        return {
            item["asset"]: float(item["free"])
            for item in data.get("balances", [])
            if float(item.get("free", 0)) > 0 or float(item.get("locked", 0)) > 0
        }

    async def get_free_balance(self, asset: str) -> float:
        data = await self._request("GET", "/api/v3/account", signed=True)
        for item in data.get("balances", []):
            if item["asset"] == asset:
                return float(item["free"])
        return 0.0

    async def place_market_order(self, symbol: str, side: str, quantity: str) -> Dict[str, Any]:
        return await self._request(
            "POST",
            "/api/v3/order",
            {
                "symbol": symbol,
                "side": side,
                "type": "MARKET",
                "quantity": quantity,
                "newOrderRespType": "FULL",
            },
            signed=True,
            retries=1,
        )

    # --------------------------------------------------------------- filters

    def symbol_filters(self, symbol: str) -> Optional[Dict[str, Any]]:
        """Extrait LOT_SIZE / NOTIONAL / PRICE_FILTER pour un symbole."""
        if symbol in self._filters_cache:
            return self._filters_cache[symbol]
        if not self._exchange_info:
            return None

        for info in self._exchange_info.get("symbols", []):
            if info.get("symbol") != symbol:
                continue
            filters = {f.get("filterType"): f for f in info.get("filters", [])}
            lot = filters.get("LOT_SIZE")
            if not lot:
                return None
            notional = filters.get("NOTIONAL") or filters.get("MIN_NOTIONAL") or {}
            price_filter = filters.get("PRICE_FILTER") or {}
            parsed = {
                "base_asset": info.get("baseAsset", ""),
                "quote_asset": info.get("quoteAsset", ""),
                "min_qty": float(lot.get("minQty", 0) or 0),
                "max_qty": float(lot.get("maxQty", 0) or 0),
                "step_size": float(lot.get("stepSize", 0) or 0),
                "min_notional": float(
                    notional.get("minNotional", notional.get("notional", 0)) or 0
                ),
                "tick_size": float(price_filter.get("tickSize", 0) or 0),
                "base_precision": int(info.get("baseAssetPrecision", 8)),
            }
            self._filters_cache[symbol] = parsed
            return parsed
        return None

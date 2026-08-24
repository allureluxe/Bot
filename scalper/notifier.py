"""Notifications Telegram non bloquantes.

Les envois passent par une file : si Telegram est lent ou en panne, le moteur
de trading continue de tourner. C'est optionnel : sans BOT_TOKEN ni
TELEGRAM_CHAT_ID, le bot fonctionne normalement et se contente des logs.
"""
import asyncio
import logging
from typing import Optional

import aiohttp

logger = logging.getLogger(__name__)


class TelegramNotifier:
    def __init__(self, token: str, chat_id: str, enabled: bool = True):
        self.token = token
        self.chat_id = chat_id
        self.enabled = bool(enabled and token and chat_id)
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=200)
        self._session: Optional[aiohttp.ClientSession] = None
        self._worker: Optional[asyncio.Task] = None

    async def start(self) -> None:
        if not self.enabled:
            logger.info("Notifications Telegram desactivees (token ou chat_id absent)")
            return
        self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10))
        self._worker = asyncio.create_task(self._run(), name="telegram-notifier")

    async def close(self) -> None:
        if self._worker:
            self._worker.cancel()
            try:
                await self._worker
            except asyncio.CancelledError:
                pass
            self._worker = None
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

    def send(self, text: str) -> None:
        """Depose un message dans la file. Ne bloque jamais, ne leve jamais."""
        if not self.enabled:
            return
        try:
            self._queue.put_nowait(text)
        except asyncio.QueueFull:
            logger.warning("File Telegram saturee, message ignore")

    async def _run(self) -> None:
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        while True:
            text = await self._queue.get()
            try:
                assert self._session is not None
                async with self._session.post(
                    url,
                    json={
                        "chat_id": self.chat_id,
                        "text": text,
                        "parse_mode": "HTML",
                        "disable_web_page_preview": True,
                    },
                ) as response:
                    if response.status == 429:
                        retry_after = 3
                        try:
                            payload = await response.json(content_type=None)
                            retry_after = int(payload.get("parameters", {}).get("retry_after", 3))
                        except Exception:
                            pass
                        await asyncio.sleep(retry_after)
                        self.send(text)
                    elif response.status != 200:
                        logger.warning(
                            "Telegram a repondu %s : %s", response.status, await response.text()
                        )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("Envoi Telegram echoue : %s", exc)
            finally:
                self._queue.task_done()
            # Respect de la limite Telegram (~30 messages/seconde).
            await asyncio.sleep(0.06)

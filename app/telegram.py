import asyncio
import logging
import secrets
import time
from typing import Awaitable, Callable

import httpx

from .store import Store

log = logging.getLogger(__name__)

API = "https://api.telegram.org"
PAIRING_TTL = 600
POLL_TIMEOUT = 25

Handler = Callable[[str, str], Awaitable[str]]


class Telegram:
    """Envoi des alertes et bot à commandes, configurés depuis l'interface."""

    def __init__(self, store: Store, http: httpx.AsyncClient, transport: httpx.AsyncBaseTransport | None = None):
        self.store = store
        self.http = http
        # Client séparé : la longue attente de getUpdates dépasse le timeout des autres requêtes.
        self.poll_http = httpx.AsyncClient(timeout=POLL_TIMEOUT + 10, transport=transport)
        self.handler: Handler | None = None
        self._pairing: tuple[str, float] | None = None
        self._task: asyncio.Task | None = None
        self.bot_name: str | None = None

    @property
    def token(self) -> str:
        return self.store.get("telegram_token").strip()

    @property
    def chat_id(self) -> str:
        return self.store.get("telegram_chat_id").strip()

    @property
    def configured(self) -> bool:
        return bool(self.token and self.chat_id)

    async def _call(self, client: httpx.AsyncClient, method: str, *, token: str | None = None, **params) -> dict:
        try:
            response = await client.post(f"{API}/bot{token or self.token}/{method}", json=params)
        except httpx.InvalidURL as exc:
            # Jeton avec un caractère interdit dans une URL (collé depuis un terminal) : un refus, pas une
            # panne qui remonterait jusqu'aux alertes et arrêterait le bot.
            raise TelegramError(f"jeton inutilisable ({exc})") from None
        try:
            data = response.json()
        except ValueError:
            data = {}
        if response.status_code != 200 or not data.get("ok"):
            raise TelegramError(data.get("description") or f"HTTP {response.status_code}")
        return data["result"]

    async def check_token(self, token: str | None = None) -> str:
        """Nom du bot si le jeton est valide : celui enregistré, ou un autre à essayer avant de l'enregistrer."""
        me = await self._call(self.http, "getMe", token=token)
        self.bot_name = me.get("username")
        return self.bot_name or ""

    async def send(self, text: str, chat_id: str | None = None) -> str | None:
        """None si le message est parti, sinon le motif de l'échec."""
        target = chat_id or self.chat_id
        if not self.token or not target:
            return "Telegram n'est pas configuré."
        try:
            await self._call(self.http, "sendMessage", chat_id=target, text=text, disable_web_page_preview=True)
        except TelegramError as exc:
            log.warning("Telegram a refusé le message : %s", exc)
            return f"Telegram a refusé le message : {exc}."
        except httpx.HTTPError as exc:
            log.warning("Telegram injoignable : %s", exc.__class__.__name__)
            return f"Telegram injoignable ({exc.__class__.__name__})."
        return None

    def start_pairing(self) -> str:
        code = f"{secrets.randbelow(10**6):06d}"
        self._pairing = (code, time.time() + PAIRING_TTL)
        return code

    @property
    def pairing_active(self) -> bool:
        return self._pairing_code() is not None

    def _pairing_code(self) -> str | None:
        if self._pairing and self._pairing[1] > time.time():
            return self._pairing[0]
        return None

    async def handle_update(self, update: dict) -> None:
        message = update.get("message") or {}
        text = (message.get("text") or "").strip()
        chat = str((message.get("chat") or {}).get("id", ""))
        if not text or not chat:
            return

        code = self._pairing_code()
        if code and code in text.split():
            self.store.set("telegram_chat_id", chat)
            self._pairing = None
            await self.send("Associé. Ce chat recevra les alertes de domain-watch. /aide pour les commandes.", chat)
            return

        # Seul le chat associé peut piloter l'appli : les autres messages sont ignorés sans réponse.
        if chat != self.chat_id or not text.startswith("/") or self.handler is None:
            return
        command, _, argument = text.partition(" ")
        command = command.split("@", 1)[0].lower()
        try:
            reply = await self.handler(command, argument.strip())
        except Exception:
            log.exception("Commande Telegram en échec : %s", command)
            reply = "Erreur interne, voir le journal du conteneur."
        if reply:
            await self.send(reply)

    async def poll_forever(self) -> None:
        offset = 0
        last_token = ""
        while True:
            token = self.token
            if not token:
                await asyncio.sleep(10)
                continue
            if token != last_token:
                offset, last_token = 0, token
            try:
                updates = await self._call(
                    self.poll_http, "getUpdates", offset=offset, timeout=POLL_TIMEOUT, allowed_updates=["message"]
                )
            except TelegramError as exc:
                # 401 : jeton invalide. 409 : webhook actif ou autre instance qui lit le même bot.
                log.warning("Bot Telegram : %s", exc)
                await asyncio.sleep(60)
                continue
            except httpx.HTTPError:
                await asyncio.sleep(15)
                continue
            for update in updates:
                offset = max(offset, update.get("update_id", 0) + 1)
                await self.handle_update(update)

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self.poll_forever())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
        await self.poll_http.aclose()


class TelegramError(Exception):
    pass

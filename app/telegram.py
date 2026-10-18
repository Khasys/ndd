import asyncio
import logging
import secrets
import time
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable

import httpx

from .store import Store

log = logging.getLogger(__name__)

API = "https://api.telegram.org"
PAIRING_TTL = 600
POLL_TIMEOUT = 25
# File de rejeu : premier nouvel essai après 5 min, délai doublé à chaque échec jusqu'à 6 h, abandon après 48 h.
OUTBOX_FIRST_RETRY = timedelta(minutes=5)
OUTBOX_MAX_RETRY = timedelta(hours=6)
OUTBOX_TTL = timedelta(hours=48)
MAX_TEXT = 4000  # caractères par message (limite Telegram : 4 096)

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
            raise TelegramError(data.get("description") or f"HTTP {response.status_code}", response.status_code)
        return data["result"]

    async def check_token(self, token: str | None = None) -> str:
        """Nom du bot si le jeton est valide : celui enregistré, ou un autre à essayer avant de l'enregistrer."""
        me = await self._call(self.http, "getMe", token=token)
        self.bot_name = me.get("username")
        return self.bot_name or ""

    async def send(self, text: str, chat_id: str | None = None, retry: bool = True) -> str | None:
        """None si le message est parti (ou attend en file de rejeu), sinon le motif de l'échec.

        retry : un échec passager (429, 5xx, réseau) vers le chat associé met le message en file, renvoyé
        par flush. False pour un message de test ou un rejeu, qui doivent dire tout de suite s'ils passent."""
        target = chat_id or self.chat_id
        if not self.token or not target:
            return "Telegram n'est pas configuré."
        queue = retry and chat_id is None
        try:
            await self._call(self.http, "sendMessage", chat_id=target, text=text, disable_web_page_preview=True)
        except TelegramError as exc:
            log.warning("Telegram a refusé le message : %s", exc)
            if queue and exc.transient:
                self._enqueue(text, str(exc))
                return None
            return f"Telegram a refusé le message : {exc}."
        except httpx.HTTPError as exc:
            log.warning("Telegram injoignable : %s", exc.__class__.__name__)
            if queue:
                self._enqueue(text, exc.__class__.__name__)
                return None
            return f"Telegram injoignable ({exc.__class__.__name__})."
        return None

    # File de rejeu

    def _enqueue(self, text: str, error: str) -> None:
        now = _now()
        self.store.conn.execute(
            "INSERT INTO telegram_outbox (text, created_at, attempts, next_try_at, last_error) VALUES (?, ?, 0, ?, ?)",
            (text, now.isoformat(), now.isoformat(), error[:200]),
        )
        log.warning("Telegram : message mis en file de rejeu (%s)", error)

    def pending(self) -> int:
        return self.store.conn.execute("SELECT COUNT(*) FROM telegram_outbox").fetchone()[0]

    async def flush(self) -> int:
        """Renvoie ce qui est dû dans la file, regroupé en un seul message. Nombre de messages partis."""
        conn = self.store.conn
        now = _now()
        for row in conn.execute("SELECT * FROM telegram_outbox WHERE created_at < ?", ((now - OUTBOX_TTL).isoformat(),)):
            log.error("Telegram : message abandonné après %s essais depuis le %s (%s) : %s",
                      row["attempts"], row["created_at"][:16], row["last_error"], row["text"][:120].replace("\n", " "))
            conn.execute("DELETE FROM telegram_outbox WHERE id = ?", (row["id"],))
        if not self.configured:
            return 0
        rows = conn.execute("SELECT * FROM telegram_outbox WHERE next_try_at <= ? ORDER BY id", (now.isoformat(),)).fetchall()
        if not rows:
            return 0
        batch, text = [], ""
        for row in rows:
            joined = f"{text}\n\n{row['text']}" if text else row["text"]
            if batch and len(joined) > MAX_TEXT:
                break  # le reste partira au passage suivant
            batch.append(row)
            text = joined
        error = await self.send(text[:MAX_TEXT], retry=False)
        if error is None:
            conn.executemany("DELETE FROM telegram_outbox WHERE id = ?", [(r["id"],) for r in batch])
            log.info("Telegram : %d message(s) de la file renvoyé(s)", len(batch))
            return len(batch)
        for row in batch:
            attempts = row["attempts"] + 1
            delay = min(OUTBOX_MAX_RETRY, OUTBOX_FIRST_RETRY * (2 ** (attempts - 1)))
            conn.execute("UPDATE telegram_outbox SET attempts = ?, next_try_at = ?, last_error = ? WHERE id = ?",
                         (attempts, (now + delay).isoformat(), error[:200], row["id"]))
        return 0

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
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status

    @property
    def transient(self) -> bool:
        """Refus passager (trop de requêtes, panne côté Telegram) : le message mérite un nouvel essai."""
        return self.status == 429 or (self.status or 0) >= 500


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)

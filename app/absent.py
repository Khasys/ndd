"""Noms absents d'une zone ICANN (.com, .net, .org…) : leur état réel, demandé au registre (RDAP).

Absent de la zone ne veut pas dire libre : un nom en rédemption, en suppression, suspendu (hold) ou enregistré sans
serveurs DNS n'y figure pas non plus. Les listes ajoutées après coup (composés, inventés) en ont des dizaines de
milliers, que seul le bouton Vérifier tranchait. Cette passe les vérifie tous, les meilleurs scores d'abord : la
réponse va dans la Veille (drops) avec l'origine « rdap », comme celle d'une vérification à la main, mais sans entrer
dans la Veille, les alertes ni le quotidien (drops.QUIET) : ce n'est pas une libération constatée. L'Explorer, la fiche
et les pépites lisent l'état comme pour un nom vérifié à la main. Gandi ne passe ensuite que sur les libres des
extensions à premiums (gandi.NightPass) : un .com libre au registre est achetable au prix normal.

Rythme : la limite des registres n'est pas publiée. La passe la cherche : elle part de START_RATE requêtes par seconde
(PARALLEL en vol au plus), accélère de GROW tous les GROW_EVERY succès d'affilée, jusqu'à MAX_RATE, et divise son
rythme par deux au premier refus (429, 403, 5xx, réseau), avec une pause (Retry-After, sinon PAUSE). Le rythme appris
est gardé (réglage absent_pass_rate). Client HTTP à part : la Veille et Vérifier gardent leur file (RDAP_MIN_DELAY).
Un nom vu enregistré est revu après RECHECK_DAYS jours : il peut entrer en suppression sans revenir dans la zone.
"""

import asyncio
import logging
import time
from datetime import datetime, timedelta

import httpx

from . import ccdns, drops, rdapinfo
from .checker import iso, utcnow
from .rdap import LookupResult, _retry_after, parse_domain

log = logging.getLogger(__name__)

ORIGIN = "rdap"
START_RATE = 3.0          # requêtes par seconde au départ
MIN_RATE = 0.5
MAX_RATE = 25.0
GROW = 1.25
GROW_EVERY = 500          # succès d'affilée avant d'accélérer
PARALLEL = 16             # requêtes en vol au plus : un registre met 0,2 à 1 s à répondre
PAUSE = 120               # pause après un refus sans Retry-After, en secondes
MAX_PAUSE = 1800
QUEUE = 20_000            # file calculée d'un coup
QUEUE_SECONDS = 3600      # file recalculée au plus tard après ce délai (zone relue, listes changées)
RECHECK_DAYS = 30
TIMEOUT = 20


class Refused(Exception):
    """Le registre limite, refuse ou ne répond pas : rien n'est conclu sur le nom."""

    def __init__(self, reason: str, retry_after: float | None = None):
        super().__init__(reason)
        self.retry_after = retry_after


class AbsentPass:
    key = "absent_pass"

    def __init__(self, explorer, store, http_factory=None, servers=None, sleep=None):
        self.explorer = explorer
        self.store = store
        self._http_factory = http_factory or (lambda: httpx.AsyncClient(
            timeout=TIMEOUT, follow_redirects=True,
            headers={"Accept": "application/rdap+json, application/json", "User-Agent": "domain-watch"},
            limits=httpx.Limits(max_connections=PARALLEL, max_keepalive_connections=PARALLEL)))
        self._servers = servers  # async (nom) -> adresse du serveur RDAP ; par défaut l'annuaire de la Veille
        self.sleep = sleep or asyncio.sleep  # remplaçable dans les tests
        self.running = False
        self.stop = False
        self._queue: list[str] = []
        self._queue_at = 0.0
        self._ok_run = 0
        self._pause_end = 0.0  # time.monotonic() de fin de la pause en cours
        self.last_error: str | None = None
        self.paused_until: str | None = None
        self.today: dict = {}

    # --- Réglages ---------------------------------------------------------------------

    @property
    def enabled(self) -> bool:
        return self.store.get(f"{self.key}_enabled", "1") == "1"

    @property
    def rate(self) -> float:
        try:
            value = float(self.store.get(f"{self.key}_rate") or START_RATE)
        except ValueError:
            value = START_RATE
        return min(MAX_RATE, max(MIN_RATE, value))

    def _set_rate(self, value: float) -> None:
        self.store.set(f"{self.key}_rate", f"{min(MAX_RATE, max(MIN_RATE, value)):.2f}")

    def tlds(self) -> list[str]:
        """Extensions lues dans une zone ICANN : ni le .fr (open data et DNS), ni .co/.ai/.io (DNS seulement)."""
        return [t for t in self.explorer.tlds() if t != "fr" and t not in ccdns.TLDS]

    # --- File -------------------------------------------------------------------------

    def pending(self, limit: int, now: datetime | None = None) -> list[str]:
        """Noms absents de la zone jamais demandés au registre, du meilleur score de base au moins bon, toutes extensions
        mêlées ; puis ceux vus enregistrés par cette passe il y a plus de RECHECK_DAYS jours. Seuls les noms qui ont un
        sens, et pas les noms de 1 ou 2 caractères (gardés par les registres)."""
        from .explore import ABSENT  # import local : explore importe drops, qui ne connaît pas ce module

        now = now or utcnow()
        ex = self.explorer
        tlds = self.tlds()
        if not tlds:
            return []
        u = ex.universe
        buckets = u.buckets()
        data = {t: ex.status_bytes(t) for t in tlds}
        known = {r[0] for r in ex.conn.execute(
            f"SELECT name FROM drops WHERE tld IN ({','.join('?' * len(tlds))})", tlds)}
        picked: list[str] = []
        for score in range(len(buckets) - 1, -1, -1):
            for i in buckets[score]:
                if not u.good[i]:
                    continue
                for tld in tlds:
                    if data[tld][i] != ABSENT:
                        continue
                    label = u.label(i)
                    name = f"{label}.{tld}"
                    if len(label) <= 2 or name in known:
                        continue
                    picked.append(name)
                    if len(picked) >= limit:
                        return picked
        stale = iso(now - timedelta(days=RECHECK_DAYS))
        picked += [r[0] for r in ex.conn.execute(
            "SELECT name FROM drops WHERE origin = ? AND state = ? AND checked_at < ? ORDER BY checked_at LIMIT ?",
            (ORIGIN, drops.TAKEN, stale, limit - len(picked)))]
        return picked

    # --- Requête et enregistrement ------------------------------------------------------

    async def _server(self, name: str) -> str | None:
        if self._servers is not None:
            return await self._servers(name)
        return await self.explorer.drops.rdap.server_for(name)

    async def fetch(self, http, name: str) -> LookupResult:
        """Une requête RDAP : 404 libre, 200 enregistré. Refused : le registre limite, refuse ou ne répond pas."""
        base = await self._server(name)
        if not base:
            raise Refused(f"pas de serveur RDAP pour {name}")
        try:
            response = await http.get(f"{base.rstrip('/')}/domain/{name}")
        except httpx.HTTPError as exc:
            raise Refused(f"Erreur réseau : {exc.__class__.__name__}") from None
        if response.status_code == 404:
            return LookupResult(available=True)
        if response.status_code == 200:
            try:
                return LookupResult(found=True, info=parse_domain(response.json()))
            except ValueError:
                raise Refused("Réponse RDAP illisible") from None
        raise Refused(f"Réponse {response.status_code} du registre", _retry_after(response))

    def record(self, name: str, result: LookupResult) -> str | None:
        """La réponse du registre, écrite comme une vérification à la main, avec l'origine rdap. None : nom passé
        dans la Veille ou vérifié à la main entre-temps, rien d'écrit."""
        ex = self.explorer
        watcher = ex.drops
        row = watcher.row(name)
        if row is not None and row["origin"] != ORIGIN:
            return None
        rdapinfo.save(ex.conn, name, result)
        now = utcnow()
        row = row or watcher.ensure(name, now, origin=ORIGIN)
        return watcher.apply(row, result, now)

    def _count(self, kind: str) -> None:
        day = utcnow().date().isoformat()
        if self.today.get("day") != day:
            self.today = {"day": day, "checked": 0, "refused": 0, "kinds": {}}
        if kind == "refused":
            self.today["refused"] += 1
        else:
            self.today["checked"] += 1
            self.today["kinds"][kind] = self.today["kinds"].get(kind, 0) + 1

    async def _one(self, http, name: str) -> None:
        try:
            result = await self.fetch(http, name)
        except Refused as exc:
            self._queue.insert(0, name)  # redemandé après la pause
            self._count("refused")
            if time.monotonic() >= self._pause_end:  # une salve de refus : un seul ralentissement
                self._ok_run = 0
                self._set_rate(self.rate / 2)
                pause = min(MAX_PAUSE, exc.retry_after or PAUSE)
                self._pause_end = time.monotonic() + pause
                self.last_error = str(exc)
                self.paused_until = iso(utcnow() + timedelta(seconds=pause))
                log.info("Passe des absents : %s, rythme ramené à %.2f/s, pause %d s", exc, self.rate, pause)
            return
        try:
            state = self.record(name, result)
        except Exception:
            log.exception("Passe des absents : %s non enregistré", name)
            return
        self._count(state or "skipped")
        self._ok_run += 1
        if self._ok_run >= GROW_EVERY:
            self._ok_run = 0
            self._set_rate(self.rate * GROW)

    # --- Passe ------------------------------------------------------------------------

    async def run(self, off, busy=lambda: False, limit: int | None = None) -> dict:
        """Tourne jusqu'à vider la file (ou limit requêtes). off(fonction, *args) calcule la file hors de la boucle
        (main._off) ; busy() vrai pendant la lecture des zones, qui occupe le NAS : la passe attend."""
        if self.running or not self.enabled:
            return {"started": False}
        self.running, self.stop = True, False
        asked = 0
        flight: set[asyncio.Task] = set()
        try:
            async with self._http_factory() as http:
                while not self.stop and self.enabled and (limit is None or asked < limit):
                    if busy():
                        await self.sleep(60)
                        continue
                    wait = self._pause_end - time.monotonic()
                    if wait > 0:
                        await _drain(flight)
                        await self.sleep(wait)
                        continue
                    if not self._queue or time.time() - self._queue_at > QUEUE_SECONDS:
                        await _drain(flight)
                        if not self._queue or time.time() - self._queue_at > QUEUE_SECONDS:
                            self._queue = await off(self.pending, QUEUE)
                            self._queue_at = time.time()
                        if not self._queue:
                            break
                        continue
                    if len(flight) >= PARALLEL:
                        await asyncio.wait(flight, return_when=asyncio.FIRST_COMPLETED)
                        continue
                    name = self._queue.pop(0)
                    asked += 1
                    task = asyncio.create_task(self._one(http, name))
                    flight.add(task)
                    task.add_done_callback(flight.discard)
                    await self.sleep(1 / self.rate)
                await _drain(flight)
        finally:
            self.running = False
        return {"started": True, "asked": asked, "queued": len(self._queue), "rate": self.rate}

    # --- Suivi ------------------------------------------------------------------------

    def totals(self) -> dict:
        rows = self.explorer.conn.execute(
            "SELECT state, COUNT(*) FROM drops WHERE origin = ? GROUP BY state", (ORIGIN,)).fetchall()
        return {r[0] or "pending": r[1] for r in rows}

    def status(self) -> dict:
        return {"enabled": self.enabled, "running": self.running, "tlds": self.tlds(), "rate": self.rate,
                "max_rate": MAX_RATE, "queued": len(self._queue), "today": self.today, "last_error": self.last_error,
                "paused_until": self.paused_until, "totals": self.totals()}


async def _drain(flight: set) -> None:
    if flight:
        await asyncio.gather(*list(flight), return_exceptions=True)

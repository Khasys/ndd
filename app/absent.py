"""Noms absents d'une zone ICANN (.com, .net, .org…) : leur état réel, demandé au registre (RDAP).

Absent de la zone ne veut pas dire libre : un nom en rédemption, en suppression, suspendu (hold) ou enregistré sans
serveurs DNS n'y figure pas non plus. Les listes ajoutées après coup (composés, inventés) en ont des dizaines de
milliers, que seul le bouton Vérifier tranchait. Cette passe les vérifie tous, les meilleurs scores d'abord : la
réponse va dans la Veille (drops) avec l'origine « rdap », comme celle d'une vérification à la main, mais sans entrer
dans la Veille, les alertes ni le quotidien (drops.QUIET) : ce n'est pas une libération constatée. L'Explorer, la fiche
et les pépites lisent l'état comme pour un nom vérifié à la main. Gandi ne passe ensuite que sur les libres des
extensions à premiums (gandi.NightPass) : un .com libre au registre est achetable au prix normal.

Rythme, registre par registre (serveur RDAP : Verisign pour .com et .net, PIR pour .org…) : leur limite n'est pas
publiée. La passe la cherche : elle part de START_RATE requêtes par seconde (PARALLEL en vol au plus, tous registres
confondus), accélère de GROW tous les GROW_EVERY succès d'affilée, jusqu'à MAX_RATE, et divise le rythme du registre
par deux au premier refus, avec une pause : Retry-After, sinon PAUSE (429, 5xx, réseau) ou BLOCKED_PAUSE (403, accès
refusé : insister aggraverait le blocage). Une coupure réseau isolée ne fait qu'une courte pause (NET_PAUSE). Un
registre qui refuse tout depuis le matin (SKIP_AFTER refus, aucune réponse) est écarté jusqu'au lendemain. Un registre
en pause n'arrête pas les autres. Le rythme appris est gardé (réglage absent_pass_rates). Client HTTP à part : la Veille et Vérifier gardent leur file (RDAP_MIN_DELAY).
Un nom vu enregistré est revu après RECHECK_DAYS jours : il peut entrer en suppression sans revenir dans la zone.
"""

import asyncio
import collections
import json
import logging
import time
from datetime import datetime, timedelta
from urllib.parse import urlsplit

import httpx

from . import ccdns, drops, rdapinfo
from .checker import iso, utcnow
from .rdap import LookupResult, _retry_after, parse_domain

log = logging.getLogger(__name__)

ORIGIN = "rdap"
START_RATE = 1.0          # requêtes par seconde au départ, par registre (429 vu à 3/s, 403 ensuite, le 24/10)
MIN_RATE = 0.5
MAX_RATE = 25.0
GROW = 1.25
GROW_EVERY = 500          # succès d'affilée avant d'accélérer
PARALLEL = 16             # requêtes en vol au plus : un registre met 0,2 à 1 s à répondre
PAUSE = 120               # pause après un refus sans Retry-After, en secondes
BLOCKED_PAUSE = 1800      # 403 : accès refusé, pause longue
NET_PAUSE = 30            # coupure réseau isolée : courte pause, rythme gardé
NET_STRIKES = 3           # coupures d'affilée avant de ralentir comme pour un refus
SKIP_AFTER = 3            # refus d'affilée sans aucune réponse du jour : registre écarté jusqu'au lendemain (.biz, 24/10)
MAX_PAUSE = 3600
QUEUE = 10_000            # file calculée d'un coup, par extension
# Ordre des extensions dans la file d'un même registre (Verisign : le .com avant le .net) ; les autres ensuite.
FIRST = ("com", "net", "org")
QUEUE_SECONDS = 3600      # file recalculée au plus tard après ce délai (zone relue, listes changées)
RECHECK_DAYS = 30
TIMEOUT = 20
RATES_KEY = "absent_pass_rates"


class Refused(Exception):
    """Le registre limite, refuse ou ne répond pas : rien n'est conclu sur le nom."""

    def __init__(self, reason: str, retry_after: float | None = None, status: int | None = None):
        super().__init__(reason)
        self.retry_after = retry_after
        self.status = status


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
        self.sleep = sleep or asyncio.sleep  # remplaçables dans les tests
        self.clock = time.monotonic
        self.running = False
        self.stop = False
        self._queues: dict[str, collections.deque] = {}  # serveur -> noms à demander
        self._queue_at = 0.0
        self._hosts: dict[str, dict] = {}  # serveur -> succès d'affilée, pause, prochain départ, refus, dernière erreur
        self.today: dict = {}

    # --- Réglages ---------------------------------------------------------------------

    @property
    def enabled(self) -> bool:
        return self.store.get(f"{self.key}_enabled", "1") == "1"

    def _rates(self) -> dict[str, float]:
        try:
            return json.loads(self.store.get(RATES_KEY) or "{}")
        except ValueError:
            return {}

    def rate_of(self, host: str) -> float:
        try:
            value = float(self._rates().get(host, START_RATE))
        except (TypeError, ValueError):
            value = START_RATE
        return min(MAX_RATE, max(MIN_RATE, value))

    def _set_rate(self, host: str, value: float) -> None:
        rates = self._rates()
        rates[host] = round(min(MAX_RATE, max(MIN_RATE, value)), 2)
        self.store.set(RATES_KEY, json.dumps(rates))

    def _host(self, host: str) -> dict:
        state = self._hosts.setdefault(host, {"ok_run": 0, "pause_end": 0.0, "next_at": 0.0, "refused": 0,
                                              "checked": 0, "last_error": None, "paused_until": None,
                                              "strikes": 0, "net_strikes": 0, "day": None, "answered": 0, "off_day": None})
        day = utcnow().date().isoformat()
        if state["day"] != day:  # chaque jour, un registre écarté la veille a de nouveau sa chance
            state.update(day=day, answered=0, strikes=0, off_day=None if state["off_day"] != day else day)
        return state

    def _off_today(self, host: str) -> bool:
        return self._host(host)["off_day"] == utcnow().date().isoformat()

    def tlds(self) -> list[str]:
        """Extensions lues dans une zone ICANN : ni le .fr (open data et DNS), ni .co/.ai/.io (DNS seulement)."""
        return [t for t in self.explorer.tlds() if t != "fr" and t not in ccdns.TLDS]

    # --- File -------------------------------------------------------------------------

    def pending(self, limit: int, now: datetime | None = None) -> list[str]:
        """Noms absents de la zone jamais demandés au registre, limit au plus par extension, du meilleur score de base au
        moins bon ; puis ceux vus enregistrés par cette passe il y a plus de RECHECK_DAYS jours. Extension par extension,
        le .com d'abord (FIRST) : mêlées, un bon mot étant presque toujours pris en .com, le .com passait après tous les
        autres (5 .com vérifiés sur 23 466 noms le 24/10). Seuls les noms qui ont un sens, et pas les noms de 1 ou 2
        caractères (gardés par les registres)."""
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
        picked: dict[str, list[str]] = {t: [] for t in tlds}
        open_tlds = list(tlds)
        for score in range(len(buckets) - 1, -1, -1):
            if not open_tlds:
                break
            for i in buckets[score]:
                if not u.good[i]:
                    continue
                for tld in open_tlds:
                    if data[tld][i] != ABSENT:
                        continue
                    label = u.label(i)
                    name = f"{label}.{tld}"
                    if len(label) <= 2 or name in known:
                        continue
                    picked[tld].append(name)
                if any(len(picked[t]) >= limit for t in open_tlds):
                    open_tlds = [t for t in open_tlds if len(picked[t]) < limit]
                    if not open_tlds:
                        break
        stale = iso(now - timedelta(days=RECHECK_DAYS))
        for name, tld in ex.conn.execute(
                "SELECT name, tld FROM drops WHERE origin = ? AND state = ? AND checked_at < ? ORDER BY checked_at",
                (ORIGIN, drops.TAKEN, stale)):
            if tld in picked and len(picked[tld]) < limit:
                picked[tld].append(name)
        order = sorted(tlds, key=lambda t: (FIRST.index(t) if t in FIRST else len(FIRST), t))
        return [name for t in order for name in picked[t]]

    async def _server(self, name: str) -> str | None:
        if self._servers is not None:
            return await self._servers(name)
        return await self.explorer.drops.rdap.server_for(name)

    async def _fill(self, off) -> None:
        """File du moment, rangée par serveur RDAP (un registre en pause n'arrête pas les autres)."""
        names = await off(self.pending, QUEUE)
        bases: dict[str, str | None] = {}
        queues: dict[str, collections.deque] = {}
        for name in names:
            tld = name.rsplit(".", 1)[1]
            if tld not in bases:
                bases[tld] = await self._server(name)
            if bases[tld] and not self._off_today(bases[tld]):
                queues.setdefault(bases[tld], collections.deque()).append(name)
        self._queues = queues
        self._queue_at = time.time()

    def queued(self) -> int:
        return sum(len(q) for q in self._queues.values())

    # --- Requête et enregistrement ------------------------------------------------------

    async def fetch(self, http, base: str, name: str) -> LookupResult:
        """Une requête RDAP : 404 libre, 200 enregistré. Refused : le registre limite, refuse ou ne répond pas."""
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
        raise Refused(f"Réponse {response.status_code} du registre", _retry_after(response), response.status_code)

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

    async def _one(self, http, base: str, name: str) -> None:
        state = self._host(base)
        try:
            result = await self.fetch(http, base, name)
        except Refused as exc:
            self._queues.setdefault(base, collections.deque()).appendleft(name)  # redemandé après la pause
            self._count("refused")
            state["refused"] += 1
            if self.clock() < state["pause_end"]:
                return  # une salve de refus : un seul ralentissement
            state["last_error"] = str(exc)
            host = urlsplit(base).netloc
            if exc.status is None and state["net_strikes"] + 1 < NET_STRIKES:
                # Coupure réseau isolée : le registre n'a rien refusé, courte pause sans ralentir.
                state["net_strikes"] += 1
                pause = NET_PAUSE
            else:
                state["net_strikes"] = 0
                state["strikes"] += 1
                state["ok_run"] = 0
                if state["strikes"] >= SKIP_AFTER and not state["answered"]:
                    # Refuse tout depuis ce matin (le .biz, 403 dès la première requête) : écarté jusqu'à demain,
                    # ses noms restent « à vérifier ».
                    state["off_day"] = state["day"]
                    self._queues.pop(base, None)
                    log.warning("Passe des absents : %s refuse tout (%s), écarté jusqu'à demain", host, exc)
                    return
                self._set_rate(base, self.rate_of(base) / 2)
                pause = min(MAX_PAUSE, exc.retry_after or (BLOCKED_PAUSE if exc.status == 403 else PAUSE))
            state["pause_end"] = self.clock() + pause
            state["paused_until"] = iso(utcnow() + timedelta(seconds=pause))
            log.warning("Passe des absents : %s refuse (%s), rythme %.2f/s, pause %d s", host, exc, self.rate_of(base), pause)
            return
        try:
            kind = self.record(name, result)
        except Exception:
            log.exception("Passe des absents : %s non enregistré", name)
            return
        self._count(kind or "skipped")
        state["checked"] += 1
        state["answered"] += 1
        state["strikes"] = state["net_strikes"] = 0
        state["ok_run"] += 1
        if state["ok_run"] >= GROW_EVERY:
            state["ok_run"] = 0
            self._set_rate(base, self.rate_of(base) * GROW)

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
                    if not self.queued() or time.time() - self._queue_at > QUEUE_SECONDS:
                        await _drain(flight)
                        if not self.queued() or time.time() - self._queue_at > QUEUE_SECONDS:
                            await self._fill(off)
                        if not self.queued():
                            break
                        continue
                    if len(flight) >= PARALLEL:
                        await asyncio.wait(flight, return_when=asyncio.FIRST_COMPLETED)
                        continue
                    now = self.clock()
                    waiting = [b for b, q in self._queues.items() if q]
                    ready = [b for b in waiting if max(self._host(b)["pause_end"], self._host(b)["next_at"]) <= now]
                    if not ready:
                        # Tous les registres en attente (pause ou cadence) : jusqu'au premier qui reprend.
                        soonest = min(max(self._host(b)["pause_end"], self._host(b)["next_at"]) for b in waiting)
                        if soonest - now > 1:
                            await _drain(flight)  # un refus en vol peut repousser la reprise
                        await self.sleep(max(0.01, soonest - now))
                        continue
                    for base in ready:
                        if len(flight) >= PARALLEL or (limit is not None and asked >= limit):
                            break
                        name = self._queues[base].popleft()
                        self._host(base)["next_at"] = now + 1 / self.rate_of(base)
                        asked += 1
                        task = asyncio.create_task(self._one(http, base, name))
                        flight.add(task)
                        task.add_done_callback(flight.discard)
                    await self.sleep(0)
                await _drain(flight)
        finally:
            self.running = False
        return {"started": True, "asked": asked, "queued": self.queued()}

    # --- Suivi ------------------------------------------------------------------------

    def totals(self) -> dict:
        rows = self.explorer.conn.execute(
            "SELECT state, COUNT(*) FROM drops WHERE origin = ? GROUP BY state", (ORIGIN,)).fetchall()
        return {r[0] or "pending": r[1] for r in rows}

    def status(self) -> dict:
        hosts = {}
        for base in sorted(set(self._hosts) | set(self._rates()) | set(self._queues)):
            state = self._host(base)
            hosts[urlsplit(base).netloc or base] = {
                "rate": self.rate_of(base), "queued": len(self._queues.get(base, ())), "checked": state["checked"],
                "refused": state["refused"], "last_error": state["last_error"],
                "paused_until": state["paused_until"] if self.clock() < state["pause_end"] else None,
                "off_today": self._off_today(base)}
        return {"enabled": self.enabled, "running": self.running, "tlds": self.tlds(), "max_rate": MAX_RATE,
                "queued": self.queued(), "today": self.today, "registries": hosts, "totals": self.totals()}


async def _drain(flight: set) -> None:
    if flight:
        await asyncio.gather(*list(flight), return_exceptions=True)

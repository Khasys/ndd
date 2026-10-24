"""Noms absents d'une zone ICANN (.com, .net, .org…) : leur état réel, demandé au registre (RDAP).

Absent de la zone ne veut pas dire libre : un nom en rédemption, en suppression, suspendu (hold) ou enregistré sans
serveurs DNS n'y figure pas non plus. Les listes ajoutées après coup (composés, inventés) en ont des dizaines de
milliers, que seul le bouton Vérifier tranchait. Cette passe les vérifie au fil de la journée, les meilleurs scores
d'abord, par la même requête que Vérifier : la réponse va dans la Veille (drops) avec l'origine « rdap », comme celle
d'une vérification à la main, mais sans entrer dans la Veille, les alertes ni le quotidien (drops.QUIET) : ce n'est pas
une libération constatée. L'Explorer, la fiche et les pépites lisent l'état comme pour un nom vérifié à la main.

Rythme : BATCH noms toutes les 5 minutes au plus, sous la cadence par serveur du client RDAP (RDAP_MIN_DELAY), partagée
avec la Veille et Vérifier. Une erreur du registre (limite de requêtes, panne) arrête le lot ; le suivant reprend.
Un nom vu enregistré est revu après RECHECK_DAYS jours : il peut entrer en suppression sans revenir dans la zone.
"""

import logging
from datetime import datetime, timedelta

from . import ccdns, drops
from .checker import iso, utcnow

log = logging.getLogger(__name__)

ORIGIN = "rdap"
BATCH = 100               # noms par passage de 5 minutes (200 s à 2 s par requête) : 1 200 par heure
QUEUE = 2_000             # file calculée d'un coup, consommée lot après lot
QUEUE_SECONDS = 3600      # file recalculée au plus tard après ce délai (zone relue, listes changées)
RECHECK_DAYS = 30
ERROR_STOP = 3            # erreurs d'affilée avant d'arrêter le lot


class AbsentPass:
    key = "absent_pass"

    def __init__(self, explorer, store):
        self.explorer = explorer
        self.store = store
        self.running = False
        self._queue: list[str] = []
        self._queue_at = 0.0
        self.last: dict | None = None
        self.today: dict = {}
        self._last_error: str | None = None

    @property
    def enabled(self) -> bool:
        return self.store.get(f"{self.key}_enabled", "1") == "1"

    def tlds(self) -> list[str]:
        """Extensions lues dans une zone ICANN : ni le .fr (open data et DNS), ni .co/.ai/.io (DNS seulement)."""
        return [t for t in self.explorer.tlds() if t != "fr" and t not in ccdns.TLDS]

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

    async def run(self, off, limit: int = BATCH) -> dict:
        """Un lot : off(fonction, *args) calcule la file hors de la boucle (main._off)."""
        if self.running or not self.enabled:
            return {"started": False}
        self.running = True
        stats = {"started_at": iso(utcnow()), "checked": 0, "kinds": {}, "error": None}
        try:
            if not self._queue or utcnow().timestamp() - self._queue_at > QUEUE_SECONDS:
                self._queue = await off(self.pending, QUEUE)
                self._queue_at = utcnow().timestamp()
            errors = 0
            while self._queue and stats["checked"] < limit:
                name = self._queue.pop(0)
                state = await self.check(name)
                if state is None:
                    errors += 1
                    stats["error"] = self._last_error
                    if errors >= ERROR_STOP:
                        break
                    continue
                errors = 0
                stats["checked"] += 1
                stats["kinds"][state] = stats["kinds"].get(state, 0) + 1
        finally:
            self.running = False
            stats["finished_at"] = iso(utcnow())
            stats["queued"] = len(self._queue)
            self.last = stats
            self._count(stats)
        return stats

    async def check(self, name: str) -> str | None:
        """La requête de Vérifier, enregistrée dans la Veille avec l'origine rdap. None : erreur, rien d'écrit."""
        from . import rdapinfo

        ex = self.explorer
        watcher = ex.drops
        row = watcher.row(name)
        if row is not None and row["origin"] != ORIGIN:
            return "skipped"  # suivi par la Veille ou vérifié à la main entre-temps
        result = await watcher.rdap.lookup(name)
        if result.error:
            self._last_error = result.error
            return None
        rdapinfo.save(ex.conn, name, result)
        now = utcnow()
        row = row or watcher.ensure(name, now, origin=ORIGIN)
        return watcher.apply(row, result, now)

    def _count(self, stats: dict) -> None:
        day = utcnow().date().isoformat()
        if self.today.get("day") != day:
            self.today = {"day": day, "checked": 0, "kinds": {}}
        self.today["checked"] += stats["checked"]
        for kind, n in stats["kinds"].items():
            self.today["kinds"][kind] = self.today["kinds"].get(kind, 0) + n

    def totals(self) -> dict:
        rows = self.explorer.conn.execute(
            "SELECT state, COUNT(*) FROM drops WHERE origin = ? GROUP BY state", (ORIGIN,)).fetchall()
        return {r[0] or "pending": r[1] for r in rows}

    def status(self) -> dict:
        return {"enabled": self.enabled, "running": self.running, "tlds": self.tlds(), "batch": BATCH,
                "queued": len(self._queue), "last": self.last, "today": self.today, "totals": self.totals()}

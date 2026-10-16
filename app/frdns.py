"""Présence DNS des .fr « probablement libres » : la fraîcheur d'une zone quotidienne, pour nos noms.

L'Afnic ne publie pas la zone .fr chaque jour ; le fichier open data a plusieurs semaines de retard.
Un .fr enregistré a presque toujours des serveurs DNS : on demande directement aux serveurs du .fr
(sans résolveur intermédiaire, donc seule l'Afnic voit les requêtes) si le nom y est délégué.

- délégué (réponse avec des serveurs) : le nom est pris, l'Explorer l'affiche « Enregistré » ;
- inconnu (NXDOMAIN) : il reste « probablement libre », avec la date de la vérification.

Limite : un nom enregistré sans serveurs DNS passe pour libre ; le RDAP (bouton Vérifier) tranche.
Pas de quota RDAP consommé. Rythme bas et adaptatif pour ne déclencher aucune limite côté Afnic.
"""

import asyncio
import json
import logging
import random
from datetime import datetime, timedelta

import dns.asyncquery
import dns.asyncresolver
import dns.exception
import dns.flags
import dns.message
import dns.rcode
import dns.rdatatype

from .checker import iso, utcnow

log = logging.getLogger(__name__)

NIGHTLY = 30_000          # noms par nuit
RATE = 3.0                # requêtes par seconde au départ (divisé par deux si l'Afnic ralentit)
MIN_RATE = 0.5
START_HOUR = 0            # heure locale de départ : bien avant la lecture des zones (3 h) et la notation IA (5 h)
FRESH_DAYS_TOP = 1        # noms à fort score de base : revus chaque nuit
FRESH_DAYS_OTHER = 7      # les autres : chaque semaine
TOP_BASE = 40
TIMEOUT = 3.0


def present(conn) -> list[str]:
    return [r[0] for r in conn.execute("SELECT label FROM name_dns WHERE present = 1")]


def load(conn, labels: list[str]) -> dict[str, dict]:
    found = {}
    for chunk in range(0, len(labels), 900):
        part = labels[chunk:chunk + 900]
        rows = conn.execute(f"SELECT * FROM name_dns WHERE label IN ({','.join('?' * len(part))})", part)
        found.update({r["label"]: {"present": bool(r["present"]), "at": r["checked_at"]} for r in rows})
    return found


class FrDns:
    def __init__(self, conn, store, explorer, local_zone):
        self.conn = conn
        self.store = store
        self.explorer = explorer
        self.local_zone = local_zone
        self.running = False
        self.servers: list[str] = []
        self.query = self._query  # remplaçable dans les tests

    # --- File ---------------------------------------------------------------------

    def due(self, now_local: datetime) -> bool:
        return (self.store.get("fr_dns_enabled", "1") == "1" and now_local.hour >= START_HOUR
                and self.store.get("fr_dns_last") != now_local.date().isoformat() and "fr" in self.explorer.tlds())

    def pending(self, limit: int, now: datetime | None = None) -> list[str]:
        """Les .fr probablement libres à revoir, du meilleur score de base au moins bon.
        Fort score : revu chaque nuit ; les autres : chaque semaine. Un nom vu délégué par le DNS
        reste dans la file : s'il disparaît du DNS, il redevient « probablement libre »."""
        from .explore import ABSENT, GONE, REGISTERED  # import local : explore n'importe pas ce module

        now = now or utcnow()
        u = self.explorer.universe
        buckets = u.buckets()
        base = self.explorer.base_bytes("fr")
        data = self.explorer.status_bytes("fr")
        seen = {r["label"]: r["checked_at"] for r in self.conn.execute("SELECT label, checked_at FROM name_dns")}
        top_after = iso(now - timedelta(days=FRESH_DAYS_TOP) + timedelta(hours=2))
        other_after = iso(now - timedelta(days=FRESH_DAYS_OTHER) + timedelta(hours=2))
        picked = []
        for score in range(len(buckets) - 1, -1, -1):
            fresh_after = top_after if score >= TOP_BASE else other_after
            for i in buckets[score]:
                if not u.good[i] or base[i] not in (GONE, ABSENT) or data[i] not in (GONE, ABSENT, REGISTERED):
                    continue
                label = u.label(i)
                if base[i] == ABSENT and len(label) <= 2:
                    continue  # formats réservés par le registre
                if seen.get(label, "") > fresh_after:
                    continue
                picked.append(label)
                if len(picked) >= limit:
                    return picked
        return picked

    # --- Requêtes -------------------------------------------------------------------

    async def _servers(self) -> list[str]:
        """Adresses des serveurs du .fr, lues dans le DNS (rien d'écrit en dur)."""
        if self.servers:
            return self.servers
        resolver = dns.asyncresolver.Resolver()
        names = [str(r.target) for r in await resolver.resolve("fr.", "NS")]
        addresses = []
        for name in names:
            try:
                addresses += [r.address for r in await resolver.resolve(name, "A")]
            except dns.exception.DNSException:
                continue
        if not addresses:
            raise RuntimeError("serveurs du .fr introuvables")
        self.servers = addresses
        return addresses

    async def _query(self, label: str) -> bool | None:
        """True : délégué ; False : inconnu ; None : pas de réponse exploitable (on réessaiera)."""
        servers = await self._servers()
        message = dns.message.make_query(f"{label}.fr.", dns.rdatatype.NS)
        message.flags &= ~dns.flags.RD  # question directe au serveur du .fr, pas de récursion
        try:
            answer = await dns.asyncquery.udp(message, random.choice(servers), timeout=TIMEOUT)
        except (dns.exception.DNSException, OSError):
            return None
        if answer.flags & dns.flags.TC:
            return None  # réponse tronquée : signe de limitation, on ralentit
        code = answer.rcode()
        if code == dns.rcode.NXDOMAIN:
            return False
        if code == dns.rcode.NOERROR:
            return True
        return None

    # --- Passe de nuit ----------------------------------------------------------------

    async def run(self, limit: int = NIGHTLY) -> dict:
        if self.running:
            return {"started": False}
        self.running = True
        stats = {"checked": 0, "present": 0, "newly_taken": 0, "skipped": 0, "started_at": iso(utcnow()),
                 "taken_names": [], "freed_names": []}
        try:
            # Parcours de l'univers hors de la boucle : l'appli reste réactive.
            labels = await asyncio.get_running_loop().run_in_executor(None, self.pending, limit)
            stats["queued"] = len(labels)
            before = load(self.conn, labels)
            rate = RATE
            for label in labels:
                started = asyncio.get_running_loop().time()
                present = await self.query(label)
                if present is None:
                    stats["skipped"] += 1
                    rate = max(MIN_RATE, rate / 2)
                else:
                    now = iso(utcnow())
                    self.conn.execute(
                        "INSERT INTO name_dns (label, present, checked_at) VALUES (?, ?, ?) "
                        "ON CONFLICT(label) DO UPDATE SET present = excluded.present, checked_at = excluded.checked_at",
                        (label, int(present), now),
                    )
                    stats["checked"] += 1
                    stats["present"] += int(present)
                    was = before.get(label, {}).get("present")
                    if present != was and (present or was):
                        if present:
                            stats["newly_taken"] += 1
                            if len(stats["taken_names"]) < 100:
                                stats["taken_names"].append(f"{label}.fr")
                        elif len(stats["freed_names"]) < 100:
                            stats["freed_names"].append(f"{label}.fr")
                        self.explorer.touch(f"{label}.fr")  # le nom change d'état dans l'Explorer
                wait = 1 / rate - (asyncio.get_running_loop().time() - started)
                if wait > 0:
                    await asyncio.sleep(wait)
        except Exception as exc:  # noqa: BLE001  le journal garde la cause, l'écran l'affiche
            log.warning("DNS .fr : %s", exc)
            stats["error"] = str(exc)
        finally:
            self.running = False
            stats["finished_at"] = iso(utcnow())
            self.store.set("fr_dns_status", json.dumps(stats))
            self.store.set("fr_dns_last", datetime.now(self.local_zone).date().isoformat())
        return stats

    def position(self, label: str) -> int | None:
        """Place d'un nom dans la prochaine passe (0 : le premier), None s'il n'y est pas.
        File recalculée au plus une fois par heure."""
        import time

        cached = getattr(self, "_queue", None)
        if not cached or time.monotonic() - cached[0] > 3600:
            cached = (time.monotonic(), {name: n for n, name in enumerate(self.pending(NIGHTLY))})
            self._queue = cached
        return cached[1].get(label)

    def status(self) -> dict:
        info = json.loads(self.store.get("fr_dns_status") or "{}")
        info["running"] = self.running
        info["enabled"] = self.store.get("fr_dns_enabled", "1") == "1"
        return info


"""Présence DNS des .fr « probablement libres » : la fraîcheur d'une zone quotidienne, pour nos noms.

L'Afnic ne publie pas la zone .fr chaque jour ; le fichier open data a plusieurs semaines de retard.
Un .fr enregistré a presque toujours des serveurs DNS : on demande directement aux serveurs du .fr
(sans résolveur intermédiaire, donc seule l'Afnic voit les requêtes) si le nom y est délégué.

- délégué (réponse avec des serveurs) : le nom est pris, l'Explorer l'affiche « Enregistré » ;
- inconnu (NXDOMAIN) : il reste « probablement libre », avec la date de la vérification.

Limite : un nom enregistré sans serveurs DNS passe pour libre ; le RDAP (bouton Vérifier) tranche.
Pas de quota RDAP consommé. Rythme bas et adaptatif pour ne déclencher aucune limite côté Afnic.

Chaque échec est journalisé avec sa cause (délai dépassé, réponse tronquée, refus, code d'erreur, réseau)
et le serveur interrogé. Garde-fous contre un blocage :
- un refus explicite (REFUSED) arrête la passe aussitôt ;
- FAIL_STOP échecs d'affilée l'arrêtent aussi (un serveur qui ignore les requêtes ne répond plus rien) ;
- dans les deux cas le plafond de rythme est divisé par deux pour les nuits suivantes, une alerte part
  sur Telegram, et le plafond ne remonte que de 25 % par nuit sans incident.
"""

import asyncio
import json
import logging
import random
from typing import NamedTuple
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

NIGHTLY = 150_000         # noms par jour au plus
# Passe étalée sur la journée plutôt que tassée la nuit : même travail, rythme plus doux pour les
# registres (moins de risque de blocage). 150 000 noms à 4 par seconde : environ 10 h 30.
RATE = 2.0                # requêtes par seconde au départ
MAX_RATE = 4.0            # plafond atteint par paliers tant que tout répond
MIN_RATE = 0.5
START_HOUR = 0            # heure locale de départ : une passe par jour, lancée à minuit
END_HOUR = 23             # au-delà, la passe s'arrête ; le reste attend le lendemain
RECOVER_AFTER = 50        # réponses sans accroc avant de réaccélérer
FRESH_DAYS_TOP = 1        # noms à fort score de base : revus chaque nuit
FRESH_DAYS_OTHER = 7      # les autres : chaque semaine
TOP_BASE = 40
TIMEOUT = 3.0
FAIL_STOP = 20            # échecs d'affilée avant d'arrêter la passe
FAIL_LOG = 30             # échecs gardés dans l'état de la passe (les plus récents)

TIMED_OUT = "délai dépassé"
TRUNCATED = "réponse tronquée"   # le serveur limite le débit (RRL) : premier signe à surveiller
REFUSED = "refusé"
NETWORK = "erreur réseau"
SILENT = "sans réponse"


class Failure(NamedTuple):
    reason: str
    server: str = ""
    detail: str = ""


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
    tld = "fr"
    key = "fr_dns"  # préfixe des réglages et de l'état gardés dans le store
    count_first = True  # un nom vu délégué à sa première interrogation compte comme « nouvellement pris »

    def __init__(self, conn, store, explorer, local_zone, notify=None):
        self.conn = conn
        self.notify = notify  # async (texte) -> motif d'échec ou None : Telegram
        self.store = store
        self.explorer = explorer
        self.local_zone = local_zone
        self.running = False
        self.servers: list[str] = []
        self.query = self._query  # remplaçable dans les tests

    # --- File ---------------------------------------------------------------------

    def due(self, now_local: datetime) -> bool:
        return (self.store.get(f"{self.key}_enabled", "1") == "1" and START_HOUR <= now_local.hour < END_HOUR
                and self.store.get(f"{self.key}_last") != now_local.date().isoformat() and self.ready())

    def ready(self) -> bool:
        return "fr" in self.explorer.tlds()

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

    def load(self, labels: list[str]) -> dict[str, dict]:
        return load(self.conn, labels)

    def save(self, label: str, present: bool, at: str) -> None:
        self.conn.execute(
            "INSERT INTO name_dns (label, present, checked_at) VALUES (?, ?, ?) "
            "ON CONFLICT(label) DO UPDATE SET present = excluded.present, checked_at = excluded.checked_at",
            (label, int(present), at),
        )

    def changed(self, label: str, present: bool) -> None:
        self.explorer.touch(f"{label}.fr")  # le nom change d'état dans l'Explorer

    # --- Requêtes -------------------------------------------------------------------

    async def _servers(self) -> list[str]:
        """Adresses des serveurs du .fr, lues dans le DNS (rien d'écrit en dur)."""
        if self.servers:
            return self.servers
        resolver = dns.asyncresolver.Resolver()
        names = [str(r.target) for r in await resolver.resolve(f"{self.tld}.", "NS")]
        addresses = []
        for name in names:
            try:
                addresses += [r.address for r in await resolver.resolve(name, "A")]
            except dns.exception.DNSException:
                continue
        if not addresses:
            raise RuntimeError(f"serveurs du .{self.tld} introuvables")
        self.servers = addresses
        return addresses

    async def _query(self, label: str) -> bool | Failure:
        """True : délégué ; False : inconnu ; Failure : pas de réponse exploitable (on réessaiera)."""
        try:
            servers = await self._servers()
        except (dns.exception.DNSException, OSError, RuntimeError) as exc:
            return Failure(NETWORK, "", f"{type(exc).__name__}: {exc}"[:200])
        server = random.choice(servers)
        message = dns.message.make_query(f"{label}.{self.tld}.", dns.rdatatype.NS)
        message.flags &= ~dns.flags.RD  # question directe au serveur du .fr, pas de récursion
        try:
            answer = await dns.asyncquery.udp(message, server, timeout=TIMEOUT)
        except dns.exception.Timeout:
            return Failure(TIMED_OUT, server)
        except (dns.exception.DNSException, OSError) as exc:
            return Failure(NETWORK, server, f"{type(exc).__name__}: {exc}"[:200])
        if answer.flags & dns.flags.TC:
            return Failure(TRUNCATED, server)
        code = answer.rcode()
        if code == dns.rcode.NXDOMAIN:
            return False
        if code == dns.rcode.NOERROR:
            return True
        if code == dns.rcode.REFUSED:
            return Failure(REFUSED, server)
        return Failure(f"code {dns.rcode.to_text(code)}", server)

    # --- Plafond de rythme ------------------------------------------------------------

    def cap(self) -> float:
        """Rythme maximal autorisé : abaissé après un refus ou un silence prolongé."""
        try:
            return min(MAX_RATE, float(self.store.get(f"{self.key}_cap") or MAX_RATE))
        except ValueError:
            return MAX_RATE

    async def _alert(self, text: str) -> None:
        if self.notify:
            try:
                await self.notify(text)
            except Exception as exc:  # noqa: BLE001  une alerte ratée ne doit pas casser la passe
                log.warning("DNS .fr : alerte non envoyée (%s)", exc)

    # --- Passe de nuit ----------------------------------------------------------------

    async def run(self, limit: int = NIGHTLY) -> dict:
        if self.running:
            return {"started": False}
        self.running = True
        stats = {"checked": 0, "present": 0, "newly_taken": 0, "skipped": 0, "started_at": iso(utcnow()),
                 "taken_names": [], "freed_names": [], "failures": {}, "failure_log": []}
        cap = self.cap()
        stats["cap"] = cap
        halted = None
        try:
            # Parcours de l'univers hors de la boucle : l'appli reste réactive.
            labels = await asyncio.get_running_loop().run_in_executor(None, self.pending, limit)
            stats["queued"] = len(labels)
            if labels:
                await self._servers()  # introuvables (résolveur du NAS muet) : erreur de passe, voir plus bas
            before = self.load(labels)
            rate = RATE if cap >= MAX_RATE else min(RATE, cap)
            streak = 0
            fails = 0
            # Passe du jour : elle s'arrête à l'heure de fin. Une passe lancée à la main va au bout.
            night = START_HOUR <= datetime.now(self.local_zone).hour < END_HOUR
            for label in labels:
                if night and datetime.now(self.local_zone).hour >= END_HOUR:
                    stats["stopped"] = "heure de fin atteinte"
                    break
                started = asyncio.get_running_loop().time()
                present = await self.query(label)
                if not isinstance(present, bool):
                    # Pas de réponse : on note la cause, on ralentit, puis on réaccélère quand tout repasse.
                    failure = present if isinstance(present, Failure) else Failure(SILENT)
                    stats["skipped"] += 1
                    fails += 1
                    first = failure.reason not in stats["failures"]
                    stats["failures"][failure.reason] = stats["failures"].get(failure.reason, 0) + 1
                    stats["failure_log"] = (stats["failure_log"] + [{
                        "at": iso(utcnow()), "name": f"{label}.{self.tld}", "server": failure.server,
                        "reason": failure.reason, "detail": failure.detail, "rate": round(rate, 1)}])[-FAIL_LOG:]
                    (log.warning if first else log.info)(
                        "DNS .%s : %s pour %s.%s (serveur %s, rythme %.1f/s)%s", self.tld, failure.reason, label, self.tld,
                        failure.server or "?", rate, f" : {failure.detail}" if failure.detail else "")
                    if failure.reason == REFUSED:
                        halted = f"refus du serveur {failure.server}"
                    elif fails >= FAIL_STOP:
                        halted = f"{FAIL_STOP} échecs d'affilée ({failure.reason})"
                    if halted:
                        stats["stopped"] = halted
                        break
                    rate = max(MIN_RATE, rate * 0.7)
                    streak = 0
                else:
                    fails = 0
                    streak += 1
                    if streak >= RECOVER_AFTER and rate < cap:
                        rate = min(cap, rate * 1.25)
                        streak = 0
                    self.save(label, present, iso(utcnow()))
                    stats["checked"] += 1
                    stats["present"] += int(present)
                    was = before.get(label, {}).get("present")
                    if present != was and (present or was) and (was is not None or self.count_first):
                        if present:
                            stats["newly_taken"] += 1
                            if len(stats["taken_names"]) < 100:
                                stats["taken_names"].append(f"{label}.{self.tld}")
                        elif len(stats["freed_names"]) < 100:
                            stats["freed_names"].append(f"{label}.{self.tld}")
                        self.changed(label, present)
                wait = 1 / rate - (asyncio.get_running_loop().time() - started)
                if wait > 0:
                    await asyncio.sleep(wait)
        except Exception as exc:  # noqa: BLE001  le journal garde la cause, l'écran l'affiche
            log.warning("DNS .%s : %s", self.tld, exc)
            stats["error"] = str(exc)
        finally:
            self.running = False
            stats["finished_at"] = iso(utcnow())
            stats["rate"] = round(rate, 1) if "rate" in locals() else None
            if halted:
                new_cap = max(MIN_RATE, round(min(cap, stats["rate"] or cap) / 2, 1))
            elif "error" not in stats and cap < MAX_RATE:
                new_cap = min(MAX_RATE, round(cap * 1.25, 1))  # nuit sans incident : on remonte doucement
            else:
                new_cap = cap
            if new_cap != cap:
                self.store.set(f"{self.key}_cap", str(new_cap))
            stats["next_cap"] = new_cap
            if stats["failures"]:
                log.warning("DNS .%s : %d échec(s) sur la passe : %s", self.tld, stats["skipped"],
                            ", ".join(f"{n} {r}" for r, n in stats["failures"].items()))
            self.store.set(f"{self.key}_status", json.dumps(stats))
            # Rien vérifié et passe en erreur : le jour n'est pas marqué, le tick suivant réessaie.
            if stats["checked"] or "error" not in stats:
                self.store.set(f"{self.key}_last", datetime.now(self.local_zone).date().isoformat())
        if halted:
            log.warning("DNS .%s : passe arrêtée, %s ; plafond ramené à %s requêtes par seconde", self.tld, halted, new_cap)
            await self._alert(f"DNS .{self.tld} : passe arrêtée ({halted}). Plafond ramené à {new_cap} requêtes par seconde. "
                              f"Échecs : {', '.join(f'{n} {r}' for r, n in stats['failures'].items())}.")
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
        info = json.loads(self.store.get(f"{self.key}_status") or "{}")
        info["running"] = self.running
        info["enabled"] = self.store.get(f"{self.key}_enabled", "1") == "1"
        return info


"""Présence DNS des .co, .ai et .io : sans fichier de zone, la seule source quotidienne.

Ces extensions de pays ne publient pas leur zone (ni CZDS ni open data). Chaque nuit, on demande
directement à leurs serveurs si les meilleurs noms des listes y sont délégués, avec le moteur et les
garde-fous du DNS .fr (rythme progressif, arrêt sur refus ou silence, plafond abaissé, alerte).

Le signal utile : un nom délégué qui ne l'est plus (« sorti du DNS »), souvent un nom qui expire.
Il passe dans la Veille comme un nom sorti d'une zone (drops.record) : vérifié auprès du registre,
puis Libérés ou Bientôt libres selon son état réel. Un nom qui revient dans le DNS en sort. Un nom
absent dès sa première interrogation n'y entre pas : rien ne dit qu'il vient d'expirer (enregistré
sans serveurs, réservé ou premium du registre) ; l'Explorer l'affiche « Absent du DNS, à vérifier ».
Pas de date de libération : les délais de ces registres ne sont pas relevés.
"""

import json
import logging
from datetime import date

from . import wordlists
from .checker import utcnow
from .frdns import NIGHTLY, FrDns

log = logging.getLogger(__name__)

TLDS = ("co", "ai", "io")
PER_TLD = 50_000          # noms suivis par extension : les meilleurs scores de base des listes ci-dessous
# .co se vend comme le .com : toutes les listes. .ai et .io visent la tech et les startups : noms
# anglais, tendances, et la liste tech et startups (anglais et français).
# Marques inventées : des noms de startup. Mes ajouts : partout, et toujours interrogés (pending).
CATEGORIES = {"co": None, "ai": ("english", "trend", "startup", "brand", "mine"),
              "io": ("english", "trend", "startup", "brand", "mine")}


def load_names(conn, names: list[str]) -> dict[str, dict]:
    found = {}
    for chunk in range(0, len(names), 900):
        part = names[chunk:chunk + 900]
        rows = conn.execute(f"SELECT * FROM cc_dns WHERE name IN ({','.join('?' * len(part))})", part)
        found.update({r["name"]: {"present": bool(r["present"]), "at": r["checked_at"], "seen_at": r["seen_at"],
                                  "lost_at": r["lost_at"]} for r in rows})
    return found


class CcDns(FrDns):
    count_first = False  # première nuit : tout est nouveau, rien n'est un changement

    def __init__(self, tld, conn, store, explorer, local_zone, notify=None):
        super().__init__(conn, store, explorer, local_zone, notify)
        self.tld = tld
        self.key = f"{tld}_dns"

    def ready(self) -> bool:
        return True

    def pending(self, limit: int, now=None) -> list[str]:
        """Les meilleurs noms des listes choisies ; jamais interrogés d'abord, puis les plus anciens. Mes ajouts
        en font toujours partie, quel que soit leur score : on les a ajoutés pour les suivre."""
        u = self.explorer.universe
        buckets = u.buckets()
        lists = wordlists.lists()
        chosen_lists = CATEGORIES.get(self.tld) or tuple(lists)
        wanted = set().union(*(lists[c] for c in chosen_lists if c in lists))
        chosen = [label for label in wordlists.mine() if len(label) > 2]
        mine = set(chosen)
        for score in range(len(buckets) - 1, -1, -1):
            for i in buckets[score]:
                if not u.good[i]:
                    continue
                label = u.label(i)
                if len(label) > 2 and label in wanted and label not in mine:  # 1 ou 2 caractères : réservés ou premium
                    chosen.append(label)
                    if len(chosen) >= PER_TLD:
                        break
            if len(chosen) >= PER_TLD:
                break
        seen = {r[0]: r[1] for r in self.conn.execute(
            "SELECT name, checked_at FROM cc_dns WHERE name LIKE ?", (f"%.{self.tld}",))}
        rank = {label: n for n, label in enumerate(chosen)}
        chosen.sort(key=lambda label: (seen.get(f"{label}.{self.tld}", ""), rank[label]))
        return chosen[:limit]

    def load(self, labels: list[str]) -> dict[str, dict]:
        found = load_names(self.conn, [f"{label}.{self.tld}" for label in labels])
        return {name.rsplit(".", 1)[0]: info for name, info in found.items()}

    def save(self, label: str, present: bool, at: str) -> None:
        self.conn.execute(
            """INSERT INTO cc_dns (name, present, checked_at, seen_at) VALUES (?, ?, ?, ?)
               ON CONFLICT(name) DO UPDATE SET
                 lost_at = CASE WHEN cc_dns.present = 1 AND excluded.present = 0 THEN excluded.checked_at
                                WHEN excluded.present = 1 THEN NULL ELSE cc_dns.lost_at END,
                 present = excluded.present, checked_at = excluded.checked_at,
                 seen_at = COALESCE(excluded.seen_at, cc_dns.seen_at)""",
            (f"{label}.{self.tld}", int(present), at, at if present else None),
        )

    def changed(self, label: str, present: bool) -> None:
        """Sorti du DNS : confié à la Veille, vérifié auprès du registre au prochain passage (check_due).
        Revenu : il sort de la Veille, comme un nom revenu dans sa zone."""
        watcher = self.explorer.drops
        if present:
            watcher.record(self.tld, [], [label], utcnow())
        else:
            watcher.record(self.tld, [label], [], utcnow())
        self.explorer.touch(f"{label}.{self.tld}")

    def resume_lost(self) -> int:
        """Noms sortis du DNS avant que la Veille ne les suive (ou sortis de nouveau depuis leur dernière
        entrée) : confiés à la Veille, datés du jour de leur sortie. Les absents d'emblée (lost_at nul) restent hors."""
        rows = self.conn.execute(
            """SELECT c.name, c.lost_at FROM cc_dns c LEFT JOIN drops d ON d.name = c.name
               WHERE c.name LIKE ? AND c.present = 0 AND c.lost_at IS NOT NULL
                 AND (d.name IS NULL OR d.removed_on IS NULL OR d.removed_on < substr(c.lost_at, 1, 10))""",
            (f"%.{self.tld}",)).fetchall()
        for name, lost_at in rows:
            self.explorer.drops.record(self.tld, [name[: -len(self.tld) - 1]], [], utcnow(),
                                       removed_on=date.fromisoformat(lost_at[:10]))
        return len(rows)

    def totals(self) -> dict:
        """Noms de l'extension déjà interrogés, présents, absents et sortis du DNS (Paramètres, Sources et lectures)."""
        known, present, lost = self.conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(present), 0), COALESCE(SUM(present = 0 AND lost_at IS NOT NULL), 0) "
            "FROM cc_dns WHERE name LIKE ?", (f"%.{self.tld}",)).fetchone()
        return {"known": known, "present": present, "absent": known - present, "lost": lost}

    async def run(self, limit: int = NIGHTLY) -> dict:
        stats = await super().run(limit)
        if stats.get("checked"):
            # Passe faite : reprise des sorties pas encore suivies, totaux, puis nouvelle version des données de
            # l'Explorer (l'état de la passe, réécrit ici, en est la version).
            stats["resumed"] = self.resume_lost()
            stats["totals"] = self.totals()
            info = json.loads(self.store.get(f"{self.key}_status") or "{}")
            self.store.set(f"{self.key}_status", json.dumps({**info, "resumed": stats["resumed"], "totals": stats["totals"]}))
            self.explorer.invalidate(self.tld)
        return stats

    def position(self, label: str):
        return None

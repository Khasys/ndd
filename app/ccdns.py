"""Présence DNS des .co, .ai et .io : sans fichier de zone, la seule source quotidienne.

Ces extensions de pays ne publient pas leur zone (ni CZDS ni open data). Chaque nuit, on demande
directement à leurs serveurs si les meilleurs noms des listes y sont délégués, avec le moteur et les
garde-fous du DNS .fr (rythme progressif, arrêt sur refus ou silence, plafond abaissé, alerte).

Le signal utile : un nom délégué qui ne l'est plus (« sorti du DNS »), souvent un nom qui expire.
Le résumé Telegram du matin liste ces noms ; le RDAP (bouton Vérifier) tranche. Pas de date de libération :
les délais de ces registres ne sont pas relevés. Un nom absent du DNS n'est pas forcément libre
(enregistré sans serveurs, réservé ou premium du registre).
"""

import logging

from . import wordlists
from .frdns import FrDns

log = logging.getLogger(__name__)

TLDS = ("co", "ai", "io")
PER_TLD = 50_000          # noms suivis par extension : les meilleurs scores de base des listes ci-dessous
# .co se vend comme le .com : toutes les listes. .ai et .io visent la tech et les startups : noms
# anglais, tendances, et la liste tech et startups (anglais et français).
CATEGORIES = {"co": None, "ai": ("english", "trend", "startup"), "io": ("english", "trend", "startup")}


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
        """Les meilleurs noms des listes choisies ; jamais interrogés d'abord, puis les plus anciens."""
        u = self.explorer.universe
        buckets = u.buckets()
        lists = wordlists.lists()
        chosen_lists = CATEGORIES.get(self.tld) or tuple(lists)
        wanted = set().union(*(lists[c] for c in chosen_lists if c in lists))
        chosen = []
        for score in range(len(buckets) - 1, -1, -1):
            for i in buckets[score]:
                if not u.good[i]:
                    continue
                label = u.label(i)
                if len(label) > 2 and label in wanted:  # 1 ou 2 caractères : réservés ou premium
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
        pass  # pas de colonne dans l'Explorer pour ces extensions : la fiche lit cc_dns

    def position(self, label: str):
        return None

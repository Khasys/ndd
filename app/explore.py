"""Explorer : tous les noms suivis, extension par extension, avec leur état connu.

L'univers est fixe : les 1 926 220 noms possibles de 1 à 4 caractères, puis les mots, noms de
famille et prénoms de 5 caractères et plus. Pour chaque extension, un octet d'état par nom
(environ 2 Mo) est tiré des données déjà sur le NAS, sans aucune requête :

- zones ICANN : carte de bits des noms courts et liste des mots présents, lues chaque jour ;
- .fr : open data de l'Afnic (fr_names) ;
- par-dessus, l'écran Suppressions (table drops), plus récent.

Recherche, filtres et comptages se font sur ces octets : bytes.find et bytes.count tournent en C.
"""

import bisect
import functools
import heapq
import itertools
import time
from array import array
from datetime import date

from . import drops, states, value, wordlists
from .checker import iso, utcnow

# États, un octet par nom.
INVALID, REGISTERED, GONE, ABSENT, COMING, FREE, SUSPENDED = range(7)

LABELS = {
    REGISTERED: "Enregistré",
    GONE: "Supprimé, non repris",
    ABSENT: "Jamais vu",
    COMING: "Libération à venir",
    FREE: "Libre (vérifié)",
    SUSPENDED: "Suspendu",
}
# Pour une zone ICANN, l'absence veut seulement dire « pas dans la zone » : un nom enregistré
# sans serveurs DNS n'y figure pas non plus. Pour le .fr, l'open data liste aussi l'historique.
ABSENT_ZONE = "Absent de la zone"
ABSENT_FR = "Jamais enregistré"

STATUS_FILTERS = {
    "available": (GONE, ABSENT, FREE, COMING),
    "maybe_free": (GONE, ABSENT),
    "coming": (COMING,),
    "free": (FREE,),
    "registered": (REGISTERED,),
    "suspended": (SUSPENDED,),
}
STATUS_NAMES = {
    "available": "À saisir",
    "maybe_free": "Probablement libres",
    "coming": "Libération à venir",
    "free": "Libres vérifiés",
    "registered": "Enregistrés",
    "suspended": "Suspendus",
}
# Pris sur une autre extension : signe de demande pour le nom.
TAKEN_CODES = (REGISTERED, SUSPENDED, COMING)

# Catégories de l'Explorer.
KINDS = {
    "letters": "Lettres",
    "digits": "Chiffres",
    "mixed": "Mélangés, tirets",
    **wordlists.LABELS,
}

# Lignes de la carte : catégorie -> libellé.
# Chaque case correspond à un filtre : catégorie et longueur.
MAP_ROWS = {
    "s1": "1 caractère",
    "s2": "2 caractères",
    "s3": "3 caractères",
    "l4": "4 lettres",
    "d4": "4 chiffres",
    "o4": "4 caractères, autres",
    "word": "Mots",
    "surname": "Noms de famille",
    "firstname": "Prénoms",
}

CACHE_SECONDS = 600
MAX_LIMIT = 200
EXPORT_LIMIT = 50_000
# Une page sans résultat ne parcourt pas plus que ceci : la suite se demande avec le curseur.
SCAN_BUDGET = 4_000_000

_EXPAND = [bytes((b >> i) & 1 for i in range(8)) for b in range(256)]
_PRESENT_TO_STATUS = bytes([ABSENT, REGISTERED]) + bytes(254)


def _words() -> list[str]:
    """Noms des listes de plus de 4 caractères, triés : ils suivent les noms courts dans l'univers."""
    return sorted({w for names in wordlists.lists().values() for w in names if len(w) > drops.MAX_SHORT})


class Universe:
    def __init__(self):
        self.words = _words()
        self.word_index = {w: i for i, w in enumerate(self.words)}
        self.size = drops.CELLS + len(self.words)
        listed = {w for names in wordlists.lists().values() for w in names}
        # Noms des listes, par position : le filtre par catégorie ne parcourt qu'eux.
        self.listed = sorted(i for i in (self.index(w) for w in listed) if i is not None)
        self.listed_sorted_labels = sorted(listed)
        self.by_list = {key: array("i", sorted(self.index(w) for w in names if self.index(w) is not None))
                        for key, names in wordlists.lists().items()}
        # Positions des noms en lettres seules et en chiffres seuls, par longueur.
        self.letters = {n: array("i", (drops.encode("".join(p)) for p in itertools.product(drops.CHARSET[:26], repeat=n)))
                        for n in range(1, drops.MAX_SHORT + 1)}
        self.digits = {n: array("i", (drops.encode("".join(p)) for p in itertools.product(drops.CHARSET[26:36], repeat=n)))
                       for n in range(1, drops.MAX_SHORT + 1)}
        self.letters4, self.digits4 = self.letters[4], self.digits[4]
        self._buckets: list[array] | None = None

    def buckets(self) -> list[array]:
        """Noms qui peuvent valoir quelque chose, rangés par score de base (0 à 70).

        Noms des listes, noms de 1 à 3 caractères, 4 lettres et 4 chiffres : environ 565 000.
        Les 4 caractères mélangés (1,4 million) valent trop peu pour le classement.
        """
        if self._buckets is not None:
            return self._buckets
        buckets = [array("i") for _ in range(71)]
        listed = set(self.listed_sorted_labels)
        for label in listed:
            i = self.index(label)
            if i is not None:
                buckets[value.base(label)].append(i)
        for length in range(1, 4):
            for i in range(drops.OFFSETS[length - 1], drops.OFFSETS[length]):
                label = drops.decode(i)
                if drops.valid_label(label) and label not in listed:
                    buckets[value.shape(label)[0]].append(i)
        for i in itertools.chain(self.letters4, self.digits4):
            label = drops.decode(i)
            if label not in listed:
                buckets[value.shape(label)[0]].append(i)
        self._buckets = buckets
        return buckets

    def index(self, label: str) -> int | None:
        if len(label) <= drops.MAX_SHORT:
            return drops.encode(label) if drops.valid_label(label) else None
        i = self.word_index.get(label)
        return None if i is None else drops.CELLS + i

    def label(self, index: int) -> str:
        return drops.decode(index) if index < drops.CELLS else self.words[index - drops.CELLS]

    def apply_valid(self, status: bytearray) -> None:
        """Met à 0 les noms qui commencent ou finissent par un tiret. Tranches, pas de boucle."""
        for length in range(1, drops.MAX_SHORT + 1):
            start, end = drops.OFFSETS[length - 1], drops.OFFSETS[length]
            lead = drops.BASE ** (length - 1) * (drops.BASE - 1)
            status[start + lead:end] = bytes(end - start - lead)
            status[start + drops.BASE - 1:end:drops.BASE] = bytes(len(range(start + drops.BASE - 1, end, drops.BASE)))

    def categories(self, label: str) -> list[str]:
        kinds = [drops.kind_of(label)] if len(label) <= drops.MAX_SHORT else []
        return kinds + wordlists.categories(label)


@functools.lru_cache(maxsize=1)
def shared_universe() -> Universe:
    """L'univers ne dépend que des listes de noms : une seule instance par processus."""
    return Universe()


class Explorer:
    def __init__(self, conn, drops_watcher: drops.DropWatcher, fr_watcher):
        self.conn = conn
        self.drops = drops_watcher
        self.fr = fr_watcher
        self._universe: Universe | None = None
        self._cache: dict[str, tuple[float, object, bytearray]] = {}

    @property
    def universe(self) -> Universe:
        if self._universe is None:
            self._universe = shared_universe()
        return self._universe

    # --- Extensions et octets d'état -------------------------------------------------

    def tlds(self) -> list[str]:
        found = []
        if self.conn.execute("SELECT 1 FROM fr_names LIMIT 1").fetchone():
            found.append("fr")
        for tld in sorted(self.drops.approved()):
            if self.drops._path(tld, "short").exists():
                found.append(tld)
        return found

    def _version(self, tld: str):
        if tld == "fr":
            return self.fr.store.get("fr_status")
        path = self.drops._path(tld, "short")
        return path.stat().st_mtime if path.exists() else None

    def _base(self, tld: str) -> bytearray:
        version = self._version(tld)
        cached = self._cache.get(tld)
        if cached and cached[1] == version and time.monotonic() - cached[0] < CACHE_SECONDS:
            return cached[2]
        status = self._build_fr() if tld == "fr" else self._build_zone(tld)
        self._cache[tld] = (time.monotonic(), version, status)
        return status

    def _build_zone(self, tld: str) -> bytearray:
        u = self.universe
        bitmap = self.drops._load(tld) or bytes(drops.BITMAP_BYTES)
        present = b"".join(map(_EXPAND.__getitem__, bitmap))[:drops.CELLS]
        status = bytearray(present.translate(_PRESENT_TO_STATUS))
        found = self.drops._load_words(tld) or set()
        status += bytes(REGISTERED if w in found else ABSENT for w in u.words)
        u.apply_valid(status)
        return status

    def _build_fr(self) -> bytearray:
        u = self.universe
        status = bytearray([ABSENT]) * u.size
        u.apply_valid(status)
        for label, removed in self.conn.execute("SELECT label, removed_on FROM fr_names"):
            i = u.index(label)
            if i is not None:
                status[i] = GONE if removed else REGISTERED
        return status

    def status_bytes(self, tld: str) -> bytearray:
        """Octets d'état, écran Suppressions par-dessus (plus récent que les fichiers)."""
        status = bytearray(self._base(tld))
        u = self.universe
        for label, state in self.conn.execute("SELECT label, state FROM drops WHERE tld = ? AND state IS NOT NULL", (tld,)):
            i = u.index(label)
            if i is None:
                continue
            if state in drops.TRACKED:
                status[i] = COMING
            elif state == states.AVAILABLE:
                status[i] = FREE
            elif state == drops.SUSPENDED:
                status[i] = SUSPENDED
            elif state == drops.TAKEN:
                status[i] = REGISTERED
        return status

    def invalidate(self, tld: str | None = None) -> None:
        if tld:
            self._cache.pop(tld, None)
        else:
            self._cache.clear()

    # --- Recherche -------------------------------------------------------------------

    @staticmethod
    def normalize(query: str, tld: str = "") -> str:
        q = query.strip().lower()
        if tld and q.endswith("." + tld):
            q = q[: -len(tld) - 1]
        return q.split(".")[0]

    def _short_matching(self, q: str, mode: str) -> list[int]:
        """Positions des noms courts qui correspondent, sans parcourir les 1,9 million."""
        if not q or len(q) > drops.MAX_SHORT or any(c not in drops.CHARSET for c in q):
            return []
        found = set()
        for length in range(len(q), drops.MAX_SHORT + 1):
            free = length - len(q)
            starts = [0] if mode == "prefix" else range(free + 1) if mode == "contains" else []
            if mode == "exact" and length == len(q):
                starts = [0]
            for start in starts:
                for fill in itertools.product(drops.CHARSET, repeat=free):
                    label = "".join(fill[:start]) + q + "".join(fill[start:])
                    if drops.valid_label(label):
                        found.add(drops.encode(label))
        return sorted(found)

    def _word_matching(self, q: str, mode: str) -> list[int]:
        words = self.universe.words
        if mode == "exact":
            i = self.universe.word_index.get(q)
            return [] if i is None else [drops.CELLS + i]
        if mode == "prefix":
            lo = bisect.bisect_left(words, q)
            hi = bisect.bisect_left(words, q + "￿")
            return [drops.CELLS + i for i in range(lo, hi)]
        return [drops.CELLS + i for i, w in enumerate(words) if q in w]

    def _candidates(self, q: str, mode: str, category: str, length: int | None):
        """Positions à examiner, dans l'ordre, ou None pour tout l'univers."""
        if q:
            return self._short_matching(q, mode) + self._word_matching(q, mode)
        if category in wordlists.LABELS:
            return self.universe.listed
        return None

    def _range(self, length: int | None) -> tuple[int, int]:
        if length and length <= drops.MAX_SHORT:
            return drops.OFFSETS[length - 1], drops.OFFSETS[length]
        if length:
            return drops.CELLS, self.universe.size
        return 0, self.universe.size

    def _keep(self, label: str, category: str, length: int | None) -> bool:
        if length and (len(label) != length if length <= drops.MAX_SHORT else len(label) < length):
            return False
        if not category:
            return True
        if category in wordlists.LABELS:
            return label in wordlists.lists()[category]
        if len(label) > drops.MAX_SHORT:
            return False
        kind = drops.kind_of(label)
        # Dans l'Explorer, « mélangés » regroupe lettres et chiffres mêlés et noms à tiret.
        return kind == category or (category == "mixed" and kind == "hyphen")

    def search(self, tld: str, q: str = "", mode: str = "prefix", status: str = "", category: str = "",
               length: int | None = None, cursor: int = -1, limit: int = 50) -> dict:
        limit = max(1, min(limit, EXPORT_LIMIT))
        codes = STATUS_FILTERS.get(status) or tuple(LABELS)
        data = self.status_bytes(tld)
        u = self.universe
        q = self.normalize(q, tld)
        hits: list[int] = []
        last = cursor
        exhausted = True
        candidates = self._candidates(q, mode, category, length)
        if candidates is not None:
            start = bisect.bisect_right(candidates, cursor)
            for pos in range(start, len(candidates)):
                i = candidates[pos]
                last = i
                if data[i] in codes and self._keep(u.label(i), category, length):
                    hits.append(i)
                    if len(hits) >= limit:
                        exhausted = pos == len(candidates) - 1
                        break
        else:
            lo, hi = self._range(length)
            pos = max(lo, cursor + 1)
            scanned = 0
            while pos < hi and len(hits) < limit:
                # Saut direct au prochain nom dans un des états voulus.
                found = [f for f in (data.find(c, pos, hi) for c in codes) if f != -1]
                if not found:
                    pos = hi
                    break
                i = min(found)
                scanned += i - pos + 1
                pos = i + 1
                last = i
                if self._keep(u.label(i), category, length):
                    hits.append(i)
                if scanned > SCAN_BUDGET:
                    break
            exhausted = pos >= hi
        return {
            "items": self.describe(tld, hits, data),
            "cursor": None if exhausted else last,
        }

    def counts(self, tld: str) -> dict:
        data = self.status_bytes(tld)
        return {key: sum(data.count(c) for c in codes) for key, codes in STATUS_FILTERS.items()}

    # --- Classement des affaires -------------------------------------------------------

    def _fr_rows(self, labels: list[str]) -> dict:
        found = {}
        for chunk in range(0, len(labels), 900):
            part = labels[chunk:chunk + 900]
            rows = self.conn.execute(
                f"SELECT * FROM fr_names WHERE label IN ({','.join('?' * len(part))})", part
            ).fetchall()
            found.update({r["label"]: r for r in rows})
        return found

    def _seo_rows(self, names: list[str]) -> dict:
        found = {}
        for chunk in range(0, len(names), 900):
            part = names[chunk:chunk + 900]
            rows = self.conn.execute(
                f"SELECT * FROM domain_seo WHERE name IN ({','.join('?' * len(part))})", part
            ).fetchall()
            found.update({r["name"]: r for r in rows})
        return found

    def _bonus_context(self, tld: str) -> dict:
        """Ce qu'il faut pour le bonus : les autres extensions, l'historique .fr, le SEO connu."""
        others = {t: self.status_bytes(t) for t in self.tlds() if t != tld}
        gone = {}
        if tld == "fr":
            u = self.universe
            for label, created, removed in self.conn.execute(
                    "SELECT label, created_on, removed_on FROM fr_names WHERE removed_on IS NOT NULL"):
                i = u.index(label)
                if i is not None:
                    gone[i] = {"created_on": created, "removed_on": removed}
        seo = {r["name"]: r for r in self.conn.execute("SELECT * FROM domain_seo WHERE name LIKE ?", (f"%.{tld}",))}
        # Bonus le plus haut possible avec ces données : sert à arrêter le classement au plus tôt.
        reach = value.demand(list(others))[0] + (15 if gone else 0) + (20 if seo else 0)
        return {"others": others, "gone": gone, "seo": seo, "today": date.today(), "reach": reach}

    def _value(self, tld: str, i: int, label: str, ctx: dict) -> dict:
        taken = [t for t, data in ctx["others"].items() if data[i] in TAKEN_CODES]
        return value.score(label, taken, ctx["gone"].get(i), ctx["seo"].get(f"{label}.{tld}"), ctx["today"])

    def rank(self, tld: str, q: str = "", mode: str = "prefix", status: str = "available", category: str = "",
             length: int | None = None, limit: int = 50) -> dict:
        """Les meilleurs noms d'abord. Sans recherche, parcours par score de base décroissant,
        arrêté dès qu'aucun nom restant ne peut plus entrer dans le classement."""
        limit = max(1, min(limit, MAX_LIMIT * 5))
        codes = STATUS_FILTERS.get(status) or STATUS_FILTERS["available"]
        data = self.status_bytes(tld)
        u = self.universe
        ctx = self._bonus_context(tld)
        q = self.normalize(q, tld)
        best: list[tuple[int, int, int, dict]] = []  # tas : (score, -position, position, valeur)

        def consider(i: int) -> None:
            if data[i] not in codes:
                return
            label = u.label(i)
            if not self._keep(label, category, length):
                return
            found = self._value(tld, i, label, ctx)
            entry = (found["score"], -i, i, found)
            if len(best) < limit:
                heapq.heappush(best, entry)
            elif entry > best[0]:
                heapq.heapreplace(best, entry)

        if q:
            for i in self._short_matching(q, mode) + self._word_matching(q, mode):
                consider(i)
        else:
            for score in range(70, -1, -1):
                if len(best) >= limit and score + ctx["reach"] <= best[0][0]:
                    break
                for i in self.universe.buckets()[score]:
                    consider(i)
        ranked = sorted(best, reverse=True)
        items = self.describe(tld, [e[2] for e in ranked], data)
        for item, entry in zip(items, ranked):
            item.update(entry[3])
        return {"items": items}

    def facets(self, tld: str, category: str = "", length: int | None = None) -> dict:
        """Nombre de noms par état, pour la catégorie et la longueur choisies."""
        data = self.status_bytes(tld)
        u = self.universe
        counts = dict.fromkeys(range(256), 0)

        def add_range(lo: int, hi: int, sign: int = 1) -> None:
            for c in LABELS:
                counts[c] += sign * data.count(c, lo, hi)

        def add_indices(indices, sign: int = 1) -> None:
            for i in indices:
                if not length or self._keep(u.label(i), "", length):
                    counts[data[i]] += sign

        lengths = [length] if length and length <= drops.MAX_SHORT else [] if length else list(range(1, drops.MAX_SHORT + 1))
        if not category:
            lo, hi = self._range(length)
            add_range(lo, hi)
        elif category in wordlists.LABELS:
            add_indices(u.by_list[category])
        elif category in ("letters", "digits"):
            arrays = u.letters if category == "letters" else u.digits
            for n in lengths:
                add_indices(arrays[n])
        else:
            # Mélangés et tirets : tout le reste, par soustraction (pas de parcours d'1,9 million de noms).
            for n in lengths:
                add_range(drops.OFFSETS[n - 1], drops.OFFSETS[n])
                add_indices(u.letters[n], -1)
                add_indices(u.digits[n], -1)
        return {key: sum(counts[c] for c in codes) for key, codes in STATUS_FILTERS.items()}

    def map(self) -> dict:
        """Carte extension x catégorie : noms à saisir par case."""
        u = self.universe
        codes = STATUS_FILTERS["available"]
        tlds = self.tlds()
        cells = {}
        for tld in tlds:
            data = self.status_bytes(tld)
            count = lambda lo, hi: sum(data.count(c, lo, hi) for c in codes)
            in_list = lambda indices: sum(1 for i in indices if data[i] in codes)
            row = {
                "s1": count(0, drops.OFFSETS[1]),
                "s2": count(drops.OFFSETS[1], drops.OFFSETS[2]),
                "s3": count(drops.OFFSETS[2], drops.OFFSETS[3]),
                "l4": in_list(u.letters4),
                "d4": in_list(u.digits4),
            }
            row["o4"] = count(drops.OFFSETS[3], drops.OFFSETS[4]) - row["l4"] - row["d4"]
            for key in wordlists.LABELS:
                row[key] = in_list(u.by_list[key])
            cells[tld] = row
        filters = {"s1": ("", 1), "s2": ("", 2), "s3": ("", 3), "l4": ("letters", 4), "d4": ("digits", 4),
                   "o4": ("mixed", 4), **{k: (k, None) for k in wordlists.LABELS}}
        return {"rows": MAP_ROWS, "tlds": tlds, "cells": cells,
                "filters": {k: {"cat": c, "len": n} for k, (c, n) in filters.items()}}

    # --- Fiches ----------------------------------------------------------------------

    def describe(self, tld: str, indices: list[int], data: bytearray | None = None) -> list[dict]:
        u = self.universe
        data = data if data is not None else self.status_bytes(tld)
        labels = [u.label(i) for i in indices]
        names = [f"{label}.{tld}" for label in labels]
        extra = {}
        if tld == "fr" and labels:
            for chunk in range(0, len(labels), 900):
                part = labels[chunk:chunk + 900]
                rows = self.conn.execute(
                    f"SELECT * FROM fr_names WHERE label IN ({','.join('?' * len(part))})", part
                ).fetchall()
                extra.update({r["label"]: r for r in rows})
        dropped = {}
        for chunk in range(0, len(names), 900):
            part = names[chunk:chunk + 900]
            rows = self.conn.execute(
                f"SELECT * FROM drops WHERE name IN ({','.join('?' * len(part))})", part
            ).fetchall()
            dropped.update({r["name"]: r for r in rows})
        watched = {r[0] for r in self.conn.execute("SELECT name FROM domains")}
        items = []
        for i, label, name in zip(indices, labels, names):
            code = data[i]
            fr = extra.get(label)
            drop = dropped.get(name)
            items.append({
                "label": label,
                "name": name,
                "tld": tld,
                "length": len(label),
                "categories": u.categories(label),
                "status": code,
                "status_label": (ABSENT_FR if tld == "fr" else ABSENT_ZONE) if code == ABSENT else LABELS.get(code, ""),
                "created_on": fr["created_on"] if fr else None,
                "removed_on": (fr["removed_on"] if fr and fr["removed_on"] else drop["removed_on"] if drop else None),
                "expires_at": fr["expires_at"] if fr else None,
                "checked_at": max(filter(None, [fr["checked_at"] if fr else None, drop["checked_at"] if drop else None]), default=None),
                "next_check_at": (drop["next_check_at"] if drop and drop["next_check_at"] else fr["next_check_at"] if fr else None),
                "drop_state": drop["state"] if drop else None,
                "release_at": drop["release_at"] if drop else None,
                "available_since": drop["available_since"] if drop else None,
                "watched": name in watched,
            })
        return items

    def card(self, label: str) -> dict:
        """Un nom sur toutes les extensions connues."""
        label = self.normalize(label)
        i = self.universe.index(label)
        if i is None:
            return {"label": label, "known": False, "items": []}
        items = []
        for tld in self.tlds():
            item = self.describe(tld, [i])[0]
            item.update(self._value(tld, i, label, self._bonus_context(tld)))
            items.append(item)
        return {"label": label, "known": True, "categories": self.universe.categories(label), "items": items}

    # --- Vérification à la demande ----------------------------------------------------

    async def verify(self, name: str) -> dict:
        """Requête RDAP sur un nom, résultat enregistré là où il sert. Le .fr passe par la file Afnic."""
        label, _, tld = name.partition(".")
        if tld == "fr":
            if self.fr.paused_until():
                raise VerifyError(f"Vérifications .fr en pause jusqu'au {iso(self.fr.paused_until())} : l'Afnic a signalé trop de requêtes.")
            result = await self.fr.lookup(name, "manuel")
            if result is None:
                raise VerifyError("L'Afnic signale trop de requêtes : vérifications .fr en pause.")
        else:
            result = await self.drops.rdap.lookup(name)
        if result.error:
            raise VerifyError(result.error)
        now = utcnow()
        registered = not result.available and states.compute_state(
            result.info.statuses, result.info.expires_at, now, 0) not in drops.TRACKED
        row = self.drops.row(name)
        state = None
        if row is not None or not registered:
            row = row or self.drops.ensure(name, now)
            state = self.drops.apply(row, result, now)
        if tld == "fr":
            if state is not None:
                self.fr._sync_name(label, result, state)
            if registered and row is None:
                self.fr._sync_name(label, result, drops.TAKEN)
            elif not registered:
                self.conn.execute("UPDATE fr_names SET checked_at = ?, next_check_at = NULL WHERE label = ?", (iso(now), label))
        i = self.universe.index(label)
        return self.describe(tld, [i])[0] if i is not None else {"name": name}


class VerifyError(Exception):
    pass

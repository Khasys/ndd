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
import itertools
import time
from . import drops, states, wordlists
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
# sans serveurs DNS n'y figure pas non plus.
ABSENT_ZONE = "Absent de la zone"

STATUS_FILTERS = {
    "registered": (REGISTERED,),
    "maybe_free": (GONE, ABSENT),
    "coming": (COMING,),
    "free": (FREE,),
    "suspended": (SUSPENDED,),
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
            self._universe = Universe()
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
        return len(label) <= drops.MAX_SHORT and drops.kind_of(label) == category

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
                "status_label": ABSENT_ZONE if code == ABSENT and tld != "fr" else LABELS.get(code, ""),
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
        return {
            "label": label,
            "known": True,
            "categories": self.universe.categories(label),
            "items": [self.describe(tld, [i])[0] for tld in self.tlds()],
        }

    def suggest(self, q: str, limit: int = 8) -> list[str]:
        q = self.normalize(q)
        if not q:
            return []
        labels = self.universe.listed_sorted_labels
        lo = bisect.bisect_left(labels, q)
        found = []
        for label in labels[lo:]:
            if not label.startswith(q) or len(found) >= limit:
                break
            found.append(label)
        if drops.valid_label(q) and q not in found:
            found.insert(0, q)
        return found[:limit]

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

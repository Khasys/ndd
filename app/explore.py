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
import json
import functools
import heapq
import itertools
import threading
import time
from array import array
from dataclasses import dataclass
from datetime import date

from . import ai, drops, frdns, precompute, rdapinfo, states, value, wordlists
from .checker import iso, utcnow

# États, un octet par nom.
INVALID, REGISTERED, GONE, ABSENT, COMING, FREE, SUSPENDED, RESERVED = range(8)

LABELS = {
    REGISTERED: "Enregistré",
    GONE: "Supprimé, non repris",
    ABSENT: "Jamais vu",
    COMING: "Libération à venir",
    FREE: "Libre (vérifié)",
    SUSPENDED: "Enregistré, suspendu (hold)",
    RESERVED: "Réservé",
}
# Pour une zone ICANN, l'absence veut seulement dire « pas dans la zone » : un nom enregistré
# sans serveurs DNS n'y figure pas non plus. Pour le .fr, l'open data liste aussi l'historique.
ABSENT_ZONE = "Absent de la zone, à vérifier"
# Libération à venir : l'état précis, avec les libellés de la légende de l'écran Domaines.
DROP_LABELS = {"expired": "Expiré", "redemption": "En rédemption", "pending_delete": "Suppression imminente"}
ABSENT_FR = "Jamais enregistré"

STATUS_FILTERS = {
    "available": (GONE, ABSENT, FREE, COMING),
    "maybe_free": (GONE, ABSENT),
    "coming": (COMING,),
    "free": (FREE,),
    "registered": (REGISTERED,),
    "suspended": (SUSPENDED,),
    "reserved": (RESERVED,),
}
STATUS_NAMES = {
    "available": "Tous non pris",
    "maybe_free": "Probablement libres",
    "coming": "Libération à venir",
    "free": "Libres vérifiés",
    "registered": "Enregistrés",
    "suspended": "Suspendus",
    "reserved": "Réservés",
}
# Réservés mêlés aux noms à saisir si on le demande dans Paramètres.
WITH_RESERVED = {"available", "maybe_free", "free", "coming"}
# Pris sur une autre extension : signe de demande pour le nom.
TAKEN_CODES = (REGISTERED, SUSPENDED, COMING)

# Filtre SEO : seuls les noms déjà analysés (bouton SEO) ont ces données.
SEO_FILTERS = {
    "any": ("Avec signal SEO", "(pr > 0 OR wb_years > 0)"),
    "pr1": ("PageRank ≥ 1", "pr >= 1"),
    "pr3": ("PageRank ≥ 3", "pr >= 3"),
    "pr5": ("PageRank ≥ 5", "pr >= 5"),
    "wb5": ("Archivé 5 ans et +", "wb_years >= 5"),
}

# Catégories de l'Explorer.
NATURES = wordlists.NATURES
KINDS = {
    "letters": "Lettres",
    "digits": "Chiffres",
    "mixed": "Mélangés, tirets",
    "hyphen": "Avec tiret",
    **wordlists.LABELS,
}

# Lignes de la carte : catégorie -> libellé.
# Chaque case correspond à un filtre : catégorie et longueur.
MAP_ROWS = {
    "short": "1 à 3 caractères",
    "l4": "4 lettres",
    **wordlists.LABELS,
}

CACHE_SECONDS = 600
# Sécurité seulement : chaque nom vérifié ou marqué est corrigé en place (touch), et un import ou
# une lecture de zone change la version des données, donc la clé.
MEMO_SECONDS = 6 * 3600
MEMO_FACETS = 24  # jeux de filtres gardés, environ 2 Mo chacun
MAX_LIMIT = 200
EXPORT_LIMIT = 50_000
# Une page sans résultat ne parcourt pas plus que ceci : la suite se demande avec le curseur.
SCAN_BUDGET = 4_000_000

_EXPAND = [bytes((b >> i) & 1 for i in range(8)) for b in range(256)]
_PRESENT_TO_STATUS = bytes([ABSENT, REGISTERED]) + bytes(254)


def _words() -> list[str]:
    """Noms des listes de plus de 4 caractères, triés : ils suivent les noms courts dans l'univers."""
    return sorted({w for names in wordlists.lists().values() for w in names if len(w) > drops.MAX_SHORT})


# Filtre sur l'avis IA : verdicts et bornes (basse, haute) de chaque note et de chaque prix.
AI_VERDICTS = {"take": "À prendre", "watch": "À surveiller", "skip": "À laisser"}
AI_FIELDS = {"exploit": "Exploitation", "resale": "Revente", "quick_low": "Revente rapide bas",
             "quick_high": "Revente rapide haut", "final_low": "Prix final bas", "final_high": "Prix final haut"}


def ai_filter(verdicts: list[str], ranges: dict[str, tuple[int | None, int | None]]) -> str:
    """Forme canonique et stable (clé de mémoire) ; vide si aucun critère."""
    parts = []
    picked = [v for v in AI_VERDICTS if v in verdicts]
    if picked:
        parts.append("v=" + ",".join(picked))
    for field in AI_FIELDS:
        lo, hi = ranges.get(field, (None, None))
        if lo is not None or hi is not None:
            parts.append(f"{field}={'' if lo is None else lo}-{'' if hi is None else hi}")
    return ";".join(parts)


def ai_condition(spec: str) -> tuple[str, list]:
    """Condition SQL sur domain_ai pour un filtre canonique (valeurs passées en paramètres)."""
    where, args = [], []
    for part in filter(None, spec.split(";")):
        key, _, val = part.partition("=")
        if key == "v":
            verdicts = [AI_VERDICTS[v] for v in val.split(",") if v in AI_VERDICTS]
            if verdicts:
                where.append("(" + " OR ".join("comment LIKE ?" for _ in verdicts) + ")")
                args += [f"{v}.%" for v in verdicts]
        elif key in AI_FIELDS:
            lo, _, hi = val.partition("-")
            if lo:
                where.append(f"{key} >= ?")
                args.append(int(lo))
            if hi:
                where.append(f"{key} <= ?")
                args.append(int(hi))
    return " AND ".join(where) or "1", args


@dataclass
class Filters:
    q: str = ""
    mode: str = "prefix"
    status: str = ""
    category: str = ""
    length: int | None = None  # > 0 exactement, 5 : 5 et plus, < 0 : au plus
    nature: str = ""  # verb, noun, adj, adv, other
    max_rank: int | None = None  # dans les N plus fréquents de sa liste
    taken: str = ""  # "any" ou une extension : pris ailleurs
    min_score: int | None = None
    junk: bool = False  # True : garder aussi les suites imprononçables, les mélanges, les tirets
    seo: str = ""  # any : analysé avec signal ; pr1, pr3, pr5 : PageRank minimum ; wb5 : archivé 5 ans et plus
    ai: str = ""  # avis IA, forme canonique de ai_filter() : « v=take,watch;exploit=40-;quick_high=-500 »
    lang: str = ""  # fr ou en (wordlists.LANGS)

    @classmethod
    def of(cls, *args, **kwargs) -> "Filters":
        if args and isinstance(args[0], Filters):
            return args[0]
        names = ["q", "mode", "status", "category", "length"]
        kwargs.update(zip(names, args))
        return cls(**{k: v for k, v in kwargs.items() if k in cls.__dataclass_fields__})


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
        self._lock = threading.Lock()
        self.good: bytearray | None = None
        # Noms des listes du plus fréquent au moins fréquent, par liste et toutes listes confondues.
        ranks = wordlists.ranks()
        self.by_frequency = {key: array("i", (self.index(w) for w in sorted(r, key=r.get) if self.index(w) is not None))
                             for key, r in ranks.items()}
        best: dict[int, float] = {}
        for key, r in ranks.items():
            for w, n in r.items():
                i = self.index(w)
                if i is not None:
                    best[i] = min(best.get(i, 1.0), n / len(r))
        self.by_frequency[""] = array("i", sorted(best, key=best.get))
        # Noms de 5 caractères et plus, du plus court au plus long puis par ordre alphabétique.
        self.by_length = array("i", sorted(range(drops.CELLS, self.size),
                                           key=lambda i: (len(self.words[i - drops.CELLS]), self.words[i - drops.CELLS])))
        self._positions: dict = {}
        natures = wordlists.natures()
        self.by_nature = {key: array("i", sorted(i for i in (self.index(w) for w, ks in natures.items() if key in ks)
                                                if i is not None))
                          for key in wordlists.NATURES}
        self.by_lang = {key: array("i", sorted(i for i in (self.index(w) for w in names) if i is not None))
                        for key, names in wordlists.languages().items()}

    def buckets(self) -> list[array]:
        """Noms qui peuvent valoir quelque chose, rangés par score de base (0 à 70).

        Noms des listes, noms de 1 à 3 caractères, 4 lettres et 4 chiffres : environ 565 000.
        Les 4 caractères mélangés (1,4 million) valent trop peu pour le classement.
        Construit aussi self.good : 1 pour un nom qui a un sens (liste, vrai mot, prononçable,
        chiffres seuls), 0 pour une suite de lettres imprononçable, un mélange ou un tiret.
        Calculé une fois, en tâche de fond au démarrage ; les autres appels attendent.
        """
        with self._lock:
            if self._buckets is not None:
                return self._buckets
            buckets = [array("i") for _ in range(71)]
            good = bytearray(self.size)
            good[drops.CELLS:] = b"\x01" * (self.size - drops.CELLS)  # noms des listes de 5 caractères et plus
            listed = set(self.listed_sorted_labels)
            for label in sorted(listed):  # ordre stable d'un démarrage à l'autre
                i = self.index(label)
                if i is not None:
                    buckets[value.base(label)].append(i)
                    good[i] = 1
            table = precompute.load() or precompute.compute()
            # Classement : noms de 1 à 3 caractères, 4 lettres et 4 chiffres. Les 4 caractères
            # mélangés (1,4 million) valent trop peu pour y entrer.
            for i in itertools.chain(range(0, drops.OFFSETS[3]), self.letters4, self.digits4):
                byte = table[i]
                if byte and not good[i]:
                    buckets[byte & 127].append(i)
            for i in range(drops.CELLS):
                if table[i] & precompute.GOOD:
                    good[i] = 1
            self.good = good
            self.good_int = int.from_bytes(good, "big")
            self._buckets = buckets
            return buckets

    def position(self, order_key: str, i: int) -> int:
        """Place d'un nom dans un ordre de la liste (fréquence, longueur), pour fusionner des listes."""
        found = self._positions.get(order_key)
        if found is None:
            order = self.by_length if order_key == "length" else self.by_frequency[order_key]
            found = self._positions[order_key] = {x: n for n, x in enumerate(order)}
        return found.get(i, len(found))

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
    def __init__(self, conn, drops_watcher: drops.DropWatcher, fr_watcher, reserved=None):
        self.conn = conn
        self.drops = drops_watcher
        self.fr = fr_watcher
        self.reserved = reserved
        self._universe: Universe | None = None
        self._cache: dict[str, tuple[float, object, bytearray]] = {}
        self._memos: dict = {}
        self.generation = 0
        # Les vérifications automatiques corrigent l'Explorer au fil de l'eau, nom par nom.
        drops_watcher.on_change = self.touch
        fr_watcher.on_change = self.touch

    @property
    def universe(self) -> Universe:
        if self._universe is None:
            universe = shared_universe()
            universe.buckets()  # construit aussi universe.good
            self._universe = universe
        return self._universe

    def _memo(self, key, compute, ttl: float = MEMO_SECONDS):
        """Résultat gardé en mémoire. Un nom qui change est corrigé en place par touch() ;
        un import ou une lecture de zone change la version des données, donc la clé."""
        now = time.monotonic()
        # Import .fr ou nouvelle lecture de zone : la version des données change, la mémoire aussi.
        key = (key, tuple((t, self._version(t)) for t in self.tlds()))
        found = self._memos.get(key)
        if found and found[0] == self.generation and now - found[1] < ttl:
            return found[2]
        result = compute()
        # Ménage : versions de données dépassées, résultats périmés, puis les plus anciens.
        # Chaque entrée pèse quelques Mo (octets d'état, masques) : on en garde peu.
        current = key[1]
        for old in [k for k, (g, at, _) in self._memos.items() if k[1] != current or g != self.generation or now - at >= ttl]:
            del self._memos[old]
        # Les compteurs (un par jeu de filtres) sont les seuls à se multiplier : on retire les plus anciens.
        facets = sorted((k for k in self._memos if k[0][0] == "facets"), key=lambda k: self._memos[k][1])
        while facets and len(facets) >= MEMO_FACETS:
            del self._memos[facets.pop(0)]
        self._memos[key] = (self.generation, now, result)
        return result

    def warm(self) -> None:
        """Prépare ce que l'ouverture de l'Explorer demande, pour que le premier écran soit immédiat."""
        for tld in self.tlds():
            self.status_bytes(tld)
            self._bonus_context(tld)
        self.map()

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
        # Liste ajoutée après la dernière lecture : ses noms ne sont pas encore cherchés dans la
        # zone. Masqués jusqu'à la prochaine lecture plutôt qu'affichés « libres » à tort.
        lists = wordlists.lists()
        covered = self.drops.covered(tld)
        known = set().union(*(lists[k] for k in covered)) if covered else set()
        for key in set(lists) - covered:
            for w in lists[key] - known:
                i = u.index(w)
                if i is not None and status[i] == ABSENT and i >= drops.CELLS:
                    status[i] = INVALID
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
        """Octets d'état, écran Suppressions par-dessus (plus récent que les fichiers).
        Gardés en mémoire une minute : chaque écran en demande plusieurs fois."""
        return self._memo(("status", tld), lambda: self._status_bytes(tld))

    def base_bytes(self, tld: str) -> bytearray:
        """Octets tirés des seuls fichiers (zone ou open data), sans les vérifications."""
        return self._base(tld)

    def _status_bytes(self, tld: str) -> bytearray:
        status = bytearray(self._base(tld))
        u = self.universe
        if tld == "fr":
            # DNS plus frais que l'open data : un nom délégué est pris.
            for label in frdns.present(self.conn):
                i = u.index(label)
                if i is not None and status[i] in (GONE, ABSENT):
                    status[i] = REGISTERED
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
        if self.reserved is not None:
            # Réservé : seulement s'il n'est pas enregistré. Un nom pris reste « Enregistré ».
            for i in self.reserved.indices(tld, u):
                if status[i] in (GONE, ABSENT, FREE, COMING):
                    status[i] = RESERVED
            if tld != "fr":
                # 1 ou 2 caractères absents de la zone : gardés par le registre (voir reserved.short_reserved).
                unflagged = self.reserved.unflagged()
                for i in range(drops.OFFSETS[2]):
                    if status[i] in (ABSENT, FREE) and f"{u.label(i)}.{tld}" not in unflagged:
                        status[i] = RESERVED
        return status

    def codes(self, status: str, default: str = "") -> tuple[int, ...]:
        key = status if status in STATUS_FILTERS else default
        codes = STATUS_FILTERS.get(key) or tuple(LABELS)
        if key in WITH_RESERVED and self.reserved is not None and self.reserved.mode == "show":
            codes = codes + (RESERVED,)
        return codes

    def invalidate(self, tld: str | None = None) -> None:
        """Données changées (vérification, marquage, import) : on recalcule au prochain affichage."""
        self.generation += 1
        self._memos.clear()
        if tld:
            self._cache.pop(tld, None)

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

    # --- Filtres ---------------------------------------------------------------------

    def _length_ok(self, label: str, length: int | None) -> bool:
        """length > 0 : exactement ; 5 : 5 et plus ; < 0 : au plus |length|."""
        if not length:
            return True
        if length < 0:
            return len(label) <= -length
        if length > drops.MAX_SHORT:
            return len(label) >= length
        return len(label) == length

    def _range(self, length: int | None) -> tuple[int, int]:
        if length and 0 < length <= drops.MAX_SHORT:
            return drops.OFFSETS[length - 1], drops.OFFSETS[length]
        if length and length < 0:
            return 0, drops.OFFSETS[min(-length, drops.MAX_SHORT)]
        if length:
            return drops.CELLS, self.universe.size
        return 0, self.universe.size

    def best_rank(self, label: str, category: str = "") -> int | None:
        ranks = wordlists.ranks()
        if category in ranks:
            return ranks[category].get(label)
        found = [r[label] for r in ranks.values() if label in r]
        return min(found) if found else None

    def _keep(self, label: str, f: Filters) -> bool:
        if not self._length_ok(label, f.length):
            return False
        if f.category:
            if f.category in wordlists.LABELS:
                if label not in wordlists.lists()[f.category]:
                    return False
            elif len(label) > drops.MAX_SHORT:
                return False
            else:
                kind = drops.kind_of(label)
                # Dans l'Explorer, « mélangés » regroupe lettres et chiffres mêlés et noms à tiret.
                if not (kind == f.category or (f.category == "mixed" and kind == "hyphen")):
                    return False
        if f.nature and f.nature not in wordlists.natures().get(label, ()):
            return False
        if f.lang and label not in wordlists.languages().get(f.lang, ()):
            return False
        if f.max_rank:
            rank = self.best_rank(label, f.category)
            if rank is None or rank > f.max_rank:
                return False
        return True

    def _hopeless(self, tld: str, f: Filters) -> bool:
        """« Pris ailleurs » sans autre extension où regarder : aucun nom ne peut passer.
        Réponse immédiate plutôt qu'un parcours de tout l'univers."""
        return bool(f.taken) and not any(t != tld and (f.taken == "any" or t == f.taken) for t in self.tlds())

    def _others(self, tld: str, f: Filters) -> list[bytearray]:
        """Octets d'état des extensions où le nom doit être pris (filtre « pris ailleurs »)."""
        if not f.taken:
            return []
        tlds = [t for t in self.tlds() if t != tld and (f.taken == "any" or t == f.taken)]
        return [self.status_bytes(t) for t in tlds] or [bytearray(self.universe.size)]

    def _good_ok(self, i: int, f: Filters) -> bool:
        return f.junk or bool(self.universe.good[i])

    @staticmethod
    def _taken_ok(i: int, others: list[bytearray]) -> bool:
        return not others or any(data[i] in TAKEN_CODES for data in others)

    def _matches(self, label: str, f: Filters, q: str) -> bool:
        if not q:
            return True
        if f.mode == "exact":
            return label == q
        return q in label if f.mode == "contains" else label.startswith(q)

    def _seo_indices(self, tld: str, key: str, order: bool = False) -> list[int]:
        """Noms analysés en SEO qui passent le seuil ; triés par PageRank puis archives si order."""
        condition = SEO_FILTERS.get(key, ("", "1"))[1]
        rows = self.conn.execute(
            f"SELECT name FROM domain_seo WHERE name LIKE ? AND {condition} "
            "ORDER BY COALESCE(pr, 0) DESC, COALESCE(wb_years, 0) DESC", (f"%.{tld}",)
        ).fetchall()
        found = [self.universe.index(r[0][: -len(tld) - 1]) for r in rows]
        found = [i for i in found if i is not None]
        return found if order else sorted(found)

    def _ai_indices(self, tld: str, spec: str) -> list[int]:
        """Noms de cette extension dont l'avis IA passe le filtre, dans l'ordre de l'univers."""
        condition, args = ai_condition(spec)
        rows = self.conn.execute(f"SELECT name FROM domain_ai WHERE name LIKE ? AND {condition}", (f"%.{tld}", *args))
        found = [self.universe.index(r[0][: -len(tld) - 1]) for r in rows]
        return sorted(i for i in found if i is not None)

    def _restrict(self, tld: str, f: Filters) -> set[int] | None:
        """Noms admis par les filtres SEO et avis IA (les tris qui suivent leur propre ordre s'en servent)."""
        if not (f.seo or f.ai):
            return None
        allowed = None
        if f.seo:
            allowed = set(self._seo_indices(tld, f.seo))
        if f.ai:
            rated = set(self._ai_indices(tld, f.ai))
            allowed = rated if allowed is None else allowed & rated
        return allowed

    def _candidates(self, f: Filters, q: str, tld: str = ""):
        """Positions à examiner, dans l'ordre, ou None pour tout l'univers."""
        u = self.universe
        if (f.seo or f.ai) and tld:
            found = None
            if f.seo:
                found = self._seo_indices(tld, f.seo)
            if f.ai:
                rated = self._ai_indices(tld, f.ai)
                found = rated if found is None else sorted(set(found) & set(rated))
            return [i for i in found if self._matches(u.label(i), f, q)] if q else found
        if q:
            return self._short_matching(q, f.mode) + self._word_matching(q, f.mode)
        if f.nature:
            return u.by_nature.get(f.nature, [])
        if f.category in wordlists.LABELS:
            return u.by_list[f.category]
        if f.lang:
            return u.by_lang.get(f.lang, [])
        if f.max_rank:
            return u.listed
        return None

    # --- Liste dans l'ordre (longueur puis nom) et export ---------------------------

    def search(self, tld: str, *args, cursor: int = -1, limit: int = 50, **kwargs) -> dict:
        f = Filters.of(*args, **kwargs)
        limit = max(1, min(limit, EXPORT_LIMIT))
        if self._hopeless(tld, f):
            return {"items": [], "cursor": None}
        codes = self.codes(f.status)
        data = self.status_bytes(tld)
        others = self._others(tld, f)
        u = self.universe
        q = self.normalize(f.q, tld)
        hits: list[int] = []
        last = cursor
        exhausted = True
        candidates = self._candidates(f, q, tld)
        if candidates is not None:
            start = bisect.bisect_right(candidates, cursor)
            for pos in range(start, len(candidates)):
                i = candidates[pos]
                last = i
                if data[i] in codes and self._good_ok(i, f) and self._taken_ok(i, others) and self._keep(u.label(i), f):
                    hits.append(i)
                    if len(hits) >= limit:
                        exhausted = pos == len(candidates) - 1
                        break
        else:
            lo, hi = self._range(f.length)
            pos = max(lo, cursor + 1)
            scanned = 0
            while pos < hi and len(hits) < limit:
                # Saut direct au prochain nom dans un des états voulus.
                found = [x for x in (data.find(c, pos, hi) for c in codes) if x != -1]
                if not found:
                    pos = hi
                    break
                i = min(found)
                scanned += i - pos + 1
                pos = i + 1
                last = i
                if self._good_ok(i, f) and self._taken_ok(i, others) and self._keep(u.label(i), f):
                    hits.append(i)
                if scanned > SCAN_BUDGET:
                    break
            exhausted = pos >= hi
        return {"items": self.describe(tld, hits, data), "cursor": None if exhausted else last}

    # --- Classement des affaires -------------------------------------------------------

    def _seo_rows(self, names: list[str]) -> dict:
        found = {}
        for chunk in range(0, len(names), 900):
            part = names[chunk:chunk + 900]
            rows = self.conn.execute(
                f"SELECT * FROM domain_seo WHERE name IN ({','.join('?' * len(part))})", part
            ).fetchall()
            found.update({r["name"]: r for r in rows})
        return found

    @property
    def weights(self) -> dict[str, float]:
        return value.weights_from(self.fr.store.get("score_weights"))

    def _bonus_context(self, tld: str) -> dict:
        return self._memo(("bonus", tld), lambda: self._build_bonus_context(tld))

    def _build_bonus_context(self, tld: str) -> dict:
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
        w = self.weights
        # Bonus le plus haut possible avec ces données : sert à arrêter le classement au plus tôt.
        parts = (value.demand(list(others))[0], 15 if gone else 0, 20)  # SEO : peut arriver à tout moment
        reach = parts[0] * w["demand"] + parts[1] * w["history"] + parts[2] * w["seo"]
        return {"others": others, "gone": gone, "seo": seo, "today": date.today(), "reach": reach,
                "reach_parts": parts, "weights": w}

    def _value(self, tld: str, i: int, label: str, ctx: dict) -> dict:
        taken = [t for t, data in ctx["others"].items() if data[i] in TAKEN_CODES]
        found = value.score(label, taken, ctx["gone"].get(i), ctx["seo"].get(f"{label}.{tld}"), ctx["today"], ctx["weights"])
        found["rank"] = self.best_rank(label)
        return found

    def rank(self, tld: str, *args, limit: int = 50, sort: str = "score", **kwargs) -> dict:
        """Les meilleurs noms d'abord. Sans recherche, parcours par score de base décroissant,
        arrêté dès qu'aucun nom restant ne peut plus entrer dans le classement.

        sort="freq" : du plus fréquent au moins fréquent (mots, formes, noms, prénoms).
        sort="length" : du plus court au plus long, puis dans l'ordre alphabétique."""
        kwargs.setdefault("status", "available")
        f = Filters.of(*args, **kwargs)
        limit = max(1, min(limit, MAX_LIMIT * 5))
        if self._hopeless(tld, f):
            return {"items": []}
        if sort == "length":
            return self._ordered(tld, f, limit)
        if sort == "freq":
            return self._by_frequency(tld, f, limit)
        if sort in ("seo", "ai"):
            # Un tri ne filtre pas : les noms analysés (ou notés) d'abord, puis les autres par score.
            first = (self._by_seo if sort == "seo" else self._by_ai)(tld, f, limit)["items"]
            if len(first) < limit:
                seen = {i["label"] for i in first}
                rest = self.rank(tld, f, limit=limit + len(first), sort="score")["items"]
                first += [i for i in rest if i["label"] not in seen][: limit - len(first)]
            return {"items": first}
        codes = self.codes(f.status, "available")
        data = self.status_bytes(tld)
        others = self._others(tld, f)
        u = self.universe
        ctx = self._bonus_context(tld)
        q = self.normalize(f.q, tld)
        best: list[tuple[int, int, int, dict]] = []  # tas : (score, -position, position, valeur)

        def consider(i: int) -> None:
            if data[i] not in codes or not self._good_ok(i, f) or not self._taken_ok(i, others):
                return
            label = u.label(i)
            if not self._keep(label, f):
                return
            found = self._value(tld, i, label, ctx)
            if f.min_score and found["score"] < f.min_score:
                return
            entry = (found["score"], -i, i, found)
            if len(best) < limit:
                heapq.heappush(best, entry)
            elif entry > best[0]:
                heapq.heapreplace(best, entry)

        candidates = self._candidates(f, q, tld)
        if candidates is not None:
            for i in candidates:
                consider(i)
        else:
            top = max(ctx["weights"]["shape"], ctx["weights"]["popularity"])
            for score in range(70, -1, -1):
                if len(best) >= limit and score * top + ctx["reach"] <= best[0][0]:
                    break
                for i in u.buckets()[score]:
                    consider(i)
        ranked = sorted(best, reverse=True)
        items = self.describe(tld, [e[2] for e in ranked], data)
        for item, entry in zip(items, ranked):
            item.update(entry[3])
        return {"items": items}

    def _scored(self, tld: str, f: Filters, hits: list[int], data, ctx) -> dict:
        items = self.describe(tld, hits, data)
        for i, item in zip(hits, items):
            item.update(self._value(tld, i, item["label"], ctx))
        return {"items": [it for it in items if not f.min_score or it["score"] >= f.min_score]}

    def _ordered(self, tld: str, f: Filters, limit: int) -> dict:
        """Du plus court au plus long. Les noms courts sont déjà dans cet ordre dans l'univers ;
        les mots (5 caractères et plus) y sont par ordre alphabétique : on suit by_length."""
        need = limit * (4 if f.min_score else 1)
        u = self.universe
        data = self.status_bytes(tld)
        lo, _ = self._range(f.length)
        hits: list[int] = []
        if lo < drops.CELLS:
            found = self.search(tld, f, limit=need)["items"]
            hits = [i for i in (u.index(item["label"]) for item in found) if i is not None and i < drops.CELLS]
        if len(hits) < need:
            codes = self.codes(f.status, "available")
            others = self._others(tld, f)
            q = self.normalize(f.q, tld)
            candidates = self._candidates(f, q, tld)
            allowed = set(candidates) if candidates is not None else None
            for i in u.by_length:
                if allowed is not None and i not in allowed:
                    continue
                if data[i] not in codes or not self._good_ok(i, f) or not self._taken_ok(i, others):
                    continue
                label = u.label(i)
                if self._matches(label, f, q) and self._keep(label, f):
                    hits.append(i)
                    if len(hits) >= need:
                        break
        return {"items": self._scored(tld, f, hits, data, self._bonus_context(tld))["items"][:limit]}

    def merge_key(self, sort: str, category: str = ""):
        """Clé d'une liste fusionnée sur plusieurs extensions : le même ordre que rank() sur une seule."""
        u = self.universe

        def where(item) -> int:
            i = u.index(item["label"])
            return i if i is not None else u.size

        if sort == "length":
            return lambda item: (where(item) if where(item) < drops.CELLS else drops.CELLS + u.position("length", where(item)), item["tld"])
        if sort == "freq":
            key = category if category in wordlists.LABELS else ""
            return lambda item: (u.position(key, where(item)), item["tld"])
        if sort == "seo":
            return lambda item: (-(item.get("_pr") or 0), -(item.get("_wb") or 0), -item["score"], where(item), item["tld"])
        if sort == "ai":
            def opinion(item):
                a = item.get("ai")
                return (0, -a["quick_high"], -a["resale"], -a["final_high"]) if a else (1, 0, 0, 0)
            return lambda item: (*opinion(item), -item["score"], where(item), item["tld"])
        return lambda item: (-item["score"], where(item), item["tld"])

    def _by_seo(self, tld: str, f: Filters, limit: int) -> dict:
        """Meilleur PageRank d'abord, puis le plus d'années d'archives ; noms analysés seulement."""
        codes = self.codes(f.status, "available")
        data = self.status_bytes(tld)
        others = self._others(tld, f)
        u = self.universe
        q = self.normalize(f.q, tld)
        hits = []
        allowed = self._restrict(tld, f)
        for i in self._seo_indices(tld, f.seo or "any", order=True):
            if allowed is not None and i not in allowed:
                continue
            label = u.label(i)
            if (data[i] in codes and self._good_ok(i, f) and self._taken_ok(i, others)
                    and self._matches(label, f, q) and self._keep(label, f)):
                hits.append(i)
        return {"items": self._scored(tld, f, hits, data, self._bonus_context(tld))["items"][:limit]}

    def _by_ai(self, tld: str, f: Filters, limit: int) -> dict:
        """Meilleure revente rapide selon l'avis IA, puis potentiel de revente ; noms notés seulement."""
        codes = self.codes(f.status, "available")
        data = self.status_bytes(tld)
        others = self._others(tld, f)
        u = self.universe
        q = self.normalize(f.q, tld)
        hits = []
        rows = self.conn.execute("SELECT name FROM domain_ai WHERE name LIKE ? ORDER BY quick_high DESC, resale DESC, "
                                 "final_high DESC, name", (f"%.{tld}",))
        allowed = self._restrict(tld, f)
        for (name,) in rows:
            label = name[: -len(tld) - 1]
            i = u.index(label)
            if allowed is not None and i not in allowed:
                continue
            if (i is not None and data[i] in codes and self._good_ok(i, f) and self._taken_ok(i, others)
                    and self._matches(label, f, q) and self._keep(label, f)):
                hits.append(i)
                if len(hits) >= limit * (4 if f.min_score else 1):
                    break
        return {"items": self._scored(tld, f, hits, data, self._bonus_context(tld))["items"][:limit]}

    def _by_frequency(self, tld: str, f: Filters, limit: int) -> dict:
        codes = self.codes(f.status, "available")
        data = self.status_bytes(tld)
        others = self._others(tld, f)
        u = self.universe
        ctx = self._bonus_context(tld)
        order = u.by_frequency.get(f.category if f.category in wordlists.LABELS else "", u.by_frequency[""])
        q = self.normalize(f.q, tld)
        allowed = self._restrict(tld, f)
        items = []
        for chunk_start in range(0, len(order), 2000):
            hits = []
            for i in order[chunk_start:chunk_start + 2000]:
                if allowed is not None and i not in allowed:
                    continue
                if data[i] not in codes or not self._good_ok(i, f) or not self._taken_ok(i, others):
                    continue
                label = u.label(i)
                if self._matches(label, f, q) and self._keep(label, f):
                    hits.append(i)
            items += self._scored(tld, f, hits, data, ctx)["items"]
            if len(items) >= limit:
                break
        return {"items": items[:limit]}

    # --- Compteurs -------------------------------------------------------------------

    def _mask(self, tld: str, f: Filters) -> int | None:
        """Les noms retenus par les filtres (hors état), un octet 0/1 par nom, sous forme d'entier."""
        u = self.universe
        parts: list[int] = []

        def from_indices(indices) -> int:
            mask = bytearray(u.size)
            for i in indices:
                mask[i] = 1
            return int.from_bytes(mask, "big")

        if f.length:
            lo, hi = self._range(f.length)
            mask = bytearray(u.size)
            mask[lo:hi] = b"\x01" * (hi - lo)
            if f.length > drops.MAX_SHORT:
                for n, w in enumerate(u.words):
                    if len(w) < f.length:
                        mask[drops.CELLS + n] = 0
            parts.append(int.from_bytes(mask, "big"))
        if f.category in wordlists.LABELS:
            parts.append(from_indices(u.by_list[f.category]))
        elif f.category in ("letters", "digits"):
            arrays = u.letters if f.category == "letters" else u.digits
            parts.append(from_indices(i for n in arrays for i in arrays[n]))
        elif f.category == "mixed":
            mask = bytearray(u.size)
            mask[:drops.CELLS] = b"\x01" * drops.CELLS
            for arrays in (u.letters, u.digits):
                for n in arrays:
                    for i in arrays[n]:
                        mask[i] = 0
            parts.append(int.from_bytes(mask, "big"))
        if f.nature:
            parts.append(from_indices(u.by_nature.get(f.nature, [])))
        if f.lang:
            parts.append(from_indices(u.by_lang.get(f.lang, [])))
        if f.max_rank:
            parts.append(from_indices(i for i in u.listed if (self.best_rank(u.label(i), f.category) or 10**9) <= f.max_rank))
        others = self._others(tld, f)
        if others:
            taken = bytes(1 if c in TAKEN_CODES else 0 for c in range(256))
            combined = 0
            for data in others:
                combined |= int.from_bytes(data.translate(taken), "big")
            parts.append(combined)
        if not f.junk:
            parts.append(u.good_int)
        if f.seo:
            parts.append(from_indices(self._seo_indices(tld, f.seo)))
        if f.ai:
            parts.append(from_indices(self._ai_indices(tld, f.ai)))
        if not parts:
            return None
        mask = parts[0]
        for part in parts[1:]:
            mask &= part
        return mask

    def facets(self, tld: str, *args, **kwargs) -> dict:
        """Nombre de noms par état, pour les filtres choisis (recherche exclue)."""
        f = Filters.of(*args, **kwargs)
        if self._hopeless(tld, f):
            return dict.fromkeys(STATUS_FILTERS, 0)
        key = ("facets", tld, f.category, f.length, f.nature, f.max_rank, f.taken, f.junk, f.seo, f.ai, f.lang)
        raw = self._memo(key, lambda: self._facets(tld, f))
        by_code = raw["by_code"]
        # Les états réservés suivent le réglage du moment : conversion à chaque appel, sans recompter.
        return {k: sum(by_code.get(c, 0) for c in self.codes(k)) for k in STATUS_FILTERS}

    def _facets(self, tld: str, f: Filters) -> dict:
        """Comptes par octet d'état, et le masque des filtres : touch() s'en sert pour corriger de ±1."""
        data = self.status_bytes(tld)
        mask = self._mask(tld, f)
        by_code = {}
        for c in LABELS:
            if mask is None:
                by_code[c] = data.count(c)
            else:
                only = bytes(1 if x == c else 0 for x in range(256))
                by_code[c] = (int.from_bytes(data.translate(only), "big") & mask).bit_count()
        return {"by_code": by_code, "mask": mask}

    # --- Mise à jour d'un seul nom -------------------------------------------------

    def _code_for(self, tld: str, i: int, label: str) -> tuple[int, int]:
        """(octet de base, octet affiché) d'un nom, relus en base pour ce seul nom."""
        if tld == "fr":
            row = self.conn.execute("SELECT removed_on FROM fr_names WHERE label = ?", (label,)).fetchone()
            base = ABSENT if row is None else GONE if row["removed_on"] else REGISTERED
        else:
            base = self._base(tld)[i]
        code = base
        if tld == "fr" and code in (GONE, ABSENT):
            seen = self.conn.execute("SELECT present FROM name_dns WHERE label = ?", (label,)).fetchone()
            if seen and seen["present"]:
                code = REGISTERED
        drop = self.conn.execute("SELECT state FROM drops WHERE name = ?", (f"{label}.{tld}",)).fetchone()
        state = drop["state"] if drop else None
        if state in drops.TRACKED:
            code = COMING
        elif state == states.AVAILABLE:
            code = FREE
        elif state == drops.SUSPENDED:
            code = SUSPENDED
        elif state == drops.TAKEN:
            code = REGISTERED
        if self.reserved is not None and f"{label}.{tld}" in self.reserved.names() and code in (GONE, ABSENT, FREE, COMING):
            code = RESERVED
        elif self.reserved is not None and code in (ABSENT, FREE) and self.reserved.short_reserved(f"{label}.{tld}"):
            code = RESERVED
        return base, code

    def touch(self, name: str) -> None:
        """Un nom a changé (vérification, marquage, SEO) : on corrige ce nom seulement, en place.
        Octet d'état, compteurs (±1), historique .fr et SEO du score ; la carte se refait depuis
        les compteurs. Aucun recalcul d'ensemble."""
        label, _, tld = name.partition(".")
        i = self.universe.index(label)
        if i is None or not tld:
            return
        base, new = self._code_for(tld, i, label)
        cached = self._cache.get(tld)
        if cached:
            cached[2][i] = base
        changed = False
        for key, (generation, at, value) in list(self._memos.items()):
            kind = key[0][0]
            if kind == "status" and key[0][1] == tld and value[i] != new:
                old, value[i] = value[i], new
                changed = True
                for fkey, (_, _, raw) in self._memos.items():
                    if fkey[0][0] == "facets" and fkey[0][1] == tld and fkey[1] == key[1]:
                        mask = raw["mask"]
                        if mask is None or (mask >> ((self.universe.size - 1 - i) * 8)) & 1:
                            raw["by_code"][old] = raw["by_code"].get(old, 0) - 1
                            raw["by_code"][new] = raw["by_code"].get(new, 0) + 1
            elif kind == "bonus" and key[0][1] == tld:
                if tld == "fr":
                    row = self.conn.execute("SELECT created_on, removed_on FROM fr_names WHERE label = ?", (label,)).fetchone()
                    if row and row["removed_on"]:
                        value["gone"][i] = {"created_on": row["created_on"], "removed_on": row["removed_on"]}
                    else:
                        value["gone"].pop(i, None)
                seo = self.conn.execute("SELECT * FROM domain_seo WHERE name = ?", (name,)).fetchone()
                if seo is not None:
                    value["seo"][name] = seo
        # Le SEO du nom a pu changer : les compteurs filtrés sur le SEO sont recomptés.
        for key in [k for k in self._memos if k[0][0] == "facets" and k[0][1] == tld and k[0][8]]:
            del self._memos[key]
        if changed:
            self._forget("map")
            # Les compteurs « pris ailleurs » des autres extensions dépendent de celle-ci.
            for key in [k for k in self._memos if k[0][0] == "facets" and k[0][1] != tld and k[0][6]]:
                del self._memos[key]

    def ai_changed(self) -> None:
        """Nouveaux avis IA : les compteurs filtrés sur l'avis sont recomptés, rien d'autre."""
        for key in [k for k in self._memos if k[0][0] == "facets" and k[0][9]]:
            del self._memos[key]

    def _forget(self, kind: str) -> None:
        for key in [k for k in self._memos if k[0][0] == kind]:
            del self._memos[key]

    def refresh_settings(self) -> None:
        """Barème ou mode des réservés changé : on met à jour ce qui en dépend, sans recompter."""
        weights = self.weights
        for key, (_, _, value) in self._memos.items():
            if key[0][0] == "bonus":
                value["weights"] = weights
                value["reach"] = (value["reach_parts"][0] * weights["demand"] + value["reach_parts"][1] * weights["history"]
                                  + value["reach_parts"][2] * weights["seo"])
        self._forget("map")

    def map(self) -> dict:
        return self._memo(("map",), self._build_map)

    def _build_map(self) -> dict:
        """Carte extension x catégorie : noms à saisir par case (noms qui ont un sens seulement)."""
        tlds = self.tlds()
        cells = {}
        for tld in tlds:
            # Mêmes compteurs que la liste : une case et le filtre qu'elle ouvre donnent le même nombre.
            cells[tld] = {
                "short": self.facets(tld, length=-3)["available"],
                "l4": self.facets(tld, category="letters", length=4)["available"],
                **{key: self.facets(tld, category=key)["available"] for key in wordlists.LABELS},
            }
        filters = {"short": ("", -3), "l4": ("letters", 4), **{k: (k, None) for k in wordlists.LABELS}}
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
        details = rdapinfo.load(self.conn, names)
        opinions = ai.load(self.conn, names)
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
                "status_label": ((ABSENT_FR if tld == "fr" else ABSENT_ZONE) if code == ABSENT
                                 else DROP_LABELS.get(drop["state"], LABELS[code]) if code == COMING and drop
                                 else LABELS.get(code, "")),
                "created_on": fr["created_on"] if fr else None,
                # Une sortie de zone repérée puis démentie (nom repris) n'est pas une suppression.
                "removed_on": (fr["removed_on"] if fr and fr["removed_on"]
                               else drop["removed_on"] if drop and drop["state"] != drops.TAKEN else None),
                "expires_at": fr["expires_at"] if fr else None,
                "checked_at": max(filter(None, [fr["checked_at"] if fr else None, drop["checked_at"] if drop else None]), default=None),
                "next_check_at": (drop["next_check_at"] if drop and drop["next_check_at"] else fr["next_check_at"] if fr else None),
                "drop_state": drop["state"] if drop else None,
                "release_at": drop["release_at"] if drop else None,
                "available_since": drop["available_since"] if drop else None,
                "watched": name in watched,
                "rdap": details.get(name),
                "reserved": self.reserved.why(name) if self.reserved is not None and code == RESERVED else [],
                "ai": opinions.get(name),
            })
        return items

    def value_of(self, names: list[str]) -> dict[str, dict]:
        """Score, raisons et filtres d'une liste de noms (écran Suppressions), mêmes calculs que l'Explorer."""
        found = {}
        u = self.universe
        for name in names:
            label, _, tld = name.partition(".")
            i = u.index(label)
            if i is None:
                found[name] = {"score": 0, "reasons": [], "parts": {}, "natures": [], "rank": None, "good": False}
                continue
            item = self._value(tld, i, label, self._bonus_context(tld))
            item.update(natures=sorted(wordlists.natures().get(label, ())), good=bool(u.good[i]), length=len(label),
                        langs=wordlists.langs_of(label))
            found[name] = item
        opinions = ai.load(self.conn, list(found))
        for name, item in found.items():
            item["ai"] = opinions.get(name)
        return found

    def fiche(self, name: str) -> dict:
        """Tout ce qu'on sait d'un nom, pour la fiche commune à Domaines, Suppressions et Explorer."""
        label, _, tld = name.partition(".")
        card = self.card(label)
        items = {i["tld"]: i for i in card["items"]}
        main = items.get(tld)
        domain = self.conn.execute("SELECT * FROM domains WHERE name = ?", (name,)).fetchone()
        rdap = (main or {}).get("rdap") or rdapinfo.load(self.conn, [name]).get(name)
        fresh = domain is not None and not domain["check_error"] and domain["last_success_at"]
        if fresh and (rdap is None or domain["last_success_at"] > (rdap.get("at") or "")):
            # Domaine surveillé : sa dernière vérification est la plus fraîche.
            rdap = {
                "at": domain["last_success_at"], "source": domain["source"] or "rdap",
                "available": domain["state"] == states.AVAILABLE,
                "statuses": json.loads(domain["epp_statuses"] or "[]"), "registrar": domain["registrar"],
                "registered_at": domain["registered_at"], "updated_at": domain["updated_at"],
                "expires_at": domain["expires_at"], "nameservers": json.loads(domain["nameservers"] or "[]"),
            }
        seo = self.conn.execute("SELECT * FROM domain_seo WHERE name = ?", (name,)).fetchone()
        return {
            "name": name, "label": label, "tld": tld, "known": card["known"],
            "item": main,
            "others": [i for t, i in items.items() if t != tld],
            "categories": card.get("categories", []),
            "natures": sorted(wordlists.natures().get(label, ())),
            "rank": self.best_rank(label),
            "seo": dict(seo) if seo else None,
            "rdap": rdap,
            "ai": ai.load(self.conn, [name]).get(name),
            "dns": frdns.load(self.conn, [label]).get(label) if tld == "fr" else None,
            "domain_id": domain["id"] if domain is not None else None,
            "rdap_next": self._rdap_next(name, tld, domain),
        }

    def _rdap_next(self, name: str, tld: str, domain) -> dict:
        """Prochaine lecture RDAP prévue pour ce nom, et pourquoi ; sinon, pourquoi aucune."""
        if domain is not None:
            return {"at": domain["next_check_at"], "why": "domaine surveillé"}
        drop = self.conn.execute("SELECT state, next_check_at FROM drops WHERE name = ?", (name,)).fetchone()
        if drop is not None and drop["next_check_at"]:
            return {"at": drop["next_check_at"], "why": "suppression suivie"}
        if tld == "fr":
            row = self.conn.execute("SELECT next_check_at, removed_on FROM fr_names WHERE label = ?",
                                    (name.partition(".")[0],)).fetchone()
            if row is not None and row["next_check_at"]:
                return {"at": row["next_check_at"], "why": "lendemain de la date anniversaire"}
            if row is not None and row["removed_on"]:
                return {"at": None, "why": "supprimé d'après l'Afnic : vérification à la demande"}
            return {"at": None, "why": "jamais enregistré d'après l'Afnic : vérification à la demande"}
        return {"at": None, "why": "aucune lecture automatique : vérification à la demande ou en le surveillant"}

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
            rdapinfo.save(self.conn, name, result)
        if result.error:
            raise VerifyError(result.error)
        now = utcnow()
        registered = not result.available and states.compute_state(
            result.info.statuses, result.info.expires_at, now, 0) not in drops.TRACKED
        row = self.drops.row(name)
        state = None
        # Hors .fr, la zone ne dit rien d'un nom enregistré sans DNS (hold) : on garde la réponse.
        if row is not None or not registered or tld != "fr":
            row = row or self.drops.ensure(name, now)
            state = self.drops.apply(row, result, now)
        if tld == "fr":
            if state is not None:
                self.fr._sync_name(label, result, state)
            if registered and row is None:
                self.fr._sync_name(label, result, drops.TAKEN)
            elif not registered:
                self.conn.execute("UPDATE fr_names SET checked_at = ?, next_check_at = NULL WHERE label = ?", (iso(now), label))
        self.touch(name)
        i = self.universe.index(label)
        return self.describe(tld, [i])[0] if i is not None else {"name": name}


class VerifyError(Exception):
    pass

"""Explorer : tous les noms suivis, extension par extension, avec leur état connu.

L'univers : les 1 926 220 noms possibles de 1 à 4 caractères, puis les noms des listes de 5 caractères
et plus (mots, noms de famille, prénoms, listes fournies, Mes ajouts). Il ne change qu'avec Mes ajouts
(reload_lists), sans redémarrage. Pour chaque extension, un octet d'état par nom
(environ 2 Mo) est tiré des données déjà sur le NAS, sans aucune requête :

- zones ICANN : carte de bits des noms courts et liste des mots présents, lues chaque jour ;
- .fr : open data de l'Afnic (fr_names) ;
- .co, .ai, .io : présence dans le DNS du registre (cc_dns), pour les noms interrogés chaque nuit ;
- par-dessus, l'écran Suppressions (table drops), plus récent.

Recherche, filtres et comptages se font sur ces octets : bytes.find et bytes.count tournent en C.
"""

import bisect
import json
import math
import operator
import heapq
import itertools
import re
import threading
import time
from array import array
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from . import ai, ccdns, drops, frdns, gandi, precompute, prenoms, quality, rdapinfo, states, value, wordlists
from .checker import iso, utcnow

# États, un octet par nom.
INVALID, REGISTERED, GONE, ABSENT, COMING, FREE, SUSPENDED, RESERVED, PREMIUM, UNCONFIRMED = range(10)

LABELS = {
    REGISTERED: "Enregistré",
    GONE: "Supprimé, non repris",
    ABSENT: "Jamais vu",
    COMING: "Libération à venir",
    FREE: "Libre (vérifié)",
    SUSPENDED: "Enregistré, suspendu (hold)",
    RESERVED: "Réservé",
    PREMIUM: "Premium probable",
    # WHOIS seul (.co, .io, .ai…) : « inconnu » ne dit rien des noms réservés ou premium du registre.
    UNCONFIRMED: "Libre selon le WHOIS, à confirmer",
}
# 3 caractères libres sur une extension générique : souvent vendus au prix premium par le registre.
# Rien ne le confirme sans accès registrar ; l'Explorer les masque sauf si on demande à les voir.
PREMIUM_LENGTH = 3
# .com et .net : prix unique imposé au registre (Verisign), jamais de premium.
PREMIUM_OFF = frozenset(("com", "net"))
# Niveaux de l'Explorer et de la Veille ; classements selon l'usage.
LEVEL_GOOD, LEVEL_GEM = 30, 50
LENSES = ("resale", "exploit")
# Pour une zone ICANN, l'absence veut seulement dire « pas dans la zone » : un nom enregistré
# sans serveurs DNS n'y figure pas non plus. Pour le .fr, l'open data liste aussi l'historique.
ABSENT_ZONE = "Absent de la zone, à vérifier"
# Libération à venir : l'état précis, avec les libellés de la légende de l'écran Domaines.
DROP_LABELS = {"expired": "Expiré", "redemption": "En rédemption", "pending_delete": "Suppression imminente"}
ABSENT_FR = "Jamais enregistré"
# .co, .ai, .io : pas de zone, seulement le DNS du registre. Absent du DNS ne veut pas dire libre
# (enregistré sans serveurs, réservé ou premium du registre) ; jamais interrogé : rien n'est connu.
ABSENT_DNS = "Absent du DNS, à vérifier"
NOT_ASKED = "Pas interrogé"
# Gandi (gandi.py) : sa réponse précise l'état d'un nom non pris, jamais celui d'un nom enregistré ou en
# rédemption. Libre au prix standard : libre ; premium : premium, au prix réel ; indisponible, réservé ou
# restreint : réservé (un nom qu'on croyait libre n'est plus jamais affiché libre). Un terme réservé d'une
# liste (Afnic, communes) ou marqué à la main le reste. Une réponse en erreur ne change rien.
GANDI_OPEN = (GONE, ABSENT, UNCONFIRMED, FREE, PREMIUM, RESERVED)
FREE_GANDI = LABELS[FREE]


def gandi_effect(code: int, check: dict | None, since: str | None = None, checked: str | None = None,
                 listed: bool = False) -> tuple[int, str | None]:
    """La seule règle de l'effet d'une réponse de Gandi sur l'état d'un nom, pour tous les écrans (Explorer, fiche,
    Veille, Aujourd'hui, Domaines) : (état, sorte qui le fait, ou None). Sans effet : réponse en erreur, d'avant le
    dernier changement d'état (since), indisponible pas encore revu au registre (checked : gandi.applies) ; nom
    enregistré, bientôt libre ou suspendu (hors GANDI_OPEN) ; terme d'une liste de réservés (listed)."""
    if not gandi.applies(check, since, checked) or code not in GANDI_OPEN or (code == RESERVED and listed):
        return code, None
    kind = check["kind"]
    if kind == "available":
        return FREE, kind
    if kind == "premium":
        return PREMIUM, kind
    if kind in gandi.BLOCKED:
        return RESERVED, kind
    return code, None


def drop_code(state: str | None) -> int | None:
    """État de l'Explorer d'un état de la Veille (registre), None sans vérification."""
    if state in drops.TRACKED:
        return COMING
    return {states.AVAILABLE: FREE, drops.UNCONFIRMED: UNCONFIRMED, drops.SUSPENDED: SUSPENDED,
            drops.TAKEN: REGISTERED, states.RENEWED: REGISTERED}.get(state)


def drop_open_code(state: str | None, checked: str | None, delegated: str | None) -> int | None:
    """drop_code, la plus récente observation l'emportant : un nom libre au registre puis vu délégué dans le DNS
    (delegated : date) a été enregistré depuis ; la revérification au registre suit (frdns.changed)."""
    code = drop_code(state)
    if code in (FREE, UNCONFIRMED) and delegated and delegated > (checked or ""):
        return REGISTERED
    return code


def absent_label(tld: str) -> str:
    """Libellé d'un nom absent de la source de l'extension : open data .fr, zone ICANN ou DNS."""
    return ABSENT_FR if tld == "fr" else ABSENT_DNS if tld in ccdns.TLDS else ABSENT_ZONE

STATUS_FILTERS = {
    "available": (GONE, ABSENT, UNCONFIRMED, FREE, COMING),
    "bookable": (GONE, ABSENT, UNCONFIRMED, FREE),
    "maybe_free": (GONE, ABSENT, UNCONFIRMED),
    "coming": (COMING,),
    "free": (FREE,),
    "registered": (REGISTERED,),
    "suspended": (SUSPENDED,),
    "reserved": (RESERVED,),
    "premium": (PREMIUM,),
    "all": (GONE, ABSENT, UNCONFIRMED, FREE, COMING, REGISTERED, SUSPENDED),
}
STATUS_NAMES = {
    "available": "Tous non pris",
    "bookable": "À réserver",
    "maybe_free": "Probablement libres",
    "coming": "Libération à venir",
    "free": "Libres vérifiés",
    "registered": "Enregistrés",
    "suspended": "Suspendus",
    "reserved": "Réservés",
    "premium": "Premiums probables",
    "all": "Tous, même pris",
}
# États qu'un terme réservé (liste ou marquage) remplace : tout ce qui n'est pas enregistré.
RESERVABLE = (GONE, ABSENT, UNCONFIRMED, FREE, COMING)
# Réservés (si on le demande dans Paramètres) et premiums mêlés aux noms à saisir maintenant. Pas « Bientôt libres » :
# un premium s'achète tout de suite, un réservé ne se libère pas.
WITH_RESERVED = {"available", "bookable", "maybe_free", "free", "all"}
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
    **wordlists.SHOWN,
}

# Lignes de la carte : catégorie -> libellé.
# Chaque case correspond à un filtre : catégorie et longueur.
MAP_ROWS = {
    "short": "1 à 3 caractères",
    "l4": "4 lettres",
    **wordlists.SHOWN,
}

CACHE_SECONDS = 600
# Sécurité seulement : chaque nom vérifié ou marqué est corrigé en place (touch), et un import ou
# une lecture de zone change la version des données, donc la clé.
MEMO_SECONDS = 6 * 3600
# Classements gardés en mémoire : courts, les dates (libération, libre depuis) et les vérifications avancent.
RANK_SECONDS = 600
# Extensions dont l'écran par défaut est classé d'avance (warm) : la première ouverte (.fr) et le .com.
WARM_TLDS = ("fr", "com")
MAP_SECONDS = 600  # carte refaite en arrière-plan au-delà
MEMO_RANKS = 40
# « Les plus courts » avec un niveau ou un score minimum : noms notés au plus, pour une page.
ORDERED_BUDGET = 50_000
MEMO_FACETS = 24  # jeux de filtres gardés, environ 2 Mo chacun
MAX_LIMIT = 200
EXPORT_LIMIT = 50_000
# Une page sans résultat ne parcourt pas plus que ceci : la suite se demande avec le curseur.
SCAN_BUDGET = 4_000_000

_EXPAND = [bytes((b >> i) & 1 for i in range(8)) for b in range(256)]
_PRESENT_TO_STATUS = bytes([ABSENT, REGISTERED]) + bytes(254)


def _words(extra=()) -> list[str]:
    """Noms des listes de plus de 4 caractères, triés : ils suivent les noms courts dans l'univers
    (liste du précalcul des scores, dans le même ordre, avec les noms de Mes ajouts qui n'y sont pas)."""
    found = precompute.words()
    known = set(found)
    added = sorted({w for w in extra if len(w) > drops.MAX_SHORT and w not in known})
    return list(heapq.merge(found, added)) if added else found


# Filtre sur l'avis IA : verdicts et bornes (basse, haute) de chaque note et de chaque prix.
AI_VERDICTS = {"take": "À prendre", "watch": "À surveiller", "skip": "À laisser"}
# Fiche : comparables (noms notés de la même famille, longueur à ±1 près) et repères du portefeuille.
COMPARABLES = 3
COMPARABLE_LENGTH = 1
# Ligne de temps : états de la Veille datés par state_since.
TIMELINE_DROP_STATES = {states.EXPIRED: "Expiré", states.REDEMPTION: "Entré en rédemption",
                        states.PENDING_DELETE: "En suppression", drops.TAKEN: "Repris",
                        states.RENEWED: "Renouvelé d'office, à confirmer",
                        drops.UNCONFIRMED: "Libre selon le WHOIS, à confirmer"}
AI_FIELDS = {"exploit": "Exploitation", "resale": "Revente", "quick_low": "Revente rapide bas",
             "quick_high": "Revente rapide haut", "final_low": "Prix final bas", "final_high": "Prix final haut"}


def ai_summary(opinion: dict | None) -> dict | None:
    """Verdict, note de revente et prix final d'un avis : ce qu'une autre extension montre de lui."""
    if not opinion:
        return None
    return {"verdict": (opinion.get("comment") or "").split(".")[0], "resale": opinion.get("resale"),
            "final_low": opinion.get("final_low"), "final_high": opinion.get("final_high"), "grade": opinion.get("grade")}


def ai_filter(verdicts: list[str], ranges: dict[str, tuple[int | None, int | None]], grades: list[str] = ()) -> str:
    """Forme canonique et stable (clé de mémoire) ; vide si aucun critère."""
    parts = []
    marks = [g for g in ai.GRADES if g in grades]
    if marks:
        parts.append("g=" + ",".join(marks))
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
        if key == "g":
            grades = [g for g in val.split(",") if g in ai.GRADES]
            if grades:
                where.append(f"({ai.grade_sql()}) IN ({','.join('?' * len(grades))})")
                args += grades
        elif key == "v":
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


def split_cats(category: str | None) -> list[str]:
    """Catégories d'un filtre : une ou plusieurs, séparées par des virgules (cat=firstname,surname), triées ;
    cat=sector se lit comme cat=premium (wordlists.ALIASES)."""
    return sorted({wordlists.canonical(c.strip()) for c in (category or "").split(",") if c.strip()})


def freq_key(category: str | None) -> str:
    """Ordre « les plus fréquents » : les listes du filtre de catégorie (une ou plusieurs), sinon toutes ("")."""
    return ",".join(c for c in split_cats(category) if c in wordlists.LABELS)


@dataclass
class Filters:
    q: str = ""
    mode: str = "prefix"
    status: str = ""
    category: str = ""  # une ou plusieurs (union), séparées par des virgules
    length: int | None = None  # > 0 exactement, 5 : 5 et plus, < 0 : au plus
    nature: str = ""  # verb, noun, adj, adv, other
    max_rank: int | None = None  # dans les N plus fréquents de sa liste
    taken: str = ""  # "any" ou une extension : pris ailleurs
    min_score: int | None = None
    junk: bool = False  # True : garder aussi les suites imprononçables, les mélanges, les tirets
    seo: str = ""  # any : analysé avec signal ; pr1, pr3, pr5 : PageRank minimum ; wb5 : archivé 5 ans et plus
    ai: str = ""  # avis IA, forme canonique de ai_filter() : « v=take,watch;exploit=40-;quick_high=-500 »
    lang: str = ""  # fr ou en (wordlists.LANGS)
    premium: bool = False  # True : les premiums (probables ou vus chez Gandi) se mêlent aux noms à saisir
    gprice: int | None = None  # prix d'achat Gandi au plus, en euros : seulement les noms vérifiés chez Gandi
    level: str = ""  # gem : pépites ; good : bons (score ou avis IA favorable) ; vide : tout

    def __post_init__(self):
        # Forme canonique (triée, sans doublon) : même clé de mémoire quel que soit l'ordre des puces.
        self.category = ",".join(split_cats(self.category))

    @property
    def cats(self) -> list[str]:
        return self.category.split(",") if self.category else []

    @classmethod
    def of(cls, *args, **kwargs) -> "Filters":
        if args and isinstance(args[0], Filters):
            return args[0]
        names = ["q", "mode", "status", "category", "length"]
        kwargs.update(zip(names, args))
        return cls(**{k: v for k, v in kwargs.items() if k in cls.__dataclass_fields__})


def lens_key(opinion: dict, lens: str) -> tuple:
    """Classement « pour revendre / pour exploiter » d'un nom noté : palier, note choisie, puis l'autre note et le
    prix final bas. Sans ces départages, des centaines de noms à 60 sortaient dans l'ordre alphabétique.
    Pour revendre, la note de A à E fait le palier et le rendement attendu départage, avant la note de revente."""
    other = "exploit" if lens == "resale" else "resale"
    tail = (opinion[lens], opinion.get(other) or 0, opinion.get("final_low") or 0)
    if lens == "resale":
        gain = opinion.get("yield")
        return (lens_tier(opinion, lens), gain if gain is not None else NO_YIELD, *tail)
    return (lens_tier(opinion, lens), *tail)


# Palier « pour revendre » d'un nom noté, selon sa note ; 1 : sans avis, 0 : « À laisser ». Un nom conseillé est
# toujours au-dessus de 1 (parcours des conseillés d'abord, dans rank).
GRADE_TIERS = {"A": 5, "B": 4, "C": 3, "D": 2}
NO_YIELD = -1e9  # avis sans rendement calculé : après ceux qui en ont un, dans leur palier


def lens_tier(opinion: dict | None, lens: str = "") -> int:
    """Ordre « pour revendre / pour exploiter » : 2 et plus conseillé par l'IA, 1 sans avis, 0 déconseillé."""
    if not opinion:
        return 1
    verdict = opinion.get("verdict") or (opinion.get("comment") or "").split(".")[0]
    if verdict == "À laisser":
        return 0
    return GRADE_TIERS.get(opinion.get("grade"), 2) if lens == "resale" else 2


class Universe:
    def __init__(self, previous: "Universe | None" = None):
        # Mes ajouts (wordlists.MINE) font partie de l'univers : il est refait quand ils changent (reload_lists).
        self.mine = wordlists.mine()
        self.words = _words(self.mine)
        self.word_index = {w: i for i, w in enumerate(self.words)}
        self.size = drops.CELLS + len(self.words)
        # Catégories : listes d'origine et noms dérivés (wordlists.classes) ; le score ne voit que les premières.
        lists = wordlists.classes()
        # Position de chaque nom des listes, calculée une fois : les index ci-dessous la relisent (deux fois plus
        # rapide que index() à chaque usage, l'univers se refait à chaque changement de Mes ajouts).
        positions = {w: i for w in wordlists.listed() if (i := self.index(w)) is not None}

        def at(w: str) -> int | None:
            i = positions.get(w)
            return i if i is not None else self.index(w)

        # Noms des listes, par position : le filtre par catégorie ne parcourt qu'eux.
        self.listed = sorted(positions.values())
        self.by_list = {key: array("i", sorted(positions[w] for w in names if w in positions))
                        for key, names in lists.items()}
        # Positions des noms en lettres seules et en chiffres seuls, par longueur (sans rapport avec les listes :
        # reprises de l'univers précédent).
        if previous is not None:
            self.letters, self.digits = previous.letters, previous.digits
        else:
            self.letters = {n: array("i", (drops.encode("".join(p)) for p in itertools.product(drops.CHARSET[:26], repeat=n)))
                            for n in range(1, drops.MAX_SHORT + 1)}
            self.digits = {n: array("i", (drops.encode("".join(p)) for p in itertools.product(drops.CHARSET[26:36], repeat=n)))
                           for n in range(1, drops.MAX_SHORT + 1)}
        self.letters4, self.digits4 = self.letters[4], self.digits[4]
        self._buckets: list[array] | None = None
        self.base_table: bytearray | None = None
        self._good_letters: dict[int, array] = {}
        self._lock = threading.Lock()
        self.good: bytearray | None = None
        # Noms des listes du plus fréquent au moins fréquent, par liste et toutes listes confondues.
        ranks = wordlists.ranks()
        best: dict[int, float] = {}
        for key, r in ranks.items():
            for w, n in r.items():
                i = positions.get(w)
                if i is not None:
                    best[i] = min(best.get(i, math.inf), wordlists.relative(key, n))
        # Rang relatif de chaque nom d'une catégorie : dans sa liste, le meilleur de ses sources pour une catégorie qui
        # en réunit plusieurs (wordlists.MERGED : le 50e prénom des États-Unis à côté du 50e prénom français) ; nom
        # dérivé, son meilleur rang relatif dans les listes d'où vient la catégorie (wordlists.DERIVED_FROM :
        # assurance, 1er terme star, parmi les premiers termes stars, pas au rang de « it » dans les mots anglais),
        # après les mots du thème pour un thème.
        self.relative: dict[str, dict[int, float]] = {}
        gained = wordlists.derived()
        for key, r in ranks.items():
            found = {positions[w]: n / len(r) for w, n in r.items() if w in positions}
            for source in wordlists.sources(key)[1:]:
                for w, n in ranks[source].items():
                    i = positions.get(w)
                    if i is not None:
                        found[i] = min(found.get(i, math.inf), wordlists.relative(source, n))
            after = 1.0 if key in wordlists.THEMES else 0.0
            sources = [ranks[k] for k in wordlists.DERIVED_FROM.get(key, ())]
            for w in gained.get(key, ()):
                i = positions.get(w)
                if i is not None and i not in found:
                    found[i] = min((s[w] / len(s) for s in sources if w in s), default=1.0) + after
            self.relative[key] = found
        self.by_frequency = {key: array("i", sorted(found, key=lambda i, f=found: (f[i], i)))
                             for key, found in self.relative.items()}
        self.by_frequency[""] = array("i", sorted(best, key=best.get))
        # Unions de listes (plusieurs catégories cochées), faites à la demande.
        self._unions: dict[str, array] = {}
        # Noms de 5 caractères et plus, du plus court au plus long puis par ordre alphabétique (tri stable : les
        # mots sont déjà dans l'ordre alphabétique).
        lengths = [len(w) for w in self.words]
        self.by_length = array("i", (drops.CELLS + n for n in sorted(range(len(lengths)), key=lengths.__getitem__)))
        self._positions: dict = {}
        natures = wordlists.natures()
        self.by_nature = {key: array("i", sorted(i for i in (at(w) for w, ks in natures.items() if key in ks)
                                                if i is not None))
                          for key in wordlists.NATURES}
        self.by_lang = {key: array("i", sorted(i for i in (at(w) for w in names) if i is not None))
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
            # Score de base précalculé (construction de l'image) : forme et sens, la meilleure extension.
            table = self._table()
            # Classement : noms de 1 à 3 caractères, 4 lettres et 4 chiffres, noms des listes. Les 4
            # caractères mélangés (1,4 million) valent trop peu pour y entrer : leur octet est à 0.
            # Noms courts : rangés une fois par processus (_short_part), copiés ici ; un nom court de Mes ajouts
            # dont l'octet diffère y est déplacé.
            short_buckets, short_good, static = _short_part()
            buckets = [array("i", b) for b in short_buckets]
            good = bytearray(short_good)
            good += b"\x01" * (self.size - drops.CELLS)  # noms des listes de 5 caractères et plus
            for w in self.mine:
                i = drops.encode(w) if len(w) <= drops.MAX_SHORT else None
                if i is None or table[i] == static[i]:
                    continue
                if static[i] & 127:
                    old = buckets[static[i] & 127]
                    del old[bisect.bisect_left(old, i)]
                good[i] = 1 if table[i] & precompute.GOOD else 0
                if table[i] & 127:
                    found = buckets[table[i] & 127]
                    found.insert(bisect.bisect_left(found, i), i)
            for i in range(drops.CELLS, self.size):
                buckets[table[i] & 127].append(i)
            self.base_table = table  # borne du score de forme et de sens, nom par nom
            self.good = good
            self.good_int = int.from_bytes(good, "big")
            self._buckets = buckets
            return buckets

    def _table(self) -> bytearray:
        """Table du précalcul, avec les noms de Mes ajouts absents des listes de l'image notés ici (une
        centaine de microsecondes par nom). Les autres noms gardent leur octet : Mes ajouts n'entrent pas
        dans le score."""
        static = precompute.table()
        if not self.mine:
            return static
        table = bytearray(self.size)
        table[:drops.CELLS] = static[:drops.CELLS]
        known = wordlists.static_lists()
        image = set().union(*known.values())
        position = drops.CELLS
        for n, w in enumerate(self.words):
            if w in image:
                table[drops.CELLS + n] = static[position]
                position += 1
            else:
                table[drops.CELLS + n] = precompute.byte(w)
        for w in self.mine:
            if len(w) <= drops.MAX_SHORT and w not in image:
                # Nom court ajouté à la main : il a un sens pour qui l'a ajouté, et son score de base.
                table[drops.encode(w)] = precompute.byte(w)
        return table

    def good_letters(self, n: int) -> array:
        """Noms de n lettres qui ont un sens ou se prononcent (après buckets())."""
        found = self._good_letters.get(n)
        if found is None:
            self.buckets()
            found = self._good_letters[n] = array("i", (i for i in self.letters[n] if self.good[i]))
        return found

    def shape_class(self, i: int) -> str:
        """Forme d'un nom pour les taux de base : L4g (4 lettres qui se prononcent), L4b, D4, M4…, W (mot)."""
        if i >= drops.CELLS:
            return "W"
        label = drops.decode(i)
        n, kind = len(label), drops.kind_of(label)
        if kind == "letters":
            return f"L{n}{'g' if self.good is not None and self.good[i] else 'b'}"
        return f"{'D' if kind == 'digits' else 'M'}{n}"

    def frequency(self, key: str) -> array:
        """Noms du plus fréquent au moins fréquent : une liste, toutes (""), ou plusieurs (« firstname,surname »,
        par le meilleur rang relatif, comme pour toutes)."""
        found = self.by_frequency.get(key)
        if found is None:
            best: dict[int, float] = {}
            for k in key.split(","):
                for i, n in self.relative[k].items():
                    best[i] = min(best.get(i, 2.0), n)
            found = self.by_frequency[key] = array("i", sorted(best, key=lambda i: (best[i], i)))
        return found

    def in_lists(self, key: str) -> array:
        """Noms d'une ou plusieurs listes (union), dans l'ordre de l'univers."""
        if "," not in key:
            return self.by_list[key]
        found = self._unions.get(key)
        if found is None:
            found = self._unions[key] = array("i", sorted({i for k in key.split(",") for i in self.by_list[k]}))
        return found

    def position(self, order_key: str, i: int) -> int:
        """Place d'un nom dans un ordre de la liste (fréquence, longueur), pour fusionner des listes."""
        found = self._positions.get(order_key)
        if found is None:
            order = self.by_length if order_key == "length" else self.frequency(order_key)
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


_SHORT: tuple[list[array], bytearray, bytes] | None = None


def _short_part() -> tuple[list[array], bytearray, bytes]:
    """Noms courts du précalcul, rangés par score de base, et leurs octets « a un sens » : ne dépendent pas de
    Mes ajouts, donc faits une fois par processus (1,9 million de noms : 0,4 s à chaque rechargement sinon)."""
    global _SHORT
    if _SHORT is None:
        table = precompute.table()
        buckets = [array("i") for _ in range(71)]
        good = bytearray(drops.CELLS)
        for i in range(drops.CELLS):
            byte = table[i]
            if byte & precompute.GOOD:
                good[i] = 1
            if byte & 127:
                buckets[byte & 127].append(i)
        _SHORT = (buckets, good, bytes(table[:drops.CELLS]))
    return _SHORT


_UNIVERSE: Universe | None = None
_UNIVERSE_LOCK = threading.Lock()


def shared_universe() -> Universe:
    """L'univers ne dépend que des listes de noms : une seule instance par processus, refaite quand
    Mes ajouts changent (les noms courts tirés de l'ancienne sont repris).
    Verrou : au démarrage, la préparation en tâche de fond et une première requête l'appellent en
    même temps depuis deux fils ; sans lui, deux univers seraient construits (mémoire doublée)."""
    global _UNIVERSE
    with _UNIVERSE_LOCK:
        if _UNIVERSE is None or _UNIVERSE.mine != wordlists.mine():
            _UNIVERSE = Universe(_UNIVERSE)
        return _UNIVERSE


class Explorer:
    def __init__(self, conn, drops_watcher: drops.DropWatcher, fr_watcher, reserved=None):
        self.conn = conn
        self.drops = drops_watcher
        self.fr = fr_watcher
        self.reserved = reserved
        self._universe: Universe | None = None
        self._cache: dict[str, tuple[float, object, bytearray]] = {}
        self._memos: dict = {}
        # Les mémos sont lus et modifiés depuis la boucle principale, le fil de l'Explorer et les fils
        # des run_in_executor : chaque accès au dict passe par ce verrou (les calculs restent dehors).
        self._memo_lock = threading.RLock()
        self.generation = 0
        # .co, .ai, .io : extension présente dès qu'une passe DNS a rangé des noms (version de la passe, oui ou non).
        self._dns_known: dict[str, tuple] = {}
        # Prénoms récents de l'Insee (prenoms.RecentNames), posés par main.py : Top, tri « recent », raison.
        self.recent = None
        # Client Gandi (gandi.Gandi), posé par main.py : le bouton Vérifier l'interroge aussi pour un nom non pris.
        self.gandi = None
        self._recent_cache: tuple[int, dict] | None = None
        # Score de chaque nom déjà noté par value_of (Veille, Aujourd'hui), avec la version des données
        # du calcul : 3 600 noms à noter à chaque ouverture coûtaient plusieurs secondes sur le NAS.
        self._values: dict[str, tuple[tuple, dict]] = {}
        # Score complet de chaque nom déjà classé, par extension (-1 : pas encore calculé), avec la version des
        # données : un classement en calculait environ 10 000 à chaque nouveau filtre (9 s sur le NAS). Un nom qui
        # change (touch) est oublié dans toutes les extensions : sa demande dépend de leur état.
        self._scores: dict[str, tuple[tuple, array]] = {}
        # Carte des Explorer (map) : (moment, carte), à refaire (touch), refaite en ce moment en arrière-plan.
        self._map_cache: tuple[float, dict, tuple] | None = None
        self._map_dirty = False
        self._map_running = False
        # Noms des listes pas encore cherchés par la dernière lecture de l'extension (zone ou fichier .fr) :
        # masqués (INVALID) jusqu'à la suivante. La passe DNS du .fr les interroge quand même.
        self._unread: dict[str, frozenset[int]] = {}
        self._lists_version = -1
        self._swap_lock = threading.Lock()
        # Les vérifications automatiques corrigent l'Explorer au fil de l'eau, nom par nom.
        drops_watcher.on_change = self.touch
        fr_watcher.on_change = self.touch

    @property
    def universe(self) -> Universe:
        if self._universe is None or self._lists_version != wordlists.version:
            self._swap_universe()
        return self._universe

    def _swap_universe(self) -> None:
        """Premier accès, ou Mes ajouts changés (wordlists.reload) : univers refait si son contenu a changé,
        et tout ce qui tient à ses positions oublié (octets d'état, compteurs, scores, carte, réservés)."""
        with self._swap_lock:
            version = wordlists.version
            if self._universe is not None and self._lists_version == version:
                return
            universe = shared_universe()
            universe.buckets()  # construit aussi universe.good
            if universe is not self._universe:
                with self._memo_lock:
                    self.generation += 1
                    self._memos.clear()
                    self._values.clear()
                    self._scores.clear()
                    self._map_cache = None
                    self._cache.clear()
                    self._unread.clear()
                    self._recent_cache = None
                if self.reserved is not None:
                    self.reserved._indices = {}  # positions des réservés dans l'ancien univers
            self._universe = universe
            self._lists_version = version

    def reload_lists(self) -> float:
        """Mes ajouts changés : univers et octets d'état refaits tout de suite, pas au prochain écran (les
        contextes de score suivent à la première fiche ou au premier classement). Renvoie la durée, en secondes."""
        started = time.monotonic()
        self.universe  # noqa: B018 refait l'univers si besoin
        for tld in self.tlds():
            self.status_bytes(tld)
        return time.monotonic() - started

    def _memo(self, key, compute, ttl: float = MEMO_SECONDS):
        """Résultat gardé en mémoire. Un nom qui change est corrigé en place par touch() ;
        un import ou une lecture de zone change la version des données, donc la clé."""
        now = time.monotonic()
        # Import .fr ou nouvelle lecture de zone : la version des données change, la mémoire aussi.
        key = (key, tuple((t, self._version(t)) for t in self.tlds()))
        with self._memo_lock:
            found = self._memos.get(key)
            generation = self.generation
        if found and found[0] == generation and now - found[1] < ttl:
            return found[2]
        result = compute()
        with self._memo_lock:
            if self.generation != generation:
                return result  # données changées pendant le calcul : pas gardé
            # Ménage : versions de données dépassées, résultats périmés, puis les plus anciens.
            # Chaque entrée pèse quelques Mo (octets d'état, masques) : on en garde peu.
            current = key[1]
            # Chaque sorte a sa durée : un classement (10 min) ne doit pas emporter les octets d'état (6 h).
            for old in [k for k, (g, at, _) in self._memos.items()
                        if k[1] != current or g != self.generation or now - at >= (RANK_SECONDS if k[0][0] == "rank" else MEMO_SECONDS)]:
                self._memos.pop(old, None)
            # Les compteurs (un par jeu de filtres) sont les seuls à se multiplier : on retire les plus anciens.
            facets = sorted((k for k in self._memos if k[0][0] == "facets"), key=lambda k: self._memos[k][1])
            while facets and len(facets) >= MEMO_FACETS:
                self._memos.pop(facets.pop(0), None)
            ranks = sorted((k for k in self._memos if k[0][0] == "rank"), key=lambda k: self._memos[k][1])
            while ranks and len(ranks) >= MEMO_RANKS:
                self._memos.pop(ranks.pop(0), None)
            self._memos[key] = (self.generation, now, result)
        return result

    def warm(self) -> None:
        """Prépare ce que l'ouverture de l'Explorer demande, pour que le premier écran soit immédiat."""
        for tld in self.tlds():
            self.status_bytes(tld)
            self._bonus_context(tld)
        self.map()
        # Scores des noms de la Veille, d'Aujourd'hui et de Domaines : notés une fois ici plutôt qu'à l'ouverture
        # de l'écran (une extension sans zone, comme .eu, a aussi son contexte de calcul à préparer).
        self.value_of([r[0] for r in self.conn.execute(
            "SELECT name FROM drops WHERE state IS NOT NULL AND COALESCE(origin, '') != 'rdap' UNION SELECT name FROM domains")])

    def warm_ranks(self) -> None:
        """Écran par défaut de l'Explorer (à réserver, bons noms, pour revendre) sur les extensions les plus ouvertes :
        son premier classement à froid prenait 9 s sur le NAS ; les scores calculés servent ensuite aux autres filtres.
        Hors du fil de l'Explorer (main) : dans warm, il faisait attendre Domaines et Aujourd'hui 15 à 19 s au démarrage."""
        for tld in [t for t in WARM_TLDS if t in self.tlds()]:
            self.rank(tld, limit=50, status="bookable", level="good", sort="resale")

    # --- Extensions et octets d'état -------------------------------------------------

    def tlds(self) -> list[str]:
        """Extensions de l'Explorer, celles qu'on lit : open data .fr, zones ICANN déjà lues (accès CZDS accordé),
        puis DNS de .co, .ai, .io."""
        found = []
        if self.conn.execute("SELECT 1 FROM fr_names LIMIT 1").fetchone():
            found.append("fr")
        for tld in sorted(self.drops.approved()):
            if self.drops._path(tld, "short").exists():
                found.append(tld)
        return found + [t for t in ccdns.TLDS if t not in found and self._dns_has(t)]

    def _dns_has(self, tld: str) -> bool:
        """Des noms de cette extension ont été interrogés dans le DNS. Relu seulement quand une passe finit."""
        version = self._version(tld)
        known = self._dns_known.get(tld)
        if known is None or known[0] != version:
            found = self.conn.execute("SELECT 1 FROM cc_dns WHERE name LIKE ? LIMIT 1", (f"%.{tld}",)).fetchone()
            known = self._dns_known[tld] = (version, found is not None)
        return known[1]

    def _version(self, tld: str):
        if tld == "fr":
            return self.fr.store.get("fr_status")
        if tld in ccdns.TLDS:
            # État de la dernière passe DNS, réécrit à la fin de chaque passe : nouvelle version des données.
            return self.fr.store.get(f"{tld}_dns_status")
        path = self.drops._path(tld, "short")
        return path.stat().st_mtime if path.exists() else None

    def _base(self, tld: str) -> bytearray:
        version = self._version(tld)
        cached = self._cache.get(tld)
        if cached and cached[1] == version and time.monotonic() - cached[0] < CACHE_SECONDS:
            return cached[2]
        status = self._build_fr() if tld == "fr" else self._build_dns(tld) if tld in ccdns.TLDS else self._build_zone(tld)
        self._cache[tld] = (time.monotonic(), version, status)
        return status

    def _build_dns(self, tld: str) -> bytearray:
        """.co, .ai, .io : délégué dans le DNS, enregistré ; absent, à vérifier ; jamais interrogé, rien
        (INVALID : hors des listes et des compteurs, comme un nom pas encore lu dans une zone). Un nom ajouté aux
        listes n'est pas encore interrogé : il l'est à la passe suivante, Mes ajouts en tête (ccdns.pending)."""
        u = self.universe
        status = bytearray(u.size)
        for name, present in self.conn.execute("SELECT name, present FROM cc_dns WHERE name LIKE ?", (f"%.{tld}",)):
            i = u.index(name[: -len(tld) - 1])
            if i is not None:
                status[i] = REGISTERED if present else ABSENT
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
        searched = self.drops.searched(tld)
        if searched is None:
            # Lecture d'avant ce relevé : on se fie aux empreintes des listes.
            lists = wordlists.lists()
            covered = self.drops.covered(tld)
            searched = set().union(*(lists[k] for k in covered)) if covered else set()
        self._mask_unread(tld, status, searched)
        return status

    def _mask_unread(self, tld: str, status: bytearray, searched: set[str]) -> None:
        """Noms des listes (5 caractères et plus) absents de la dernière lecture faute d'y avoir été cherchés :
        INVALID plutôt qu'absents à tort. Relevés dans _unread (passe DNS du .fr, touch)."""
        u = self.universe
        unread = []
        for w in wordlists.listed() - searched:
            i = u.index(w)
            if i is not None and status[i] == ABSENT and i >= drops.CELLS:
                status[i] = INVALID
                unread.append(i)
        self._unread[tld] = frozenset(unread)

    def unread(self, tld: str) -> frozenset[int]:
        """Positions masquées faute d'avoir été cherchées à la dernière lecture de l'extension."""
        self._base(tld)
        return self._unread.get(tld, frozenset())

    def _build_fr(self) -> bytearray:
        u = self.universe
        status = bytearray([ABSENT]) * u.size
        u.apply_valid(status)
        for label, removed in self.conn.execute("SELECT label, removed_on FROM fr_names"):
            i = u.index(label)
            if i is not None:
                status[i] = GONE if removed else REGISTERED
        # Un nom ajouté aux listes depuis le dernier import n'a pas été cherché dans le fichier : « Jamais
        # enregistré » serait faux. Masqué jusqu'à l'import suivant ; le DNS du .fr peut le dire pris d'ici là.
        searched = self.fr.searched()
        if searched is None:
            # Import d'avant ce relevé : il couvrait les listes d'alors, pas celles ajoutées depuis.
            lists = wordlists.lists()
            searched = set().union(*(names for key, names in lists.items() if key not in wordlists.LATE))
        self._mask_unread("fr", status, searched)
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
            # DNS plus frais que l'open data : un nom délégué est pris (aussi un nom pas encore cherché).
            unread = self._unread.get("fr", frozenset())
            for label in frdns.present(self.conn):
                i = u.index(label)
                if i is not None and (status[i] in (GONE, ABSENT) or i in unread):
                    status[i] = REGISTERED
        delegated = frdns.present_at(self.conn) if tld == "fr" else {}
        for label, state, checked in self.conn.execute(
                "SELECT label, state, checked_at FROM drops WHERE tld = ? AND state IS NOT NULL", (tld,)):
            i = u.index(label)
            code = drop_open_code(state, checked, delegated.get(label))
            if i is not None and code is not None:
                status[i] = code
        if self.reserved is not None:
            # Réservé : seulement s'il n'est pas enregistré. Un nom pris reste « Enregistré ».
            for i in self.reserved.indices(tld, u):
                if status[i] in RESERVABLE:
                    status[i] = RESERVED
            if tld != "fr":
                # 1 ou 2 caractères absents de la zone : gardés par le registre (voir reserved.short_reserved).
                unflagged = self.reserved.unflagged()
                for i in range(drops.OFFSETS[2]):
                    if status[i] in (ABSENT, UNCONFIRMED, FREE) and f"{u.label(i)}.{tld}" not in unflagged:
                        status[i] = RESERVED
        if tld != "fr" and tld not in self.premium_tlds_off():
            unflagged = self.reserved.unflagged() if self.reserved is not None else frozenset()
            for i in range(drops.OFFSETS[2], drops.OFFSETS[PREMIUM_LENGTH]):
                if status[i] in (ABSENT, UNCONFIRMED, FREE) and f"{u.label(i)}.{tld}" not in unflagged:
                    status[i] = PREMIUM
        # Gandi en dernier : ce qu'il dit l'emporte sur les estimations (premium probable, court réservé).
        listed = self.reserved.names() if self.reserved is not None else frozenset()
        cut = len(tld) + 1
        for name, kind, at, since, checked in self._gandi_rows(tld):
            i = u.index(name[:-cut])
            if i is not None:
                status[i] = gandi_effect(status[i], {"kind": kind, "checked_at": at}, since, checked, name in listed)[0]
        return status

    def _gandi_rows(self, tld: str, where: str = "", args: tuple = ()) -> list:
        """Réponses de Gandi de l'extension (hors erreurs), avec l'entrée du nom dans son état actuel (Veille).
        Par l'index de l'extension (migration 033)."""
        return self.conn.execute(
            f"""SELECT g.name, g.kind, g.checked_at, d.state_since,
                   CASE WHEN d.name IS NULL THEN NULL ELSE COALESCE(d.checked_at, '') END FROM gandi_checks g LEFT JOIN drops d ON d.name = g.name
                WHERE g.tld = ? AND g.kind != 'error' {where}""", (tld, *args)).fetchall()

    def _gandi_indices(self, tld: str, gprice: int) -> list[int]:
        """Noms de l'extension vérifiés chez Gandi, achetables au plus à ce prix (TTC), dans l'ordre de l'univers.
        Seulement ceux que l'état dit encore libres ou premiums : un nom repris depuis la réponse (zone, DNS, registre)
        n'est plus achetable, même avec un prix en base."""
        data = self.status_bytes(tld)
        cut = len(tld) + 1
        found = (self.universe.index(name[:-cut]) for name, _, at, since, _ in self._gandi_rows(
            tld, "AND g.kind IN ('available', 'premium') AND g.price IS NOT NULL AND g.price <= ?", (gprice,))
            if not (since and since > at))
        picked = {i for i in found if i is not None and data[i] in (FREE, PREMIUM)}
        if tld in PREMIUM_OFF:
            # .com, .net : prix unique du registre. Un nom libre au registre, sans réponse de Gandi (absent.py), est
            # achetable au prix de la grille de l'extension.
            price = (gandi.grid(self.fr.store).get(tld) or {}).get("price")
            if price is not None and price <= gprice:
                at = data.find(FREE)
                while at != -1:
                    picked.add(at)
                    at = data.find(FREE, at + 1)
        return sorted(picked)

    def forget_taken_prices(self) -> int:
        """Prix d'achat devenus faux : un nom dit libre ou premium par Gandi, enregistré depuis (zone, DNS, registre).
        La ligne est effacée ; s'il se libère de nouveau, Gandi est redemandé (libération : drops.on_freed). Chaque
        heure (tâche gandi_grid) ; aussi au fil de l'eau quand une passe DNS voit le nom délégué (ccdns, frdns)."""
        taken = (REGISTERED, SUSPENDED, COMING)
        u = self.universe
        gone = []
        for tld in self.tlds():
            data = self.status_bytes(tld)
            cut = len(tld) + 1
            for (name,) in self.conn.execute("SELECT name FROM gandi_checks WHERE tld = ? AND kind IN ('available', 'premium')",
                                             (tld,)):
                i = u.index(name[:-cut])
                if i is not None and data[i] in taken:
                    gone.append(name)
        for start in range(0, len(gone), 500):
            part = gone[start:start + 500]
            self.conn.execute(f"DELETE FROM gandi_checks WHERE name IN ({','.join('?' * len(part))})", part)
        if gone:
            self.invalidate()
        return len(gone)

    def gandi_premiums(self, tld: str) -> list[int]:
        """Noms de l'extension premium d'après Gandi (file de notation IA : un premium s'achète)."""
        data = self.status_bytes(tld)
        cut = len(tld) + 1
        found = (self.universe.index(name[:-cut]) for name, *_ in self._gandi_rows(tld, "AND g.kind = 'premium'"))
        return [i for i in found if i is not None and data[i] == PREMIUM]

    def codes(self, status: str, default: str = "", premium: bool = False) -> tuple[int, ...]:
        key = status if status in STATUS_FILTERS else default
        codes = STATUS_FILTERS.get(key) or tuple(LABELS)
        if key in WITH_RESERVED and self.reserved is not None and self.reserved.mode == "show":
            codes = codes + (RESERVED,)
        if key in WITH_RESERVED and premium:
            codes = codes + (PREMIUM,)
        return codes

    def invalidate(self, tld: str | None = None) -> None:
        """Données changées (vérification, marquage, import) : on recalcule au prochain affichage."""
        with self._memo_lock:
            self.generation += 1
            self._memos.clear()
            self._values.clear()
            self._scores.clear()
            self._map_cache = None
        if tld:
            self._cache.pop(tld, None)
            self._dns_known.pop(tld, None)

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
        """Meilleur rang du nom dans les listes du filtre de catégorie (une ou plusieurs, avec les listes qui y sont
        réunies : Prénoms, ceux de France et des États-Unis), sinon dans toutes. Nom dérivé dans la catégorie
        (assurance dans Termes stars) : son meilleur rang dans les listes d'où vient la catégorie
        (wordlists.DERIVED_FROM)."""
        ranks = wordlists.ranks()
        keys = [category] if category in ranks else ([c for c in category.split(",") if c in ranks] if "," in category else [])
        keys = [s for k in keys for s in wordlists.sources(k)]
        found = [ranks[k][label] for k in (keys or ranks) if label in ranks[k]]
        if keys and not found:
            sources = {s for k in keys if label in wordlists.classes()[k] for s in wordlists.DERIVED_FROM.get(k, ())}
            found = [ranks[s][label] for s in sources if label in ranks[s]]
        return min(found) if found else None

    @staticmethod
    def _in_category(label: str, category: str) -> bool:
        if category in wordlists.LABELS:
            return label in wordlists.classes()[category]
        if len(label) > drops.MAX_SHORT:
            return False
        kind = drops.kind_of(label)
        # Dans l'Explorer, « mélangés » regroupe lettres et chiffres mêlés et noms à tiret.
        return kind == category or (category == "mixed" and kind == "hyphen")

    def _keep(self, label: str, f: Filters) -> bool:
        if not self._length_ok(label, f.length):
            return False
        # Plusieurs catégories : le nom passe s'il est dans l'une d'elles.
        if f.category and not any(self._in_category(label, c) for c in f.cats):
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
        if not (f.seo or f.ai or f.gprice is not None):
            return None
        allowed = None
        if f.seo:
            allowed = set(self._seo_indices(tld, f.seo))
        if f.ai:
            rated = set(self._ai_indices(tld, f.ai))
            allowed = rated if allowed is None else allowed & rated
        if f.gprice is not None:
            priced = set(self._gandi_indices(tld, f.gprice))
            allowed = priced if allowed is None else allowed & priced
        return allowed

    def _candidates(self, f: Filters, q: str, tld: str = ""):
        """Positions à examiner, dans l'ordre, ou None pour tout l'univers."""
        u = self.universe
        if (f.seo or f.ai or f.gprice is not None) and tld:
            found = None
            if f.seo:
                found = self._seo_indices(tld, f.seo)
            if f.ai:
                rated = self._ai_indices(tld, f.ai)
                found = rated if found is None else sorted(set(found) & set(rated))
            if f.gprice is not None:
                priced = self._gandi_indices(tld, f.gprice)
                found = priced if found is None else sorted(set(found) & set(priced))
            return [i for i in found if self._matches(u.label(i), f, q)] if q else found
        if q:
            return self._short_matching(q, f.mode) + self._word_matching(q, f.mode)
        if f.nature:
            return u.by_nature.get(f.nature, [])
        if f.category and all(c in wordlists.LABELS for c in f.cats):
            return u.in_lists(f.category)
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
        codes = self.codes(f.status, premium=f.premium)
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
            # Saut direct au prochain nom dans un des états voulus : une seule passe sur les octets.
            # (Un find par état relisait jusqu'au bout, à chaque nom, les états absents de la suite.)
            wanted = re.compile(b"[" + b"".join(re.escape(bytes([c])) for c in sorted(codes)) + b"]")
            while pos < hi and len(hits) < limit:
                match = wanted.search(data, pos, hi)
                if match is None:
                    pos = hi
                    break
                i = match.start()
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

    def premium_tlds_off(self) -> frozenset[str]:
        """Extensions sans prix premium chez le registre : pas de « premium probable » sur elles."""
        return PREMIUM_OFF

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
        # Taux de base par forme : pris sur chaque autre extension ; en .fr, déjà tenus parmi les libres.
        rates = {t: self._rates(data, TAKEN_CODES) for t, data in others.items()}
        held = self._rates(self.status_bytes(tld), (GONE,), (GONE, ABSENT)) if tld == "fr" else {}
        return {"others": others, "gone": gone, "seo": seo, "today": date.today(), "reach": reach, "rates": rates, "held": held,
                "reach_parts": parts, "weights": w, "status": self.status_bytes(tld)}

    def _rates(self, data, wanted: tuple[int, ...], among: tuple[int, ...] | None = None) -> dict[str, float]:
        """Par forme de nom : part des noms dans un des états voulus (parmi tous les noms valides, ou
        parmi ceux dans un des états among). Sert à ne compter que les signaux rares pour la forme."""
        u = self.universe
        hit = data.translate(bytes(1 if c in wanted else 0 for c in range(256)))
        base = data.translate(bytes(1 if (c in among if among else c != INVALID) else 0 for c in range(256)))

        def count(table, idx) -> int:
            return sum(table[i] for i in idx) if len(idx) < 2 else sum(operator.itemgetter(*idx)(table))

        rates = {}
        for n in range(1, drops.MAX_SHORT + 1):
            lo, hi = (drops.OFFSETS[n - 1] if n > 1 else 0), drops.OFFSETS[n]
            groups = {"g": u.good_letters(n), "all": u.letters[n], "d": u.digits[n]}
            h = {k: count(hit, idx) for k, idx in groups.items()}
            b = {k: count(base, idx) for k, idx in groups.items()}
            rates[f"L{n}g"] = h["g"] / b["g"] if b["g"] else 0.0
            rates[f"L{n}b"] = (h["all"] - h["g"]) / (b["all"] - b["g"]) if b["all"] > b["g"] else 0.0
            rates[f"D{n}"] = h["d"] / b["d"] if b["d"] else 0.0
            rest = base.count(1, lo, hi) - b["all"] - b["d"]
            rates[f"M{n}"] = (hit.count(1, lo, hi) - h["all"] - h["d"]) / rest if rest > 0 else 0.0
        words = base.count(1, drops.CELLS, u.size)
        rates["W"] = hit.count(1, drops.CELLS, u.size) / words if words else 0.0
        return rates

    def _bound(self, tld: str, i: int, label: str, ctx: dict) -> float:
        """Borne haute du score d'un nom, sans le calcul de forme et de sens (score de base précalculé) :
        demande, historique et SEO sont exacts et bon marché. Sert à écarter un nom sans le noter."""
        w = ctx["weights"]
        taken = [t for t, data in ctx["others"].items() if data[i] in TAKEN_CODES]
        form = self.universe.shape_class(i)
        demand = value.demand(taken, {t: ctx["rates"][t].get(form, 0.0) for t in taken})[0] if taken else 0
        row = ctx["gone"].get(i)
        history = value.history(row["created_on"], row["removed_on"], ctx["today"], ctx["held"].get(form, 0.0))[0] if row else 0
        seo = value.seo(ctx["seo"].get(f"{label}.{tld}"))[0]
        top = max(w["shape"], w["popularity"])
        # Nom court non calculé à la construction (4 caractères mélangés ou à tiret) : borne fixe.
        base = (self.universe.base_table[i] & 127) or (precompute.UNRANKED_BOUND if i < drops.CELLS else 0)
        return base * top + demand * w["demand"] + history * w["history"] + seo * w["seo"]

    def _value(self, tld: str, i: int, label: str, ctx: dict) -> dict:
        taken = [t for t, data in ctx["others"].items() if data[i] in TAKEN_CODES]
        form = self.universe.shape_class(i)
        rates = {t: ctx["rates"][t].get(form, 0.0) for t in taken}
        # Forme et sens calés sur les avis IA (value.intrinsic) : un mot seulement anglais en .fr ou une
        # forme conjuguée y comptent déjà moins ; demande et historique ramenés à ce qui est rare.
        found = value.score(label, taken, ctx["gone"].get(i), ctx["seo"].get(f"{label}.{tld}"), ctx["today"], ctx["weights"],
                            rates, ctx["held"].get(form, 0.0), tld)
        found["rank"] = self.best_rank(label)
        recent = self.recent.rank_of(label) if self.recent is not None else None
        if recent:
            found["reasons"] = self._with_recent(found.get("reasons", []), label, recent[0])
        # Rang dans chaque liste : tri « les plus portés » et « 12e prénom le plus donné » sur la carte.
        found["ranks"] = {k: r[label] for k, r in wordlists.ranks().items() if label in r}
        return found

    def _with_recent(self, reasons: list[str], label: str, rank: int) -> list[str]:
        """Raison « 12e prénom donné en 2024 » : à côté du rang historique, devant lui s'il est meilleur,
        pour que la carte (deux raisons) la montre. Pas de points : ce n'est pas une mesure du score."""
        text = prenoms.reason(rank, self.recent.latest)
        reasons = list(reasons)
        old = next((n for n, r in enumerate(reasons) if r.endswith(value.POPULAR_PHRASE["firstname"])), None)
        historic = wordlists.ranks()["firstname"].get(label)
        if old is not None and historic and rank < historic:
            reasons.insert(old, text)
        else:
            reasons.insert(min(2, len(reasons)) if old is None else old + 1, text)
        return reasons

    def recent_order(self) -> dict | None:
        """Prénoms les plus donnés de l'année la plus récente (Insee) qui sont dans l'univers, dans l'ordre
        de l'Insee : année, positions, rang et nombre par position. None sans données."""
        recent = self.recent
        if recent is None or not recent.ranking():
            return None
        found = self._recent_cache
        if found and found[0] == recent.version:
            return found[1]
        u = self.universe
        order, ranks = array("i"), {}
        for rank, (label, count) in enumerate(recent.ranking(), 1):
            i = u.index(label)
            if i is not None and i not in ranks:
                order.append(i)
                ranks[i] = (rank, count)
        result = {"year": recent.latest, "order": order, "ranks": ranks,
                  "position": {i: n for n, i in enumerate(order)}}
        self._recent_cache = (recent.version, result)
        return result

    def recent_meta(self) -> dict | None:
        """Liste « Prénoms <année> » du Top et tri « Les plus donnés en <année> », s'il y a des données."""
        recent = self.recent_order()
        if recent is None:
            return None
        return {"key": prenoms.TOP_KEY, "year": recent["year"], "label": f"Prénoms {recent['year']}"}

    def _opinions(self, tld: str) -> dict[str, dict]:
        """Avis IA de l'extension : notes et verdict, pour les niveaux et les classements revendre / exploiter.
        Gardés en mémoire jusqu'au prochain lot d'avis (ai_changed)."""
        return self._memo(("opinions", tld), lambda: self._load_opinions(tld))

    def _load_opinions(self, tld: str) -> dict[str, dict]:
        # Prix final et version : pour les comparables de la fiche, sans relire la table.
        # Note de A à E et rendement : palier et départage du classement « pour revendre ».
        found = {}
        for r in self.conn.execute("SELECT name, resale, exploit, comment, final_low, final_high, version, yield, renewal "
                                   "FROM domain_ai WHERE name LIKE ?", (f"%.{tld}",)):
            verdict = (r[3] or "").split(".")[0]
            found[r[0]] = {"resale": r[1], "exploit": r[2], "verdict": verdict, "final_low": r[4], "final_high": r[5],
                           "version": r[6], "yield": r[7], "grade": ai.grade(verdict, r[1], r[7], r[8])}
        return found

    @staticmethod
    def _level_ok(score: int, opinion: dict | None, level: str) -> bool:
        """« Masquer les noms faibles » (good) : sans avis IA, score de 30 et plus ; avec un avis, ce que
        dit l'IA (« À laisser » masqué, même à bon score). gem : « À prendre », ou score 50 sans avis."""
        if not level:
            return True
        verdict = opinion["verdict"] if opinion else ""
        if level == "gem":
            return verdict == "À prendre" or (not verdict and score >= LEVEL_GEM)
        if verdict:
            return verdict != "À laisser"
        return score >= LEVEL_GOOD

    def rank(self, tld: str, *args, limit: int = 50, sort: str = "score", **kwargs) -> dict:
        """Les meilleurs noms d'abord. Sans recherche, parcours par score de base décroissant,
        arrêté dès qu'aucun nom restant ne peut plus entrer dans le classement.

        sort="freq" : du plus fréquent au moins fréquent (mots, formes, noms, prénoms).
        sort="length" : du plus court au plus long, puis dans l'ordre alphabétique.
        sort="recent" : les plus donnés l'année la plus récente du fichier de l'Insee, puis les autres par fréquence."""
        kwargs.setdefault("status", "available")
        f = Filters.of(*args, **kwargs)
        limit = max(1, min(limit, MAX_LIMIT * 5))
        if self._hopeless(tld, f):
            return {"items": []}
        # Même liste demandée de nouveau (retour sur l'écran, autre onglet) : gardée quelques minutes ;
        # ai_changed() l'oublie. Les vérifications changent l'état de noms toutes les minutes sur le NAS :
        # plutôt que de tout oublier à chaque fois, la liste gardée est refaite si l'un de ses noms a changé.
        key = ("rank", tld, repr(f), limit, sort)
        items = self._memo(key, lambda: self._rank(tld, f, limit, sort)["items"], RANK_SECONDS)
        data = self.status_bytes(tld)
        u = self.universe
        if any(data[i] != item["status"] for item in items if (i := u.index(item["label"])) is not None):
            self._forget_key(key)
            items = self._memo(key, lambda: self._rank(tld, f, limit, sort)["items"], RANK_SECONDS)
        return {"items": [dict(i) for i in items]}

    def _score_cache(self, tld: str) -> array:
        """Scores déjà calculés de l'extension (self._scores), refaits à neuf si les données ont changé."""
        stamp = (self.generation, date.today(), tuple((t, self._version(t)) for t in self.tlds()))
        with self._memo_lock:
            found = self._scores.get(tld)
            if found is None or found[0] != stamp or len(found[1]) != self.universe.size:
                found = self._scores[tld] = (stamp, array("h", [-1]) * self.universe.size)
        return found[1]

    def _rank(self, tld: str, f: "Filters", limit: int, sort: str) -> dict:
        if sort == "length":
            return self._ordered(tld, f, limit)
        if sort == "freq":
            return self._by_frequency(tld, f, limit)
        if sort == "release":
            return self._by_release(tld, f, limit)
        if sort == "recent":
            # Comme SEO et avis IA, le tri ne filtre pas : les prénoms classés d'abord, puis les autres.
            first = self._by_recent(tld, f, limit)["items"]
            if len(first) < limit:
                seen = {i["label"] for i in first}
                rest = self._by_frequency(tld, f, limit + len(first))["items"]
                first += [i for i in rest if i["label"] not in seen][: limit - len(first)]
            return {"items": first}
        if sort in ("seo", "ai"):
            # Un tri ne filtre pas : les noms analysés (ou notés) d'abord, puis les autres par score.
            first = (self._by_seo if sort == "seo" else self._by_ai)(tld, f, limit)["items"]
            if len(first) < limit:
                seen = {i["label"] for i in first}
                rest = self.rank(tld, f, limit=limit + len(first), sort="score")["items"]
                first += [i for i in rest if i["label"] not in seen][: limit - len(first)]
            return {"items": first}
        codes = self.codes(f.status, "available", f.premium)
        data = self.status_bytes(tld)
        others = self._others(tld, f)
        u = self.universe
        ctx = self._bonus_context(tld)
        q = self.normalize(f.q, tld)
        best: list[tuple[int, int, int, dict]] = []  # tas : (clé, -position, position, valeur)
        # Classement « pour revendre » / « pour exploiter » : la note IA quand le nom est noté, sinon le score.
        lens = sort if sort in LENSES else None
        opinions = self._opinions(tld) if (lens or f.level) else {}
        seen: set[int] = set()
        scores = self._score_cache(tld)

        def consider(i: int) -> None:
            if i in seen:
                return
            seen.add(i)
            if data[i] not in codes or not self._good_ok(i, f) or not self._taken_ok(i, others):
                return
            label = u.label(i)
            opinion = opinions.get(f"{label}.{tld}")
            key = None
            if opinion and lens:
                # La clé ne dépend pas du score : un nom qui ne peut plus entrer n'est pas noté.
                key = lens_key(opinion, lens)
                if len(best) >= limit and (key, -i) <= best[0][:2]:
                    return
            if not self._keep(label, f):
                return
            if key is None:
                # Classé par score : on écarte d'abord sur la borne, le calcul complet ne sert qu'aux noms
                # qui peuvent entrer. Avec un avis, le niveau tient au verdict, pas au score.
                bound = self._bound(tld, i, label, ctx)
                if (f.min_score and bound < f.min_score) or (f.level and not opinion and bound < LEVEL_GOOD) \
                        or (len(best) >= limit and ((1, bound), -i) <= best[0][:2]):
                    return
            known = scores[i]
            if known >= 0:
                # Score déjà calculé : le calcul complet seulement pour un nom qui entre dans la page.
                if (f.min_score and known < f.min_score) or not self._level_ok(known, opinion, f.level):
                    return
                if len(best) >= limit and ((key or (1, known)), -i) <= best[0][:2]:
                    return
            found = self._value(tld, i, label, ctx)
            scores[i] = max(0, min(32767, int(found["score"])))
            if f.min_score and found["score"] < f.min_score:
                return
            if not self._level_ok(found["score"], opinion, f.level):
                return
            entry = (key or (1, found["score"]), -i, i, found)
            if len(best) < limit:
                heapq.heappush(best, entry)
            elif entry > best[0]:
                heapq.heapreplace(best, entry)

        candidates = self._candidates(f, q, tld)
        if candidates is not None:
            for i in candidates:
                consider(i)
        else:
            if lens:
                # Noms conseillés par l'IA d'abord, meilleure note en tête : leur clé dépasse celle de tout nom
                # sans avis, le parcours par score ne les atteindrait pas toujours avant de s'arrêter. Dès que
                # la page est pleine, les suivants sont écartés sur leur clé, sans calcul. Les avis « À laisser »
                # ne passent devant personne : le parcours les trouve s'il va jusque-là.
                # Dans l'ordre final (clé, puis ordre des mots) : la page pleine écarte les suivants sans calcul.
                advised = []
                for name, o in opinions.items():
                    i = u.index(name[: -len(tld) - 1]) if lens_tier(o) >= 2 else None
                    if i is not None:
                        advised.append((lens_key(o, lens), -i, i))
                for _, _, i in sorted(advised, reverse=True):
                    consider(i)
            top = max(ctx["weights"]["shape"], ctx["weights"]["popularity"])
            for score in range(70, -1, -1):
                # Les noms du parcours n'ont pas d'avis (palier 1) : on s'arrête quand aucun ne peut plus entrer.
                # Strictement : à égalité, un nom plus haut dans l'ordre des mots entre encore (sinon les 50
                # premiers d'une page de 100 n'étaient pas ceux d'une page de 50).
                if len(best) >= limit and (1, score * top + ctx["reach"]) < best[0][0]:
                    break
                for i in u.buckets()[score]:
                    consider(i)
        ranked = sorted(best, reverse=True)
        items = self.describe(tld, [e[2] for e in ranked], data)
        for item, entry in zip(items, ranked):
            item.update(entry[3])
        return {"items": items}

    def _by_release(self, tld: str, f: Filters, limit: int) -> dict:
        """Libération la plus proche d'abord (noms en rédemption ou en suppression suivis)."""
        data = self.status_bytes(tld)
        codes = self.codes(f.status, "coming", f.premium)
        others = self._others(tld, f)
        u = self.universe
        ctx = self._bonus_context(tld)
        # Recherche, SEO et avis IA filtrent aussi ce tri (ils passaient sans contrôle).
        q = self.normalize(f.q, tld)
        allowed = self._restrict(tld, f)
        hits = []
        # Sans date estimée (délais du registre inconnus, échéance absente) : à la fin, pas masqué.
        for (name,) in self.conn.execute(
                "SELECT name FROM drops WHERE tld = ? ORDER BY release_at IS NULL, release_at, name", (tld,)):
            i = u.index(name.rsplit(".", 1)[0])
            if i is None or data[i] not in codes or not self._good_ok(i, f) or not self._taken_ok(i, others):
                continue
            if (allowed is not None and i not in allowed) or not self._matches(u.label(i), f, q) or not self._keep(u.label(i), f):
                continue
            hits.append(i)
        scored = self._scored(tld, f, hits, data, ctx)["items"]
        return {"items": scored[:limit]}

    def _scored(self, tld: str, f: Filters, hits: list[int], data, ctx) -> dict:
        items = self.describe(tld, hits, data)
        for i, item in zip(hits, items):
            item.update(self._value(tld, i, item["label"], ctx))
        opinions = self._opinions(tld) if f.level else {}
        return {"items": [it for it in items if (not f.min_score or it["score"] >= f.min_score)
                          and self._level_ok(it["score"], opinions.get(it["name"]), f.level)]}

    def _ordered(self, tld: str, f: Filters, limit: int) -> dict:
        """Du plus court au plus long. Les noms courts sont déjà dans cet ordre dans l'univers ;
        les mots (5 caractères et plus) y sont par ordre alphabétique : on suit by_length.
        Avec un score minimum ou un niveau, on note par paquets jusqu'à remplir la page (au plus
        ORDERED_BUDGET noms notés : au-delà, la page reste courte plutôt que d'attendre)."""
        data = self.status_bytes(tld)
        ctx = self._bonus_context(tld)
        filtering = bool(f.min_score or f.level)
        size = limit * 4 if filtering else limit
        out: list[dict] = []
        batch: list[int] = []
        seen = 0
        for i in self._length_order(tld, f, data):
            batch.append(i)
            seen += 1
            if len(batch) >= size:
                out += self._scored(tld, f, batch, data, ctx)["items"]
                batch = []
                if len(out) >= limit or not filtering or seen >= ORDERED_BUDGET:
                    break
        if batch:
            out += self._scored(tld, f, batch, data, ctx)["items"]
        return {"items": out[:limit]}

    def _length_order(self, tld: str, f: Filters, data):
        """Noms qui passent les filtres (hors score et niveau), du plus court au plus long."""
        u = self.universe
        lo, _ = self._range(f.length)
        if lo < drops.CELLS:
            cursor = -1
            while True:
                found = self.search(tld, f, cursor=cursor, limit=500)
                for item in found["items"]:
                    i = u.index(item["label"])
                    if i is not None and i < drops.CELLS:
                        yield i
                cursor = found["cursor"]
                if cursor is None:
                    break
        codes = self.codes(f.status, "available", f.premium)
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
                yield i

    def merge_key(self, sort: str, category: str = ""):
        """Clé d'une liste fusionnée sur plusieurs extensions : le même ordre que rank() sur une seule."""
        u = self.universe

        def where(item) -> int:
            i = u.index(item["label"])
            return i if i is not None else u.size

        if sort == "length":
            return lambda item: (where(item) if where(item) < drops.CELLS else drops.CELLS + u.position("length", where(item)), item["tld"])
        if sort == "freq":
            key = freq_key(category)
            return lambda item: (u.position(key, where(item)), item["tld"])
        if sort == "recent":
            key = freq_key(category)
            ranked = (self.recent_order() or {}).get("position", {})
            return lambda item: (ranked.get(where(item), len(ranked) + u.position(key, where(item))), item["tld"])
        if sort == "seo":
            return lambda item: (-(item.get("_pr") or 0), -(item.get("_wb") or 0), -item["score"], where(item), item["tld"])
        if sort == "ai":
            def opinion(item):
                a = item.get("ai")
                return (0, -a["quick_high"], -a["resale"], -a["final_high"]) if a else (1, 0, 0, 0)
            return lambda item: (*opinion(item), -item["score"], where(item), item["tld"])
        if sort in LENSES:
            # Même ordre que rank() : lens_key pour un nom noté, le score sinon, puis l'ordre des mots.
            def lens(item):
                return tuple(-v for v in (lens_key(item["ai"], sort) if item.get("ai") else (1, item["score"], 0, 0)))
            return lambda item: (*lens(item), where(item), item["tld"])
        if sort == "release":
            return lambda item: (item.get("release_at") or "9", where(item), item["tld"])
        return lambda item: (-item["score"], where(item), item["tld"])

    def _by_seo(self, tld: str, f: Filters, limit: int) -> dict:
        """Meilleur PageRank d'abord, puis le plus d'années d'archives ; noms analysés seulement."""
        codes = self.codes(f.status, "available", f.premium)
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
        codes = self.codes(f.status, "available", f.premium)
        data = self.status_bytes(tld)
        others = self._others(tld, f)
        u = self.universe
        q = self.normalize(f.q, tld)
        hits = []
        rows = self.conn.execute("SELECT name FROM domain_ai WHERE name LIKE ? ORDER BY quick_high DESC, resale DESC, "
                                 "final_high DESC, name", (f"%.{tld}",))
        allowed = self._restrict(tld, f)
        ctx = self._bonus_context(tld)
        items = []
        for (name,) in rows:
            label = name[: -len(tld) - 1]
            i = u.index(label)
            if allowed is not None and i not in allowed:
                continue
            if (i is not None and data[i] in codes and self._good_ok(i, f) and self._taken_ok(i, others)
                    and self._matches(label, f, q) and self._keep(label, f)):
                hits.append(i)
                # Score minimum et niveau écartent après notation : on note par paquets jusqu'à remplir la page,
                # sinon les noms notés écartés laissaient leur place à des noms sans avis.
                if len(hits) >= limit:
                    items += self._scored(tld, f, hits, data, ctx)["items"]
                    hits = []
                    if len(items) >= limit:
                        break
        items += self._scored(tld, f, hits, data, ctx)["items"]
        return {"items": items[:limit]}

    def _by_recent(self, tld: str, f: Filters, limit: int) -> dict:
        """Prénoms les plus donnés l'année la plus récente (Insee), avec tous les filtres."""
        recent = self.recent_order()
        if recent is None:
            return {"items": []}
        codes = self.codes(f.status, "available", f.premium)
        data = self.status_bytes(tld)
        others = self._others(tld, f)
        u = self.universe
        q = self.normalize(f.q, tld)
        allowed = self._restrict(tld, f)
        ctx = self._bonus_context(tld)
        items, hits = [], []
        order = recent["order"]
        for n, i in enumerate(order):
            if allowed is None or i in allowed:
                if data[i] in codes and self._good_ok(i, f) and self._taken_ok(i, others):
                    label = u.label(i)
                    if self._matches(label, f, q) and self._keep(label, f):
                        hits.append(i)
            # Notés par paquets : score minimum et niveau peuvent écarter des noms, la page se remplit quand même.
            if len(hits) >= limit or (hits and n == len(order) - 1):
                items += self._scored(tld, f, hits, data, ctx)["items"]
                hits = []
                if len(items) >= limit:
                    break
        return {"items": items[:limit]}

    def _by_frequency(self, tld: str, f: Filters, limit: int) -> dict:
        codes = self.codes(f.status, "available", f.premium)
        data = self.status_bytes(tld)
        others = self._others(tld, f)
        u = self.universe
        ctx = self._bonus_context(tld)
        key = freq_key(f.category)
        order = u.frequency(key)
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
        items = items[:limit]
        # Liste qui donne le rang : celle du filtre ; plusieurs (ou aucune, ou une catégorie de deux sources comme
        # Prénoms), celle où le nom est le mieux placé parmi elles (même calcul que l'ordre).
        keys = [s for k in key.split(",") for s in wordlists.sources(k)] if key else None
        for item in items:
            # Nom dérivé dans la catégorie du filtre : le rang de sa meilleure liste d'origine.
            ranks = ({k: n for k, n in item["ranks"].items() if keys is None or k in keys}
                     or {k: n for k, n in item["ranks"].items()
                         if any(k in wordlists.DERIVED_FROM.get(c, ()) for c in keys or ())})
            item["freq_list"] = min(ranks, key=lambda k: wordlists.relative(k, ranks[k]), default=None)
        return {"items": items}

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
        if f.category:
            # Plusieurs catégories : union de leurs masques.
            union = 0
            for c in f.cats:
                union |= self._category_mask(c, from_indices)
            parts.append(union)
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
        if f.gprice is not None:
            parts.append(from_indices(self._gandi_indices(tld, f.gprice)))
        if not parts:
            return None
        mask = parts[0]
        for part in parts[1:]:
            mask &= part
        return mask

    def _category_mask(self, category: str, from_indices) -> int:
        u = self.universe
        if category in wordlists.LABELS:
            return from_indices(u.by_list[category])
        if category in ("letters", "digits"):
            arrays = u.letters if category == "letters" else u.digits
            return from_indices(i for n in arrays for i in arrays[n])
        mask = bytearray(u.size)
        if category == "mixed":
            mask[:drops.CELLS] = b"\x01" * drops.CELLS
            for arrays in (u.letters, u.digits):
                for n in arrays:
                    for i in arrays[n]:
                        mask[i] = 0
        elif category == "hyphen":
            # Un tiret demande 3 caractères au moins (a-b) ; les compteurs sont gardés en mémoire.
            for i in range(drops.OFFSETS[2], drops.CELLS):
                if "-" in u.label(i):
                    mask[i] = 1
        return int.from_bytes(mask, "big")

    def facets(self, tld: str, *args, **kwargs) -> dict:
        """Nombre de noms par état, pour les filtres choisis (recherche exclue)."""
        f = Filters.of(*args, **kwargs)
        if self._hopeless(tld, f):
            return dict.fromkeys(STATUS_FILTERS, 0)
        key = ("facets", tld, f.category, f.length, f.nature, f.max_rank, f.taken, f.junk, f.seo, f.ai, f.lang, f.gprice)
        raw = self._memo(key, lambda: self._facets(tld, f))
        by_code = raw["by_code"]
        # Les états réservés suivent le réglage du moment : conversion à chaque appel, sans recompter.
        return {k: sum(by_code.get(c, 0) for c in self.codes(k, premium=f.premium)) for k in STATUS_FILTERS}

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
            base = (INVALID if i in self.unread("fr") else ABSENT) if row is None else GONE if row["removed_on"] else REGISTERED
        elif tld in ccdns.TLDS:
            row = self.conn.execute("SELECT present FROM cc_dns WHERE name = ?", (f"{label}.{tld}",)).fetchone()
            base = INVALID if row is None else REGISTERED if row["present"] else ABSENT
        else:
            base = self._base(tld)[i]
        code = base
        if tld == "fr" and (code in (GONE, ABSENT) or i in self.unread("fr")):
            seen = self.conn.execute("SELECT present FROM name_dns WHERE label = ?", (label,)).fetchone()
            if seen and seen["present"]:
                code = REGISTERED
        drop = self.conn.execute("SELECT state, state_since, checked_at FROM drops WHERE name = ?", (f"{label}.{tld}",)).fetchone()
        if drop is not None:
            dns = frdns.load(self.conn, [label]).get(label) if tld == "fr" else None
            code = drop_open_code(drop["state"], drop["checked_at"], dns["at"] if dns and dns["present"] else None) or code
        if self.reserved is not None and f"{label}.{tld}" in self.reserved.names() and code in RESERVABLE:
            code = RESERVED
        elif self.reserved is not None and code in (ABSENT, UNCONFIRMED, FREE) and self.reserved.short_reserved(f"{label}.{tld}"):
            code = RESERVED
        elif (tld != "fr" and tld not in self.premium_tlds_off() and code in (ABSENT, UNCONFIRMED, FREE)
              and drops.OFFSETS[2] <= i < drops.OFFSETS[PREMIUM_LENGTH]
              and (self.reserved is None or f"{label}.{tld}" not in self.reserved.unflagged())):
            code = PREMIUM
        found = self.conn.execute("SELECT kind, checked_at FROM gandi_checks WHERE name = ?", (f"{label}.{tld}",)).fetchone()
        if found is not None:
            listed = self.reserved is not None and f"{label}.{tld}" in self.reserved.names()
            code = gandi_effect(code, dict(found), drop["state_since"] if drop else None, gandi.registry_checked(drop), listed)[0]
        return base, code

    def sync_registry(self, name: str, result, now: datetime | None = None) -> None:
        """Domaine suivi lu au registre (écran Domaines) : sa ligne de Veille, créée au besoin comme pour Vérifier,
        prend la même lecture ; l'Explorer et la fiche disent donc la même chose que Domaines. Sans cela, un nom
        suivi restait « libre » dans la Veille une fois repris, ou « probablement libre » dans l'Explorer."""
        if result is None or result.error:
            return
        now = now or utcnow()
        rdapinfo.save(self.conn, name, result)
        row = self.drops.ensure(name, now)
        self.drops.apply(row, result, now)
        self.touch(name)

    def recheck(self, name: str, now: datetime, create: bool = True) -> None:
        """Indisponible chez Gandi pour un nom qu'on croit libre : sans doute enregistré depuis la dernière lecture
        (zone, open data, vérification). Le registre tranche tout de suite : pris, ou réservé s'il le dit libre.
        Gandi.on_blocked. Pas un réservé de liste ; un domaine suivi est aussi revu côté Domaines."""
        if self.reserved is not None and name in self.reserved.names():
            return
        self.conn.execute("UPDATE domains SET next_check_at = ? WHERE name = ?", (iso(now), name))
        row = self.drops.row(name)
        if row is None and not create:
            return
        if row is None:
            row = self.drops.ensure(name, now, "gandi")  # sans sortie constatée, hors Veille et Aujourd'hui
        elif row["state"] not in (states.AVAILABLE, drops.UNCONFIRMED, None):
            return  # pris, en rédemption : Gandi a raison, rien à revoir
        self.conn.execute("UPDATE drops SET next_check_at = ?, state_since = COALESCE(state_since, ?) WHERE name = ?",
                          (iso(now), iso(now), name))

    def touch(self, name: str) -> None:
        """Un nom a changé (vérification, marquage, SEO) : on corrige ce nom seulement, en place.
        Octet d'état, compteurs (±1), historique .fr et SEO du score ; la carte se refait depuis
        les compteurs. Aucun recalcul d'ensemble."""
        label, _, tld = name.partition(".")
        i = self.universe.index(label)
        if i is None or not tld:
            return
        with self._memo_lock:
            # Score du nom dans chaque extension : la demande d'une extension dépend de l'état des autres.
            for key in [k for k in self._values if k.partition(".")[0] == label]:
                self._values.pop(key, None)
            for _, known in self._scores.values():
                known[i] = -1
        base, new = self._code_for(tld, i, label)
        cached = self._cache.get(tld)
        if cached:
            cached[2][i] = base
        with self._memo_lock:
            changed = False
            for key, (generation, at, value) in list(self._memos.items()):
                kind = key[0][0]
                if kind == "status" and key[0][1] == tld and value[i] != new:
                    old, value[i] = value[i], new
                    changed = True
                    for fkey, (_, _, raw) in list(self._memos.items()):
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
            # Le SEO ou le prix Gandi du nom ont pu changer : les compteurs filtrés dessus sont recomptés.
            for key in [k for k in self._memos if k[0][0] == "facets" and k[0][1] == tld and (k[0][8] or k[0][11] is not None)]:
                self._memos.pop(key, None)
            if changed:
                self._map_dirty = True
                # Les compteurs « pris ailleurs » des autres extensions dépendent de celle-ci.
                for key in [k for k in self._memos if k[0][0] == "facets" and k[0][1] != tld and k[0][6]]:
                    self._memos.pop(key, None)

    def ai_changed(self) -> None:
        """Nouveaux avis IA : les avis gardés en mémoire et les compteurs filtrés sur l'avis sont relus, rien d'autre."""
        with self._memo_lock:
            for key in [k for k in self._memos if k[0][0] in ("opinions", "rank") or (k[0][0] == "facets" and k[0][9])]:
                self._memos.pop(key, None)

    def _forget_key(self, key) -> None:
        with self._memo_lock:
            for k in [k for k in self._memos if k[0] == key]:
                self._memos.pop(k, None)

    def _forget(self, kind: str) -> None:
        with self._memo_lock:
            for key in [k for k in self._memos if k[0][0] == kind]:
                self._memos.pop(key, None)

    def refresh_settings(self) -> None:
        """Barème ou mode des réservés changé : on met à jour ce qui en dépend, sans recompter."""
        weights = self.weights
        with self._memo_lock:
            for key, (_, _, value) in list(self._memos.items()):
                if key[0][0] == "bonus":
                    value["weights"] = weights
                    value["reach"] = (value["reach_parts"][0] * weights["demand"] + value["reach_parts"][1] * weights["history"]
                                      + value["reach_parts"][2] * weights["seo"])
            # Scores gardés, classements et carte dépendent du barème et du mode des réservés.
            self._values.clear()
            self._scores.clear()
            self._map_cache = None
        self._forget("rank")

    def map(self) -> dict:
        """Carte gardée à part des autres mémos. Un nom qui change (touch) la marque à refaire : elle est
        refaite en arrière-plan et l'écran lit la précédente, juste à quelques noms près. Un import ou un
        réglage (invalidate) l'efface : la suivante est calculée tout de suite."""
        # Nouvelle zone, import .fr ou passe DNS (version des données) : calculée tout de suite aussi.
        version = tuple((t, self._version(t)) for t in self.tlds())
        with self._memo_lock:
            cached = self._map_cache if self._map_cache and self._map_cache[2] == version else None
            stale = cached is not None and (self._map_dirty or time.monotonic() - cached[0] > MAP_SECONDS)
            if stale and not self._map_running:
                self._map_running = True
                threading.Thread(target=self._refresh_map, args=(version,), daemon=True).start()
        if cached is None:
            return self._refresh_map(version, own=False)
        return cached[1]

    def _refresh_map(self, version: tuple, own: bool = True) -> dict:
        try:
            with self._memo_lock:
                self._map_dirty = False
                generation = self.generation
            found = self._build_map()
            with self._memo_lock:
                if self.generation == generation:
                    self._map_cache = (time.monotonic(), found, version)
            return found
        finally:
            if own:
                with self._memo_lock:
                    self._map_running = False

    def _build_map(self) -> dict:
        """Carte extension x catégorie : noms à saisir par case (noms qui ont un sens seulement)."""
        tlds = self.tlds()
        filters = {"short": ("", -3), "l4": ("letters", 4), **{k: (k, None) for k in wordlists.SHOWN}}
        # Mêmes compteurs que la puce « Libres » de la liste par défaut (réservables, premiums compris, libération à venir exclue) :
        # une case et le filtre qu'elle ouvre donnent le même nombre. Le masque d'une ligne ne dépend pas
        # de l'extension : fait une fois. Seuls les états réservables sont comptés, en un passage par case.
        masks = {key: self._mask(tlds[0], Filters.of(category=c, length=n)) for key, (c, n) in filters.items()} if tlds else {}
        cells = {}
        for tld in tlds:
            data = self.status_bytes(tld)
            wanted = set(self.codes("bookable", premium=True))
            hit = int.from_bytes(data.translate(bytes(1 if c in wanted else 0 for c in range(256))), "big")
            cells[tld] = {key: (hit & mask).bit_count() if mask is not None else bin(hit).count("1")
                          for key, mask in masks.items()}
        return {"rows": MAP_ROWS, "tlds": tlds, "cells": cells,
                "filters": {k: {"cat": c, "len": n} for k, (c, n) in filters.items()}}

    # --- Fiches ----------------------------------------------------------------------

    def _shown_price(self, tld: str, code: int, shown: dict | None, grid: dict | None = None) -> dict | None:
        """Réponse de Gandi montrée ; pour un .com ou un .net libre au registre sans réponse utile de Gandi (absent.py,
        Vérifier), le prix normal de l'extension (grille) : prix unique du registre, jamais de premium."""
        if tld not in PREMIUM_OFF or code != FREE or (shown and shown["kind"] != "error"):
            return shown
        g = (grid if grid is not None else gandi.grid(self.fr.store)).get(tld) or {}
        if g.get("price") is None:
            return shown
        return {"kind": "available", "status": "available", "label": "Libre au prix normal de l'extension",
                "price": g["price"], "price_ht": g.get("price_ht"), "currency": g.get("currency") or "EUR",
                "checked_at": g.get("at"), "error": None, "stale": False, "pending": False, "grid": True}

    def describe(self, tld: str, indices: list[int], data: bytearray | None = None) -> list[dict]:
        u = self.universe
        grid = gandi.grid(self.fr.store) if tld in PREMIUM_OFF else {}
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
        owned = {r[0] for r in self.conn.execute("SELECT name FROM portfolio")}
        details = rdapinfo.load(self.conn, names)
        opinions = ai.load(self.conn, names)
        checks = gandi.load(self.conn, names)
        listed = self.reserved.names() if self.reserved is not None else frozenset()
        items = []
        for i, label, name in zip(indices, labels, names):
            code = data[i]
            fr = extra.get(label)
            drop = dropped.get(name)
            check = checks.get(name)
            since = drop["state_since"] if drop else None
            checked = gandi.registry_checked(drop)
            seen = self._gandi_seen(code, check, since, name in listed, checked)
            items.append({
                "label": label,
                "name": name,
                "tld": tld,
                "length": len(label),
                "categories": u.categories(label),
                "status": code,
                "status_label": (absent_label(tld) if code == ABSENT
                                 else DROP_LABELS.get(drop["state"], LABELS[code]) if code == COMING and drop
                                 else FREE_GANDI if seen and code == FREE and not (drop and drop["state"] == states.AVAILABLE)
                                 else gandi.KIND_LABELS["premium"] if seen and code == PREMIUM
                                 else gandi.KIND_LABELS[check["kind"]] if seen and code == RESERVED
                                 else self._status_label(tld, code)),
                "created_on": fr["created_on"] if fr else None,
                # Une sortie de zone repérée puis démentie (nom repris) n'est pas une suppression.
                # Ligne créée par une vérification à la main : removed_on vide, aucune sortie constatée.
                "removed_on": (fr["removed_on"] if fr and fr["removed_on"]
                               else drop["removed_on"] or None if drop and drop["state"] != drops.TAKEN else None),
                "expires_at": fr["expires_at"] if fr else None,
                "checked_at": max(filter(None, [fr["checked_at"] if fr else None, drop["checked_at"] if drop else None]), default=None),
                "next_check_at": (drop["next_check_at"] if drop and drop["next_check_at"] else fr["next_check_at"] if fr else None),
                "drop_state": drop["state"] if drop else None,
                "release_at": drop["release_at"] if drop else None,
                "available_since": drop["available_since"] if drop else None,
                "watched": name in watched,
                "in_portfolio": name in owned,
                "rdap": details.get(name),
                "reserved": self._reserved_why(name, code, check if seen else None),
                "ai": opinions.get(name),
                "premium_price": round(check["price"]) if seen and code == PREMIUM and check["price"] is not None else None,
                "gandi": self._shown_price(tld, code, gandi.public(check, since, checked), grid),
                "gandi_state": check["kind"] if seen else None,  # sorte qui fait l'état affiché (gandi_effect)
            })
        return items

    @staticmethod
    def _gandi_seen(code: int, check: dict | None, since: str | None, listed: bool, checked: str | None = None) -> bool:
        """La réponse de Gandi fait l'état affiché du nom (et pas une liste de réservés ni un nom enregistré)."""
        effect, kind = gandi_effect(code, check, since, checked, listed)
        return kind is not None and effect == code

    def _reserved_why(self, name: str, code: int, check: dict | None) -> list[str]:
        """Raisons d'un nom réservé : listes et marquages, puis la réponse de Gandi."""
        if code != RESERVED:
            return []
        found = self.reserved.why(name) if self.reserved is not None else []
        if check is not None and check["kind"] in gandi.BLOCKED:
            found = found + [gandi.KIND_LABELS[check["kind"]]]
        return found

    def value_of(self, names: list[str]) -> dict[str, dict]:
        """Score, raisons et filtres d'une liste de noms (écran Suppressions), mêmes calculs que l'Explorer.
        Un nom déjà noté avec les mêmes données est repris tel quel ; touch() oublie un nom qui change."""
        found = {}
        u = self.universe
        # Version des données, lue une fois pour toute la liste : ensemble et versions des extensions,
        # génération (réglages, imports), jour (l'historique compte en jours).
        stamp = (self.generation, date.today(), tuple((t, self._version(t)) for t in self.tlds()))
        contexts: dict[str, dict] = {}
        with self._memo_lock:
            known = dict(self._values)
        fresh = {}
        for name in names:
            label, _, tld = name.partition(".")
            i = u.index(label)
            if i is None:
                found[name] = {"score": 0, "reasons": [], "parts": {}, "natures": [], "rank": None, "good": False}
                continue
            hit = known.get(name)
            if hit is not None and hit[0] == stamp:
                found[name] = dict(hit[1])
                continue
            if tld not in contexts:
                contexts[tld] = self._bonus_context(tld)
            item = self._value(tld, i, label, contexts[tld])
            item.update(natures=sorted(wordlists.natures().get(label, ())), good=bool(u.good[i]), length=len(label),
                        langs=wordlists.langs_of(label))
            fresh[name] = (stamp, item)
            found[name] = dict(item)
        if fresh:
            with self._memo_lock:
                if self.generation == stamp[0]:
                    if any(v[0] != stamp for v in self._values.values()):
                        self._values = {k: v for k, v in self._values.items() if v[0] == stamp}
                    self._values.update(fresh)
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
        if main is not None and domain is not None and domain["state"] and domain["state"] != states.AVAILABLE:
            # Nom surveillé : l'état connu du suivi (même si le dernier check a échoué) l'emporte sur celui de
            # l'Explorer (zone, open data qui datent) ; sinon la fiche dirait « Enregistré » d'un nom en rédemption.
            code = COMING if domain["state"] in (states.EXPIRED, states.REDEMPTION, states.PENDING_DELETE) else REGISTERED
            # Pris : le libellé commun (« Enregistré »), pas « Actif » de l'écran Domaines ; à venir : l'état précis.
            label = states.LABELS.get(domain["state"], main["status_label"]) if code == COMING else LABELS[REGISTERED]
            main = {**main, "status": code, "status_label": label}
        seo = self.conn.execute("SELECT * FROM domain_seo WHERE name = ?", (name,)).fetchone()
        others = [i for t, i in items.items() if t != tld]
        for other in others:
            other["ai_summary"] = ai_summary(other.get("ai"))
        return {
            "name": name, "label": label, "tld": tld, "known": card["known"], "mine": card["mine"],
            "item": main,
            "others": others,
            "comparables": self.comparables(name) if card["known"] else [],
            # Ventes réelles proches (DNJournal) : ce que vaut ce genre de nom quand il se vend.
            "sales": self.market.comparables(name) if getattr(self, "market", None) is not None else [],
            "references": self.references(name),
            "timeline": self.timeline(name, domain, rdap),
            "categories": card.get("categories", []),
            "natures": sorted(wordlists.natures().get(label, ())),
            "rank": self.best_rank(label),
            "seo": dict(seo) if seo else None,
            "rdap": rdap,
            "ai": ai.load(self.conn, [name]).get(name),
            "dns": frdns.load(self.conn, [label]).get(label) if tld == "fr" else None,
            # Dernière réponse de Gandi (prix, statut, date), aussi pour un nom hors des listes.
            "gandi": (main or {}).get("gandi") or gandi.public(gandi.load(self.conn, [name]).get(name), *self._drop_since(name)),
            "domain_id": domain["id"] if domain is not None else None,
            "rdap_next": self._rdap_next(name, tld, domain),
        }

    def _drop_since(self, name: str) -> tuple[str | None, str | None]:
        """Entrée du nom dans son état actuel et dernière vérification au registre (Veille)."""
        row = self.conn.execute("SELECT state_since, checked_at FROM drops WHERE name = ?", (name,)).fetchone()
        return (row["state_since"], gandi.registry_checked(row)) if row else (None, None)

    def _status_label(self, tld: str, code: int) -> str:
        if code == INVALID and tld in ccdns.TLDS:
            return NOT_ASKED
        return absent_label(tld) if code == ABSENT else LABELS.get(code, "")

    def _family(self, i: int, label: str) -> str:
        """Liste principale d'un nom (la première de ses listes d'origine) ; pour un nom court hors des listes, sa
        forme et sa longueur (L3g : 3 lettres prononçables, D3 : 3 chiffres…)."""
        listed = wordlists.own(label)
        # Prénom ou nom des seuls États-Unis : la liste de sa catégorie, comme les autres prénoms et noms.
        return wordlists.MERGED.get(listed[0], listed[0]) if listed else self.universe.shape_class(i)

    def comparables(self, name: str) -> list[dict]:
        """Jusqu'à COMPARABLES noms déjà notés avec la grille (v5) sur la même extension, de la même liste
        principale (pour un nom court : même forme et même longueur) et de longueur proche, les plus
        proches par note de revente. Part des avis de l'extension déjà en mémoire : pas de parcours de l'univers."""
        label, _, tld = name.partition(".")
        u = self.universe
        i = u.index(label)
        if i is None:
            return []
        family = self._family(i, label)
        opinions = self._opinions(tld)
        own = opinions.get(name)
        # Sans avis sur ce nom : l'estimation du score tient lieu de note de revente.
        target = own["resale"] if own and own.get("version", 0) >= 5 else round(quality.predict(label, tld)[0])
        data = self.status_bytes(tld)
        found = []
        for other, o in opinions.items():
            if other == name or o.get("version", 0) < 5:
                continue
            olabel = other.rpartition(".")[0]
            if abs(len(olabel) - len(label)) > COMPARABLE_LENGTH:
                continue
            j = u.index(olabel)
            if j is None or self._family(j, olabel) != family:
                continue
            found.append((abs(o["resale"] - target), other, o, data[j]))
        found.sort(key=lambda x: (x[0], x[1]))
        return [{"name": other, "verdict": o["verdict"], "grade": o.get("grade"), "resale": o["resale"], "exploit": o["exploit"],
                 "final_low": o["final_low"], "final_high": o["final_high"], "status_label": self._status_label(tld, code)}
                for _, other, o, code in found[:COMPARABLES]]

    def references(self, name: str) -> list[dict]:
        """Ventes et offres reçues du portefeuille, les plus proches en longueur : des prix réels à côté de l'avis."""
        label = name.partition(".")[0]
        rows = self.conn.execute("SELECT name, status, sold_price, best_offer, sold_on FROM portfolio "
                                 "WHERE name != ? AND ((status = 'sold' AND sold_price IS NOT NULL) OR best_offer IS NOT NULL)",
                                 (name,)).fetchall()
        rows = sorted(rows, key=lambda r: (abs(len(r["name"].partition(".")[0]) - len(label)), r["name"]))
        return [{"name": r["name"], "kind": "vendu" if r["status"] == "sold" and r["sold_price"] is not None else "offre reçue",
                 "sold_price": r["sold_price"] if r["status"] == "sold" else None, "best_offer": r["best_offer"],
                 "sold_on": r["sold_on"]} for r in rows[:COMPARABLES]]

    def timeline(self, name: str, domain=None, rdap: dict | None = None, release: str | None = None) -> list[dict]:
        """Ce que la base sait du nom, daté : Afnic, registre, zones et Veille, avis IA, surveillance, portefeuille ;
        puis ce qui est à venir (expiration, libération estimée, prochaine vérification), marqué future."""
        label, _, tld = name.partition(".")
        found: list[tuple[str, str, str]] = []

        def add(at, kind: str, text: str) -> None:
            if at:
                found.append((str(at), kind, text))

        created = None
        if tld == "fr":
            fr = self.conn.execute("SELECT created_on, removed_on, expires_at FROM fr_names WHERE label = ?", (label,)).fetchone()
            if fr is not None:
                created = fr["created_on"]
                add(fr["created_on"], "created", "Créé (Afnic)")
                add(fr["removed_on"], "removed", "Supprimé (Afnic)")
                if not (rdap or {}).get("expires_at"):
                    add(fr["expires_at"], "expires", "Expiration")
        rdap = rdap or {}
        if rdap.get("registered_at") and not created:
            add(rdap["registered_at"], "created", "Enregistré (registre)")
        if rdap.get("expires_at") and not rdap.get("available"):
            expires = rdap["expires_at"]
            if tld != "fr" and any(states._normalize(s) == "autorenewperiod" for s in rdap.get("statuses") or []):
                # Période de grâce : le registre affiche déjà l'échéance prolongée d'un an, que personne n'a payée
                # (states.effective_expiry, comme le suivi des domaines). La vraie échéance est un an plus tôt.
                expires = iso(datetime.fromisoformat(str(expires)) - timedelta(days=365))
                add(expires, "expires", "Expiration (renouvelée d'office par le registre, paiement pas encore confirmé)")
            else:
                add(expires, "expires", "Expiration")
        drop = self.conn.execute("SELECT * FROM drops WHERE name = ?", (name,)).fetchone()
        if tld in ccdns.TLDS:
            # Sorti du DNS : seulement un nom vu délégué puis absent. Un nom absent dès la première nuit
            # (réservé, premium, enregistré sans serveurs) n'est jamais « sorti », même vérifié à la main.
            dns = ccdns.load_names(self.conn, [name]).get(name)
            if dns and dns["seen_at"] and dns["lost_at"]:
                add(dns["lost_at"], "dns_out", "Sorti du DNS")
        elif drop is not None and tld != "fr":
            add(drop["removed_on"], "zone_out", "Sorti de la zone")  # vide si vérifié à la main : rien
        # Vérifié sans sortie constatée nulle part (zone, DNS, open data .fr) : pas une libération.
        exited = bool((drop is not None and drop["removed_on"]) or any(k in ("dns_out", "removed") for _, k, _ in found))
        if drop is not None and drop["origin"] and not exited:
            # Vérifié sans être sorti (bouton Vérifier, refus d'achat) : ce que le registre a dit, pas une libération.
            if drop["state"] == states.AVAILABLE:
                add(drop["state_since"], "drop_seen", "Vu libre au registre")
            elif drop["state"] in (drops.TAKEN, states.RENEWED):
                add(drop["state_since"], "drop_seen", "Vu enregistré au registre")
            elif drop["state"] in TIMELINE_DROP_STATES:
                add(drop["state_since"], "drop_" + drop["state"], TIMELINE_DROP_STATES[drop["state"]])
        elif drop is not None:
            if drop["state"] in TIMELINE_DROP_STATES:
                add(drop["state_since"], "drop_" + drop["state"], self._drop_event(drop, rdap))
            add(drop["available_since"], "available", "Libération constatée")
            if drop["state"] != states.AVAILABLE and not drop["available_since"]:
                release = drop["release_at"] or release
        if release:
            add(release, "release", "Libération estimée")
        check = gandi.load(self.conn, [name]).get(name)
        if check is not None:
            add(check["checked_at"], "gandi", "Prix vérifié" + _gandi_detail(check))
        rated = self.conn.execute("SELECT rated_at, reviewed_at FROM domain_ai WHERE name = ?", (name,)).fetchone()
        if rated is not None:
            add(rated["rated_at"], "rated", "Noté par l'IA" if not rated["reviewed_at"] else "Renoté par l'IA (second passage)")
        if domain is not None:
            add(domain["added_at"], "watched", "Surveillé")
            # Dernier check en erreur : l'échéance connue du suivi reste une date utile.
            if not rdap.get("expires_at") and domain["expires_at"] and domain["state"] != states.AVAILABLE:
                add(domain["expires_at"], "expires", "Expiration")
            changes = 0
            for h in self.conn.execute("SELECT at, field, old_value, new_value FROM history WHERE domain_id = ? "
                                       "AND field IN ('state', 'registrar') ORDER BY at", (domain["id"],)):
                if h["field"] == "state":
                    changes += 1
                    add(h["at"], "state", states.LABELS.get(h["new_value"], h["new_value"] or "inconnu"))
                else:
                    add(h["at"], "registrar", f"Registrar : {h['new_value'] or 'inconnu'}")
            # Pas d'historique d'état (vu d'emblée dans cet état) : l'entrée dans l'état courant, si elle est datée.
            if not changes and domain["state"] and domain["state_since"]:
                add(domain["state_since"], "state", "Depuis : " + states.LABELS.get(domain["state"], domain["state"]).lower())
        owned = self.conn.execute("SELECT bought_on, sold_on, status, sold_price FROM portfolio WHERE name = ?", (name,)).fetchone()
        if owned is not None:
            add(owned["bought_on"], "bought", "Acheté")
            if owned["status"] == "sold":
                add(owned["sold_on"], "sold", "Vendu" + (f" ({owned['sold_price']} €)" if owned["sold_price"] is not None else ""))
        nxt = self._rdap_next(name, tld, domain).get("at")
        add(nxt, "next_check", "Prochaine vérification")
        found.sort(key=lambda x: x[0])  # stable : au même instant, l'ordre d'enregistrement
        now = iso(utcnow())
        # Une date seule (AAAA-MM-JJ) est à venir si elle tombe après aujourd'hui.
        return [{"at": at, "kind": kind, "label": text, "future": (at > now[:10]) if len(at) <= 10 else at > now}
                for at, kind, text in found]

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
                return {"at": row["next_check_at"], "why": "suivi des échéances .fr"}
            if row is not None and row["removed_on"]:
                return {"at": None, "why": "supprimé d'après l'Afnic : vérification à la demande"}
            if row is not None:
                return {"at": None, "why": "aucune lecture prévue : vérification à la demande"}
            return {"at": None, "why": "jamais enregistré d'après l'Afnic : vérification à la demande"}
        if tld in ccdns.TLDS:
            best = f"{ccdns.PER_TLD:,}".replace(",", " ")
            return {"at": None, "why": f"pas de lecture RDAP : le DNS du .{tld}, interrogé chaque nuit pour les {best} "
                                      "meilleurs noms, signale le jour où il en sort ; le surveiller pour suivre son échéance"}
        if tld in self.tlds():
            return {"at": None, "why": f"pas de lecture RDAP : la zone .{tld}, lue chaque nuit, signale le jour où il en sort ; "
                                      "le surveiller pour suivre son échéance"}
        return {"at": None, "why": "aucune lecture automatique : vérification à la demande ou en le surveillant"}

    def card(self, label: str) -> dict:
        """Un nom sur toutes les extensions connues."""
        label = self.normalize(label)
        i = self.universe.index(label)
        # mine : dans Mes ajouts (bouton « Retirer de mes ajouts ») ; un nom inconnu peut y être ajouté.
        mine = label in wordlists.lists()[wordlists.MINE]
        if i is None:
            return {"label": label, "known": False, "mine": mine, "items": []}
        items = []
        for tld in self.tlds():
            item = self.describe(tld, [i])[0]
            item.update(self._value(tld, i, label, self._bonus_context(tld)))
            items.append(item)
        return {"label": label, "known": True, "mine": mine, "categories": self.universe.categories(label), "items": items}

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
        state = None if result.available else states.compute_state(result.info.statuses, result.info.expires_at, now, 0, tld)
        # .fr sans délai de grâce : échéance passée sans rédemption ou renouvellement automatique = à quelqu'un.
        registered = not result.available and (state not in drops.TRACKED or (tld == "fr" and state == states.EXPIRED))
        row = self.drops.row(name)
        state = None
        # Hors .fr, la zone ne dit rien d'un nom enregistré sans DNS (hold) : on garde la réponse.
        if row is not None or not registered or tld != "fr":
            # ensure aussi pour une ligne existante : vérifié à la main, un nom d'origine discrète entre dans la Veille.
            row = self.drops.ensure(name, now)
            state = self.drops.apply(row, result, now)
        if tld == "fr":
            # Un nom repris est déjà revenu dans les dates anniversaire par on_fr_taken (drops.apply).
            if registered and row is None:
                self.fr._sync_name(label, result, drops.TAKEN)
            elif not registered:
                self.conn.execute("UPDATE fr_names SET checked_at = ?, next_check_at = NULL WHERE label = ?", (iso(now), label))
        # Nom non pris (libre, à confirmer, bientôt libre) : Gandi dit aussi s'il est premium, à quel prix, ou
        # indisponible. Une panne de Gandi n'empêche pas la vérification : elle est dite à côté.
        gandi_note = None
        # .com, .net : prix unique du registre, celui de la grille ; Gandi n'y répond plus (réponses vides, 10/2026).
        if not registered and self.gandi is not None and self.gandi.key and tld not in PREMIUM_OFF:
            try:
                await self.gandi.refresh(name)
            except gandi.GandiError as exc:
                gandi_note = str(exc)
        self.touch(name)
        i = self.universe.index(label)
        item = self.describe(tld, [i])[0] if i is not None else self._describe_unknown(name, now)
        # Repli sur le WHOIS (RDAP en panne) : dit à l'interface, avec le serveur et le type d'erreur.
        item["note"] = getattr(result, "note", None)
        item["gandi_note"] = gandi_note
        return item

    @staticmethod
    def _drop_event(drop, rdap: dict | None) -> str:
        """Libellé de l'état de la Veille dans Dates clés. « Repris » seulement pour un nouvel enregistrement :
        une date de création antérieure à la sortie du nom (ou d'un an au moins avant l'état vu) dit que le
        titulaire l'a gardé, renouvelé."""
        label = TIMELINE_DROP_STATES[drop["state"]]
        if drop["state"] != drops.TAKEN:
            return label
        created, since = _day((rdap or {}).get("registered_at")), _day(drop["state_since"])
        gone = _day(drop["removed_on"]) or (since and since - timedelta(days=365))
        return "Renouvelé par le titulaire" if created and gone and created < gone else label

    def _describe_unknown(self, name: str, now: datetime) -> dict:
        """Nom hors des listes suivies (tapé dans l'Explorer) : le minimum pour l'afficher, le réserver
        ou le surveiller. Mêmes champs que describe, sans score ni catégorie."""
        label, _, tld = name.partition(".")
        drop = self.drops.row(name)
        state = drop["state"] if drop else None
        rdap = rdapinfo.load(self.conn, [name]).get(name)
        if rdap and rdap["available"]:
            code = UNCONFIRMED if rdapinfo.whois_only(rdap) else FREE
        elif state in drops.TRACKED:
            code = COMING
        else:
            code = SUSPENDED if state == drops.SUSPENDED else REGISTERED
        check = gandi.load(self.conn, [name]).get(name)
        since = drop["state_since"] if drop else None
        checked = gandi.registry_checked(drop)
        code = gandi_effect(code, check, since, checked)[0]
        seen = self._gandi_seen(code, check, since, False, checked)
        watched = self.conn.execute("SELECT 1 FROM domains WHERE name = ?", (name,)).fetchone() is not None
        owned = self.conn.execute("SELECT 1 FROM portfolio WHERE name = ?", (name,)).fetchone() is not None
        return {
            "label": label, "name": name, "tld": tld, "length": len(label), "categories": [], "known": False,
            "status": code, "status_label": (DROP_LABELS.get(state, LABELS[code]) if code == COMING
                                             else FREE_GANDI if seen and code == FREE and state != states.AVAILABLE
                                             else gandi.KIND_LABELS["premium"] if seen and code == PREMIUM
                                             else gandi.KIND_LABELS[check["kind"]] if seen and code == RESERVED else LABELS[code]),
            "drop_state": state, "release_at": drop["release_at"] if drop else None,
            "available_since": drop["available_since"] if drop else None,
            "checked_at": iso(now), "watched": watched, "in_portfolio": owned, "rdap": rdap,
            "reserved": self._reserved_why(name, code, check if seen else None),
            "ai": ai.load(self.conn, [name]).get(name),
            "premium_price": round(check["price"]) if seen and code == PREMIUM and check["price"] is not None else None,
            "gandi": self._shown_price(tld, code, gandi.public(check, since, checked)),
            "gandi_state": check["kind"] if seen else None,
            "score": 0, "reasons": [], "parts": {},
        }



def _day(value: str | None) -> date | None:
    """Jour d'une date ISO, avec ou sans heure (les dates de l'Afnic n'ont pas d'heure)."""
    return date.fromisoformat(value[:10]) if value else None

class VerifyError(Exception):
    pass


def _gandi_detail(check: dict) -> str:
    """« : premium, 878 € », « : libre, 13 € », « : indisponible » pour la ligne de temps."""
    kind = check["kind"]
    word = {"available": "libre", "premium": "premium", "unavailable": "indisponible", "reserved": "réservé",
            "restricted": "restreint", "error": "sans réponse exploitable"}.get(kind, kind)
    price = f", {round(check['price'])} €" if check["price"] is not None and kind in ("available", "premium") else ""
    return f" : {word}{price}"

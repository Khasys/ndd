"""Listes de noms surveillés au-delà des noms courts : mots français, noms de famille, prénoms (France et
États-Unis), mots anglais, secteurs porteurs, tendances, combinaisons, sites de niche et noms de médias (France et
États-Unis) et listes thématiques (métiers, sport, animaux, cuisine), construites par scripts/build_lists.py ; termes
stars, mots-clés à forte valeur, métier + ville (France et États-Unis) et marques inventées, fournis tels quels ;
« Mes ajouts », les noms ajoutés depuis l'Explorer.

Catégories : un nom a ses listes d'origine, plus les catégories dérivées (categories.txt, calculé par
scripts/build_lists.py : assurance aussi dans Termes stars, plombierlyon aussi dans Métiers). Les dérivées
servent à classer et filtrer (classes, categories) ; le score ne connaît que les listes d'origine (lists, ranks).

Fichiers dans app/data, une entrée par ligne, déjà en minuscules et sans accents, du plus
fréquent au moins fréquent. Sources et licences : app/data/SOURCES.md. « Mes ajouts » vit dans le
dossier de données (mes_ajouts.txt), pas dans l'image : il change sans redémarrage (reload).
"""

import os
import threading
import unicodedata
from functools import lru_cache
from pathlib import Path

DATA = Path(__file__).parent / "data"
LABELS = {"word": "Mots", "form": "Formes fléchies", "surname": "Noms de famille", "firstname": "Prénoms",
          "english": "Mots anglais", "sector": "Secteurs porteurs", "trend": "Tendances", "combo": "Noms composés",
          "niche": "Sites de niche", "expr": "Expressions", "media": "Noms de médias",
          "startup": "Tech et startups", "job": "Métiers", "sport": "Sport", "animal": "Animaux",
          "food": "Cuisine", "premium": "Termes stars", "keyword": "Mots-clés à forte valeur",
          "local": "Métier + ville", "brand": "Noms de marque", "firstname_us": "Prénoms des États-Unis",
          "surname_us": "Noms de famille des États-Unis", "niche_us": "Sites de niche des États-Unis",
          "media_us": "Noms de médias des États-Unis", "local_us": "Métier + ville des États-Unis",
          "compound_en": "Composés anglais", "compound_fr": "Composés français", "invented": "Inventés, sélection",
          "mine": "Mes ajouts"}
# Nature des mots et des formes, tirée du Lexique.
NATURES = {"verb": "Verbes", "noun": "Noms communs", "adj": "Adjectifs", "adv": "Adverbes", "other": "Mots outils"}


FILES = {"word": "mots.txt", "form": "formes.txt", "surname": "noms.txt", "firstname": "prenoms.txt",
         "english": "anglais.txt", "sector": "secteurs.txt", "trend": "tendances.txt", "combo": "combinaisons.txt",
         "niche": "niches.txt", "expr": "expressions.txt", "media": "medias.txt",
         "startup": "startup.txt", "job": "metiers.txt", "sport": "sport.txt", "animal": "animaux.txt",
         "food": "cuisine.txt", "premium": "premium.txt", "keyword": "motscles.txt", "local": "local.txt",
         "brand": "marques_inventees.txt", "firstname_us": "prenoms_us.txt", "surname_us": "noms_us.txt",
         "niche_us": "niches_us.txt", "media_us": "medias_us.txt", "local_us": "local_us.txt",
         "compound_en": "composes_en.txt", "compound_fr": "composes_fr.txt", "invented": "inventes.txt"}
# Listes fournies telles quelles (rédigées ailleurs) : scripts/build_lists.py ne les écrit jamais, il en
# relève seulement la langue (--provided).
PROVIDED = ("premium", "keyword", "local", "local_us", "brand")
# Noms ajoutés à la main depuis l'Explorer, dans l'ordre d'ajout, gardés dans le dossier de données.
MINE = "mine"
MINE_FILE = "mes_ajouts.txt"
# Listes d'avant les mots anglais, secteurs, tendances et combinaisons : ce que couvrent les
# lectures de zone faites avant leur ajout.
LEGACY = ("word", "form", "surname", "firstname")
# Listes ajoutées après les imports .fr sans relevé des noms cherchés (zones/fr.searched) : leurs noms
# attendent l'import suivant.
LATE = (*PROVIDED, MINE, "firstname_us", "surname_us", "niche_us", "media_us", "compound_en", "compound_fr", "invented")
# Listes sans sens : une marque inventée vaut par sa sonorité ; « Mes ajouts » ne dit rien du nom.
NO_MEANING = ("brand", "invented", MINE)
# Rang sans valeur pour le score : l'ordre d'ajout ne classe rien.
NO_RANK = (MINE,)
# Catégories dérivées (nom, catégories gagnées), écrites par scripts/build_lists.py. Hors de l'empreinte du score
# (precompute) : elles ne changent aucun score.
DERIVED_FILE = "categories.txt"
# Jamais dérivées : la langue a son propre filtre, une marque inventée vaut par sa sonorité, Mes ajouts sont à part ;
# listes des États-Unis : réunies à leur catégorie (MERGED), pas dérivées.
NEVER_DERIVED = ("word", "form", "english", "surname", "firstname", "firstname_us", "surname_us", "niche_us",
                 "media_us", "local_us", "compound_en", "compound_fr", "brand", "invented", MINE)
# Thèmes : les noms composés gagnés viennent après les mots du thème dans l'ordre « les plus fréquents » (le Top
# Sport commence par football, pas par coachsportif).
THEMES = ("startup", "job", "sport", "animal", "food", "trend")
# Noms composés, découpés en mots pour leur donner un thème.
COMPOUNDS = ("combo", "keyword", "local", "premium", "niche", "expr", "media", "niche_us", "media_us", "local_us",
             "compound_en", "compound_fr")
# Listes d'où vient chaque catégorie dérivée (règles de scripts/build_lists.py) : un nom dérivé y prend son rang
# (Top, « les plus fréquents », filtre de fréquence) : assurance, 1er terme star, parmi les premiers termes stars.
DERIVED_FROM = {"premium": ("sector", "trend"), "keyword": ("combo",), "local": ("combo", "keyword"),
                **{key: COMPOUNDS for key in THEMES}}
# Listes d'une autre source réunies dans une catégorie : prénoms et noms de famille des États-Unis dans Prénoms et
# Noms de famille ; sites de niche, noms de médias et métier + ville en anglais pour les États-Unis dans Sites de
# niche, Noms de médias et Métier + ville. Chacune garde ses rangs (score, ordre « les plus fréquents », « 12e prénom
# le plus donné aux États-Unis ») ; un nom des deux sources est un seul nom de la catégorie.
MERGED = {"firstname_us": "firstname", "surname_us": "surname", "niche_us": "niche", "media_us": "media",
          "local_us": "local", "compound_en": "combo", "compound_fr": "combo", "invented": "brand"}
# Listes sans puce : les secteurs porteurs sont tous dans Termes stars (catégorie dérivée), la liste reste pour le
# score et le calage IA ; les listes des États-Unis sont dans leur catégorie, chacune reste pour ses rangs.
# cat=sector (vues enregistrées, adresses anciennes) se lit comme cat=premium, cat=firstname_us comme cat=firstname.
HIDDEN = ("sector", *MERGED)
ALIASES = {"sector": "premium", **MERGED}
SHOWN = {key: label for key, label in LABELS.items() if key not in HIDDEN}
# Listes sans avis IA de calage (score_model.json) : leur rang compte avec le poids de la liste la plus proche.
# Listes des États-Unis : le poids de leur catégorie (prénoms, noms, sites de niche, médias ; métier + ville : noms
# composés), au meilleur des deux rangs.
SCORE_AS = {"premium": "sector", "keyword": "sector", "local": "combo", "brand": "startup", **MERGED,
            "local_us": "combo", "invented": "startup"}


def _ordered(name: str) -> list[str]:
    path = DATA / name
    provided = any(FILES[k] == name for k in PROVIDED)
    # Liste fournie pas encore livrée : vide plutôt qu'une erreur au démarrage.
    if provided and not path.exists():
        return []
    found = [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if provided:
        # Rédigée ailleurs : une entrée hors format (majuscule, accent, espace) ou en double est écartée, le
        # premier rang gardé (scripts/build_lists.py --provided les signale).
        found = list(dict.fromkeys(w for w in found if valid_label(w)))
    return found


# --- Mes ajouts ----------------------------------------------------------------------------

_mine_path: Path | None = None
_mine_lock = threading.Lock()


def configure(data_dir: Path | None) -> None:
    """Dossier de données de l'appli (main.py) : « Mes ajouts » y est lu et écrit."""
    global _mine_path
    _mine_path = None if data_dir is None else Path(data_dir) / MINE_FILE
    reload()


def mine() -> tuple[str, ...]:
    """Mes ajouts, dans l'ordre d'ajout."""
    return _read_mine()


@lru_cache(maxsize=1)
def _read_mine() -> tuple[str, ...]:
    if _mine_path is None or not _mine_path.exists():
        return ()
    seen: dict[str, None] = {}
    for line in _mine_path.read_text(encoding="utf-8").splitlines():
        if valid_label(line.strip()):
            seen.setdefault(line.strip(), None)
    return tuple(seen)


def _write_mine(names: list[str]) -> None:
    if _mine_path is None:
        raise RuntimeError("Dossier de données inconnu : wordlists.configure() d'abord.")
    _mine_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = _mine_path.with_suffix(".tmp")
    tmp.write_text("".join(f"{n}\n" for n in names), encoding="utf-8")
    os.replace(tmp, _mine_path)


def add_mine(label: str) -> bool:
    """Ajoute un libellé normalisé à Mes ajouts ; False s'il y était déjà. Les caches sont rechargés."""
    with _mine_lock:
        current = list(_read_mine())
        if label in current:
            return False
        _write_mine(current + [label])
        reload()
    return True


def remove_mine(label: str) -> bool:
    with _mine_lock:
        current = list(_read_mine())
        if label not in current:
            return False
        _write_mine([n for n in current if n != label])
        reload()
    return True


# Change à chaque rechargement : l'Explorer y voit qu'il doit refaire son univers.
version = 0


def reload() -> None:
    """Mes ajouts changés : tout ce qui en dépend est relu. Les listes du dossier app/data restent en mémoire."""
    global version
    for cached in (_read_mine, lists, ranks, watched, fingerprints, languages, listed, meaningful, classes):
        cached.cache_clear()
    version += 1


class InvalidLabel(ValueError):
    pass


def valid_label(label: str) -> bool:
    return (1 <= len(label) <= 63 and all(c in "abcdefghijklmnopqrstuvwxyz0123456789-" for c in label)
            and label[0] != "-" and label[-1] != "-")


def normalize(text: str) -> str:
    """Libellé d'un nom tapé (« Mon-Café.fr ») : sans extension, minuscules, sans accents. InvalidLabel sinon."""
    label = text.strip().lower().split(".")[0].replace("œ", "oe").replace("æ", "ae")
    label = "".join(c for c in unicodedata.normalize("NFKD", label) if not unicodedata.combining(c))
    if not label:
        raise InvalidLabel("Nom vide.")
    if len(label) > 63:
        raise InvalidLabel("Nom trop long : 63 caractères au plus.")
    if not valid_label(label):
        raise InvalidLabel("Nom invalide : lettres, chiffres et tirets seulement, sans tiret au début ni à la fin.")
    return label


# --- Listes --------------------------------------------------------------------------------


@lru_cache(maxsize=1)
def _static() -> dict[str, tuple[str, ...]]:
    """Listes du dossier app/data, dans leur ordre : lues une fois par processus."""
    return {key: tuple(_ordered(name)) for key, name in FILES.items()}


@lru_cache(maxsize=1)
def static_lists() -> dict[str, frozenset[str]]:
    """Listes de l'image seulement (sans Mes ajouts) : le précalcul des scores ne connaît qu'elles."""
    return {key: frozenset(names) for key, names in _static().items()}


@lru_cache(maxsize=1)
def lists() -> dict[str, frozenset[str]]:
    return {**static_lists(), MINE: frozenset(mine())}


@lru_cache(maxsize=1)
def _static_ranks() -> dict[str, dict[str, int]]:
    return {key: {w: i + 1 for i, w in enumerate(names)} for key, names in _static().items()}


@lru_cache(maxsize=1)
def ranks() -> dict[str, dict[str, int]]:
    """Rang de chaque nom dans sa liste, 1 pour le plus fréquent (Mes ajouts : ordre d'ajout)."""
    return {**_static_ranks(), MINE: {w: i + 1 for i, w in enumerate(mine())}}


@lru_cache(maxsize=1)
def listed() -> frozenset[str]:
    """Tous les noms des listes."""
    return frozenset().union(*lists().values())


@lru_cache(maxsize=1)
def meaningful() -> frozenset[str]:
    """Noms des listes qui ont un sens (hors marques inventées et Mes ajouts) : des vrais mots pour le score."""
    return frozenset().union(*(names for key, names in lists().items() if key not in NO_MEANING))


@lru_cache(maxsize=1)
def watched() -> frozenset[bytes]:
    """Tous les noms des listes, en octets, pour l'intersection rapide pendant la lecture d'une zone."""
    return frozenset(w.encode() for w in listed())


@lru_cache(maxsize=1)
def fingerprints() -> dict[str, str]:
    """Empreinte de chaque liste : une lecture de zone sait quelles listes elle a couvertes."""
    import hashlib

    return {key: hashlib.sha256("\n".join(sorted(names)).encode()).hexdigest()[:16] for key, names in lists().items()}


@lru_cache(maxsize=1)
def derived() -> dict[str, frozenset[str]]:
    """Noms gagnés par chaque catégorie (absents de sa liste d'origine), lus dans categories.txt. Un nom sorti des
    listes ou une catégorie interdite (dictionnaires, marques inventées, Mes ajouts) est ignoré."""
    path = DATA / DERIVED_FILE
    if not path.exists():
        return {}
    known = frozenset().union(*static_lists().values())
    found: dict[str, set[str]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        name, _, keys = line.partition("\t")
        if name in known:
            for key in keys.split(","):
                if key in LABELS and key not in NEVER_DERIVED:
                    found.setdefault(key, set()).add(name)
    return {key: frozenset(names) for key, names in found.items()}


@lru_cache(maxsize=1)
def classes() -> dict[str, frozenset[str]]:
    """Noms de chaque catégorie : sa liste, les listes qui y sont réunies (MERGED) et les noms dérivés. Classement,
    filtres, compteurs, carte, Top."""
    gained = derived()
    found = {key: names | gained.get(key, frozenset()) for key, names in lists().items()}
    for key, into in MERGED.items():
        found[into] = found[into] | found[key]
    return found


def sources(key: str) -> list[str]:
    """Listes d'origine d'une catégorie : la sienne et celles qui y sont réunies (firstname, firstname_us)."""
    return [key, *(k for k, into in MERGED.items() if into == key)]


def relative(key: str, rank: int) -> float:
    """Rang relatif dans une liste (rang sur taille), pour ranger ensemble des listes de tailles différentes (ordre
    « les plus fréquents », Top). Liste réunie à une catégorie (MERGED) : son rang relatif, juste après le même rang
    relatif de la liste de la catégorie (le 50e prénom des États-Unis entre le 50e et le 51e prénom français ; le
    100e de 7 500 métier + ville des États-Unis près du 200e de 15 000 de France)."""
    into = MERGED.get(key)
    if into is None:
        return rank / len(ranks()[key])
    return rank / len(ranks()[key]) + 0.5 / len(ranks()[into])


def own(label: str) -> list[str]:
    """Listes d'origine du nom (sans les catégories dérivées) : ce que voit le score."""
    return [key for key, names in lists().items() if label in names]


def categories(label: str) -> list[str]:
    """Catégories du nom, listes d'origine et dérivées, dans l'ordre de LABELS (sans les listes masquées)."""
    return [key for key, names in classes().items() if label in names and key not in HIDDEN]


def canonical(key: str) -> str:
    """Catégorie demandée (filtre, Top) : cat=sector se lit comme cat=premium."""
    return ALIASES.get(key, key)


@lru_cache(maxsize=1)
def natures() -> dict[str, frozenset[str]]:
    """Nature de chaque mot ou forme : verb, noun, adj, adv, other (un mot peut en avoir plusieurs)."""
    found = {}
    for line in (DATA / "natures.txt").read_text(encoding="utf-8").splitlines():
        word, _, kinds = line.partition("\t")
        if word:
            found[word] = frozenset(kinds.split(","))
    return found


LANGS = {"fr": "Français", "en": "Anglais"}
# Listes sans langue : le filtre de langue ne s'y applique pas. Marques inventées : pas de sens, pas de
# langue ; Mes ajouts : un nom y garde la langue qu'il a dans une autre liste, la liste entière reste visible.
NO_LANG = ("brand", "invented", MINE)
# Listes d'une seule langue, tout entières : Lexique en français, mots anglais en anglais, prénoms et noms de famille
# en français pour l'Insee, en anglais pour les États-Unis (un nom des deux sources a les deux langues).
LIST_LANG = {"word": "fr", "form": "fr", "surname": "fr", "firstname": "fr", "english": "en", "firstname_us": "en",
             "surname_us": "en", "compound_en": "en", "compound_fr": "fr"}


@lru_cache(maxsize=1)
def _static_languages() -> dict[str, frozenset[str]]:
    lists = static_lists()
    found: dict[str, set[str]] = {lang: set() for lang in LANGS}
    for key, lang in LIST_LANG.items():
        found[lang] |= lists[key]
    path = DATA / "langues.txt"
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            name, _, langs = line.partition("\t")
            for lang in langs.split(","):
                if lang in found:
                    found[lang].add(name)
    return {k: frozenset(v) for k, v in found.items()}


@lru_cache(maxsize=1)
def languages() -> dict[str, frozenset[str]]:
    """Noms par langue : mots et formes en français, mots anglais en anglais, prénoms et noms de famille selon
    leur source (France : français, États-Unis : anglais), et pour les listes mêlées (secteurs, tendances,
    combinaisons, sites de niche, listes thématiques, listes fournies) la langue relevée à la construction.
    Marques inventées : sans langue. Mes ajouts : la langue d'une autre liste, sinon aucune."""
    return _static_languages()


def langs_of(label: str) -> list[str]:
    return [k for k, names in languages().items() if label in names]

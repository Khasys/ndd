"""Listes de noms surveillés au-delà des noms courts : mots français, noms de famille, prénoms,
mots anglais, secteurs porteurs, tendances, combinaisons et listes thématiques (métiers, sport, animaux,
cuisine), construites par scripts/build_lists.py ; termes premium, mots-clés à forte valeur, métier + ville
et marques inventées, fournis tels quels ; « Mes ajouts », les noms ajoutés depuis l'Explorer.

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
          "english": "Mots anglais", "sector": "Secteurs porteurs", "trend": "Tendances", "combo": "Combinaisons",
          "niche": "Sites de niche", "expr": "Expressions", "media": "Médias et marques",
          "startup": "Tech et startups", "job": "Métiers", "sport": "Sport", "animal": "Animaux",
          "food": "Cuisine", "premium": "Termes premium", "keyword": "Mots-clés à forte valeur",
          "local": "Métier + ville", "brand": "Marques inventées", "mine": "Mes ajouts"}
# Nature des mots et des formes, tirée du Lexique.
NATURES = {"verb": "Verbes", "noun": "Noms communs", "adj": "Adjectifs", "adv": "Adverbes", "other": "Mots outils"}


FILES = {"word": "mots.txt", "form": "formes.txt", "surname": "noms.txt", "firstname": "prenoms.txt",
         "english": "anglais.txt", "sector": "secteurs.txt", "trend": "tendances.txt", "combo": "combinaisons.txt",
         "niche": "niches.txt", "expr": "expressions.txt", "media": "medias.txt",
         "startup": "startup.txt", "job": "metiers.txt", "sport": "sport.txt", "animal": "animaux.txt",
         "food": "cuisine.txt", "premium": "premium.txt", "keyword": "motscles.txt", "local": "local.txt",
         "brand": "marques_inventees.txt"}
# Listes fournies telles quelles (rédigées ailleurs) : scripts/build_lists.py ne les écrit jamais, il en
# relève seulement la langue (--provided).
PROVIDED = ("premium", "keyword", "local", "brand")
# Noms ajoutés à la main depuis l'Explorer, dans l'ordre d'ajout, gardés dans le dossier de données.
MINE = "mine"
MINE_FILE = "mes_ajouts.txt"
# Listes d'avant les mots anglais, secteurs, tendances et combinaisons : ce que couvrent les
# lectures de zone faites avant leur ajout.
LEGACY = ("word", "form", "surname", "firstname")
# Listes ajoutées après les imports .fr sans relevé des noms cherchés (zones/fr.searched) : leurs noms
# attendent l'import suivant.
LATE = (*PROVIDED, MINE)
# Listes sans sens : une marque inventée vaut par sa sonorité ; « Mes ajouts » ne dit rien du nom.
NO_MEANING = ("brand", MINE)
# Rang sans valeur pour le score : l'ordre d'ajout ne classe rien.
NO_RANK = (MINE,)
# Listes sans avis IA de calage (score_model.json) : leur rang compte avec le poids de la liste la plus proche.
SCORE_AS = {"premium": "sector", "keyword": "sector", "local": "combo", "brand": "startup"}


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
    for cached in (_read_mine, lists, ranks, watched, fingerprints, languages, listed, meaningful):
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


def categories(label: str) -> list[str]:
    return [key for key, names in lists().items() if label in names]


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
NO_LANG = ("surname", "firstname", "brand", MINE)


@lru_cache(maxsize=1)
def _static_languages() -> dict[str, frozenset[str]]:
    lists = static_lists()
    found = {"fr": set(lists["word"]) | set(lists["form"]), "en": set(lists["english"])}
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
    """Noms par langue : mots et formes en français, mots anglais en anglais, et pour les listes
    mêlées (secteurs, tendances, combinaisons, sites de niche, listes thématiques, listes fournies) la
    langue relevée à la construction. Noms de famille, prénoms et marques inventées : sans langue.
    Mes ajouts : la langue d'une autre liste, sinon aucune."""
    return _static_languages()


def langs_of(label: str) -> list[str]:
    return [k for k, names in languages().items() if label in names]

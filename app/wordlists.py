"""Listes de noms surveillés au-delà des noms courts : mots français, noms de famille, prénoms,
mots anglais, secteurs porteurs, tendances et combinaisons (construites par scripts/build_lists.py).

Fichiers dans app/data, une entrée par ligne, déjà en minuscules et sans accents, du plus
fréquent au moins fréquent. Sources et licences : app/data/SOURCES.md.
"""

from functools import lru_cache
from pathlib import Path

DATA = Path(__file__).parent / "data"
LABELS = {"word": "Mots", "form": "Formes fléchies", "surname": "Noms de famille", "firstname": "Prénoms",
          "english": "Mots anglais", "sector": "Secteurs porteurs", "trend": "Tendances", "combo": "Combinaisons",
          "niche": "Sites de niche", "expr": "Expressions"}
# Nature des mots et des formes, tirée du Lexique.
NATURES = {"verb": "Verbes", "noun": "Noms communs", "adj": "Adjectifs", "adv": "Adverbes", "other": "Mots outils"}


FILES = {"word": "mots.txt", "form": "formes.txt", "surname": "noms.txt", "firstname": "prenoms.txt",
         "english": "anglais.txt", "sector": "secteurs.txt", "trend": "tendances.txt", "combo": "combinaisons.txt",
         "niche": "niches.txt", "expr": "expressions.txt"}
# Listes d'avant les mots anglais, secteurs, tendances et combinaisons : ce que couvrent les
# lectures de zone faites avant leur ajout.
LEGACY = ("word", "form", "surname", "firstname")


def _ordered(name: str) -> list[str]:
    path = DATA / name
    return [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


@lru_cache(maxsize=1)
def lists() -> dict[str, frozenset[str]]:
    return {key: frozenset(_ordered(name)) for key, name in FILES.items()}


@lru_cache(maxsize=1)
def ranks() -> dict[str, dict[str, int]]:
    """Rang de chaque nom dans sa liste, 1 pour le plus fréquent."""
    return {key: {w: i + 1 for i, w in enumerate(_ordered(name))} for key, name in FILES.items()}


@lru_cache(maxsize=1)
def watched() -> frozenset[bytes]:
    """Tous les noms des listes, en octets, pour l'intersection rapide pendant la lecture d'une zone."""
    return frozenset(w.encode() for names in lists().values() for w in names)


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
# Listes sans langue : le filtre de langue ne s'y applique pas.
NO_LANG = ("surname", "firstname")


@lru_cache(maxsize=1)
def languages() -> dict[str, frozenset[str]]:
    """Noms par langue : mots et formes en français, mots anglais en anglais, et pour les listes
    mêlées (secteurs, tendances, combinaisons, sites de niche) la langue relevée à la construction.
    Noms de famille et prénoms : sans langue."""
    found = {"fr": set(lists()["word"]) | set(lists()["form"]), "en": set(lists()["english"])}
    path = DATA / "langues.txt"
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            name, _, langs = line.partition("\t")
            for lang in langs.split(","):
                if lang in found:
                    found[lang].add(name)
    return {k: frozenset(v) for k, v in found.items()}


def langs_of(label: str) -> list[str]:
    return [k for k, names in languages().items() if label in names]

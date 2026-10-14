"""Listes de noms surveillés au-delà des noms courts : mots français, noms de famille, prénoms.

Fichiers dans app/data, une entrée par ligne, déjà en minuscules et sans accents, du plus
fréquent au moins fréquent. Sources et licences : app/data/SOURCES.md.
"""

from functools import lru_cache
from pathlib import Path

DATA = Path(__file__).parent / "data"
LABELS = {"word": "Mots", "surname": "Noms de famille", "firstname": "Prénoms"}


FILES = {"word": "mots.txt", "surname": "noms.txt", "firstname": "prenoms.txt"}


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


def categories(label: str) -> list[str]:
    return [key for key, names in lists().items() if label in names]

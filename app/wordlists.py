"""Listes de noms surveillés au-delà des noms courts : mots français et noms de famille.

Fichiers dans app/data, une entrée par ligne, déjà en minuscules et sans accents.
Sources et licences : app/data/SOURCES.md.
"""

from functools import lru_cache
from pathlib import Path

DATA = Path(__file__).parent / "data"
LABELS = {"word": "Mots", "surname": "Noms de famille"}


def _read(name: str) -> frozenset[str]:
    path = DATA / name
    return frozenset(line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip())


@lru_cache(maxsize=1)
def lists() -> dict[str, frozenset[str]]:
    return {"word": _read("mots.txt"), "surname": _read("noms.txt")}


@lru_cache(maxsize=1)
def watched() -> frozenset[bytes]:
    """Tous les noms des listes, en octets, pour l'intersection rapide pendant la lecture d'une zone."""
    return frozenset(w.encode() for names in lists().values() for w in names)


def categories(label: str) -> list[str]:
    return [key for key, names in lists().items() if label in names]

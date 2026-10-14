"""Un nom court se prononce-t-il ? Modèle de trigrammes de lettres appris sur le Lexique.

Moyenne du logarithme de la probabilité de chaque lettre sachant les deux précédentes, début et
fin de mot compris. Seuil choisi sur le Lexique : 92 % des vrais mots de 3 et 4 lettres le
passent, contre 8 % des suites de lettres tirées au hasard (oye passe, qsd non).
"""

import json
import math
from functools import lru_cache
from pathlib import Path

DATA = Path(__file__).parent / "data"
THRESHOLD = -3.4
SMOOTHING = 0.1
ALPHABET = 28


@lru_cache(maxsize=1)
def _model() -> tuple[dict, dict]:
    data = json.loads((DATA / "trigrammes.json").read_text())
    return data["tri"], data["bi"]


@lru_cache(maxsize=1)
def real_words() -> frozenset[str]:
    """Toutes les formes de 2 à 4 lettres du Lexique, même rares."""
    return frozenset((DATA / "mots_courts.txt").read_text().split())


@lru_cache(maxsize=None)
def _step(trigram: str) -> float:
    tri, bi = _model()
    return math.log((tri.get(trigram, 0) + SMOOTHING) / (bi.get(trigram[:2], 0) + SMOOTHING * ALPHABET))


def log_prob(label: str) -> float | None:
    if not label.isalpha() or not label.isascii():
        return None
    s = "^^" + label + "$"
    return sum(_step(s[i:i + 3]) for i in range(len(s) - 2)) / (len(s) - 2)


def pronounceable(label: str) -> bool:
    value = log_prob(label)
    return value is not None and value >= THRESHOLD

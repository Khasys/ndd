"""Calcul long fait une fois, à la construction de l'image : forme des 1,9 million de noms courts.

Un octet par nom de 1 à 4 caractères : points de forme (0 à 40) + 128 si le nom a un sens
(vrai mot, prononçable, chiffres seuls). En tête, l'empreinte des fichiers de données utilisés :
si elle ne correspond plus, le fichier est ignoré et le calcul refait au démarrage.

    python -m app.precompute    (Dockerfile)
"""

import hashlib
import itertools
from pathlib import Path

from . import drops, pronounce, value, wordlists

DATA = Path(__file__).parent / "data"
TARGET = DATA / "courts.bin"
SOURCES = ("mots.txt", "formes.txt", "noms.txt", "prenoms.txt", "mots_courts.txt", "trigrammes.json")
GOOD = 128


def fingerprint() -> bytes:
    digest = hashlib.sha256()
    for name in SOURCES:
        digest.update((DATA / name).read_bytes())
    digest.update(Path(value.__file__).read_bytes())
    digest.update(Path(pronounce.__file__).read_bytes())
    return digest.digest()


def compute() -> bytearray:
    table = bytearray(drops.CELLS)
    listed = {w for names in wordlists.lists().values() for w in names}
    for i in range(drops.CELLS):
        label = drops.decode(i)
        if not drops.valid_label(label):
            continue
        points = value.shape(label)[0]
        kind = drops.kind_of(label)
        good = label in listed or kind == "digits" or (kind == "letters" and value.quality(label) != "letters")
        table[i] = points | (GOOD if good else 0)
    return table


def load() -> bytearray | None:
    try:
        data = TARGET.read_bytes()
    except OSError:
        return None
    if len(data) != 32 + drops.CELLS or data[:32] != fingerprint():
        return None
    return bytearray(data[32:])


def build() -> None:
    TARGET.write_bytes(fingerprint() + compute())


if __name__ == "__main__":
    build()

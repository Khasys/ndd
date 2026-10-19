"""Calcul long fait une fois, à la construction de l'image : score de base de tous les noms suivis.

Un octet par nom de l'univers (1,9 million de noms courts, puis les mots des listes) : points de forme et
de sens (0 à 70, la meilleure de .fr et .com, plus une marge) + 128 si le nom a un sens (vrai mot,
prononçable, chiffres seuls). En tête, l'empreinte des fichiers utilisés : si elle ne correspond plus, le
fichier est ignoré et le calcul refait au démarrage.

Seuls les noms classables sont calculés (1 à 3 caractères, 4 lettres, 4 chiffres, mots des listes) : les
4 caractères mélangés (1,4 million) valent trop peu pour le classement.

    python -m app.precompute    (Dockerfile)
"""

import hashlib
import itertools
from functools import lru_cache
from pathlib import Path

from . import drops, pronounce, quality, value, wordlists

DATA = Path(__file__).parent / "data"
TARGET = DATA / "scores.bin"
GOOD = 128
# Marge sur la borne : une autre extension (.io pour un prénom) peut dépasser un peu .fr et .com.
MARGIN = 2
# Les 4 caractères mélangés ou à tiret ne sont pas calculés (table à 0) : leur forme et sens vaut 9 au
# plus (mesuré sur les 1,4 million, aa-h), d'où cette borne quand le classement en rencontre un.
UNRANKED_BOUND = 9 + MARGIN


def fingerprint() -> bytes:
    digest = hashlib.sha256()
    for path in sorted(DATA.iterdir()):
        # Catégories dérivées : classement et filtres seulement, jamais le score (tests/test_lists.py le vérifie).
        if path.suffix in (".txt", ".json") and path.name != wordlists.DERIVED_FILE:
            digest.update(path.name.encode())
            digest.update(path.read_bytes())
    # Les règles de calcul (ce module : marge, noms classables ; drops : alphabet et ordre des noms) aussi.
    for path in (Path(__file__), *(Path(module.__file__) for module in (drops, value, pronounce, quality, wordlists))):
        digest.update(path.read_bytes())
    return digest.digest()


@lru_cache(maxsize=1)
def words() -> list[str]:
    """Mots des listes de l'image de plus de 4 caractères, triés : ils suivent les noms courts dans l'univers.
    Mes ajouts (dossier de données) n'y sont pas : l'Explorer les note à part, au chargement (explore.Universe).
    Gardés par le processus (listes de l'image : fixes) ; à ne pas modifier."""
    return sorted({w for names in wordlists.static_lists().values() for w in names if len(w) > drops.MAX_SHORT})


def base(label: str) -> int:
    return min(70, value.base(label) + MARGIN)


def compute() -> bytearray:
    found = words()
    table = bytearray(drops.CELLS + len(found))
    listed = {w for names in wordlists.static_lists().values() for w in names}
    letters4 = (drops.encode("".join(p)) for p in itertools.product(drops.CHARSET[:26], repeat=4))
    digits4 = (drops.encode("".join(p)) for p in itertools.product(drops.CHARSET[26:36], repeat=4))
    ranked = set(itertools.chain(range(0, drops.OFFSETS[3]), letters4, digits4))
    for i in range(drops.CELLS):
        label = drops.decode(i)
        if not drops.valid_label(label):
            continue
        kind = drops.kind_of(label)
        good = label in listed or kind == "digits" or (kind == "letters" and value.quality(label) != "letters")
        table[i] = (base(label) if i in ranked or label in listed else 0) | (GOOD if good else 0)
    for n, label in enumerate(found):
        table[drops.CELLS + n] = base(label) | GOOD
    return table


def byte(label: str) -> int:
    """Octet d'un nom de Mes ajouts, calculé au chargement : même règle que compute pour un nom des listes."""
    return base(label) | GOOD


def load() -> bytearray | None:
    try:
        data = TARGET.read_bytes()
    except OSError:
        return None
    if len(data) != 32 + drops.CELLS + len(words()) or data[:32] != fingerprint():
        return None
    return bytearray(data[32:])


_table: bytes | None = None


def table() -> bytearray:
    """Table du fichier si elle correspond, sinon calculée. Gardée par le processus : l'univers se refait à
    chaque changement de Mes ajouts, et l'empreinte seule (tous les fichiers de app/data) coûte 0,2 s."""
    global _table
    if _table is None:
        _table = bytes(load() or compute())
    return bytearray(_table)


def build() -> None:
    TARGET.write_bytes(fingerprint() + compute())


if __name__ == "__main__":
    build()

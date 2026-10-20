"""Construit app/data/local_us.txt : métier + ville des États-Unis, en anglais, collés dans les deux sens
(phoenixplumber, plumberphoenix), pour le marché américain du .com.

Usage : python scripts/build_local_us.py, puis python scripts/build_lists.py --provided (langue, catégories).

Liste fournie comme local.txt (scripts/build_lists.py ne l'écrit pas, --provided en vérifie le format et en relève la
langue), réunie à la catégorie Métier + ville (wordlists.MERGED). Vocabulaire : scripts/lists/metier_ville_us.txt,
villes de la plus peuplée à la moins, métiers du plus demandé au moins. Paires rangées par produit des rangs (les
plus grandes villes avec les métiers les plus demandés d'abord), la ville devant puis derrière le métier. Un nom déjà
dans une autre liste n'y entre pas (les combinaisons ont déjà les 42 services de trades_en.txt avec les 52 villes de
cities_us.txt : dallasplumber), ni un nom de plus de LONGEST lettres.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
from app import wordlists  # noqa: E402
import build_lists  # noqa: E402  (lecture des listes à la main, rangement par produit des rangs)

# Autant de noms que la catégorie Métier + ville en a en français (11 855) avec les 4 368 noms service + ville des
# combinaisons : environ 11 900 noms en anglais.
SIZE = 7_500
LONGEST = 24  # injurylawyersanantonio oui, truckaccidentlawyersanfrancisco non


def build() -> list[str]:
    vocab = build_lists.sections("metier_ville_us.txt")
    others = set().union(*(wordlists._ordered(file) for key, file in wordlists.FILES.items() if key != "local_us"))
    pairs = build_lists.ranked(vocab["villes"], vocab["metiers"], lambda city, trade: [city + trade, trade + city])
    found = [n for n in build_lists.unique(pairs) if n not in others and len(n) <= LONGEST]
    return found[:SIZE]


if __name__ == "__main__":
    names = build()
    (wordlists.DATA / wordlists.FILES["local_us"]).write_text("\n".join(names) + "\n", encoding="utf-8")
    print(f"{wordlists.FILES['local_us']}: {len(names)}")

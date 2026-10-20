"""Construit les prénoms et les noms de famille des États-Unis dans app/data (prenoms_us.txt, noms_us.txt).

Usage : python scripts/build_names_us.py PRENOMS_CSV NOMS_CSV

Sans réseau : les deux fichiers sont téléchargés avant, données publiques du gouvernement américain.
PRENOMS_CSV : naissances par prénom de 1880 à 2017, filles et garçons réunis (colonnes name,n), tirées du jeu
babynames (Social Security Administration, github.com/hadley/babynames : total de la table babynames par prénom).
NOMS_CSV : surnames.csv du recensement de 2010 (github.com/fivethirtyeight/data, dossier most-common-name ;
colonnes name,rank,count…), en majuscules.

Même normalisation que les listes françaises (minuscules, sans accents), libellé valide pour l'appli
(wordlists.valid_label), du plus fréquent au moins fréquent : 10 000 prénoms, autant de noms que noms.txt.
Sources et seuils : app/data/SOURCES.md.
"""

import csv
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from app import wordlists  # noqa: E402  normalisation et format des libellés

DATA = ROOT / "app" / "data"
FIRST_NAMES = 10_000
# Lignes du recensement qui ne sont pas un nom (absentes du fichier de 2010, présentes dans d'autres éditions).
NOT_NAMES = {"all other names"}
# Noms courts : un nom de 1 ou 2 lettres (de, ng) n'a pas de sens comme nom de domaine, et un nom rare de 3 lettres
# (abt, eck) passerait pour un vrai mot court, très valorisé. 3 lettres : seulement parmi les plus portés.
SHORT_TOP = 2_000


def ranked(path: Path, count: str, size: int) -> list[str]:
    """Libellés du fichier, du plus fréquent au moins fréquent (ordre du fichier à fréquence égale), sans doublon."""
    with path.open(encoding="utf-8", newline="") as f:
        rows = [(row["name"], int(row[count])) for row in csv.DictReader(f)]
    rows.sort(key=lambda r: -r[1])  # tri stable : à égalité, l'ordre de la source
    found: dict[str, None] = {}
    for name, _ in rows:
        if name.strip().lower() in NOT_NAMES:
            continue
        try:
            label = wordlists.normalize(name.replace(" ", "").replace("'", ""))
        except wordlists.InvalidLabel:
            continue
        if len(label) <= 2 or (len(label) == 3 and len(found) >= SHORT_TOP):
            continue
        found.setdefault(label, None)
        if len(found) >= size:
            break
    return list(found)


def write(name: str, labels: list[str]) -> None:
    (DATA / name).write_text("\n".join(labels) + "\n", encoding="utf-8")
    print(f"{name}: {len(labels)}")


def main() -> None:
    first_csv, last_csv = Path(sys.argv[1]), Path(sys.argv[2])
    # Autant de noms que la liste française (Insee) : les deux sources pèsent pareil dans Noms de famille.
    french = len((DATA / wordlists.FILES["surname"]).read_text(encoding="utf-8").split())
    write(wordlists.FILES["firstname_us"], ranked(first_csv, "n", FIRST_NAMES))
    write(wordlists.FILES["surname_us"], ranked(last_csv, "count", french))


if __name__ == "__main__":
    main()

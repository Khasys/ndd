"""Score « affaire » d'un nom, de 0 à 100, avec ses raisons.

Barème fixe et lisible plutôt qu'un modèle : chaque point vient d'une raison affichée.

- Forme (40 au plus) : plus c'est court, mieux c'est ; lettres avant chiffres, chiffres avant
  mélanges, tiret en dernier. Un nom de 4 lettres prononçable vaut plus qu'une suite de consonnes.
- Popularité (30 au plus) : rang du mot, du nom de famille ou du prénom dans sa liste.
- Demande (15 au plus) : le même nom est pris sur d'autres extensions, .com surtout.
- Historique (15 au plus) : pour un .fr supprimé, années de détention et suppression récente.
- SEO (20 au plus) : Open PageRank et archives Wayback, si l'analyse a été faite.

Forme + popularité ne dépendent que du nom : c'est la « base », calculée une fois.
Le reste dépend de l'extension et des données du moment : c'est le « bonus », 50 au plus.
"""

import math
from datetime import date

from . import drops, wordlists

MAX_BONUS = 50
VOWELS = set("aeiouy")


def pronounceable(label: str) -> bool:
    """Au moins une voyelle et jamais trois consonnes de suite (lettres seulement)."""
    if not any(c in VOWELS for c in label):
        return False
    run = 0
    for c in label:
        run = 0 if c in VOWELS else run + 1
        if run >= 3:
            return False
    return True


def shape(label: str) -> tuple[int, str | None]:
    n = len(label)
    if n <= drops.MAX_SHORT:
        kind = drops.kind_of(label)
        table = {
            "letters": {1: 40, 2: 40, 3: 34, 4: 22},
            "digits": {1: 30, 2: 30, 3: 22, 4: 14},
            "mixed": {2: 26, 3: 16, 4: 7},
            "hyphen": {3: 4, 4: 2},
        }
        points = table[kind].get(n, 0)
        if kind == "letters" and n == 4 and pronounceable(label):
            points += 6
            return points, "4 lettres prononçables"
        names = {"letters": "lettre", "digits": "chiffre", "mixed": "caractère", "hyphen": "caractère"}
        unit = names[kind] + ("s" if n > 1 else "")
        return points, f"{n} {unit}" + (" avec tiret" if kind == "hyphen" else "")
    if n <= 6:
        return 10, f"{n} lettres"
    if n <= 8:
        return 6, None
    return 2, None


POPULAR = {"word": "mot", "surname": "nom", "firstname": "prénom"}


def popularity(label: str) -> tuple[int, str | None]:
    """Le meilleur rang parmi les listes : 30 pour le premier, 0 pour le dernier."""
    best, reason = 0, None
    for key, ranks in wordlists.ranks().items():
        rank = ranks.get(label)
        if rank is None:
            continue
        points = round(30 * (1 - math.sqrt((rank - 1) / len(ranks))))
        if points > best:
            best, reason = points, f"{POPULAR[key]} n° {rank:,}".replace(",", " ")
    return best, reason


def base(label: str) -> int:
    return min(70, shape(label)[0] + popularity(label)[0])


def demand(taken: list[str]) -> tuple[int, str | None]:
    if not taken:
        return 0, None
    points = (10 if "com" in taken else 4) + 3 * len([t for t in taken if t != "com"])
    return min(15, points), "pris en " + ", ".join(f".{t}" for t in taken)


def history(created: str | None, removed: str | None, today: date) -> tuple[int, str | None]:
    if not created or not removed:
        return 0, None
    years = (date.fromisoformat(removed) - date.fromisoformat(created)).days / 365.25
    points = min(10, int(years))
    reason = f"tenu {int(years)} an{'s' if int(years) > 1 else ''}" if years >= 1 else None
    if (today - date.fromisoformat(removed)).days < 730:
        points += 5
        reason = (reason + ", " if reason else "") + f"supprimé en {removed[:4]}"
    return min(15, points), reason


def seo(row) -> tuple[int, list[str]]:
    if row is None:
        return 0, []
    points, reasons = 0, []
    if row["pr"] is not None and row["pr"] > 0:
        points += min(14, round(row["pr"] * 2))
        reasons.append(f"PageRank {row['pr']:.1f}".replace(".", ","))
    if row["wb_first"]:
        points += min(6, (row["wb_years"] or 0) // 2)
        reasons.append(f"archivé depuis {row['wb_first'][:4]}" + (f" ({row['wb_years']} ans actifs)" if row["wb_years"] else ""))
    return min(20, points), reasons


def score(label: str, taken: list[str], fr_row=None, seo_row=None, today: date | None = None) -> dict:
    today = today or date.today()
    reasons = []
    total = 0
    for points, reason in (shape(label), popularity(label), demand(taken)):
        total += points
        if reason:
            reasons.append(reason)
    if fr_row is not None:
        points, reason = history(fr_row["created_on"], fr_row["removed_on"], today)
        total += points
        if reason:
            reasons.append(reason)
    points, extra = seo(seo_row)
    total += points
    reasons += extra
    return {"score": min(100, total), "reasons": reasons}

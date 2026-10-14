"""Score « affaire » d'un nom, de 0 à 100, avec ses raisons.

Barème fixe et lisible plutôt qu'un modèle : chaque point vient d'une raison affichée.

- Forme (40 au plus) : plus c'est court, mieux c'est ; lettres avant chiffres, chiffres avant
  mélanges, tiret en dernier. En lettres : un vrai mot (même rare, d'après le Lexique) vaut plus
  qu'un nom prononçable, qui vaut bien plus qu'une suite de lettres (pronounce.py).
- Popularité (30 au plus) : rang du mot, du nom de famille ou du prénom dans sa liste.
- Demande (15 au plus) : le même nom est pris sur d'autres extensions, .com surtout.
- Historique (15 au plus) : pour un .fr supprimé, années de détention et suppression récente.
- SEO (20 au plus) : Open PageRank et archives Wayback, si l'analyse a été faite.

Forme + popularité ne dépendent que du nom : c'est la « base », calculée une fois.
Le reste dépend de l'extension et des données du moment : c'est le « bonus », 50 au plus.
"""

import math
from datetime import date

from . import drops, pronounce, wordlists

MAX_BONUS = 50


def quality(label: str) -> str:
    """word : vrai mot ; pron : prononçable ; letters : suite de lettres ; autre sinon."""
    if label in pronounce.real_words() or any(label in names for names in wordlists.lists().values()):
        return "word"
    if label.isalpha():
        return "pron" if len(label) <= 2 or pronounce.pronounceable(label) else "letters"
    return "other"


def shape(label: str) -> tuple[int, str | None]:
    n = len(label)
    if n <= drops.MAX_SHORT:
        kind = drops.kind_of(label)
        if kind == "letters":
            q = quality(label)
            table = {"word": {1: 40, 2: 40, 3: 36, 4: 30}, "pron": {1: 40, 2: 40, 3: 30, 4: 24},
                     "letters": {3: 16, 4: 6}}[q]
            reason = {"word": "vrai mot", "pron": "prononçable", "letters": "imprononçable"}[q]
            return table.get(n, 0), f"{n} lettre{'s' if n > 1 else ''}, {reason}" if n > 2 else f"{n} lettre{'s' if n > 1 else ''}"
        table = {
            "digits": {1: 30, 2: 30, 3: 22, 4: 14},
            "mixed": {2: 26, 3: 16, 4: 7},
            "hyphen": {3: 4, 4: 2},
        }
        points = table[kind].get(n, 0)
        names = {"digits": "chiffre", "mixed": "caractère", "hyphen": "caractère"}
        unit = names[kind] + ("s" if n > 1 else "")
        return points, f"{n} {unit}" + (" avec tiret" if kind == "hyphen" else "")
    if n <= 6:
        return 10, f"{n} lettres"
    if n <= 8:
        return 6, None
    return 2, None


POPULAR = {"word": "mot", "form": "forme", "surname": "nom", "firstname": "prénom"}


def popularity(label: str) -> tuple[int, str | None]:
    """Le meilleur rang parmi les listes : 30 pour le premier, 0 pour le dernier."""
    best, reason = 0, None
    for key, ranks in wordlists.ranks().items():
        rank = ranks.get(label)
        if rank is None:
            continue
        points = round(30 * (1 - math.sqrt((rank - 1) / len(ranks))))
        if key == "form":
            points = round(points * 0.7)  # une forme fléchie vaut moins que le mot de base
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


# Poids de chaque critère, de 0 (ignoré) à 2 (doublé), réglables dans Paramètres.
CRITERIA = {"shape": "Forme", "popularity": "Popularité", "demand": "Demande", "history": "Historique", "seo": "SEO"}
DEFAULT_WEIGHTS = dict.fromkeys(CRITERIA, 1.0)


def weights_from(raw: str | None) -> dict[str, float]:
    import json

    try:
        found = json.loads(raw) if raw else {}
    except ValueError:
        found = {}
    return {k: max(0.0, min(2.0, float(found.get(k, 1.0)))) for k in CRITERIA}


def score(label: str, taken: list[str], fr_row=None, seo_row=None, today: date | None = None,
          weights: dict[str, float] | None = None) -> dict:
    today = today or date.today()
    weights = weights or DEFAULT_WEIGHTS
    parts: dict[str, int] = {}
    reasons = []
    for key, (points, reason) in (("shape", shape(label)), ("popularity", popularity(label)), ("demand", demand(taken))):
        parts[key] = points
        if reason:
            reasons.append(reason)
    parts["history"] = 0
    if fr_row is not None:
        points, reason = history(fr_row["created_on"], fr_row["removed_on"], today)
        parts["history"] = points
        if reason:
            reasons.append(reason)
    parts["seo"], extra = seo(seo_row)
    reasons += extra
    weighted = {k: round(v * weights[k]) for k, v in parts.items()}
    return {"score": min(100, sum(weighted.values())), "reasons": reasons, "parts": weighted}

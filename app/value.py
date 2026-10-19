"""Score « affaire » d'un nom, de 0 à 100, avec ses raisons.

Barème fixe et lisible plutôt qu'un modèle : chaque point vient d'une raison affichée.

- Forme et sonorité, sens et marché (70 au plus à eux deux) : calés sur des avis IA détaillés
  (quality.py, score_model.json). L'ancien barème de forme (vrai mot, prononçable, suite de lettres)
  et de popularité (rang dans sa liste) y entre comme une mesure parmi d'autres.
- Demande (15 au plus) : le même nom est pris sur d'autres extensions, .com surtout.
- Historique (15 au plus) : pour un .fr supprimé, années de détention et suppression récente.
- SEO (20 au plus) : Open PageRank et archives Wayback, si l'analyse a été faite.

Forme et sens ne dépendent que du nom et de l'extension : c'est la « base », précalculée.
Le reste dépend de l'extension et des données du moment : c'est le « bonus », 50 au plus.
"""

import math
from datetime import date

from . import drops, pronounce, wordlists
from . import quality as measures  # le nom quality est pris par la fonction plus bas


def quality(label: str) -> str:
    """word : vrai mot ; pron : prononçable ; letters : suite de lettres ; autre sinon."""
    # Une marque inventée ou un nom de Mes ajouts n'est pas un vrai mot : sa forme se juge à la sonorité.
    if label in pronounce.real_words() or label in wordlists.meaningful():
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


POPULAR = {"word": "mot", "form": "forme", "surname": "nom", "firstname": "prénom", "english": "anglais",
           "sector": "secteur", "trend": "tendance", "combo": "combinaison", "niche": "site de niche",
           "expr": "expression", "media": "nom de média",
           "startup": "tech et startups", "job": "métier", "sport": "sport", "animal": "animal", "food": "cuisine",
           "premium": "terme premium", "keyword": "mot-clé", "local": "métier + ville", "brand": "marque inventée"}
# Une forme fléchie vaut moins que le mot de base ; une combinaison bien moins qu'un mot seul (métier + ville
# en est une ; une marque inventée n'a pas de sens, seulement un rang parmi les autres).
POPULAR_FACTOR = {"form": 0.7, "combo": 0.5, "niche": 0.5, "expr": 0.8, "media": 0.6, "local": 0.5, "brand": 0.5}


def popularity(label: str) -> tuple[int, str | None]:
    """Le meilleur rang parmi les listes : 30 pour le premier, 0 pour le dernier."""
    best, reason = 0, None
    for key, ranks in wordlists.ranks().items():
        rank = ranks.get(label)
        if rank is None or key in wordlists.NO_RANK:
            continue
        points = round(30 * (1 - math.sqrt((rank - 1) / len(ranks))))
        points = round(points * POPULAR_FACTOR.get(key, 1))
        if points > best:
            best, reason = points, f"{_nth(rank)} {POPULAR_PHRASE.get(key, POPULAR[key])}"
    return best, reason


# Raison lisible : « 153e mot le plus courant » plutôt que « mot n° 153 ».
POPULAR_PHRASE = {"word": "mot le plus courant", "form": "forme la plus courante", "surname": "nom de famille le plus porté",
                  "firstname": "prénom le plus donné", "english": "mot anglais le plus courant", "sector": "secteur porteur",
                  "trend": "tendance", "combo": "combinaison", "niche": "site de niche", "expr": "expression courante",
                  "media": "nom de média", "startup": "mot tech et startups", "job": "métier le plus demandé",
                  "sport": "mot du sport", "animal": "animal", "food": "mot de cuisine", "premium": "terme premium",
                  "keyword": "mot-clé à forte valeur", "local": "métier + ville", "brand": "nom de marque inventé"}


def _nth(rank: int) -> str:
    return "1er" if rank == 1 else f"{rank:,}e".replace(",", "\u202f")


def base(label: str) -> int:
    """Points de forme et de sens, la meilleure extension (.fr ou .com) : sert à ranger et à borner."""
    return min(70, max(sum(measures.points(label, tld)) for tld in ("fr", "com")))


# Demande : un nom pris ailleurs ne compte que si c'est rare pour un nom de sa forme. 91 % des
# 4 lettres prononçables sont pris en .org : y être pris ne dit presque rien. rates donne, par
# extension, la part des noms de la même forme qui y sont pris (mesurée sur les zones lues).
DEMAND_POINTS = {"com": 10}
OTHER_DEMAND_POINTS = 4
COMMON_SIGNAL = 0.5  # en dessous de la moitié des points possibles : « courant pour cette forme »


def demand(taken: list[str], rates: dict[str, float] | None = None) -> tuple[int, str | None]:
    if not taken:
        return 0, None
    rates = rates or {}
    full = sum(DEMAND_POINTS.get(t, OTHER_DEMAND_POINTS) for t in taken)
    raw = sum(DEMAND_POINTS.get(t, OTHER_DEMAND_POINTS) * (1 - rates.get(t, 0.0)) for t in taken)
    reason = "pris en " + ", ".join(f".{t}" for t in taken)
    if rates and raw < COMMON_SIGNAL * full:
        reason += " (courant pour cette forme)"
    return min(15, round(raw)), reason


def history(created: str | None, removed: str | None, today: date, common: float = 0.0) -> tuple[int, str | None]:
    """Années de détention et suppression récente. common : part des .fr libres de la même forme qui ont
    déjà été tenus ; au-delà de la moitié (portefeuilles de spéculateurs vidés), le signal s'efface."""
    if not created or not removed:
        return 0, None
    years = (date.fromisoformat(removed) - date.fromisoformat(created)).days / 365.25
    points = min(10, int(years))
    reason = f"tenu {int(years)} an{'s' if int(years) > 1 else ''}" if years >= 1 else None
    if (today - date.fromisoformat(removed)).days < 730:
        points += 5
        reason = (reason + ", " if reason else "") + f"supprimé en {removed[:4]}"
    factor = min(1.0, 2 * (1 - common))
    if reason and factor < COMMON_SIGNAL:
        reason += " (courant pour cette forme)"
    return min(15, round(points * factor)), reason


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
CRITERIA = {"shape": "Forme et sonorité", "popularity": "Sens et marché", "demand": "Demande", "history": "Historique", "seo": "SEO"}
DEFAULT_WEIGHTS = dict.fromkeys(CRITERIA, 1.0)


def weights_from(raw: str | None) -> dict[str, float]:
    import json

    try:
        found = json.loads(raw) if raw else {}
    except ValueError:
        found = {}
    return {k: max(0.0, min(2.0, float(found.get(k, 1.0)))) for k in CRITERIA}


def intrinsic(label: str, tld: str = "fr", f: dict | None = None) -> tuple[int, int, list[str]]:
    """Forme et sens du nom pour une extension : (points de forme, points de sens, raisons).

    Points calés sur des avis IA détaillés (quality.py) ; raisons en mots simples : la forme (« 4 lettres,
    prononçable »), le rang dans sa liste, puis ce qui aide ou freine à l'oral."""
    f = f or measures.features(label, tld)
    form, meaning = measures.points(label, tld, f)
    reasons = [r for r in (shape(label)[1], popularity(label)[1]) if r] + measures.sound_reasons(label, tld, f)
    return form, meaning, reasons


def score(label: str, taken: list[str], fr_row=None, seo_row=None, today: date | None = None,
          weights: dict[str, float] | None = None, rates: dict[str, float] | None = None, common: float = 0.0,
          tld: str = "fr") -> dict:
    today = today or date.today()
    weights = weights or DEFAULT_WEIGHTS
    parts: dict[str, int] = {}
    form, meaning, reasons = intrinsic(label, tld)
    parts["shape"], parts["popularity"] = form, meaning
    points, reason = demand(taken, rates)
    parts["demand"] = points
    if reason:
        reasons.append(reason)
    parts["history"] = 0
    if fr_row is not None:
        points, reason = history(fr_row["created_on"], fr_row["removed_on"], today, common)
        parts["history"] = points
        if reason:
            reasons.append(reason)
    parts["seo"], extra = seo(seo_row)
    reasons += extra
    weighted = {k: round(v * weights[k]) for k, v in parts.items()}
    return {"score": min(100, sum(weighted.values())), "reasons": reasons, "parts": weighted}

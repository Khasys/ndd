"""Mesures intrinsèques d'un nom pour le score : sonorité, structure, test radio, longueur, sens.

Chaque mesure est simple et lisible ; leur poids dans le score vient d'un calage sur des avis IA
détaillés (grille v5) d'un échantillon de 5 000 noms de toutes les catégories (value.py).
"""

import math
from collections import Counter
from functools import lru_cache
from pathlib import Path

from . import pronounce, wordlists

DATA = Path(__file__).parent / "data"
VOWELS = frozenset("aeiouy")
# Graphies qui se disent de plusieurs façons, ou s'écrivent de plusieurs façons à l'oral.
AMBIGUOUS = ("ph", "gh", "ck", "th", "w", "x", "q", "k", "h")
LIST_KEYS = ("word", "form", "english", "firstname", "surname", "sector", "trend", "combo", "niche", "expr",
             "media", "startup", "job", "sport", "animal", "food")


@lru_cache(maxsize=1)
def _english_model() -> tuple[Counter, Counter]:
    """Trigrammes de lettres des mots anglais (mêmes conventions que le modèle français)."""
    tri, bi = Counter(), Counter()
    for word in wordlists.lists().get("english", ()):
        if word.isalpha():
            s = "^^" + word + "$"
            for i in range(len(s) - 2):
                tri[s[i:i + 3]] += 1
                bi[s[i:i + 2]] += 1
    return tri, bi


def english_log_prob(label: str) -> float | None:
    if not label.isalpha() or not label.isascii():
        return None
    tri, bi = _english_model()
    s = "^^" + label + "$"
    return sum(math.log((tri.get(s[i:i + 3], 0) + pronounce.SMOOTHING) / (bi.get(s[i:i + 2], 0) + pronounce.SMOOTHING
                                                                          * pronounce.ALPHABET))
               for i in range(len(s) - 2)) / (len(s) - 2)


def _words(name: str) -> frozenset[str]:
    path = DATA / name
    if not path.exists():
        return frozenset()
    return frozenset(line.strip() for line in path.read_text().splitlines() if line.strip() and not line.startswith("#"))


@lru_cache(maxsize=1)
def negatives() -> frozenset[str]:
    return _words("negatifs.txt")


@lru_cache(maxsize=1)
def acronyms() -> frozenset[str]:
    """Sigles et abréviations d'usage courant (tv, cv, rh, gps) : un sens sans être un mot."""
    return _words("sigles.txt")


@lru_cache(maxsize=1)
def brands() -> frozenset[str]:
    """Marques connues (la liste qui écarte les marques de anglais.txt) : revente risquée."""
    return _words("marques.txt")


def _likeness(log_prob: float | None) -> float:
    """Ressemblance à un vrai mot, de 0 (suite au hasard) à 1 (mot typique)."""
    if log_prob is None:
        return 0.0
    return max(0.0, min(1.0, (log_prob + 4.5) / 2.5))


def _runs(pattern: str, char: str) -> int:
    best = run = 0
    for c in pattern:
        run = run + 1 if c == char else 0
        best = max(best, run)
    return best


def _list_strength(label: str) -> dict[str, float]:
    """Pour chaque liste où le nom figure : 1 pour le premier rang, vers 0 pour le dernier (Mes ajouts : aucun
    rang, l'ordre d'ajout ne classe rien)."""
    found = {}
    for key, ranks in wordlists.ranks().items():
        rank = ranks.get(label)
        if rank is not None and key not in wordlists.NO_RANK:
            found[key] = 1 - math.sqrt((rank - 1) / len(ranks))
    return found


def features(label: str, tld: str) -> dict[str, float]:
    """Mesures d'un nom pour une extension ; toutes entre 0 et 1, sauf la longueur (en caractères)."""
    n = len(label)
    alpha = label.isalpha()
    digits = label.isdigit()
    f: dict[str, float] = {"len": float(n), "letters": float(alpha), "digits": float(digits),
                           "hyphen": float("-" in label), "mixed": float(not alpha and not digits)}
    for size, key in ((1, "len1"), (2, "len2"), (3, "len3"), (4, "len4")):
        f[key] = float(n == size)
    f["len5_6"] = float(5 <= n <= 6)
    f["len7_8"] = float(7 <= n <= 8)
    f["len9_12"] = float(9 <= n <= 12)
    f["len13"] = float(n >= 13)
    english_ext = tld != "fr"
    if alpha:
        fr, en = _likeness(pronounce.log_prob(label)), _likeness(english_log_prob(label))
        f["like_fr"], f["like_en"] = fr, en
        f["like_ext"] = en if english_ext else fr
        pattern = "".join("v" if c in VOWELS else "c" for c in label)
        f["alternation"] = sum(a != b for a, b in zip(pattern, pattern[1:])) / max(1, n - 1)
        f["cv_perfect"] = float(n >= 3 and f["alternation"] == 1)
        f["cons_run3"] = float(_runs(pattern, "c") >= 3)
        f["vow_run3"] = float(_runs(pattern, "v") >= 3)
        f["no_vowel"] = float("v" not in pattern)
        f["ends_vowel"] = float(pattern[-1] == "v")
        f["double"] = float(any(a == b for a, b in zip(label, label[1:])))
        f["ambiguous"] = min(1.0, sum(label.count(g) for g in AMBIGUOUS) / 2)
        f["acronym"] = float(label in acronyms())
    elif digits:
        # Motifs qui se disent et se retiennent, à partir de 3 chiffres (deux chiffres sont déjà un nombre) :
        # 100, 777, 123, 101, 360, 2015, 8899 ; un zéro devant (007) se lit mal, sauf un département en .fr (06).
        f["repeat"] = float(n >= 3 and len(set(label)) == 1)
        f["sequence"] = float(n >= 3 and all(int(b) - int(a) == 1 for a, b in zip(label, label[1:])))
        f["round"] = float(n >= 3 and label[0] != "0" and label[1:] == "0" * (n - 1))
        f["zero_lead"] = float(n >= 3 and label[0] == "0")
        f["palindrome"] = float(n >= 3 and label == label[::-1] and not f["repeat"])
        f["tens"] = float(n >= 3 and label[-1] == "0" and not f["round"])
        f["year"] = float(n == 4 and 1900 <= int(label) <= 2030)
        f["pairs"] = float(n == 4 and not f["repeat"] and (label[:2] == label[2:] or (label[0] == label[1] and label[2] == label[3])))
        f["dept"] = float(n == 2 and tld == "fr" and label != "00")
        # Jusqu'à 3 chiffres, le .com se vend (marché asiatique) ; au-delà, et ailleurs, bien moins.
        f["digits_com"] = float(n <= 3 and tld == "com")
    strength = _list_strength(label)
    # Listes sans avis IA de calage (termes stars, mots-clés, métier + ville, marques inventées) : leur rang
    # compte avec le poids de la liste calée la plus proche (wordlists.SCORE_AS).
    borrowed = {k: v for k, v in strength.items() if k not in wordlists.SCORE_AS}
    for key, like in wordlists.SCORE_AS.items():
        if key in strength:
            borrowed[like] = max(borrowed.get(like, 0.0), strength[key])
    for key in LIST_KEYS:
        f["in_" + key] = borrowed.get(key, 0.0)
    # Une marque inventée n'a pas de sens : seule sa sonorité et son rang comptent.
    f["meaning"] = max((v for k, v in strength.items() if k not in wordlists.NO_MEANING), default=0.0)
    f["real_short"] = float(n <= 4 and label in pronounce.real_words())
    f["negative"] = float(label in negatives())
    f["brand"] = float(label in brands())
    # Mot anglais en .fr, mot français ailleurs : moins lisible pour l'acheteur visé.
    langs = wordlists.languages()
    # Un prénom ou un nom de famille n'a pas de langue : lea.fr n'est pas « un mot anglais en .fr ».
    person = strength.get("firstname", 0.0) > 0 or strength.get("surname", 0.0) > 0
    f["lang_mismatch"] = float(not person and (
        (label in langs.get("en", ()) and label not in langs.get("fr", ()) and not english_ext)
        or (label in langs.get("fr", ()) and label not in langs.get("en", ()) and english_ext)))
    for ext in ("fr", "com", "io", "ai", "org"):
        f["tld_" + ext] = float(tld == ext)
    # Nature du mot : un nom commun se porte mieux qu'un verbe conjugué ou un adverbe.
    natures = wordlists.natures().get(label, ())
    for nature in ("noun", "verb", "adj", "adv"):
        f["nat_" + nature] = float(nature in natures)
    # Croisements : un mot qui a un sens vaut plus s'il est court ; un mot français en .fr, un mot
    # anglais ailleurs ; pour les 4 lettres, la structure compte plus qu'ailleurs.
    f["meaning_short"] = f["meaning"] * float(n <= 6)
    f["fr_word_in_fr"] = max(f["in_word"], f["in_form"]) * float(tld == "fr")
    f["en_word_abroad"] = f["in_english"] * float(english_ext)
    if alpha and n == 4:
        f["l4_like"] = f["like_ext"]
        f["l4_cv"] = f["cv_perfect"]
        f["l4_vowel_end"] = f["ends_vowel"]
    # L'ancien barème (forme et popularité) garde une information utile : il entre comme mesure.
    from . import value  # import local : value importe ce module

    f["legacy_shape"] = value.shape(label)[0] / 40
    f["legacy_pop"] = value.popularity(label)[0] / 30
    return f


# --- Modèle calé sur les avis IA ------------------------------------------------------------------
# Quatre familles, chacune son jeu de poids : les mêmes mesures n'ont pas le même rôle pour un mot,
# un nom de personne, une suite courte de lettres ou des chiffres. Les poids sont des points de note IA (0 à 100) par unité.
MEANING_KEYS = ("in_word", "in_english", "in_form", "in_sector", "in_trend", "in_expr", "in_media", "in_combo",
                "in_niche", "in_startup", "in_job", "in_sport", "in_animal", "in_food")
# Mesures de forme et de sonorité ; les autres (sens, langue, extension) font la part « sens et marché ».
FORM_KEYS = frozenset({"len", "letters", "digits", "hyphen", "mixed", "len1", "len2", "len3", "len4", "len5_6",
                       "len7_8", "len9_12", "len13", "like_fr", "like_en", "like_ext", "alternation", "cv_perfect",
                       "cons_run3", "vow_run3", "no_vowel", "ends_vowel", "double", "ambiguous", "repeat", "sequence",
                       "round", "zero_lead", "palindrome", "tens", "year", "pairs", "l4_like", "l4_cv", "l4_vowel_end", "legacy_shape"})
SCALE = 0.7  # note IA prévue (0 à 100) vers les points du score (0 à 70), le reste pour demande, historique, SEO


@lru_cache(maxsize=1)
def model() -> dict:
    import json

    return json.loads((DATA / "score_model.json").read_text())


FAMILIES = ("words", "names", "short", "digits")


def family(f: dict, label: str) -> str:
    """digits : chiffres seuls ; names : prénom ou nom de famille sans autre sens ; short : 1 ou 2 caractères,
    ou 4 au plus sans sens ; words : le reste. Presque toute suite de 2 lettres figure dans la liste anglaise
    (tv, jx) : ce « sens » ne dit rien, et les poids des mots ne valent pas pour une suite de 2 lettres."""
    if label.isdigit():
        return "digits"
    if len(label) <= 2:
        return "short"
    if (f.get("in_firstname", 0) > 0 or f.get("in_surname", 0) > 0) and max(f.get(k, 0) for k in MEANING_KEYS) == 0:
        return "names"
    if len(label) <= 4 and f.get("meaning", 0) == 0:
        return "short"
    return "words"


def predict(label: str, tld: str, f: dict | None = None) -> tuple[float, float, float]:
    """(note IA prévue, part forme, part sens), en points de note IA."""
    f = f or features(label, tld)
    fam = model()["families"][family(f, label)]
    form = fam["const"] + sum(w * f.get(k, 0.0) for k, w in fam["weights"].items() if k in FORM_KEYS)
    meaning = sum(w * f.get(k, 0.0) for k, w in fam["weights"].items() if k not in FORM_KEYS)
    return max(0.0, min(100.0, form + meaning)), form, meaning


def points(label: str, tld: str, f: dict | None = None) -> tuple[int, int]:
    """(points de forme, points de sens) du score, 70 au plus à eux deux."""
    total, _, meaning = predict(label, tld, f)
    total_pts = round(SCALE * total)
    meaning_pts = round(SCALE * max(0.0, min(total, meaning)))
    return total_pts - meaning_pts, meaning_pts


@lru_cache(maxsize=1)
def conjugated() -> frozenset[str]:
    """Formes fléchies qui ne sont que des verbes conjugués (vais, dits), pas des mots de base."""
    lists = wordlists.lists()
    natures = wordlists.natures()
    base = lists.get("word", frozenset())
    return frozenset(w for w in lists.get("form", ()) if w not in base and natures.get(w) == {"verb"})


def sound_reasons(label: str, tld: str, f: dict | None = None) -> list[str]:
    """Ce qui aide ou freine à l'oral, en mots simples (noms en lettres seulement)."""
    f = f or features(label, tld)
    found = ["forme conjuguée"] if label in conjugated() else []
    if not f.get("letters"):
        return found
    if f.get("cv_perfect") and len(label) <= 6:
        found.append("consonnes et voyelles alternées")
    if f.get("no_vowel"):
        found.append("sans voyelle")
    elif f.get("cons_run3"):
        found.append("trois consonnes de suite")
    if f.get("ambiguous", 0) >= 0.5 and len(label) <= 8:
        found.append("lettres ambiguës à l'oral")
    if f.get("negative"):
        found.append("image négative")
    if f.get("lang_mismatch"):
        found.append("mot anglais en .fr" if tld == "fr" else "mot français hors .fr")
    return found

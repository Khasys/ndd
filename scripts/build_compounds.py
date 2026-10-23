"""Construit les composés anglais et français (app/data/composes_en.txt, composes_fr.txt), du plus porteur au moins
porteur, COUNT noms au plus chacun, et les noms inventés (app/data/inventes.txt, INVENTED noms).

Usage : python scripts/build_compounds.py LEXIQUE_TXT   (puis python scripts/build_lists.py --provided)

Anglais : premier mot × second mot de scripts/lists/composes_en.txt (appris des ventes publiques de noms de deux mots
courants, 2015 à 2026), chaque paire notée par le rang de ses deux mots et sa longueur. Réglage mesuré à l'aveugle :
avec le vocabulaire des ventes jusqu'à 2025 seulement, la liste retrouve 18 % des composés anglais vendus en 2026
(3 % pour les listes d'avant). Français : scripts/lists/composes_fr.txt (jugement manuel, trop peu de ventes) ;
mon/ma, petit/petite… accordés au genre du nom (Lexique383, comme scripts/build_lists.py).

Inventés : racine évocatrice + finale de marque (scripts/lists/inventes.txt : lumora, vitaly), et noms tirés d'un
modèle de lettres appris des marques inventées déjà gardées (marques_inventees.txt) ; triés par leur sonorité en
français et en anglais (app/quality.py), au plus DIVERSITY par début de trois lettres.

Un nom déjà dans une autre liste, un mot seul (mots, formes, anglais, prénoms, noms), une marque connue ou un nom trop
long n'entre pas.
"""

import collections
import math
import random
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_lists as b  # noqa: E402  (lecture des listes à la main, Lexique, dossier de données)
from app import quality, wordlists  # noqa: E402

COUNT = 100_000
EN_LENGTH = (5, 15)
FR_LENGTH = (5, 20)
LENGTH_COST = {"en": 0.4, "fr": 0.25}  # au-delà, les noms courts l'emportent trop (mesuré sur les ventes 2026)
SERVICE_BONUS = 1.0  # location + voiture, cours + guitare : une intention d'achat, devant tête + mot quelconque
OFFSET = 3           # rang 0 : log(3), pour qu'un mot du haut ne pèse pas infiniment plus que le deuxième
BAD = re.compile(r"(.)\1\1|sex|porn|fuck|shit|nazi|cunt|whore|slut|penis|nigg|salope|merde|connard")
OWN = ("compound_en", "compound_fr", "invented")
INVENTED = 10_000
INVENTED_LENGTH = (5, 8)
DIVERSITY = (20, 500)  # au plus 20 noms par début de trois lettres, 500 par fin de deux lettres
SAMPLES = 400_000      # tirages du modèle de lettres (graine fixe : la liste se refait à l'identique)
BRAND_END = ("a", "o", "io", "ia", "ix", "ex", "on", "ly", "ra", "va", "na", "ro", "eo", "um", "us", "os", "ar", "el", "yx",
             "is", "ys", "ea")
# Finales de mots ordinaires : un nom qui finit ainsi sonne comme un mot raté plutôt que comme une marque.
WORD_END = ("ate", "ite", "ice", "ine", "age", "ere", "ive", "ise", "ure", "ent", "ant", "eur", "oir", "ism", "ness", "ing",
            "ed", "ery", "ary", "ory", "ty", "ble", "que", "ue", "le", "de", "te", "ne", "me", "se")
# Débuts de préfixes savants : tirés du modèle, ils donnent des mots ratés (comon, repon, dison).
WORD_START = ("com", "con", "re", "de", "dis", "sub", "pre", "par", "per", "pro", "sup", "in", "im")
INVENTED_BAD = re.compile(r"(.)\1\1|[^aeiouy]{3}|[aeiouy]{3}|q[^u]|sex|porn|fuck|nazi|cul|con$|pute|bite")


def _cost(*ranks: int) -> float:
    return sum(math.log(r + OFFSET) for r in ranks)


def english(taken: set[str]) -> list[str]:
    vocab = b.sections("composes_en.txt")
    heads, tails = vocab["tetes"], vocab["queues"]
    scored = []
    for i, a in enumerate(heads):
        for j, c in enumerate(tails):
            name = a + c
            if a != c and EN_LENGTH[0] <= len(name) <= EN_LENGTH[1] and name not in taken and not BAD.search(name):
                scored.append((_cost(i, j) + LENGTH_COST["en"] * len(name), name))
    return _best(scored)


def _forms(word: str) -> tuple[str, str]:
    m, _, f = word.partition("|")
    return m, f or m


# Groupes de mots du français : (source, nombre gardé). Les animaux sauvages et les plats rares valent peu en composé.
GROUPS = {"metiers": (lambda v: b.sections("metiers.txt")["fr"], None),
          "secteurs": (lambda v: b.read("secteurs_fr.txt"), None),
          "themes": (lambda v: b.read("themes_fr.txt"), None),
          "cuisine": (lambda v: b.sections("cuisine.txt")["fr"], 250),
          "sport": (lambda v: b.sections("sport.txt")["fr"], 160),
          "animaux": (lambda v: b.sections("animaux.txt")["fr"], 60),
          "biens": (lambda v: v["biens"], None)}
# Service + objet : (services, objets) ; None : les plats de la liste cuisine.
SERVICES = (("services-biens", "objets-biens"), ("services-activites", "objets-activites"), ("services-cuisine", None))


def french(taken: set[str], lexique: Path) -> list[str]:
    vocab = b.sections("composes_fr.txt")
    kinds = {}
    for _, word, cgram, genre, nombre in b.lexique_rows(lexique):
        if cgram == "NOM":
            kinds.setdefault(word, (genre if genre in ("m", "f") else "", nombre))

    def agree(modifier: str, word: str) -> str | None:
        """Forme du modificateur pour ce nom ; None si l'accord n'est pas sûr (genre inconnu, pluriel)."""
        genre, nombre = kinds.get(word, ("", ""))
        vowel = word[0] in "aeiouyh"
        if modifier == "mon":
            if nombre == "p":
                return "mes" if genre else None
            return "mon" if vowel or genre == "m" else ("ma" if genre == "f" else None)
        if modifier == "le":
            if vowel or not genre:
                return None
            return "les" if nombre == "p" else ("le" if genre == "m" else "la")
        m, f = _forms(modifier)
        if m == f:
            return m
        if not genre or nombre == "p":
            return None
        return m if genre == "m" else f

    scored = []

    def add(name: str, cost: float) -> None:
        if FR_LENGTH[0] <= len(name) <= FR_LENGTH[1] and name not in taken and not BAD.search(name) and b.valid(name):
            scored.append((cost + LENGTH_COST["fr"] * len(name), name))

    for group, (source, keep) in GROUPS.items():
        words = list(dict.fromkeys(w.replace("-", "") for w in source(vocab) if b.valid(w.replace("-", ""))))[:keep]
        for i, head in enumerate(vocab[f"avant-{group}"]):
            for j, word in enumerate(words):
                form = agree(head, word)
                if form and not word.startswith(form):
                    add(form + word, _cost(i, j))
        for i, tail in enumerate(vocab[f"apres-{group}"]):
            for j, word in enumerate(words):
                form = agree(tail, word)
                if form and not word.endswith(form):
                    add(word + form, _cost(j, i))
    for services, objects in SERVICES:
        targets = vocab[objects] if objects else b.sections("cuisine.txt")["fr"][:GROUPS["cuisine"][1]]
        for i, service in enumerate(vocab[services]):
            for j, target in enumerate(targets):
                target = target.replace("-", "")
                if service not in target and target not in service:
                    add(service + target, _cost(i, j) - SERVICE_BONUS)
    return _best(scored)


def invented(taken: set[str]) -> list[str]:
    vocab = b.sections("inventes.txt")
    roots, ends = list(dict.fromkeys(vocab["racines"])), list(dict.fromkeys(vocab["finales"]))
    found: dict[str, tuple[float, str]] = {}

    def add(name: str, prior: float, family: str) -> None:
        if (INVENTED_LENGTH[0] <= len(name) <= INVENTED_LENGTH[1] and name.isalpha() and name not in taken
                and not INVENTED_BAD.search(name) and prior > found.get(name, (-math.inf, ""))[0]):
            found[name] = (prior, family)

    # Racine + finale ; deux voyelles à la jonction se fondent (nova + a : nova, nova + ia : novia).
    for i, root in enumerate(roots):
        for j, end in enumerate(ends):
            name = root + (end[1:] if root[-1] in "aeiouy" and end[0] in "aeiouy" else end)
            add(name, -0.4 * math.log(i + OFFSET) - 0.3 * math.log(j + OFFSET), "root")
    # Modèle de lettres (ordre 3) appris des marques inventées gardées.
    model = collections.defaultdict(collections.Counter)
    for word in wordlists._ordered(wordlists.FILES["brand"]):
        if INVENTED_LENGTH[0] <= len(word) <= INVENTED_LENGTH[1]:
            chain = "^^" + word + "$"
            for k in range(2, len(chain)):
                model[chain[k - 2:k]][chain[k]] += 1
    rng = random.Random(7)
    for _ in range(SAMPLES):
        chain = "^^"
        while len(chain) < 12 and model[chain[-2:]]:
            nxt = model[chain[-2:]]
            char = rng.choices(list(nxt), weights=list(nxt.values()))[0]
            if char == "$":
                add(chain[2:], -1.0, "model")
                break
            chain += char

    def sound(name: str, prior: float, family: str) -> float:
        f = quality.features(name, "com")
        score = 2.4 * min(f["like_fr"], f["like_en"]) + 0.8 * f["alternation"] + 0.6 * f["cv_perfect"]
        score += 0.6 * name.endswith(BRAND_END) - 0.9 * name.endswith(WORD_END)
        score -= 1.5 * f["ambiguous"] + f["double"] + 2 * f["cons_run3"] + 2 * f["vow_run3"]
        score += {5: 0.6, 6: 0.5, 7: 0.2, 8: -0.2}[len(name)]
        if family == "model" and (not name.endswith(BRAND_END) or name.startswith(WORD_START)):
            score -= 3
        return score + prior + (1.5 if family == "root" else 0)

    ranked = sorted(((sound(n, p, fam), n) for n, (p, fam) in found.items()), key=lambda x: (-x[0], x[1]))
    starts, endings, out = collections.Counter(), collections.Counter(), []
    for _, name in ranked:
        if starts[name[:3]] >= DIVERSITY[0] or endings[name[-2:]] >= DIVERSITY[1]:
            continue
        starts[name[:3]] += 1
        endings[name[-2:]] += 1
        out.append(name)
        if len(out) == INVENTED:
            break
    return out


def _best(scored: list[tuple[float, str]]) -> list[str]:
    scored.sort()
    return list(dict.fromkeys(name for _, name in scored))[:COUNT]


def main() -> None:
    lexique = Path(sys.argv[1])
    others = set().union(*(wordlists._ordered(f) for key, f in wordlists.FILES.items() if key not in OWN))
    singles = set().union(*(set((b.DATA / f).read_text(encoding="utf-8").split())
                            for f in ("mots.txt", "formes.txt", "anglais.txt", "prenoms.txt", "noms.txt", "prenoms_us.txt",
                                      "noms_us.txt", "marques.txt")))
    taken = others | singles
    en = english(taken)
    fr = french(taken | set(en), lexique)
    names = invented(taken | set(en) | set(fr))
    b.write(wordlists.FILES["compound_en"], en)
    b.write(wordlists.FILES["compound_fr"], fr)
    b.write(wordlists.FILES["invented"], names)


if __name__ == "__main__":
    main()

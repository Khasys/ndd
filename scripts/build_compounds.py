"""Construit les composés anglais et français (app/data/composes_en.txt, composes_fr.txt), du plus porteur au moins
porteur, COUNT noms au plus chacun, et les noms inventés (app/data/inventes.txt, INVENTED noms).

Usage : python scripts/build_compounds.py LEXIQUE_TXT   (puis python scripts/build_lists.py --provided)

Anglais : premier mot × second mot de scripts/lists/composes_en.txt (appris des ventes publiques de noms de deux mots
courants, 2015 à 2026), chaque paire notée par le rang de ses deux mots et sa longueur. Réglage mesuré à l'aveugle :
avec le vocabulaire des ventes jusqu'à 2025 seulement, la liste retrouve 18 % des composés anglais vendus en 2026
(3 % pour les listes d'avant). Français : scripts/lists/composes_fr.txt (jugement manuel, trop peu de ventes) ;
mon/ma, petit/petite… accordés au genre du nom (Lexique383, comme scripts/build_lists.py).

Inventés : mot commercial courant + twist (scripts/lists/inventes.txt) : suffixe (cloudify, travelio), préfixe
(upmarket, ihealth) ou orthographe (healthr, kloud), comme la plupart des noms inventés qui se vendent ; triés par le
rang du mot et de l'ajout, la longueur et la lisibilité en anglais (app/quality.py), au plus PER_BASE par mot.

Un nom déjà dans une autre liste, un mot seul (mots, formes, anglais, prénoms, noms), une marque connue ou un nom trop
long n'entre pas.
"""

import collections
import math
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
INVENTED_MAX = INVENTED
INVENTED_LENGTH = (5, 10)
BASE_LENGTH = (3, 7)   # mot de base : court, pour que le nom tordu le reste
PER_BASE = 5           # au plus cinq noms par mot de base
SOUND_EVERY = 4        # un nom au beau son pour quatre « mot + twist »
# Mots-outils : un twist sur « the » ou « your » ne fait pas une marque.
BASE_STOP = frozenset("the you your our all for one two get new big top best free more just only this that with and"
                      .split())
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


def _respell(word: str) -> list[str]:
    """Déformations d'orthographe qui gardent le mot lisible : healthr (flickr), kloud, trakk, boostz, fone."""
    out = []
    if word.endswith("er") and len(word) > 4:
        out.append(word[:-2] + "r")
    if word.endswith("s") and len(word) > 4:
        out.append(word[:-1] + "z")
    if "ck" in word:
        out += [word.replace("ck", "k", 1), word.replace("ck", "q", 1)]
    if "ph" in word:
        out.append(word.replace("ph", "f", 1))
    for i, char in enumerate(word):
        if char == "c" and (i + 1 == len(word) or word[i + 1] not in "hiey"):
            out.append(word[:i] + "k" + word[i + 1:])
            break
    if word.endswith(("que", "c")):
        out.append(word[:-3] + "q" if word.endswith("que") else word[:-1] + "q")
    return [w for w in out if w != word]


def invented(taken: set[str]) -> list[str]:
    """Mot commercial courant + twist : suffixe (cloudify, travelio), préfixe (upmarket, ihealth) ou orthographe
    (healthr, kloud). Mots de base : ceux des composés anglais, les plus vendus d'abord ; un mot sert PER_BASE fois au
    plus, pour ne pas faire 50 variantes de cloud."""
    vocab, words = b.sections("inventes.txt"), b.sections("composes_en.txt")
    ranks: dict[str, int] = {}
    for section in ("tetes", "queues"):
        for i, word in enumerate(words[section]):
            # Un pluriel tordu se lit mal (heroesa) : mots au singulier seulement.
            plural = word.endswith("s") and not word.endswith("ss")
            if word.isalpha() and BASE_LENGTH[0] <= len(word) <= BASE_LENGTH[1] and word not in BASE_STOP and not plural:
                ranks[word] = min(ranks.get(word, i), i)
    suffixes, prefixes = list(dict.fromkeys(vocab["suffixes"])), list(dict.fromkeys(vocab["prefixes"]))
    found: dict[str, tuple[float, str]] = {}

    def add(name: str, base: str, cost: float) -> None:
        if (INVENTED_LENGTH[0] <= len(name) <= INVENTED_LENGTH[1] and name not in taken and not INVENTED_BAD.search(name)
                and cost < found.get(name, (math.inf, ""))[0]):
            found[name] = (cost, base)

    for base, r in ranks.items():
        weight = 0.8 * math.log(r + OFFSET)
        for j, suffix in enumerate(suffixes):
            # La voyelle finale du mot et celle du suffixe se fondent : data + a ne fait pas dataa.
            name = base[:-1] + suffix if len(base) > 3 and base[-1] in "aeiou" and suffix[0] in "aeiouy" else base + suffix
            # Pas de lettre doublée à la jonction (superr), ni de mot presque existant (better tordu en beter).
            if base[-1] != suffix[0] and not (suffix in ("r", "er") and base.endswith("t")):
                add(name, base, weight + 0.6 * math.log(j + OFFSET))
        for j, prefix in enumerate(prefixes):
            if prefix[-1] != base[0] and not base.startswith(prefix):  # rered, ppay
                add(prefix + base, base, weight + 0.6 * math.log(j + OFFSET) + 0.3)
        for name in _respell(base):
            add(name, base, weight + 0.6 * math.log(3 + OFFSET) + 0.4)

    def cost(name: str, value: float) -> float:
        f = quality.features(name, "com")
        return value + LENGTH_COST["en"] * len(name) - 1.5 * f["like_en"] + 2 * f["cons_run3"] + 2 * f["vow_run3"]

    ranked = sorted((cost(n, v), n, base) for n, (v, base) in found.items())
    per_base, twists = collections.Counter(), []
    for _, name, base in ranked:
        if per_base[base] >= PER_BASE:
            continue
        per_base[base] += 1
        twists.append(name)
    # Noms au beau son gardés : un pour SOUND_EVERY « mot + twist ».
    sounds = [n for n in vocab.get("sonores", []) if n not in taken and n not in found]
    out = []
    while twists or sounds:
        out += twists[:SOUND_EVERY]
        del twists[:SOUND_EVERY]
        out += sounds[:1]
        del sounds[:1]
    return list(dict.fromkeys(out))[:INVENTED]


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

"""Construit les listes anglais, secteurs, tendances, combinaisons, sites de niche, expressions,
noms de média et listes thématiques (métiers, sport, animaux, cuisine) dans app/data, et les sites de niche et noms
de médias anglais des États-Unis (niches_us.txt, medias_us.txt).

Usage : python scripts/build_lists.py WORDFREQ_DIR LEXIQUE_TXT
        python scripts/build_lists.py --themes   (listes thématiques et langues.txt seulement, sans Lexique)
        python scripts/build_lists.py --provided (langue des listes fournies dans langues.txt, sans Lexique)
        python scripts/build_lists.py --us       (sites de niche et noms de médias anglais, puis --provided)

Listes fournies (termes stars, mots-clés à forte valeur, métier + ville de France et des États-Unis, marques
inventées : premium.txt, motscles.txt, local.txt, local_us.txt, marques_inventees.txt) : rédigées ailleurs (local_us.txt
par scripts/build_local_us.py), jamais écrites ici. Le script en vérifie le format et en relève la langue
(langues.txt) ; une marque inventée n'en a pas.

Chaque usage finit par les catégories dérivées (categories.txt, voir « Catégories dérivées » plus bas).

WORDFREQ_DIR : dossier du paquet wordfreq 3.1.1 décompressé (contient data/large_en.msgpack.gz).
LEXIQUE_TXT : Lexique383.txt (paquet pylexique 1.5.1), encodé en Latin-1. Il faut aussi le module msgpack.
Les listes choisies à la main sont dans scripts/lists ; sources et licences : app/data/SOURCES.md.
"""

import gzip
import itertools
import math
import sys
import unicodedata
from functools import lru_cache
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from app import wordlists  # noqa: E402  listes et catégories (clés, fichiers, règles des catégories dérivées)
from app.pronounce import pronounceable  # noqa: E402  filtre des noms « fr + mot tech »

LISTS = ROOT / "scripts" / "lists"
DATA = ROOT / "app" / "data"
ENGLISH_SIZE = 30_000
# Langue de chaque nom des listes mêlées (secteurs, tendances, combinaisons, sites de niche).
LANG: dict[str, set[str]] = {"fr": set(), "en": set()}
TOP_NOUNS = 1_500


def fold(text: str) -> str:
    text = text.strip().lower().replace("œ", "oe").replace("æ", "ae")
    text = unicodedata.normalize("NFKD", text)
    return "".join(c for c in text if not unicodedata.combining(c))


def valid(label: str) -> bool:
    return (2 <= len(label) <= 63 and all(c.isascii() and (c.isalnum() or c == "-") for c in label)
            and not label.startswith("-") and not label.endswith("-") and "--" not in label)


def read(name: str, folder: Path = LISTS) -> list[str]:
    out = []
    for line in (folder / name).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            out.append(line if line.startswith("[") else fold(line))
    return out


def sections(name: str) -> dict[str, list[str]]:
    found, current = {}, None
    for line in read(name):
        if line.startswith("["):
            current = found.setdefault(line.strip("[]"), [])
        else:
            current.append(line)
    return found


def unique(words) -> list[str]:
    seen, out = set(), []
    for w in words:
        if w not in seen and valid(w):
            seen.add(w)
            out.append(w)
    return out


def english(wordfreq_dir: Path, banned: set[str]) -> list[str]:
    import msgpack  # import local : seule la construction complète en a besoin (pas --provided, pas les tests)

    raw = msgpack.unpackb(gzip.open(wordfreq_dir / "data" / "large_en.msgpack.gz").read(), raw=False)
    words = (w for bucket in raw[1:] for w in bucket)
    picked = []
    sigles = set(read("sigles.txt", DATA))
    for w in words:
        # Sans voyelle : une abréviation (qld, nsw, tbh), pas un mot ; gardée seulement si c'est un sigle connu (pdf).
        if not any(c in "aeiouy" for c in w) and w not in sigles:
            continue
        if w.isascii() and w.isalpha() and len(w) >= 2 and w not in banned:
            picked.append(w)
            if len(picked) >= ENGLISH_SIZE:
                break
    return picked


def lexique_rows(lexique: Path) -> list[tuple]:
    """Noms et adjectifs du Lexique : (fréquence, mot sans accent, catégorie, genre, nombre), du plus fréquent.
    Le fichier est en Latin-1 : lu en UTF-8, chaque mot accentué serait perdu."""
    rows = []
    with lexique.open(encoding="latin-1") as f:
        header = f.readline().rstrip("\n").split("\t")
        col = {name: i for i, name in enumerate(header)}
        for line in f:
            cells = line.rstrip("\n").split("\t")
            cgram = cells[col["4_cgram"]]
            word = fold(cells[col["1_ortho"]])
            if cgram not in ("NOM", "ADJ") or not word.isalpha() or not word.isascii() or not 3 <= len(word) <= 12:
                continue
            freq = float(cells[col["9_freqfilms2"]].replace(",", ".")) + float(cells[col["10_freqlivres"]].replace(",", "."))
            rows.append((freq, word, cgram, cells[col["5_genre"]], cells[col["6_nombre"]]))
    rows.sort(reverse=True)
    return rows


def nouns(lexique: Path) -> dict[str, str]:
    """Genre des noms communs (m ou f), du plus fréquent au moins fréquent."""
    rows = []
    with lexique.open(encoding="latin-1") as f:
        header = f.readline().rstrip("\n").split("\t")
        col = {name: i for i, name in enumerate(header)}
        for line in f:
            cells = line.rstrip("\n").split("\t")
            if cells[col["4_cgram"]] != "NOM" or cells[col["14_islem"]] != "1":
                continue
            genre = cells[col["5_genre"]]
            word = fold(cells[col["1_ortho"]])
            if genre not in ("m", "f") or not word.isalpha() or not 3 <= len(word) <= 12:
                continue
            freq = float(cells[col["7_freqlemfilms2"]].replace(",", ".")) + float(cells[col["8_freqlemlivres"]].replace(",", "."))
            rows.append((freq, word, genre))
    rows.sort(reverse=True)
    found = {}
    for _, word, genre in rows:
        found.setdefault(word, genre)
    return found


def ranked(bases: list[str], mods: list, make) -> list[str]:
    """Combinaisons rangées par produit des rangs : les meilleures bases avec les meilleurs ajouts."""
    pairs = sorted(((b + 1) * (m + 1), b, m) for b in range(len(bases)) for m in range(len(mods)))
    return [x for _, b, m in pairs for x in make(bases[b], mods[m])]


def combos(sectors_fr, sectors_en, trends, english_words, genders) -> list[str]:
    mods = sections("modifiers.txt")
    english_set = set(english_words) | set(sectors_en)

    # Noms français avec « mon / ma » et « le / la » selon le genre ; voyelle : « mon », pas de « l' ».
    fr_nouns = unique([w for w in sectors_fr if w in genders] + list(genders)[:TOP_NOUNS])

    def possessive(noun, kind):
        vowel = noun[0] in "aeiouy"
        if kind == "mon":
            return ["mon" + noun] if vowel or genders[noun] == "m" else ["ma" + noun]
        return [] if vowel else [("le" if genders[noun] == "m" else "la") + noun]

    families = [
        ranked(fr_nouns, ["mon", "le"], possessive),
        ranked(sectors_fr, mods["fr-avant"], lambda b, m: [m + b]),
        ranked(sectors_fr, mods["fr-apres"], lambda b, m: [b + m]),
        ranked(read("metiers_fr.txt"), read("villes_fr.txt"), lambda b, m: [b + m.replace("-", ""), f"{b}-{m}"]),
    ]
    for family in families:
        LANG["fr"].update(family)
    en_bases = unique(sectors_en + [t for t in trends if t in english_set])
    english_families = [
        ranked(en_bases, mods["en-before"], lambda b, m: [m + b]),
        ranked(en_bases, mods["en-after"], lambda b, m: [b + m]),
        ranked(read("trades_en.txt"), read("cities_us.txt"), lambda b, m: [b + m, m + b]),
    ]
    for family in english_families:
        LANG["en"].update(family)
    families += english_families
    # Les familles se partagent les premiers rangs : une à une, chacune dans son ordre.
    out = []
    for i in range(max(map(len, families))):
        out += [f[i] for f in families if i < len(f)]
    return out


def niche() -> list[str]:
    """Format de site + thème de passion en français : forumdomotique, guidepotager, jardinagefacile (l'anglais :
    niche_en)."""
    formats = sections("formats.txt")
    fr = unique(read("themes_fr.txt"))
    families = [
        ranked(fr, formats["fr"], lambda b, m: [m + b]),
        ranked(fr, formats["fr-apres"], lambda b, m: [b + m]),
    ]
    LANG["fr"].update(families[0] + families[1])
    out = []
    for i in range(max(map(len, families))):
        out += [f[i] for f in families if i < len(f)]
    return out


# --- Listes anglaises des États-Unis (sites de niche, noms de médias) -------------------------------------------
# Construites sans Lexique ni wordfreq (--us), après toutes les autres : un nom déjà dans une autre liste n'y entre
# pas. Autant de noms que la liste française du même genre (niches.txt, medias.txt), les meilleurs d'abord.
# Métier + ville des États-Unis : liste fournie (local_us.txt, scripts/build_local_us.py), pas construite ici.
US_BUILT = ("niche_us", "media_us")


def tagged(lines: list[str]) -> list[tuple[str, str, frozenset[str]]]:
    """Lignes « thème[/forme] : genres » ou « format : genres » : (thème ou format, forme devant un nom, genres)."""
    out = []
    for line in lines:
        word, _, kinds = line.partition(":")
        name, _, form = word.strip().partition("/")
        out.append((name, form or name, frozenset(kinds.split())))
    return out


def affix(line: str) -> str:
    """Format ou tête de média d'une ligne à genres, sans ses genres ni son tiret (allabout- : allabout)."""
    return line.partition(":")[0].strip().strip("-")


def compose(theme: tuple, fmt: tuple) -> list[str]:
    """Thème + format anglais, si le format va au thème : la forme devant un nom avant un format (dog + forum :
    dogforum, jamais dogsforum ni chickenszone), le thème tel qu'on le nomme après un format qui finit par un tiret
    (allabout- + dogs : allaboutdogs). Pas de mot répété (vanlife + life) ni de lettre trois fois (chess + shop)."""
    (topic, form, kinds), (word, _, fits) = theme, fmt
    if not (fits & kinds or ("all" in fits and "-all" not in kinds)):
        return []
    if word.endswith("-"):
        head = word[:-1]
        name = "" if topic.startswith(head) else head + topic
    else:
        name = "" if form.endswith(word) else form + word
    if not name or any(a == b == c for a, b, c in zip(name, name[1:], name[2:])):
        return []
    return [name]


def famous() -> tuple[set[str], list[str]]:
    """Noms connus écartés des listes anglaises (scripts/lists/connus_en.txt, marques de app/data/marques.txt) et
    marques de 5 lettres et plus, écartées aussi au début ou à la fin d'un nom (teslaforum ; pas au milieu :
    educationnews n'a rien de ducati)."""
    brands = read("marques.txt", DATA)
    return set(read("connus_en.txt")) | set(brands), [b for b in brands if len(b) >= 5]


def english_names(families: list[list[str]], taken: set[str], size: int) -> list[str]:
    """Familles mêlées une à une, chacune dans son ordre ; sans doublon, nom connu ni nom déjà pris ; size au plus."""
    banned, inside = famous()
    out = []
    for i in range(max(map(len, families))):
        out += [f[i] for f in families if i < len(f)]
    found = [n for n in unique(out) if n not in taken and n not in banned
             and not any(n.startswith(b) or n.endswith(b) for b in inside)]
    return found[:size]


def niche_en(taken: set[str], size: int) -> list[str]:
    """Sites de niche anglais : thème + format qui lui va (gardeningforum, dogtraining, sailinglessons, easybaking),
    rangés par produit des rangs du thème et du format."""
    themes = tagged(read("themes_en.txt"))
    formats = tagged(sections("formats.txt")["en"])
    return english_names([ranked(themes, formats, compose)], taken, size)


def medias_en(taken: set[str], size: int) -> list[str]:
    """Noms de médias anglais : thème + tête de média (golfdaily, healthinsider, worldofgolf) et the + mot
    (thegardener, theworkshop), en alternance."""
    src = sections("medias_en.txt")
    heads = ranked(tagged(src["themes"]), tagged(src["tetes"]), compose)
    return english_names([heads, ["the" + w for w in src["the"]]], taken, size)


def us_lists() -> dict[str, list[str]]:
    """Sites de niche et noms de médias anglais (niches_us.txt, medias_us.txt), d'après les listes de app/data :
    hors de toute autre liste, autant que leur liste française. Langue : anglais."""
    others = {key: wordlists._ordered(file) for key, file in wordlists.FILES.items() if key not in US_BUILT}
    taken = set().union(*map(set, others.values()))
    niches = niche_en(taken, len(others["niche"]))
    medias = medias_en(taken | set(niches), len(others["media"]))
    LANG["en"].update(niches + medias)
    write(wordlists.FILES["niche_us"], niches)
    write(wordlists.FILES["media_us"], medias)
    return {"niche_us": niches, "media_us": medias}


ARTICLE_TOP = 600    # noms par genre (et pluriels) pour « le / la / l' / les + mot »
MEDIA_HEADS = ["journal", "planete", "monde", "univers", "gazette"]
FEMININE = {"peche"}  # genre du Lexique trompeur pour ces thèmes


def medias(lexique: Path, genders: dict[str, str], banned: set[str]) -> list[str]:
    """Noms de média ou de marque : lemonde, lequipe, lesnumeriques, frandroid, journaldelinformatique."""
    rows = lexique_rows(lexique)

    def top(keep, n):
        out, seen = [], set()
        for _, word, cgram, genre, number in rows:
            if keep(cgram, genre, number) and word not in seen:
                seen.add(word)
                out.append(word)
                if len(out) >= n:
                    break
        return out

    # Mot qui commence par h : élision incertaine (l'homme, la honte), on l'écarte plutôt que de se tromper.
    def article(word, base):
        return None if word[0] == "h" else ("l" + word if word[0] in "aeiouy" else base + word)

    # Noms seulement ; pluriels de 5 lettres au moins (ni lesans ni lesfois) ; mots négatifs écartés.
    negative = set(read("negatifs.txt"))
    singular_m = [w for w in top(lambda c, g, n: c == "NOM" and g == "m" and n == "s", ARTICLE_TOP) if w not in negative]
    singular_f = [w for w in top(lambda c, g, n: c == "NOM" and g == "f" and n == "s", ARTICLE_TOP) if w not in negative]
    plural = [w for w in top(lambda c, g, n: c == "NOM" and n == "p", ARTICLE_TOP) if len(w) >= 5 and w not in negative]
    articles = [x for pair in itertools.zip_longest(
        [article(w, "le") for w in singular_m], [article(w, "la") for w in singular_f], ["les" + w for w in plural])
        for x in pair if x]
    # « les » + thème porteur au pluriel, si ce pluriel existe dans le Lexique (lesnumeriques, pas lesdomotiques).
    plurals = {w for _, w, _, _, n in rows if n == "p"}
    # Pluriel employé pour de vrai (fréquence d'au moins 1 par million) : lesrecettes oui, lessantes non.
    common = {w for f, w, _, _, n in rows if n == "p" and f >= 1}
    themes = [t for t in unique(read("themes_fr.txt") + read("secteurs_fr.txt")) if t not in negative]
    les_themes = ["les" + (t if t in common else t + "s") for t in themes if t in common or t + "s" in common]
    # fr / french + mot tech, seulement s'il se prononce (frandroid oui, frweb non).
    tech = sections("tech_en.txt")
    fr_only = set(tech["fr"])
    fr_tech = [p + w for w in unique(tech["french"] + tech["fr"]) for p in ("fr", "french")
               if (p == "french" or w in fr_only) and pronounceable(p + w)]
    # Tête de média + du / de la / de l' / des + thème : journaldelinformatique, planetedujardinage.
    # Le y initial ne s'élide pas (du yoga) ; un pluriel inconnu du Lexique finit par s (des ados) ; pêche, féminin
    # pour le sens qui compte ici, est aussi « le pêcher » au masculin dans le Lexique.
    def linked(head, theme):
        if (theme in plurals or theme.endswith("s")) and theme not in genders:
            return head + "des" + theme
        if theme[0] in "aeiou":
            return head + "del" + theme
        gender = "f" if theme in FEMININE else genders.get(theme)
        return head + ("du" if gender == "m" else "dela") + theme if gender else None
    heads = [x for t in read("themes_fr.txt") for h in MEDIA_HEADS if (x := linked(h, t))]
    families = [articles, les_themes, fr_tech, heads]
    out = []
    for i in range(max(map(len, families))):
        out += [f[i] for f in families if i < len(f)]
    found = [x for x in unique(out) if x not in banned]
    LANG["fr"].update(found)
    return found


# Listes thématiques rédigées à la main : fichier source -> fichier de app/data.
THEMES = {"metiers.txt": "metiers.txt", "sport.txt": "sport.txt", "animaux.txt": "animaux.txt",
          "cuisine.txt": "cuisine.txt"}


def themes(english_words: set[str]) -> set[str]:
    """Écrit les listes thématiques : le français d'abord, puis les équivalents anglais absents de la liste
    anglaise (un nom déjà dans anglais.txt y garde son rang). Renvoie l'ensemble des noms écrits."""
    found = set()
    for source, target in THEMES.items():
        parts = sections(source)
        fr = unique(parts.get("fr", []))
        en = [w for w in unique(parts.get("en", [])) if w not in english_words and w not in fr]
        LANG["fr"].update(fr)
        LANG["en"].update(en)
        write(target, fr + en)
        found |= set(fr + en)
    return found


def write(name: str, words: list[str]) -> None:
    (DATA / name).write_text("\n".join(words) + "\n", encoding="utf-8")
    print(f"{name}: {len(words)}")


def main() -> None:
    wordfreq_dir, lexique = Path(sys.argv[1]), Path(sys.argv[2])
    banned = set(read("marques.txt", DATA))  # la liste sert aussi au score (quality.brands)
    english_words = unique(english(wordfreq_dir, banned))
    sectors_fr, sectors_en = unique(read("secteurs_fr.txt")), unique(read("sectors_en.txt"))
    # Secteurs : français et anglais en alternance, chacun dans son ordre.
    sectors = unique(x for pair in zip(sectors_fr, sectors_en) for x in pair)
    sectors = unique(sectors + sectors_fr + sectors_en)
    trends = unique(read("tendances.txt"))
    existing = set()
    for name in ("mots.txt", "formes.txt", "noms.txt", "prenoms.txt"):
        existing |= set((DATA / name).read_text(encoding="utf-8").split())
    taken = existing | set(english_words) | set(sectors) | set(trends)
    niches = [n for n in unique(niche()) if n not in taken and n not in banned]
    taken |= set(niches)
    combined = [c for c in unique(combos(sectors_fr, sectors_en, trends, english_words, nouns(lexique)))
                if c not in taken and c not in banned]
    write("anglais.txt", english_words)
    write("secteurs.txt", sectors)
    write("tendances.txt", trends)
    write("combinaisons.txt", combined)
    write("niches.txt", niches)
    # Expressions courantes, rédigées à la main : français et anglais en alternance.
    expr_fr, expr_en = unique(read("expressions_fr.txt")), unique(read("expressions_en.txt"))
    LANG["fr"].update(expr_fr)
    LANG["en"].update(expr_en)
    taken |= set(combined)
    expressions = [e for e in unique(x for pair in itertools.zip_longest(expr_fr, expr_en) for x in pair if x)
                   if e not in taken and e not in banned]
    write("expressions.txt", expressions)
    taken |= set(expressions)
    media = [m for m in medias(lexique, nouns(lexique), banned) if m not in taken]
    write("medias.txt", media)
    # Tendances : anglais si le mot est dans la liste anglaise, français s'il est dans le Lexique.
    french = existing | set(nouns(lexique))
    LANG["fr"].update(sectors_fr + [t for t in trends if t in french])
    LANG["en"].update(sectors_en + [t for t in trends if t in set(english_words)])
    # Jargon des tendances absent des deux listes (fintech, padel, web3…) : employé dans les deux langues.
    for t in trends:
        if t not in LANG["fr"] and t not in LANG["en"]:
            LANG["fr"].add(t)
            LANG["en"].add(t)
    mixed = set(sectors) | set(trends) | set(combined) | set(niches) | set(expressions) | set(media)
    mixed |= themes(set(english_words))
    # Listes anglaises des États-Unis : après toutes les autres (us_lists relit celles écrites ci-dessus).
    mixed |= {name for names in us_lists().values() for name in names}
    # Listes fournies : jamais réécrites, leur langue relevée comme celle des autres.
    known = {"fr": existing | LANG["fr"], "en": set(english_words) | LANG["en"]}
    for file, with_lang in PROVIDED.items():
        entries = check_provided(file)
        if with_lang:
            for name, langs in provided_langs(entries, known, vocabularies(file), file).items():
                for lang in langs:
                    LANG[lang].add(name)
                mixed.add(name)
    lines = []
    for name in sorted(mixed):
        langs = [k for k in ("fr", "en") if name in LANG[k]]
        if langs:
            lines.append(f"{name}\t{','.join(langs)}")
    write("langues.txt", lines)


def themes_only() -> None:
    """Sans wordfreq ni Lexique : réécrit les listes thématiques et complète langues.txt avec leurs noms."""
    english_words = set((DATA / "anglais.txt").read_text(encoding="utf-8").split())
    found = themes(english_words)
    lines = {}
    for line in (DATA / "langues.txt").read_text(encoding="utf-8").splitlines():
        name, _, langs = line.partition("\t")
        if name and name not in found:
            lines[name] = langs
    for name in found:
        langs = [k for k in ("fr", "en") if name in LANG[k]]
        if langs:
            lines[name] = ",".join(langs)
    write("langues.txt", [f"{name}\t{langs}" for name, langs in sorted(lines.items())])


# Listes fournies : fichier de app/data -> langue à relever (une marque inventée n'en a pas).
PROVIDED = {"premium.txt": True, "motscles.txt": True, "local.txt": True, "local_us.txt": True,
            "marques_inventees.txt": False}
PIECE = 3  # une partie de nom composé fait 3 lettres au moins, hors mots-outils (« de », « my » collent à tout)
# Découpe pondérée : chaque morceau coûte 1 + log10(rang + 10) dans sa langue (un mot courant coûte peu), plus pour un
# morceau court ou une forme fléchie retrouvée par sa terminaison. Seuls les mots-outils passent sous PIECE lettres ;
# « d' » et « l' » seulement devant une voyelle (decorationdinterieur).
TOOLS = {"fr": {"de", "du", "la", "le", "les", "des", "un", "en", "au", "aux", "et", "ma", "mon", "mes", "sur", "pour", "chez"},
         "en": {"my", "of", "to", "in", "on", "go", "the", "and", "for", "get"}}
ELISION = {"d", "l"}
TOOL_COST = 2.0  # celui de « de », premier mot du Lexique
LOCAL_RANK = 400  # villes, régions, métiers et services des listes à la main : propres à leur langue
SHORT = {3: 1.5, 4: 0.5}  # morceaux courts : ils découpent n'importe quoi (club + can + in)
VARIANT = 0.5
ENDINGS = {"fr": (("s", ""), ("x", ""), ("e", ""), ("es", ""), ("ee", "e"), ("ees", "e"), ("ive", "if"), ("ives", "if"),
                  ("euse", "eur"), ("euses", "eur"), ("elle", "el"), ("elles", "el"), ("ienne", "ien"), ("iennes", "ien")),
           "en": (("s", ""), ("ses", "s"), ("xes", "x"), ("ches", "ch"), ("shes", "sh"), ("ies", "y"))}
# Mot anglais plus rare que ce rang : souvent un emprunt (creme, kwh) ; il sert à découper, pas à dire la langue.
RARE_EN = 15_000
PARTS = 4


def rank_cost(rank: int) -> float:
    return 1 + math.log10(rank + 10)


def vocabularies(file: str = "") -> dict:
    """Coût des mots par langue, pour découper un nom composé de la liste fournie file : Lexique (mots, formes à rang
    doublé), mots anglais, villes, métiers et services des listes à la main ; "weak" : les mots anglais rares. Villes
    et métiers des États-Unis de metier_ville_us.txt seulement pour local_us.txt."""
    own = {"fr": {}, "en": {}}

    def put(lang: str, word: str, cost: float) -> None:
        word = word.replace("-", "")
        if len(word) >= PIECE and cost < own[lang].get(word, math.inf):
            own[lang][word] = cost

    def ranked_words(name: str) -> list[str]:
        return (DATA / name).read_text(encoding="utf-8").split()

    for i, w in enumerate(ranked_words("mots.txt")):
        put("fr", w, rank_cost(i))
    for i, w in enumerate(ranked_words("formes.txt")):
        put("fr", w, rank_cost(2 * i))
    english_words = ranked_words("anglais.txt")
    for i, w in enumerate(english_words):
        put("en", w, rank_cost(i))
    local = {w for words in sections("metier_ville.txt").values() for w in words}
    for w in set(read("villes_fr.txt")) | set(read("metiers_fr.txt")) | local:
        put("fr", w, rank_cost(LOCAL_RANK))
    local_en = set(read("cities_us.txt")) | set(read("trades_en.txt"))
    if file == wordlists.FILES["local_us"]:
        us = sections("metier_ville_us.txt")
        local_en |= set(us["villes"] + us["metiers"])
    for w in local_en:
        put("en", w, rank_cost(LOCAL_RANK))
    return {**own, "weak": set(english_words[RARE_EN:]) - local_en}


def piece_cost(piece: str, vocab: dict) -> dict[str, float]:
    """{langue: coût} d'un morceau, formes fléchies comprises ; un mot-outil n'est que de sa langue."""
    for lang, words in TOOLS.items():
        if piece in words:
            return {lang: TOOL_COST}
    if piece in ELISION:
        return {"fr": TOOL_COST}
    found = {}
    for lang in ("fr", "en"):
        cost = vocab[lang].get(piece) if len(piece) >= PIECE else None
        word = piece
        for end, base in ENDINGS[lang] if len(piece) >= PIECE else ():
            root = piece[:len(piece) - len(end)] + base
            if piece.endswith(end) and len(root) >= PIECE and root in vocab[lang] and vocab[lang][root] + VARIANT < (cost or math.inf):
                cost, word = vocab[lang][root] + VARIANT, root
        if cost is not None:
            found[lang] = cost + SHORT.get(len(piece), 0)
            if lang == "en" and word in vocab["weak"]:
                found["weak"] = 0.0  # marque : mot anglais rare (forme fléchie comprise)
    return found


def split_langs(name: str, vocab: dict) -> list[str]:
    """Langue d'un nom composé : découpe la moins chère (PARTS morceaux au plus) des mots des deux langues, puis langue
    des morceaux propres à une seule (plombier + paris : français ; car + insurance : anglais ; meilleur + broker :
    les deux) ; que des mots communs aux deux (education + positive) : les deux ; ne se découpe pas : aucune.
    Deux mots-outils de langues différentes ne se suivent pas (au + to n'est pas auto)."""
    @lru_cache(maxsize=None)
    def best(i: int, left: int, tool: str):
        if i == len(name):
            return 0.0, ()
        if not left:
            return None
        found = None
        for j in range(i + 1, len(name) + 1):
            piece = name[i:j]
            if piece in ELISION and (j == len(name) or name[j] not in "aeiouyh"):
                continue
            costs = piece_cost(piece, vocab)
            weak = costs.pop("weak", None) is not None
            if not costs:
                continue
            here = next((k for k, words in TOOLS.items() if piece in words), "")
            if here and tool and here != tool:
                continue
            rest = best(j, left - 1, here)
            if rest is None:
                continue
            langs = frozenset(k for k in costs if not (k == "en" and weak))
            total = min(costs.values()) + rest[0]
            if found is None or total < found[0]:
                found = (total, (langs,) + rest[1])
        return found

    found = best(0, PARTS, "")
    if found is None:
        return []
    marks = set().union(*(langs for langs in found[1] if len(langs) == 1))
    return [k for k in ("fr", "en") if k in (marks or {"fr", "en"})]


def provided_langs(names: list[str], known: dict[str, set[str]], vocab: dict, file: str = "") -> dict[str, list[str]]:
    """Langue des noms d'une liste fournie qui n'en ont pas encore (Lexique, anglais, autres listes) : celle des mots
    qui les composent (split_langs) ; ne se découpe pas : sans langue (absent du résultat). Métier + ville des
    États-Unis (local_us.txt) : anglais, par construction."""
    found = {}
    for name in names:
        if any(name in known[k] for k in ("fr", "en")):
            continue  # langue déjà connue de l'appli
        langs = ["en"] if file == wordlists.FILES["local_us"] else split_langs(name.replace("-", ""), vocab)
        if langs:
            found[name] = langs
    return found


def check_provided(name: str) -> list[str]:
    """Entrées d'une liste fournie, dans son ordre ; signale ce qui ne passe pas (format, doublons)."""
    path = DATA / name
    if not path.exists():
        print(f"{name}: absent")
        return []
    entries, bad, seen = [], [], set()
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        entry = line.strip()
        if not entry:
            continue
        if not valid(entry) and not (len(entry) == 1 and entry.isascii() and entry.isalnum()) or entry != entry.lower():
            bad.append(f"ligne {n} : {entry!r}")
        elif entry in seen:
            bad.append(f"ligne {n} : {entry!r} en double")
        else:
            seen.add(entry)
            entries.append(entry)
    print(f"{name}: {len(entries)}" + (f", {len(bad)} entrée(s) ignorée(s) par l'appli : " + "; ".join(bad[:10]) if bad else ""))
    return entries


def provided_only(added: dict[str, set[str]] | None = None) -> None:
    """Sans wordlists ni Lexique : vérifie les listes fournies (sans les réécrire) et complète langues.txt, avec la
    langue des noms ajoutés (added : listes anglaises de --us). Un nom sorti de toutes les listes en est retiré ; un
    nom des seules listes fournies est reclassé (la découpe s'améliore), les autres gardent la langue de leur liste."""
    elsewhere = set().union(*(wordlists._ordered(file) for key, file in wordlists.FILES.items()
                              if key not in wordlists.PROVIDED))
    lines = {}
    for line in (DATA / "langues.txt").read_text(encoding="utf-8").splitlines():
        name, _, langs = line.partition("\t")
        if name in elsewhere:
            lines[name] = set(langs.split(","))
    for name, langs in (added or {}).items():
        lines.setdefault(name, set()).update(langs)
    known = {"fr": set((DATA / "mots.txt").read_text(encoding="utf-8").split())
             | set((DATA / "formes.txt").read_text(encoding="utf-8").split()),
             "en": set((DATA / "anglais.txt").read_text(encoding="utf-8").split())}
    for name, langs in lines.items():
        for lang in langs:
            known.setdefault(lang, set()).add(name)
    without = 0
    for file, with_lang in PROVIDED.items():
        entries = check_provided(file)
        if not with_lang:
            continue
        found = provided_langs(entries, known, vocabularies(file), file)
        without += sum(1 for e in entries if e not in found and not any(e in known[k] for k in ("fr", "en")))
        for name, langs in found.items():
            lines.setdefault(name, set()).update(langs)
    print(f"sans langue (listes fournies, hors marques inventées) : {without}")
    write("langues.txt", [f"{name}\t{','.join(k for k in ('fr', 'en') if k in langs)}" for name, langs in sorted(lines.items())])


# --- Catégories dérivées ---------------------------------------------------------------------------------
# Un nom garde ses listes et reçoit en plus les catégories que lui donnent les règles ci-dessous, écrites dans
# app/data/categories.txt (nom, catégories gagnées) et lues par app.wordlists : classement et filtres seulement,
# jamais le score (rangs et traits restent ceux des listes d'origine). À relancer quand une liste change :
# python scripts/build_lists.py --provided (ou --themes, ou la construction complète), qui finissent par là.
# Listes d'où vient chaque catégorie (wordlists.DERIVED_FROM) : règle a, secteurs porteurs et tendances sont aussi
# des termes stars ; b, combinaisons vers mots-clés ; c, combinaisons et mots-clés vers métier + ville ; d, noms
# composés (wordlists.COMPOUNDS) vers les thèmes.
TO_PREMIUM = wordlists.DERIVED_FROM["premium"]
COMPOUNDS = wordlists.COMPOUNDS
# Thèmes et leur source à la main (sections [fr] et [en] : un mot anglais compte même s'il est resté dans
# anglais.txt). Tech et startups, tendances : la liste elle-même.
THEME_SOURCES = {"job": "metiers.txt", "sport": "sport.txt", "animal": "animaux.txt", "food": "cuisine.txt",
                 "startup": None, "trend": None}
# Mots d'un thème trop généraux ou ambigus pour le donner à un nom composé, relevés sur 50 noms tirés au hasard
# par thème : « bear » ferait de bearmarket un animal, « terrain » de terrainconstructible un nom de sport, « the »
# de theinsurance un nom de cuisine, « chat » de mychat un animal, « expert » d'expertassurance un métier.
WEAK = {
    "job": {"expert", "guide", "assistant", "assistante", "conseiller", "conseillere", "commercial", "responsable",
            "manager", "cadre", "patron", "pilote", "gardien", "auteur", "independant", "employe", "clerc", "moniteur",
            "acheteur", "vendeur", "builder", "builders", "trainer", "trainers", "commissaire", "mandataire",
            "controleur", "poseur", "installateur", "auditeur", "juge"},
    "sport": {"terrain", "piscine", "coach", "team", "club", "clubs", "match", "record", "ring", "piste", "circuit",
              "parcours", "filet", "course", "marche", "tir", "casque", "gants", "bandeau", "gourde", "casquette",
              "equipe", "ligue", "coupe", "transfert", "capitaine", "gardien", "cavalier", "fan", "fans", "supporter",
              "supporters", "ultras", "arena", "league", "trainer", "entraineur", "polo", "lutte", "champion",
              "champions", "championne", "joueur", "joueurs", "joueuse", "sprint", "competition", "chasse", "ballon",
              "moto"},
    "animal": {"chat", "pension", "compagnie", "niche", "adoption", "collier", "bete", "betes", "ver", "pie", "ours",
               "python", "puma", "jaguar", "morse", "sole", "raie", "carpe", "bull", "bear", "shark", "whale", "seal",
               "bat", "ant", "fox", "wolf", "eagle", "swan", "owl", "turkey", "spider", "refuge", "faune", "mascotte",
               "souris", "renne", "elan", "ane", "rat", "oie", "boa", "cygne", "lynx", "bouc"},
    "food": {"the", "cote", "lentille", "lentilles", "rouge", "blanc", "rose", "orange", "sel", "filet", "four",
             "poele", "cocotte", "couteau", "menu", "menus", "punch", "pate", "creme", "huile", "eau", "vapeur",
             "regime", "chef", "plat", "entree", "pain", "pie", "apple", "cookie", "cookies", "comte", "olive",
             "sauce", "salt", "pepper", "grill", "dish", "dishes", "bio", "organic", "healthy", "cuisson", "mixeur",
             "blender", "tablier", "toque", "lait", "diner", "cola", "soda", "rhum", "gin", "wrap", "lemon"},
    "startup": set(),
    "trend": {"local", "alpha", "storage", "stockage", "clean", "impact", "silver", "space", "edge", "remote",
              "cargo", "chat", "agent", "assistant", "influence", "createur", "borne", "bornes", "recharge", "defi",
              "chips", "vegetal"},
}
# Tech et startups : une liste de mots évocateurs (nova, forge, hub, lab, smart, auto) ; seuls ses mots techniques
# donnent le thème à un nom composé (aiagents, stockagecloud, logicielcomptable).
STRONG = {"startup": {
    "agents", "copilot", "prompt", "neural", "inference", "tensor", "vector", "gpu", "api", "apis", "sdk", "cli",
    "infra", "cloud", "compute", "cluster", "dataset", "data", "dev", "devs", "stack", "bot", "bots", "robot",
    "qubit", "quantum", "cipher", "startup", "founder", "venture", "numerique", "logiciel", "appli", "robotique",
    "donnees", "algorithme", "serveur", "jeunepousse", "fondateur", "chip", "byte", "token", "deploy", "kernel",
    "autopilot", "ledger", "wallet"}}
# Dans les listes commerciales (combinaisons, mots-clés, métier + ville, termes stars), « cuisine » et « kitchen »
# sont le cuisiniste (cuisine-metz, deviscuisine, kitchenquotes), pas la table ; dans un site de niche ou un média
# (forumcuisine, guidecuisine), la table. De même « fencing » y est le poseur de clôtures (dallasfencing), pas
# l'escrime.
TRADE_LISTS = ("combo", "keyword", "local", "local_us", "premium")
WEAK_TRADE = {"food": {"cuisine", "cuisines", "kitchen", "kitchens", "cocktail"}, "sport": {"fencing"}}
# Règle b : modificateurs d'intention de recherche (prix, comparaison, en ligne, devis). Les formes possessives et
# de média (mon, my, le, the, info, guide, hub, hq, top, sos, allo…) n'en sont pas, ni les ajouts de marque (pro,
# expert, direct, express, 24, now, instant, quick, buy, find) : monassurance, mytravel, hairexpress, buyteeth sont
# des noms de marque ou de média, pas des requêtes. Un devis se demande à un métier ou à un service (assurancedevis,
# moversquotes ; pas matelasdevis ni churchquotes).
INTENT = {"pascher", "discount", "enligne", "comparateur", "comparatif", "tarif", "tarifs", "simulation",
          "simulateur", "best", "finder", "online", "cheap", "compare", "rates"}
QUOTE = {"devis", "quotes", "quote"}
# Mots de liaison et possessifs des noms construits (monassurance, journaldelinformatique) : des morceaux de
# découpe, jamais un thème.
LINKS = {"mon", "ma", "le", "la", "les", "my", "the", "de", "du", "des", "del", "dela", "d", "l"}


def splits(name: str, vocabulary: set[str], parts: int = 4, keep: int = 8) -> list[tuple[str, ...]]:
    """Découpes les plus économes du nom en mots du vocabulaire (au plus parts mots, keep découpes) ; vide si le
    nom ne se découpe pas entièrement. Pas de sous-chaîne : chaque lettre appartient à un mot."""
    from functools import lru_cache

    @lru_cache(maxsize=None)
    def best(i: int, left: int):
        if i == len(name):
            return 0, ((),)
        if left == 0:
            return None
        found = None
        for j in range(i + 1, len(name) + 1):
            if name[i:j] not in vocabulary or (rest := best(j, left - 1)) is None:
                continue
            ways = tuple((name[i:j], *r) for r in rest[1])
            if found is None or rest[0] + 1 < found[0]:
                found = (rest[0] + 1, ways)
            elif rest[0] + 1 == found[0]:
                found = (found[0], (found[1] + ways)[:keep])
        return found

    found = best(0, parts)
    return list(found[1]) if found else []


def derive_vocab(lists: dict[str, list[str]]) -> dict:
    """Vocabulaires des règles, tirés des listes et des listes à la main : mots (Lexique, formes, anglais), thèmes,
    secteurs et thèmes des sites de niche, villes et métiers des listes « métier + ville » et des combinaisons,
    affixes des combinaisons, des sites de niche et des médias anglais."""
    local, local_us = sections("metier_ville.txt"), sections("metier_ville_us.txt")
    mods, formats = sections("modifiers.txt"), sections("formats.txt")
    themes = {}
    for key, source in THEME_SOURCES.items():
        found = set(lists.get(key, ()))
        if source:
            parts = sections(source)
            found |= set(parts.get("fr", [])) | set(parts.get("en", []))
        themes[key] = {w.replace("-", "") for w in found}
    cities = {c.replace("-", "") for c in local["villes"] + local["regions"] + read("villes_fr.txt")}
    # Villes des États-Unis : celles des combinaisons et de local_us.txt (découpe de scottsdaleplumber). Les métiers
    # de local_us.txt n'entrent pas dans les services : un nom de cette liste est déjà Métier + ville.
    us = set(read("cities_us.txt") + local_us["villes"])
    trades_en = set(read("trades_en.txt"))
    services = {w.replace("-", "") for w in local["services"] + read("metiers_fr.txt")} | trades_en
    services |= themes["job"]
    # Affixes : ajouts des combinaisons, formats des sites de niche et têtes des médias anglais (sans leurs genres).
    added = {affix(a) for part in (*mods.values(), formats["fr"], formats["fr-apres"]) for a in part}
    affixes = added | {affix(a) for a in formats["en"] + sections("medias_en.txt")["tetes"]} | LINKS | INTENT | QUOTE
    # Racine commerciale : un secteur ou un terme star qui n'est pas un ajout de combinaison ni un format français
    # (expert, app, pro, boutique). Un format anglais peut en être une (training, store, coach : besttraining,
    # coachpascher). Une ville n'en est pas une (paris : aussi un secteur, les paris sportifs).
    roots = (set(lists.get("sector", ())) | set(lists.get("premium", ()))) - added - LINKS - INTENT - QUOTE - cities - us
    # Découpe en vrais mots : Lexique, formes et anglais de 3 lettres et plus (un mot de 2 lettres colle à tout),
    # thèmes (ia et ai compris), thèmes des sites de niche, villes et affixes connus, même courts (my, hq, d, l).
    # Pas les noms composés des listes (coachsportif, assurancevie) : ils cacheraient leurs mots.
    words = {w for key in ("word", "form", "english") for w in lists.get(key, ()) if len(w) >= 3}
    words |= set(read("themes_fr.txt"))
    pieces = words | {w for t in themes.values() for w in t if len(w) >= 2} | cities | us | affixes
    return {"themes": themes, "cities": cities, "us": us, "places": cities | us, "services": services,
            "trades_en": trades_en, "before": set(local["avant-ville"]), "roots": roots,
            "affixes": affixes, "pieces": pieces, "commerce": pieces | roots}


def theme_of(name: str, vocab: dict, trade: bool = False) -> set[str]:
    """Thèmes d'un nom composé : ceux d'un de ses mots (3 lettres au moins), dans chacune de ses découpes les plus
    économes (marathon reste un mot entier : pas de « thon » pour la cuisine). Un mot de 3 lettres ne compte que si
    les autres sont des mots de 4 lettres et plus ou des affixes connus (petsitting, thevet ; pas colo+cat+ion+hosting
    ni tele+pho+nie). trade : nom d'une liste commerciale."""
    def gives(key: str, piece: str, way: tuple[str, ...]) -> bool:
        if len(piece) < 3 or piece not in vocab["themes"][key]:
            return False
        if len(piece) == 3 and any(len(p) < 4 and p not in vocab["affixes"] for p in way if p != piece):
            return False
        if key in STRONG:
            return piece in STRONG[key]
        return piece not in WEAK[key] and not (trade and piece in WEAK_TRADE.get(key, ()))

    found = None
    for way in splits(name.replace("-", ""), vocab["pieces"]):
        got = {key for key in vocab["themes"] for piece in way if gives(key, piece, way)}
        found = got if found is None else found & got
    return found or set()


def is_keyword(name: str, vocab: dict) -> bool:
    """Combinaison qui associe une racine commerciale (secteur, terme star) à une intention de recherche ou à
    une autre racine (assurancepascher, bestinsurance, assurancedevis), dans chacune de ses découpes les plus
    économes ; un devis seulement pour un métier ou un service."""
    ways = splits(name.replace("-", ""), vocab["commerce"])
    if not ways:
        return False
    for way in ways:
        roots = [p for p in way if p in vocab["roots"]]
        quote = any(p in QUOTE for p in way)
        if len(way) < 2 or not roots or any(p not in vocab["roots"] and p not in INTENT | QUOTE for p in way):
            return False
        if quote and not any(p in vocab["services"] for p in roots):
            return False
    return True


def is_local(name: str, vocab: dict) -> bool:
    """Métier + ville : plombierlyon, injurylawyernewyork, arlingtonlawyer ; ville d'abord seulement pour une ville
    américaine (comme les combinaisons) et pour immo, immobilier, auto, garage (lyonimmobilier)."""
    label = name.replace("-", "")
    for city in vocab["places"]:
        if label.endswith(city) and label[:-len(city)] in vocab["services"]:
            return True
        if label.startswith(city):
            rest = label[len(city):]
            if rest in vocab["before"] or (city in vocab["us"] and rest in vocab["trades_en"]):
                return True
    return False


def derive(lists: dict[str, list[str]], vocab: dict | None = None) -> dict[str, list[str]]:
    """Catégories gagnées par nom (absentes de ses listes d'origine), dans l'ordre de app.wordlists.LABELS."""
    vocab = vocab or derive_vocab(lists)
    own: dict[str, set[str]] = {}
    for key, names in lists.items():
        for name in names:
            own.setdefault(name, set()).add(key)
    gained: dict[str, set[str]] = {}

    def give(name: str, key: str) -> None:
        if key not in own[name]:
            gained.setdefault(name, set()).add(key)

    for key in TO_PREMIUM:
        for name in lists.get(key, ()):
            give(name, "premium")
    for name in (n for key in wordlists.DERIVED_FROM["keyword"] for n in lists.get(key, ())):
        if is_keyword(name, vocab):
            give(name, "keyword")
    for key in wordlists.DERIVED_FROM["local"]:
        for name in lists.get(key, ()):
            if is_local(name, vocab):
                give(name, "local")
    trade = {name for key in TRADE_LISTS for name in lists.get(key, ())}
    for key in COMPOUNDS:
        for name in lists.get(key, ()):
            for theme in theme_of(name, vocab, trade=name in trade):
                give(name, theme)
    order = list(wordlists.LABELS)
    # Règle e : jamais vers les dictionnaires, les marques inventées ni Mes ajouts (la langue a son propre filtre).
    assert not {k for keys in gained.values() for k in keys} & set(wordlists.NEVER_DERIVED)
    return {name: sorted(keys, key=order.index) for name, keys in sorted(gained.items())}


def write_derived() -> dict[str, list[str]]:
    """Relit les listes de app/data et réécrit categories.txt ; affiche les noms gagnés par catégorie."""
    lists = {key: wordlists._ordered(file) for key, file in wordlists.FILES.items()}
    found = derive(lists)
    lines = "".join(f"{name}\t{','.join(keys)}\n" for name, keys in found.items())
    (DATA / wordlists.DERIVED_FILE).write_text(lines, encoding="utf-8")
    counts: dict[str, int] = {}
    for keys in found.values():
        for key in keys:
            counts[key] = counts.get(key, 0) + 1
    print(f"{wordlists.DERIVED_FILE}: {len(found)} noms ; " + ", ".join(
        f"{key} {len(lists[key])} + {n}" for key, n in sorted(counts.items(), key=lambda kv: -kv[1])))
    return found


def us_only() -> None:
    """Sans wordlists ni Lexique : listes anglaises des États-Unis (sites de niche, noms de médias), puis les
    listes fournies et langues.txt comme --provided."""
    found = us_lists()
    provided_only({name: {"en"} for names in found.values() for name in names})


if __name__ == "__main__":
    if sys.argv[1:] == ["--themes"]:
        themes_only()
    elif sys.argv[1:] == ["--provided"]:
        provided_only()
    elif sys.argv[1:] == ["--us"]:
        us_only()
    else:
        main()
    write_derived()

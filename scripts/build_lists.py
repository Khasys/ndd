"""Construit les listes anglais, secteurs, tendances, combinaisons, sites de niche, expressions,
noms de média et listes thématiques (métiers, sport, animaux, cuisine) dans app/data.

Usage : python scripts/build_lists.py WORDFREQ_DIR LEXIQUE_TXT
        python scripts/build_lists.py --themes   (listes thématiques et langues.txt seulement, sans Lexique)
        python scripts/build_lists.py --provided (langue des listes fournies dans langues.txt, sans Lexique)

Listes fournies (termes premium, mots-clés à forte valeur, métier + ville, marques inventées : premium.txt,
motscles.txt, local.txt, marques_inventees.txt) : rédigées ailleurs, jamais écrites ici. Le script en vérifie
le format et en relève la langue (langues.txt) ; une marque inventée n'en a pas.

Chaque usage finit par les catégories dérivées (categories.txt, voir « Catégories dérivées » plus bas).

WORDFREQ_DIR : dossier du paquet wordfreq 3.1.1 décompressé (contient data/large_en.msgpack.gz).
LEXIQUE_TXT : Lexique383.txt (paquet pylexique 1.5.1), encodé en Latin-1. Il faut aussi le module msgpack.
Les listes choisies à la main sont dans scripts/lists ; sources et licences : app/data/SOURCES.md.
"""

import gzip
import itertools
import sys
import unicodedata
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
    for w in words:
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
    """Format de site + thème de passion : forumdomotique, guidepotager, gardeningforum."""
    formats = sections("formats.txt")
    fr, en = unique(read("themes_fr.txt")), unique(read("themes_en.txt"))
    families = [
        ranked(fr, formats["fr"], lambda b, m: [m + b]),
        ranked(fr, formats["fr-apres"], lambda b, m: [b + m]),
        ranked(en, formats["en"], lambda b, m: [b + m]),
    ]
    LANG["fr"].update(families[0] + families[1])
    LANG["en"].update(families[2])
    out = []
    for i in range(max(map(len, families))):
        out += [f[i] for f in families if i < len(f)]
    return out


ARTICLE_TOP = 600    # noms par genre (et pluriels) pour « le / la / l' / les + mot »
MEDIA_HEADS = ["journal", "planete", "monde", "univers", "gazette"]


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
    def linked(head, theme):
        if theme in plurals and theme not in genders:
            return head + "des" + theme
        if theme[0] in "aeiouy":
            return head + "del" + theme
        gender = genders.get(theme)
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
    # Listes fournies : jamais réécrites, leur langue relevée comme celle des autres.
    known = {"fr": existing | LANG["fr"], "en": set(english_words) | LANG["en"]}
    vocab = vocabularies()
    for file, with_lang in PROVIDED.items():
        entries = check_provided(file)
        if with_lang:
            for name, langs in provided_langs(entries, known, vocab).items():
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
PROVIDED = {"premium.txt": True, "motscles.txt": True, "local.txt": True, "marques_inventees.txt": False}
PIECE = 3  # une partie de nom composé fait 3 lettres au moins (« de », « my » collent à tout)


def vocabularies() -> dict[str, set[str]]:
    """Mots connus par langue, pour découper un nom composé : Lexique (mots, formes), mots anglais, villes,
    métiers et services des listes à la main."""
    def words(name: str) -> set[str]:
        return set((DATA / name).read_text(encoding="utf-8").split())

    fr = words("mots.txt") | words("formes.txt") | set(read("villes_fr.txt")) | set(read("metiers_fr.txt"))
    en = words("anglais.txt") | set(read("cities_us.txt")) | set(read("trades_en.txt"))
    return {"fr": {w.replace("-", "") for w in fr if len(w) >= PIECE}, "en": {w for w in en if len(w) >= PIECE}}


def pieces(name: str, vocabulary: set[str], parts: int = 3) -> int | None:
    """Nombre de mots du vocabulaire (1 à parts) qui composent le nom (plombierparis : 2), None s'il ne se
    découpe pas."""
    if name in vocabulary:
        return 1
    if parts <= 1:
        return None
    found = [n for cut in range(PIECE, len(name) - PIECE + 1)
             if name[:cut] in vocabulary and (n := pieces(name[cut:], vocabulary, parts - 1)) is not None]
    return 1 + min(found) if found else None


def provided_langs(names: list[str], known: dict[str, set[str]], vocab: dict[str, set[str]]) -> dict[str, list[str]]:
    """Langue des noms d'une liste fournie qui n'en ont pas encore (Lexique, anglais, langues.txt) : celle des
    mots qui les composent, la plus économe en mots l'emportant (carinsurance : car + insurance, pas
    car + insu + rance) ; ne se découpe pas : sans langue (absent du résultat)."""
    found = {}
    for name in names:
        if any(name in known[k] for k in ("fr", "en")):
            continue  # langue déjà connue de l'appli
        counts = {k: pieces(name.replace("-", ""), vocab[k]) for k in ("fr", "en")}
        best = min((n for n in counts.values() if n is not None), default=None)
        if best is not None:
            found[name] = [k for k, n in counts.items() if n == best]
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


def provided_only() -> None:
    """Sans wordlists ni Lexique : vérifie les listes fournies (sans les réécrire) et complète langues.txt."""
    lines = {}
    for line in (DATA / "langues.txt").read_text(encoding="utf-8").splitlines():
        name, _, langs = line.partition("\t")
        if name:
            lines[name] = set(langs.split(","))
    known = {"fr": set((DATA / "mots.txt").read_text(encoding="utf-8").split())
             | set((DATA / "formes.txt").read_text(encoding="utf-8").split()),
             "en": set((DATA / "anglais.txt").read_text(encoding="utf-8").split())}
    for name, langs in lines.items():
        for lang in langs:
            known.setdefault(lang, set()).add(name)
    vocab = vocabularies()
    without = 0
    for file, with_lang in PROVIDED.items():
        entries = check_provided(file)
        if not with_lang:
            continue
        found = provided_langs(entries, known, vocab)
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
# des termes premium ; b, combinaisons vers mots-clés ; c, combinaisons et mots-clés vers métier + ville ; d, noms
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
# Dans les listes commerciales (combinaisons, mots-clés, métier + ville, termes premium), « cuisine » et « kitchen »
# sont le cuisiniste (cuisine-metz, deviscuisine, kitchenquotes), pas la table ; dans un site de niche ou un média
# (forumcuisine, guidecuisine), la table.
TRADE_LISTS = ("combo", "keyword", "local", "premium")
WEAK_TRADE = {"food": {"cuisine", "cuisines", "kitchen", "kitchens", "cocktail"}}
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
    affixes des combinaisons et des sites de niche."""
    local = sections("metier_ville.txt")
    mods, formats = sections("modifiers.txt"), sections("formats.txt")
    themes = {}
    for key, source in THEME_SOURCES.items():
        found = set(lists.get(key, ()))
        if source:
            parts = sections(source)
            found |= set(parts.get("fr", [])) | set(parts.get("en", []))
        themes[key] = {w.replace("-", "") for w in found}
    cities = {c.replace("-", "") for c in local["villes"] + local["regions"] + read("villes_fr.txt")}
    us = set(read("cities_us.txt"))
    services = {w.replace("-", "") for w in local["services"] + read("metiers_fr.txt") + read("trades_en.txt")}
    services |= themes["job"]
    affixes = {a for part in (*mods.values(), *formats.values()) for a in part} | LINKS | INTENT | QUOTE
    # Racine commerciale : un secteur ou un terme premium qui n'est pas un ajout de combinaison (expert, app, pro).
    # Une ville n'en est pas une (paris : aussi un secteur, les paris sportifs).
    roots = (set(lists.get("sector", ())) | set(lists.get("premium", ()))) - affixes - cities - us
    # Découpe en vrais mots : Lexique, formes et anglais de 3 lettres et plus (un mot de 2 lettres colle à tout),
    # thèmes (ia et ai compris), thèmes des sites de niche, villes et affixes connus, même courts (my, hq, d, l).
    # Pas les noms composés des listes (coachsportif, assurancevie) : ils cacheraient leurs mots.
    words = {w for key in ("word", "form", "english") for w in lists.get(key, ()) if len(w) >= 3}
    words |= set(read("themes_fr.txt")) | set(read("themes_en.txt"))
    pieces = words | {w for t in themes.values() for w in t if len(w) >= 2} | cities | us | affixes
    return {"themes": themes, "cities": cities, "us": us, "places": cities | us, "services": services,
            "trades_en": set(read("trades_en.txt")), "before": set(local["avant-ville"]), "roots": roots,
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
    """Combinaison qui associe une racine commerciale (secteur, terme premium) à une intention de recherche ou à
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


if __name__ == "__main__":
    if sys.argv[1:] == ["--themes"]:
        themes_only()
    elif sys.argv[1:] == ["--provided"]:
        provided_only()
    else:
        main()
    write_derived()

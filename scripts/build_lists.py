"""Construit les listes anglais, secteurs, tendances, combinaisons, sites de niche, expressions,
noms de média et listes thématiques (métiers, sport, animaux, cuisine) dans app/data.

Usage : python scripts/build_lists.py WORDFREQ_DIR LEXIQUE_TXT
        python scripts/build_lists.py --themes   (listes thématiques et langues.txt seulement, sans Lexique)
        python scripts/build_lists.py --provided (langue des listes fournies dans langues.txt, sans Lexique)

Listes fournies (termes premium, mots-clés à forte valeur, métier + ville, marques inventées : premium.txt,
motscles.txt, local.txt, marques_inventees.txt) : rédigées ailleurs, jamais écrites ici. Le script en vérifie
le format et en relève la langue (langues.txt) ; une marque inventée n'en a pas.

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


if __name__ == "__main__":
    if sys.argv[1:] == ["--themes"]:
        themes_only()
    elif sys.argv[1:] == ["--provided"]:
        provided_only()
    else:
        main()

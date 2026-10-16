"""Construit les listes anglais, secteurs, tendances, combinaisons et sites de niche dans app/data.

Usage : python scripts/build_lists.py WORDFREQ_DIR LEXIQUE_TXT

WORDFREQ_DIR : dossier du paquet wordfreq 3.1.1 décompressé (contient data/large_en.msgpack.gz).
LEXIQUE_TXT : Lexique383.txt (paquet pylexique 1.5.1). Il faut aussi le module msgpack.
Les listes choisies à la main sont dans scripts/lists ; sources et licences : app/data/SOURCES.md.
"""

import gzip
import itertools
import sys
import unicodedata
from pathlib import Path

import msgpack

ROOT = Path(__file__).resolve().parent.parent
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


def read(name: str) -> list[str]:
    out = []
    for line in (LISTS / name).read_text(encoding="utf-8").splitlines():
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
    raw = msgpack.unpackb(gzip.open(wordfreq_dir / "data" / "large_en.msgpack.gz").read(), raw=False)
    words = (w for bucket in raw[1:] for w in bucket)
    picked = []
    for w in words:
        if w.isascii() and w.isalpha() and len(w) >= 2 and w not in banned:
            picked.append(w)
            if len(picked) >= ENGLISH_SIZE:
                break
    return picked


def nouns(lexique: Path) -> dict[str, str]:
    """Genre des noms communs (m ou f), du plus fréquent au moins fréquent."""
    rows = []
    with lexique.open(encoding="utf-8", errors="replace") as f:
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


def write(name: str, words: list[str]) -> None:
    (DATA / name).write_text("\n".join(words) + "\n", encoding="utf-8")
    print(f"{name}: {len(words)}")


def main() -> None:
    wordfreq_dir, lexique = Path(sys.argv[1]), Path(sys.argv[2])
    banned = set(read("marques.txt"))
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
    # Tendances : anglais si le mot est dans la liste anglaise, français s'il est dans le Lexique.
    french = existing | set(nouns(lexique))
    LANG["fr"].update(sectors_fr + [t for t in trends if t in french])
    LANG["en"].update(sectors_en + [t for t in trends if t in set(english_words)])
    # Jargon des tendances absent des deux listes (fintech, padel, web3…) : employé dans les deux langues.
    for t in trends:
        if t not in LANG["fr"] and t not in LANG["en"]:
            LANG["fr"].add(t)
            LANG["en"].add(t)
    mixed = set(sectors) | set(trends) | set(combined) | set(niches) | set(expressions)
    lines = []
    for name in sorted(mixed):
        langs = [k for k in ("fr", "en") if name in LANG[k]]
        if langs:
            lines.append(f"{name}\t{','.join(langs)}")
    write("langues.txt", lines)


if __name__ == "__main__":
    main()

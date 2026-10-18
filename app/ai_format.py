"""Format imposé des avis IA : même règle pour le NAS (à l'import) et pour la tâche de notation
(avant l'envoi). Aucune dépendance hors de la bibliothèque standard : le script de notation
l'importe dans une session qui n'a pas les paquets de l'appli."""

import re

# Format imposé, le même pour chaque avis : clés, types, pas des notes et des prix, gabarit du commentaire.
FIELDS = ("name", "exploit", "resale", "quick_low", "quick_high", "final_low", "final_high", "comment")
SCORE_STEP = 5
PRICE_STEPS = (0, 50, 100, 150, 200, 300, 500, 800, 1000, 1500, 2000, 3000, 5000, 8000, 10000, 15000,
               20000, 30000, 50000, 80000, 100000, 150000, 200000, 300000, 500000, 1000000)
COMMENT_MAX = 200
# « Verdict. Raison. Acheteur type : qui. » Le verdict est l'un des trois, en tête.
VERDICTS = ("À prendre", "À surveiller", "À laisser")
COMMENT_SHAPE = re.compile(r"^(À prendre|À surveiller|À laisser)\. [^\s].{5,}?\. Acheteur type : .{3,}?\.$")
# Cohérence avec les notes (la meilleure des deux) : un verdict ne contredit pas les chiffres.
PRENDRE_MIN = 60
LAISSER_MAX = 45
FORBIDDEN = re.compile("[\u2013\u2014\U0001F000-\U0001FAFF\u2600-\u27BF]")  # tirets longs, emoji


def check(item, known) -> str | None:
    """Raison du refus d'un avis, ou None s'il respecte le format. Même règle pour la tâche
    de notation (avant envoi) et pour le NAS (à l'import)."""
    if not isinstance(item, dict):
        return "pas un objet"
    keys = set(item)
    if keys != set(FIELDS):
        missing, extra = set(FIELDS) - keys, keys - set(FIELDS)
        return "clés attendues : " + ", ".join(FIELDS) + (f" (manque {', '.join(sorted(missing))})" if missing else "") + \
            (f" (en trop {', '.join(sorted(extra))})" if extra else "")
    name = item["name"]
    if not isinstance(name, str) or not known(name):
        return "nom inconnu"
    numbers = [item[k] for k in FIELDS[1:7]]
    if any(not isinstance(v, int) or isinstance(v, bool) for v in numbers):
        return "notes et prix : entiers attendus"
    exploit, resale, quick_low, quick_high, final_low, final_high = numbers
    if not all(0 <= v <= 100 and v % SCORE_STEP == 0 for v in (exploit, resale)):
        return "notes : de 0 à 100, multiples de 5"
    if any(v not in PRICE_STEPS for v in numbers[2:]):
        return "prix hors de l'échelle"
    if quick_low > quick_high or final_low > final_high:
        return "fourchette inversée"
    if quick_high > final_high or quick_low > final_low:
        return "revente rapide plus chère que l'utilisateur final"
    comment = item["comment"]
    if not isinstance(comment, str) or comment != " ".join(comment.split()):
        return "commentaire : texte sur une ligne, sans espaces en trop"
    if len(comment) > COMMENT_MAX:
        return f"commentaire : {COMMENT_MAX} caractères au plus"
    if FORBIDDEN.search(comment):
        return "commentaire : ni emoji ni tiret long"
    shape = COMMENT_SHAPE.match(comment)
    if not shape:
        return "commentaire : « À prendre|À surveiller|À laisser. Raison. Acheteur type : … »"
    best = max(exploit, resale)
    if shape.group(1) == "À prendre" and best < PRENDRE_MIN:
        return f"« À prendre » demande une note d'au moins {PRENDRE_MIN}"
    if shape.group(1) == "À laisser" and best > LAISSER_MAX:
        return f"« À laisser » demande des notes de {LAISSER_MAX} au plus"
    return None


# --- Version 5 : grille détaillée -------------------------------------------------------------
# L'IA donne des sous-notes de 0 à 10, deux fourchettes de prix et une raison. Les notes sur 100
# et le verdict en découlent par une règle fixe (derive) : ils ne peuvent plus se contredire.
# Les prix restent un jugement, encadrés par des paliers selon la valeur et l'extension.

CRITERIA_V5 = {
    "oral": "Prononciation : se dit d'une seule façon, sans hésiter (français pour le .fr, anglais pour .com, .io, .ai)",
    "radio": "Test radio : entendu une fois, on sait l'écrire (pas de c/k/q, ph/f, y/i ambigus, ni chiffre ou tiret)",
    "memo": "Mémorisation : court, rythmé, frappant",
    "sens": "Sens : mot réel, clair, positif et courant (0 si aucun sens)",
    "marque": "Potentiel de marque : évocateur, adaptable à plusieurs activités, beau à l'écrit",
    "marche": "Valeur commerciale : secteur qui paie, intention d'achat, mot-clé recherché",
    "ext": "Adéquation à l'extension : .fr pour le marché français, .com international, .org associatif, .io et .ai tech",
    "acheteurs": "Acheteurs possibles : beaucoup d'entreprises plausibles (10) ou un seul acheteur imaginable (1)",
    "risque": "Risque : marque déposée connue, sens négatif, vulgaire ou gênant dans une autre langue (0 aucun, 10 fort)",
}
FIELDS_V5 = ("name", *CRITERIA_V5, "quick_low", "quick_high", "final_low", "final_high", "comment")
COMMENT_V5_MAX = 180
# « Raison. Acheteur type : qui. » Le verdict n'est plus écrit par l'IA : il est calculé.
COMMENT_V5_SHAPE = re.compile(r"^[^\s].{5,}?\. Acheteur type : .{3,}?\.$")

# Notes sur 100 : une valeur de fond (sens, marque, marché…) multipliée par la forme orale. Un nom
# qui se dit et s'écrit sans faute garde toute sa valeur ; un nom difficile en perd jusqu'à 40 %.
# La forme n'invente pas de valeur : sans sens ni acheteurs, une belle sonorité ne suffit pas.
EXPLOIT_WEIGHTS = {"sens": 0.38, "marque": 0.37, "ext": 0.10, "memo": 0.15}
RESALE_WEIGHTS = {"marche": 0.25, "acheteurs": 0.25, "sens": 0.15, "marque": 0.10, "ext": 0.10, "memo": 0.15}
FORM_FLOOR = 0.6
RISK_COST = 3     # points de note retirés par point de risque
TAKE_MIN = 60     # « À prendre » : meilleure note d'au moins 60, risque de 4 au plus, acheteurs de 4 et plus
TAKE_BUYERS = 4
LEAVE_MAX = 40    # « À laisser » : meilleure note de 40 au plus, ou risque de 8 et plus

# Prix utilisateur final selon la meilleure des deux notes, pour un .fr (repères indicatifs, pas une
# cote : à recaler sur les ventes réelles du portefeuille). Autres extensions : coefficient.
PRICE_TIERS = ((90, 10000, 100000), (80, 5000, 30000), (70, 3000, 15000), (60, 1500, 8000), (50, 1000, 5000),
               (40, 500, 2000), (30, 200, 1000), (20, 100, 800), (10, 50, 300), (0, 0, 100))
EXT_FACTOR = {"com": 1.5, "fr": 1.0, "io": 0.8, "ai": 0.8, "co": 0.6, "org": 0.5, "app": 0.5, "dev": 0.5,
              "net": 0.4, "info": 0.25, "biz": 0.25, "xyz": 0.25}
OTHER_EXT_FACTOR = 0.4
QUICK_SHARE = 0.4  # revente rapide : 40 % du prix utilisateur final au plus
SHORT_UNCAPPED = 2  # noms de 1 ou 2 caractères : pas de plafond de palier
SHORT_UNCAPPED_BY_TLD = {"com": 3}  # en .com, jusqu'à 3 caractères (marché porté par la rareté)
UNCAPPED_FROM = 90  # ni pour une note de 90 et plus


def _round5(x: float) -> int:
    return max(0, min(100, int(5 * round(x / 5))))


def derive(item: dict) -> dict:
    """Notes sur 100 et verdict, calculés depuis les sous-notes : même règle partout."""
    risk = item["risque"]
    form = FORM_FLOOR + (1 - FORM_FLOOR) * (item["oral"] + item["radio"]) / 20
    exploit = _round5(10 * sum(item[k] * w for k, w in EXPLOIT_WEIGHTS.items()) * form - RISK_COST * risk)
    resale = _round5(10 * sum(item[k] * w for k, w in RESALE_WEIGHTS.items()) * form - RISK_COST * risk)
    best = max(exploit, resale)
    if best >= TAKE_MIN and risk <= 4 and item["acheteurs"] >= TAKE_BUYERS:
        verdict = "À prendre"
    elif best <= LEAVE_MAX or risk >= 8:
        verdict = "À laisser"
    else:
        verdict = "À surveiller"
    return {"exploit": exploit, "resale": resale, "verdict": verdict}


def _step_down(x: float) -> int:
    return max([p for p in PRICE_STEPS if p <= x] or [0])


def _step_up(x: float) -> int:
    return min([p for p in PRICE_STEPS if p >= x] or [PRICE_STEPS[-1]])


def price_band(best: int, tld: str) -> tuple[int, int]:
    """Fourchette de prix utilisateur final attendue pour une note (0 à 100) et une extension."""
    factor = EXT_FACTOR.get(tld, OTHER_EXT_FACTOR)
    for floor, low, high in PRICE_TIERS:
        if best >= floor:
            return _step_down(low * factor), _step_up(high * factor)
    return 0, _step_up(100 * factor)


def check_v5(item, known) -> str | None:
    """Raison du refus d'un avis v5, ou None. Même règle pour la tâche de notation et pour le NAS."""
    if not isinstance(item, dict):
        return "pas un objet"
    keys = set(item)
    if keys != set(FIELDS_V5):
        missing, extra = set(FIELDS_V5) - keys, keys - set(FIELDS_V5)
        return "clés attendues : " + ", ".join(FIELDS_V5) + (f" (manque {', '.join(sorted(missing))})" if missing else "") + \
            (f" (en trop {', '.join(sorted(extra))})" if extra else "")
    name = item["name"]
    if not isinstance(name, str) or not known(name):
        return "nom inconnu"
    subs = [item[k] for k in CRITERIA_V5]
    if any(not isinstance(v, int) or isinstance(v, bool) or not 0 <= v <= 10 for v in subs):
        return "sous-notes : entiers de 0 à 10"
    prices = [item[k] for k in ("quick_low", "quick_high", "final_low", "final_high")]
    if any(not isinstance(v, int) or isinstance(v, bool) or v not in PRICE_STEPS for v in prices):
        return "prix hors de l'échelle"
    quick_low, quick_high, final_low, final_high = prices
    if quick_low > quick_high or final_low > final_high:
        return "fourchette inversée"
    if quick_low > final_low:
        return "revente rapide plus chère que l'utilisateur final"
    if quick_high > _step_up(final_high * QUICK_SHARE):
        return f"revente rapide : {int(QUICK_SHARE * 100)} % du prix utilisateur final au plus"
    notes = derive(item)
    best = max(notes["exploit"], notes["resale"])
    # Le prix suit la note de revente : elle tient compte du marché et des acheteurs, l'exploitation non.
    resale = notes["resale"]
    label, _, tld = name.rpartition(".")
    # Un palier de tolérance de chaque côté : le prix suit la note sans être une formule. Noms de 1 ou
    # 2 caractères : rares, ils se vendent au-dessus des paliers (ev.fr, 120 000 euros en 2022) ; pas de plafond.
    floor = price_band(max(0, resale - 10), tld)[0]
    # Meilleurs mots du marché (note de 90 et plus) : pas de plafond non plus.
    uncapped = len(label) <= SHORT_UNCAPPED_BY_TLD.get(tld, SHORT_UNCAPPED) or best >= UNCAPPED_FROM
    ceiling = PRICE_STEPS[-1] if uncapped else price_band(min(100, resale + 10), tld)[1]
    if final_low < floor or final_high > ceiling:
        return f"prix utilisateur final hors palier pour une note de revente de {resale} en .{tld} : entre {floor} et {ceiling} euros"
    comment = item["comment"]
    if not isinstance(comment, str) or comment != " ".join(comment.split()):
        return "commentaire : texte sur une ligne, sans espaces en trop"
    if len(comment) > COMMENT_V5_MAX:
        return f"commentaire : {COMMENT_V5_MAX} caractères au plus"
    if FORBIDDEN.search(comment):
        return "commentaire : ni emoji ni tiret long"
    if not COMMENT_V5_SHAPE.match(comment) or comment.startswith(VERDICTS):
        return "commentaire : « Raison. Acheteur type : qui. » (sans verdict, il est calculé)"
    return None


FORMAT_V5 = {
    "type": "object",
    "additionalProperties": False,
    "required": list(FIELDS_V5),
    "properties": {
        "name": {"type": "string", "description": "nom complet reçu, tel quel (ex. velo.fr)"},
        **{key: {"type": "integer", "minimum": 0, "maximum": 10, "description": text} for key, text in CRITERIA_V5.items()},
        "quick_low": {"type": "integer", "enum": list(PRICE_STEPS)},
        "quick_high": {"type": "integer", "enum": list(PRICE_STEPS)},
        "final_low": {"type": "integer", "enum": list(PRICE_STEPS)},
        "final_high": {"type": "integer", "enum": list(PRICE_STEPS)},
        "comment": {"type": "string", "maxLength": COMMENT_V5_MAX, "pattern": "Raison en une phrase. Acheteur type : qui."},
    },
}


def is_v5(item) -> bool:
    return isinstance(item, dict) and "oral" in item


def check_any(item, known) -> str | None:
    """Avis v5 (sous-notes) ou v4 (notes et verdict écrits) : le contrôle qui correspond."""
    return check_v5(item, known) if is_v5(item) else check(item, known)


def summary(item) -> str:
    """« nom : verdict (exploitation X, revente Y) », pour les comptes rendus des tâches."""
    if is_v5(item):
        d = derive(item)
        return f"{item['name']} : {d['verdict']} (exploitation {d['exploit']}, revente {d['resale']})"
    return f"{item['name']} : {item['comment'].split('.')[0]} (exploitation {item['exploit']}, revente {item['resale']})"

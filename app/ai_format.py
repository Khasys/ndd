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

FORMAT = {
    "type": "object",
    "additionalProperties": False,
    "required": list(FIELDS),
    "properties": {
        "name": {"type": "string", "description": "nom complet reçu, tel quel (ex. velo.fr)"},
        "exploit": {"type": "integer", "minimum": 0, "maximum": 100, "multipleOf": SCORE_STEP},
        "resale": {"type": "integer", "minimum": 0, "maximum": 100, "multipleOf": SCORE_STEP},
        "quick_low": {"type": "integer", "enum": list(PRICE_STEPS)},
        "quick_high": {"type": "integer", "enum": list(PRICE_STEPS)},
        "final_low": {"type": "integer", "enum": list(PRICE_STEPS)},
        "final_high": {"type": "integer", "enum": list(PRICE_STEPS)},
        "comment": {"type": "string", "maxLength": COMMENT_MAX,
                    "pattern": "À prendre|À surveiller|À laisser. Raison en une phrase. Acheteur type : qui."},
    },
}

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

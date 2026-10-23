"""Format imposé des avis IA : même règle pour le NAS (à l'import) et pour la tâche de notation
(avant l'envoi). Aucune dépendance hors de la bibliothèque standard : le script de notation
l'importe dans une session qui n'a pas les paquets de l'appli."""

import math
import re
from functools import lru_cache
from pathlib import Path

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
# Délais estimés par l'IA (version 6) : plus demandés, la chance de vente vient des données réelles (sell_rate).
# Encore acceptés à l'envoi le temps qu'une tâche à jour remplace l'ancienne, puis ignorés.
MONTH_STEPS = (1, 3, 6, 12, 24, 36, 60, 120)
OPTIONAL_V5 = ("quick_months", "final_months")

# --- Chance de vente et rendement : repères réels (étalonnage du 20/10/2026, sources dans le README) ----------
# Un portefeuille passif vend 1 à 2 % de ses noms par an (Allemann, DomainNameWire, 2022-2024 ; Afternic juge 1 %
# « ambitieux » pour un débutant, 2026). Chez Squadhelp (2021), la part vendue par an monte avec le nombre
# d'extensions où le nom est déjà pris : 2,84 % (une), 5,59 %, 9,46 %, 11,61 %, 14,66 % (cinq et plus), sur des noms
# triés à l'entrée ; divisée par deux ici pour des noms non triés. Le potentiel du nom (note de revente) la
# multiplie (×2 tous les 15 points au-dessus de 50, ÷2 tous les 15 points en dessous), entre 0,5 % et 15 % par an.
SELL_RATE_TAKEN = (0.014, 0.028, 0.047, 0.058, 0.073)  # pris dans 0, 1, 2, 3, 4 autres extensions et plus
SELL_RATE_MIN, SELL_RATE_MAX = 0.005, 0.15
COMMISSION = 0.20        # commission de la place de marché : hypothèse prudente
DEFAULT_RENEWAL = 15.0   # euros par an, quand le prix Gandi de l'extension n'est pas connu
TAKE_YIELD = 2.0         # « À prendre » : rapporte au moins deux fois son renouvellement chaque année
# Premium (prix d'achat au-dessus du tarif normal) : pas de « À prendre » si le prix final bas ne couvre pas deux fois
# l'achat (test.co : achat 4 376 €, revente finale 3 000 € au plus bas). Le rendement ne compte que le renouvellement.
PREMIUM_COVER = 2.0


def sell_rate(resale: int, taken: int) -> float:
    """Part de chance de vendre dans l'année au prix utilisateur final."""
    base = SELL_RATE_TAKEN[max(0, min(taken, len(SELL_RATE_TAKEN) - 1))]
    return max(SELL_RATE_MIN, min(SELL_RATE_MAX, base * 2 ** ((resale - 50) / 15)))


def annual_yield(final_low: int, rate: float, renewal: float) -> float:
    """Rendement attendu par an, en euros : vente probable au prix bas, commission ôtée, moins le renouvellement."""
    return rate * final_low * (1 - COMMISSION) - renewal


def median_years(rate: float) -> float:
    """Années pour avoir une chance sur deux d'avoir vendu."""
    return math.log(0.5) / math.log(1 - rate)
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
TAKE_MIN = 60     # « À prendre » : note de revente d'au moins 60, risque de 4 au plus, acheteurs de 4 et plus
TAKE_BUYERS = 4
LEAVE_MAX = 40    # « À laisser » : note de revente de 40 au plus, ou risque de 8 et plus (la revente décide)
# Longueur : au-delà de 12 lettres, un nom se lit et se retient mal (assurancesantesenior), quel que soit son
# marché. 3 % de note en moins par lettre de plus, 30 % au plus (22 lettres et au-delà).
LENGTH_FREE = 12
LENGTH_STEP = 0.03
LENGTH_FLOOR = 0.7

# Prix utilisateur final selon la note de revente, pour un .fr (repères indicatifs, pas une cote : à
# recaler sur les ventes réelles du portefeuille). Autres extensions : coefficient. Paliers de 5 points
# à partir de 85 : sans eux, le prix sautait de 30 000 à 100 000 entre 89 et 90, et rien n'encadrait
# plus un 90 ; les meilleurs mots du marché restent plafonnés, à 300 000 (credit.fr, 587 500 en 2010,
# reste l'exception).
PRICE_TIERS = ((95, 20000, 300000), (90, 10000, 150000), (85, 8000, 80000), (80, 5000, 30000), (70, 3000, 15000),
               (60, 1500, 8000), (50, 1000, 5000), (40, 500, 2000), (30, 200, 1000), (20, 100, 800), (10, 50, 300),
               (0, 0, 100))
EXT_FACTOR = {"com": 1.5, "fr": 1.0, "io": 0.8, "ai": 0.8, "co": 0.6, "org": 0.5, "app": 0.5, "dev": 0.5,
              "net": 0.4, "info": 0.25, "biz": 0.25, "xyz": 0.25}
OTHER_EXT_FACTOR = 0.4
QUICK_SHARE = 0.4  # revente rapide : 40 % du prix utilisateur final au plus
SHORT_UNCAPPED = 2  # noms de 1 ou 2 caractères : pas de plafond de palier
SHORT_UNCAPPED_BY_TLD = {"com": 3}  # en .com, jusqu'à 3 caractères (marché porté par la rareté)


def _round5(x: float) -> int:
    return max(0, min(100, int(5 * round(x / 5))))


# Mémorisation calculée par l'appli, pas par l'IA : la même grille pour tous les correcteurs (deux sessions
# donnaient 3 et 7 à deux noms de même longueur). 10 jusqu'à 4 lettres ou mot très courant ; 7 de 5 à 7 lettres,
# ou mot courant jusqu'à 12 ; 4 de 8 à 12 lettres ; 2 au-delà.
MEMO_VERY_COMMON = 2000   # rang dans mots.txt ou anglais.txt
MEMO_COMMON = 10000


@lru_cache(maxsize=1)
def _word_ranks() -> dict[str, int]:
    ranks: dict[str, int] = {}
    data = Path(__file__).parent / "data"
    for name in ("mots.txt", "anglais.txt"):
        path = data / name
        if path.exists():
            for rank, line in enumerate(path.read_text().split(), 1):
                ranks[line] = min(rank, ranks.get(line, rank))
    return ranks


def memo_for(name: str) -> int | None:
    """Sous-note de mémorisation d'après la longueur et la fréquence du mot ; None sans nom."""
    label = name.rpartition(".")[0] or name
    if not label:
        return None
    rank = _word_ranks().get(label)
    if len(label) <= 4 or (rank and rank <= MEMO_VERY_COMMON):
        return 10
    if len(label) <= 7 or (rank and rank <= MEMO_COMMON and len(label) <= 12):
        return 7
    return 4 if len(label) <= 12 else 2


def normalized(item: dict) -> dict:
    """Sous-notes telles que l'appli les retient : la mémorisation est la sienne."""
    memo = memo_for(item.get("name") or "")
    return {**item, "memo": memo} if memo is not None else item


def length_factor(name: str) -> float:
    """Part de la note gardée selon la longueur du nom, sans l'extension."""
    label = name.rpartition(".")[0] or name
    return max(LENGTH_FLOOR, 1 - LENGTH_STEP * max(0, len(label) - LENGTH_FREE))


def derive(item: dict) -> dict:
    """Notes sur 100 et verdict, calculés depuis les sous-notes : même règle partout. La note de revente est le
    potentiel du nom. Avec la demande mesurée (taken : autres extensions où il est pris) et le prix utilisateur final,
    l'appli en tire une chance de vente par an et un rendement attendu (euros par an, renouvellement ôté), qui
    décident du verdict. Sans ces données (avis d'avant, essais), le verdict suit la note de revente. Un premium
    (purchase : prix d'achat) n'est « À prendre » que si le prix final bas couvre l'achat (covers_purchase)."""
    item = normalized(item)
    risk = item["risque"]
    form = (FORM_FLOOR + (1 - FORM_FLOOR) * (item["oral"] + item["radio"]) / 20) * length_factor(item.get("name") or "")
    exploit = _round5(10 * sum(item[k] * w for k, w in EXPLOIT_WEIGHTS.items()) * form - RISK_COST * risk)
    resale = _round5(10 * sum(item[k] * w for k, w in RESALE_WEIGHTS.items()) * form - RISK_COST * risk)
    found = {"exploit": exploit, "resale": resale}
    if item.get("taken") is not None and item.get("final_low") is not None:
        renewal = item.get("renewal") or DEFAULT_RENEWAL
        rate = sell_rate(resale, item["taken"])
        gain = annual_yield(item["final_low"], rate, renewal)
        found.update(sell_rate=round(rate, 4), yield_=round(gain, 1))
        if risk >= 8 or gain <= 0:
            verdict = "À laisser"
        elif gain >= TAKE_YIELD * renewal and risk <= 4 and item["acheteurs"] >= TAKE_BUYERS:
            verdict = "À prendre"
        else:
            verdict = "À surveiller"
    elif resale >= TAKE_MIN and risk <= 4 and item["acheteurs"] >= TAKE_BUYERS:
        verdict = "À prendre"
    elif resale <= LEAVE_MAX or risk >= 8:
        verdict = "À laisser"
    else:
        verdict = "À surveiller"
    if verdict == "À prendre" and not covers_purchase(item):
        verdict = "À surveiller"
    return {**found, "verdict": verdict}


def covers_purchase(item: dict) -> bool:
    """Le prix final bas couvre PREMIUM_COVER fois le prix d'achat premium (purchase, euros) ; sans premium : oui."""
    purchase, low = item.get("purchase"), item.get("final_low")
    return not purchase or low is None or low >= PREMIUM_COVER * purchase


def _step_down(x: float) -> int:
    return max([p for p in PRICE_STEPS if p <= x] or [0])


def _step_up(x: float) -> int:
    return min([p for p in PRICE_STEPS if p >= x] or [PRICE_STEPS[-1]])


def _tier(best: int) -> int:
    """Rang du palier d'une note (0 : le plus haut)."""
    for n, (floor, _, _) in enumerate(PRICE_TIERS):
        if best >= floor:
            return n
    return len(PRICE_TIERS) - 1


def _band_at(tier: int, tld: str) -> tuple[int, int]:
    factor = EXT_FACTOR.get(tld, OTHER_EXT_FACTOR)
    _, low, high = PRICE_TIERS[max(0, min(len(PRICE_TIERS) - 1, tier))]
    return _step_down(low * factor), _step_up(high * factor)


def price_band(best: int, tld: str) -> tuple[int, int]:
    """Fourchette de prix utilisateur final attendue pour une note (0 à 100) et une extension."""
    return _band_at(_tier(best), tld)


def price_limits(resale: int, tld: str, label: str = "") -> tuple[int, int]:
    """Bornes acceptées du prix utilisateur final : le palier de la note, plus un palier de tolérance
    de chaque côté (le prix suit la note sans être une formule). Noms de 1 ou 2 caractères (3 en .com) :
    rares, ils se vendent au-dessus des paliers (ev.fr, 120 000 euros en 2022) ; pas de plafond."""
    tier = _tier(resale)
    floor = _band_at(tier + 1, tld)[0]
    uncapped = 0 < len(label) <= SHORT_UNCAPPED_BY_TLD.get(tld, SHORT_UNCAPPED)
    ceiling = PRICE_STEPS[-1] if uncapped else _band_at(tier - 1, tld)[1]
    return floor, ceiling



def default_prices(item: dict) -> dict:
    """Prix par défaut d'un avis v5 dont seules les sous-notes sont données : le palier de la note de revente,
    placé plus haut quand marché et acheteurs sont forts ; revente rapide à 40 % du prix final au plus.
    Sert au format compact de la tâche de notation (prix « - »), qui peut toujours donner les siens."""
    resale = derive(item)["resale"]
    label, _, tld = item["name"].rpartition(".")
    low, high = price_band(resale, tld)
    floor, ceiling = price_limits(resale, tld, label)
    strength = item["marche"] + item["acheteurs"]
    weight = 0.25 if strength <= 4 else 0.5 if strength <= 10 else 0.8
    final_low, final_high = low, max(low, _step_down(low + (high - low) * weight))
    if resale < 10:
        final_low, final_high = 0, 50 if item["acheteurs"] >= 2 else 0
    final_low = max(final_low, floor)
    final_high = min(max(final_high, final_low), ceiling)
    quick_high = _step_down(final_high * QUICK_SHARE)
    quick_low = _step_down(quick_high * QUICK_SHARE)
    if resale < 25 or quick_high < 50:
        quick_low = quick_high = 0
    return {"quick_low": quick_low, "quick_high": quick_high, "final_low": final_low, "final_high": final_high}

def fit_prices(item: dict) -> dict:
    """Prix d'un avis v5 ramenés sur l'échelle et dans le palier de sa note de revente, sans toucher aux
    sous-notes : l'escalade garde ainsi chaque correcteur au lieu de le faire renoter pour un prix (coûteux)."""
    try:
        resale = derive(item)["resale"]
        label, _, tld = item["name"].rpartition(".")
        floor, ceiling = price_limits(resale, tld, label)
        nearest = lambda v: min(PRICE_STEPS, key=lambda p: abs(p - v))  # noqa: E731
        final_low = min(max(nearest(item["final_low"]), floor), ceiling)
        final_high = min(max(nearest(item["final_high"]), final_low), ceiling)
        quick_high = min(nearest(item["quick_high"]), _step_up(final_high * QUICK_SHARE))
        quick_low = min(nearest(item["quick_low"]), quick_high, final_low)
    except (KeyError, TypeError, ValueError):
        return item  # avis mal formé : le contrôle dira pourquoi
    return {**item, "quick_low": quick_low, "quick_high": quick_high, "final_low": final_low, "final_high": final_high}


def check_v5(item, known) -> str | None:
    """Raison du refus d'un avis v5, ou None. Même règle pour la tâche de notation et pour le NAS."""
    if not isinstance(item, dict):
        return "pas un objet"
    keys = set(item) - set(OPTIONAL_V5)
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
    # Le prix suit la note de revente : elle tient compte du marché et des acheteurs, l'exploitation non.
    resale = derive(item)["resale"]
    label, _, tld = name.rpartition(".")
    floor, ceiling = price_limits(resale, tld, label)
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

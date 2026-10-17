"""Avis IA sur les noms de domaine : le NAS publie la liste à noter, une tâche planifiée la note,
le NAS réimporte.

L'avis porte sur le nom complet (velo.fr, ciel.org) : l'extension change l'usage et le marché.
Notes sur 100 : exploitation (projet, marque, site) et revente. Deux fourchettes de prix en euros :
revente rapide (à un investisseur, en quelques semaines) et utilisateur final (le bon acheteur,
avec de la patience). Ce sont des avis, pas des cotes du marché.
"""

import hashlib
import hmac
import json
import secrets

from . import rdapinfo, value, wordlists
from .checker import iso, utcnow

# Version de la consigne : la monter fait renoter tous les noms, petit à petit.
VERSION = 5  # 5 : grille détaillée (sous-notes), notes et verdict calculés, prix par paliers
MAX_PENDING = 5000
MAX_RESULTS = 5000
MAX_PRICE = 10_000_000

# Codes d'état de l'Explorer (explore.py) : libres ou bientôt libres d'abord, enregistrés en dernier.
FREE_SOON = (5, 4)        # libre vérifié, libération à venir
TAKEN_MIN_BASE = 40       # noms enregistrés notés seulement à partir de ce score de base
MAYBE_FREE = (2, 3)       # supprimé non repris, jamais vu
TAKEN = (1, 6, 4)         # enregistré, suspendu, libération à venir (pris ailleurs = demande)

from .ai_format import (COMMENT_MAX, CRITERIA_V5, FIELDS, FIELDS_V5, FORMAT_V5, PRICE_STEPS, check,  # noqa: F401  (réexportés)
                        check_any, derive, is_v5)

FORMAT = FORMAT_V5

# Consigne remise avec la liste : la tâche de notation la suit telle quelle.
GUIDE = """Tu notes des noms de domaine pour un investisseur francophone qui achète des noms libres pour les exploiter ou les revendre.

Pour chaque nom : neuf sous-notes de 0 à 10, deux fourchettes de prix et une raison. Les notes sur 100 (exploitation, revente) et le verdict (À prendre, À surveiller, À laisser) sont calculés par l'appli à partir de tes sous-notes : ne les donne pas, mais vise juste, ils en découlent directement.

Sous-notes, entiers de 0 à 10, avec la même grille du premier au dernier nom :
- oral (prononciation) : 10 se dit d'une seule façon sans hésiter (velo, mai) ; 5 une hésitation (oye) ; 0 imprononçable (qsd). Un .fr se juge en français ; .com, .io, .ai en anglais ; un nom qui se dit pareil dans les deux langues prend 1 point de plus.
- radio (entendu une fois, on sait l'écrire) : 10 une seule orthographe (velo) ; 5 une ambiguïté (lea ou léa, kado ou cado) ; 0 chiffres mêlés aux lettres, tiret ou plusieurs ambiguïtés.
- memo (mémorisation) : 10 deux à quatre lettres ou mot très courant ; 7 cinq à sept lettres claires ; 4 huit à douze lettres ; 2 plus long ou deux mots mal liés.
- sens : 10 mot très courant, clair et positif (velo, maison) ; 7 mot connu moins courant ou nom propre très connu ; 4 sens faible, rare ou technique ; 0 aucun sens (kalo, qsd).
- marque (potentiel de marque) : 10 court, évocateur, utilisable par beaucoup d'activités ; 7 invention élégante ou mot fort ; 4 descriptif plat ; 1 impossible à porter.
- marche (valeur commerciale) : 10 secteur qui paie avec intention d'achat (crédit, assurance, immobilier, avocat, voyage) ; 6 thème de consommation courant ; 3 loisir ou thème étroit ; 0 aucun marché.
- ext (adéquation à l'extension) : en .fr, 10 pour un mot français, 6 pour un mot anglais compris en France, 3 sans lien avec la France ; en .com, 10 pour un mot anglais ou international ; en .io et .ai, 10 pour un mot tech ; en .org, 10 pour une cause ou une association, 4 pour un mot commercial.
- acheteurs : 10 des centaines d'entreprises plausibles ; 5 quelques dizaines ; 1 un seul acheteur imaginable ; 0 aucun.
- risque : 0 aucun ; 3 léger (homonyme d'une petite marque) ; 5 marque connue dans un secteur ; 8 marque célèbre, sens vulgaire, négatif ou gênant dans une langue courante ; 10 injurieux ou interdit.

Prix en euros, uniquement ces valeurs : 0, 50, 100, 150, 200, 300, 500, 800, 1000, 1500, 2000, 3000, 5000, 8000, 10000, 15000, 20000, 30000, 50000, 80000, 100000, 150000, 200000, 300000, 500000, 1000000.
- final_low, final_high : prix pour un utilisateur final, avec de la patience (mois ou années).
- quick_low, quick_high : revente rapide à un investisseur, 40 % du prix final au plus ; 0 et 0 si personne ne l'achèterait vite.
- Bas inférieur ou égal au haut.
- Grille : le prix final suit la meilleure des deux notes calculées. Pour un .fr : 90 et plus, 10 000 à 100 000 ; 80, 5 000 à 30 000 ; 70, 3 000 à 15 000 ; 60, 1 500 à 8 000 ; 50, 1 000 à 5 000 ; 40, 500 à 2 000 ; 30, 200 à 1 000 ; 20, 100 à 800 ; 10, 50 à 300 ; en dessous, 0 à 100.
- Autres extensions, mêmes paliers multipliés par : .com 1,5 ; .io et .ai 0,8 ; .co 0,6 ; .org, .app et .dev 0,5 ; .net 0,4 ; .info, .biz et .xyz 0,25 ; autres 0,4.
- Un palier de tolérance au-dessus ou au-dessous est accepté. Dans le palier, monte si marche et acheteurs sont forts, descends sinon.
- Exceptions sans plafond de palier : noms de 1 ou 2 caractères (jusqu'à 3 en .com, marché porté par la rareté), et noms dont la meilleure note atteint 90 (mots génériques majeurs). Appuie-toi alors sur les repères publics ci-dessous.
- Repères publics, ventes exceptionnelles et non la norme : credit.fr 587 500 euros (2010), auto.fr 100 000 euros (2008), ev.fr 120 000 euros (2022). Prix médian des ventes sur Sedo en 2012, toutes extensions : 461 euros.

Raison (comment) : exactement deux phrases sur ce gabarit : « Raison. Acheteur type : qui. »
- raison : ce qui fait la valeur ou ce qui la freine (sens, sonorité, longueur, marché, risque de marque) ;
- acheteur type : à qui le revendre ou qui l'exploiterait ;
- 180 caractères au plus, en français, sans emoji ni tiret long, sans verdict, sans notes ni prix.

Second passage : un nom qui porte « review » a déjà un avis de ta part, très éloigné de l'estimation calculée par l'appli (previous_note : ta meilleure note d'alors ; score_estimate : l'estimation). Renote-le à froid avec la même grille, sans te caler sur l'un ou l'autre chiffre : garde ton avis s'il tient, corrige-le s'il était faux.

Crédibilité avant tout :
- La plupart des noms valent peu. Des 9 et des 10 partout sont réservés aux meilleurs mots du marché.
- Le statut du nom (libre, libération à venir, jamais enregistré, supprimé, enregistré) ne change pas sa valeur : juge le nom comme s'il était libre.
- N'invente rien : un nom sans sens connu a sens 0, même s'il sonne bien.

Format : une liste JSON d'objets avec exactement ces clés : name, oral, radio, memo, sens, marque, marche, ext, acheteurs, risque, quick_low, quick_high, final_low, final_high, comment ; name est le nom complet reçu, tel quel ; un objet par nom, dans l'ordre reçu, sans texte autour.

Exemples de référence :
{"name": "velo.fr", "oral": 10, "radio": 10, "memo": 10, "sens": 10, "marque": 9, "marche": 8, "ext": 10, "acheteurs": 9, "risque": 0, "quick_low": 3000, "quick_high": 8000, "final_low": 15000, "final_high": 50000, "comment": "Mot que tout le monde tape, rare en 4 lettres. Acheteur type : enseigne, marketplace ou start-up vélo."}
  (calculé : exploitation 95, revente 90, À prendre)
{"name": "femmes.fr", "oral": 9, "radio": 8, "memo": 8, "sens": 9, "marque": 7, "marche": 8, "ext": 9, "acheteurs": 8, "risque": 0, "quick_low": 1000, "quick_high": 3000, "final_low": 5000, "final_high": 15000, "comment": "Thème large et porteur, se lit bien au pluriel. Acheteur type : média, marque de mode ou de santé."}
  (calculé : exploitation 75, revente 75, À prendre)
{"name": "mai.fr", "oral": 9, "radio": 7, "memo": 9, "sens": 7, "marque": 8, "marche": 6, "ext": 9, "acheteurs": 8, "risque": 0, "quick_low": 800, "quick_high": 2000, "final_low": 3000, "final_high": 10000, "comment": "Trois lettres et un mot du calendrier, rare en .fr. Acheteur type : marque courte ou agence événementielle."}
  (calculé : exploitation 70, revente 70, À prendre)
{"name": "kalo.fr", "oral": 9, "radio": 8, "memo": 9, "sens": 0, "marque": 7, "marche": 2, "ext": 6, "acheteurs": 4, "risque": 0, "quick_low": 100, "quick_high": 300, "final_low": 800, "final_high": 2000, "comment": "Quatre lettres qui se disent d'une traite, sans sens, bon socle de marque inventée. Acheteur type : créateur de marque."}
  (calculé : exploitation 45, revente 40, À surveiller)
{"name": "lea.fr", "oral": 9, "radio": 6, "memo": 8, "sens": 5, "marque": 6, "marche": 3, "ext": 8, "acheteurs": 5, "risque": 0, "quick_low": 200, "quick_high": 500, "final_low": 1000, "final_high": 3000, "comment": "Prénom court très donné, Léa ou Lea à l'oral. Acheteur type : parent, marque cosmétique ou enfant."}
  (calculé : exploitation 55, revente 50, À surveiller)
{"name": "monassurance.fr", "oral": 8, "radio": 8, "memo": 6, "sens": 8, "marque": 4, "marche": 9, "ext": 9, "acheteurs": 6, "risque": 1, "quick_low": 500, "quick_high": 1500, "final_low": 3000, "final_high": 8000, "comment": "Secteur qui paie, intention claire, mais formule générique. Acheteur type : courtier ou comparateur d'assurance."}
  (calculé : exploitation 55, revente 65, À prendre)
{"name": "forumdomotique.fr", "oral": 7, "radio": 8, "memo": 5, "sens": 8, "marque": 3, "marche": 5, "ext": 9, "acheteurs": 3, "risque": 0, "quick_low": 100, "quick_high": 300, "final_low": 500, "final_high": 2000, "comment": "Usage évident pour un forum, peu d'acheteurs hors de ce projet. Acheteur type : éditeur de site domotique."}
  (calculé : exploitation 50, revente 45, À surveiller)
{"name": "premiere.fr", "oral": 9, "radio": 6, "memo": 7, "sens": 8, "marque": 6, "marche": 5, "ext": 8, "acheteurs": 5, "risque": 5, "quick_low": 150, "quick_high": 500, "final_low": 1000, "final_high": 3000, "comment": "Mot fort, mais le magazine Première peut s'y opposer. Acheteur type : organisateur d'événements."}
  (calculé : exploitation 50, revente 40, À surveiller)
{"name": "stack.io", "oral": 9, "radio": 9, "memo": 9, "sens": 8, "marque": 7, "marche": 6, "ext": 9, "acheteurs": 6, "risque": 3, "quick_low": 500, "quick_high": 1500, "final_low": 3000, "final_high": 10000, "comment": "Mot tech court et clair, très utilisé par les éditeurs de logiciels. Acheteur type : start-up d'outils pour développeurs."}
  (calculé : exploitation 65, revente 60, À prendre)
{"name": "ciel.org", "oral": 9, "radio": 8, "memo": 8, "sens": 8, "marque": 4, "marche": 2, "ext": 3, "acheteurs": 3, "risque": 0, "quick_low": 100, "quick_high": 300, "final_low": 800, "final_high": 3000, "comment": "Mot simple et poétique, le .org limite les acheteurs. Acheteur type : association ou projet culturel."}
  (calculé : exploitation 55, revente 40, À surveiller)
{"name": "sommes.fr", "oral": 8, "radio": 5, "memo": 5, "sens": 3, "marque": 2, "marche": 2, "ext": 7, "acheteurs": 2, "risque": 1, "quick_low": 0, "quick_high": 100, "final_low": 200, "final_high": 800, "comment": "Forme de « être » ou pluriel de « somme », sens flou. Acheteur type : rare, à la marge en finance."}
  (calculé : exploitation 25, revente 25, À laisser)
{"name": "382.fr", "oral": 3, "radio": 6, "memo": 4, "sens": 0, "marque": 1, "marche": 1, "ext": 5, "acheteurs": 2, "risque": 0, "quick_low": 0, "quick_high": 0, "final_low": 100, "final_high": 300, "comment": "Trois chiffres sans motif, se dit mal et s'oublie. Acheteur type : entreprise qui porte ce numéro."}
  (calculé : exploitation 10, revente 15, À laisser)
{"name": "qsd.fr", "oral": 1, "radio": 4, "memo": 4, "sens": 0, "marque": 1, "marche": 1, "ext": 5, "acheteurs": 3, "risque": 0, "quick_low": 0, "quick_high": 0, "final_low": 100, "final_high": 300, "comment": "Suite imprononçable sans sens, seule la brièveté compte. Acheteur type : entreprise au sigle QSD."}
  (calculé : exploitation 10, revente 15, À laisser)
{"name": "puer.fr", "oral": 8, "radio": 7, "memo": 7, "sens": 6, "marque": 1, "marche": 1, "ext": 6, "acheteurs": 1, "risque": 8, "quick_low": 0, "quick_high": 0, "final_low": 50, "final_high": 200, "comment": "Verbe à l'image repoussante, aucune marque ne voudra s'y associer. Acheteur type : aucun crédible."}
  (calculé : exploitation 15, revente 5, À laisser)"""


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def new_token(store) -> str:
    """Jeton d'accès de la tâche de notation : montré une fois, seule son empreinte est gardée."""
    token = "dw_" + secrets.token_urlsafe(32)
    store.set("ai_token_hash", hash_token(token))
    return token


def token_ok(store, header: str | None) -> bool:
    expected = store.get("ai_token_hash")
    if not expected or not header or not header.startswith("Bearer "):
        return False
    return hmac.compare_digest(hash_token(header[7:].strip()), expected)


def public(row) -> dict:
    found = {k: row[k] for k in ("exploit", "resale", "quick_low", "quick_high", "final_low", "final_high",
                                 "comment", "rated_at")}
    grid = row["grid"] if "grid" in row.keys() else None
    found["grid"] = json.loads(grid) if grid else None
    return found


def load(conn, names: list[str]) -> dict[str, dict]:
    found = {}
    for chunk in range(0, len(names), 900):
        part = names[chunk:chunk + 900]
        rows = conn.execute(f"SELECT * FROM domain_ai WHERE name IN ({','.join('?' * len(part))})", part).fetchall()
        found.update({r["name"]: public(r) for r in rows})
    return found


def last_rated(conn) -> str | None:
    """Dernier avis reçu, toutes versions de consigne : après un changement de version, la nuit
    continue de noter et ce n'est pas une panne."""
    return conn.execute("SELECT MAX(rated_at) FROM domain_ai").fetchone()[0]


def stats(explorer) -> dict:
    rated = explorer.conn.execute("SELECT COUNT(*), MAX(rated_at) FROM domain_ai WHERE version >= ?", (VERSION,)).fetchone()
    total = sum(explorer.universe.good) * len([t for t in explorer.tlds() if t in AI_TLDS])
    return {"rated": rated[0], "last": rated[1], "total": total, "pending": max(0, total - rated[0]),
            "token_set": bool(explorer.fr.store.get("ai_token_hash"))}


# Extensions notées : .biz, .info et les autres valent trop peu pour dépenser la notation de nuit.
AI_TLDS = ("fr", "com", "org")
# Marché francophone : à valeur égale, un .fr ou un .com passe avant un .org.
TLD_WEIGHT = {"fr": 1.0, "com": 1.0, "org": 0.8}
OTHER_TLD_WEIGHT = 0.6


def _positions(data: bytearray, codes) -> list[int]:
    """Positions des noms dans ces états, par saut direct (pas de boucle sur 1,9 million d'octets)."""
    found = []
    for code in codes:
        at = data.find(code)
        while at != -1:
            found.append(at)
            at = data.find(code, at + 1)
    return found


PER_NIGHT = 1000   # noms notés par la tâche de nuit
HORIZON_NIGHTS = 30
_eta_cache: dict = {}


# Second passage : chaque nuit, les avis v5 qui s'écartent le plus de l'estimation du score sont
# renotés à froid, une seule fois (reviewed_at), en tête de lot.
REVIEW_PER_NIGHT = 50
REVIEW_GAP = 25  # écart minimal, en points de note, entre l'avis et l'estimation


def to_review(explorer, limit: int = REVIEW_PER_NIGHT) -> list[tuple[str, int, int]]:
    """(nom, meilleure note de l'avis, estimation du score) des avis à revoir, les plus gros écarts d'abord."""
    from . import quality

    found = []
    for name, exploit, resale in explorer.conn.execute(
            "SELECT name, exploit, resale FROM domain_ai WHERE version >= ? AND reviewed_at IS NULL", (VERSION,)):
        label, _, tld = name.rpartition(".")
        estimate = round(quality.predict(label, tld)[0])
        best = max(exploit, resale)
        if abs(best - estimate) >= REVIEW_GAP:
            found.append((abs(best - estimate), name, best, estimate))
    return [(name, best, estimate) for _, name, best, estimate in sorted(found, reverse=True)[:limit]]


def pending(explorer, limit: int) -> list[dict]:
    """Noms complets sans avis à jour, dans l'ordre où ils servent ; les avis à revoir d'abord."""
    review = to_review(explorer, min(REVIEW_PER_NIGHT, limit))
    picked, data = _queue(explorer, limit - len(review)) if limit > len(review) else ([], {t: explorer.status_bytes(t) for t in explorer.tlds()})
    u = explorer.universe
    head = []
    for name, best, estimate in review:
        label, _, tld = name.rpartition(".")
        i = u.index(label)
        if i is not None and tld in data:
            head.append(((i, label, tld), {"previous_note": best, "score_estimate": estimate}))
    items = _describe(explorer, [p for p, _ in head] + picked, data)
    for item, (_, review_info) in zip(items, head):
        item["review"] = review_info
    return items


def queue_position(explorer, name: str) -> int | None:
    """Place d'un nom dans la file de notation (0 : premier noté la nuit prochaine), None au-delà
    de l'horizon ou hors file. File recalculée au plus une fois par heure."""
    import time

    key = explorer.generation
    cached = _eta_cache.get("positions")
    if not cached or cached[0] != key or time.monotonic() - cached[1] > 3600:
        picked, _ = _queue(explorer, PER_NIGHT * HORIZON_NIGHTS)
        cached = (key, time.monotonic(), {f"{label}.{tld}": n for n, (_, label, tld) in enumerate(picked)})
        _eta_cache["positions"] = cached
    return cached[2].get(name)


def _queue(explorer, limit: int):
    """1. libres ou bientôt libres, du meilleur score au moins bon (extension pondérée) ;
    2. probablement libres, puis 3. enregistrés, par score de base et extension.
    Ni les noms réservés ni ceux encore masqués (liste pas encore cherchée dans la zone)."""
    u = explorer.universe
    u.buckets()  # construit u.good
    done = {r[0] for r in explorer.conn.execute("SELECT name FROM domain_ai WHERE version >= ?", (VERSION,))}
    tlds = sorted((t for t in explorer.tlds() if t in AI_TLDS), key=lambda t: -TLD_WEIGHT.get(t, OTHER_TLD_WEIGHT))
    # Toutes les extensions pour « pris ailleurs » (signe de demande), même celles qu'on ne note pas.
    data = {t: explorer.status_bytes(t) for t in explorer.tlds()}
    picked: list[tuple[int, str, str]] = []
    seen = set()

    def take(i: int, label: str, tld: str) -> bool:
        name = f"{label}.{tld}"
        if name in done or name in seen:
            return False
        seen.add(name)
        picked.append((i, label, tld))
        return len(picked) >= limit

    # 1. Peu nombreux : on les classe tous par score complet (demande, historique, SEO compris).
    soon = []
    for tld in tlds:
        ctx = explorer._bonus_context(tld)
        for i in _positions(data[tld], FREE_SOON):
            if u.good[i]:
                label = u.label(i)
                weight = TLD_WEIGHT.get(tld, OTHER_TLD_WEIGHT)
                soon.append((explorer._value(tld, i, label, ctx)["score"] * weight, i, label, tld))
    for _, i, label, tld in sorted(soon, key=lambda x: (-x[0], x[2], x[3])):
        if take(i, label, tld):
            return picked, data
    # 2 et 3. Trop nombreux pour tout classer : par paliers d'extension (.fr et .com, puis .org,
    # puis le reste), et dans chaque palier par score de base. Un nom de 1 ou 2 caractères jamais
    # vu n'est pas « probablement libre » : les registres réservent ces formats. On ne le note pas.
    from .explore import ABSENT, INVALID, PREMIUM, RESERVED  # import local : explore importe ce module

    tiers: dict[float, list[str]] = {}
    for tld in tlds:
        tiers.setdefault(TLD_WEIGHT.get(tld, OTHER_TLD_WEIGHT), []).append(tld)
    for group in (MAYBE_FREE, None):
        for weight in sorted(tiers, reverse=True):
            for score in range(70, -1, -1):
                if group is None and score < TAKEN_MIN_BASE:
                    break  # noms pris : seulement les meilleurs, la notation sert d'abord aux noms à saisir
                for i in u.buckets()[score]:
                    if not u.good[i]:
                        continue
                    for tld in tiers[weight]:
                        code = data[tld][i]
                        if (group is not None and code not in group) or code in (INVALID, RESERVED, PREMIUM):
                            continue
                        label = u.label(i)
                        if code == ABSENT and len(label) <= 2:
                            continue
                        if take(i, label, tld):
                            return picked, data
    return picked, data


def _describe(explorer, picked, data) -> list[dict]:
    from .explore import ABSENT, ABSENT_FR, ABSENT_ZONE, LABELS  # import local : explore importe ce module

    u = explorer.universe
    ranks = wordlists.ranks()
    natures = wordlists.natures()
    names = [f"{label}.{tld}" for _, label, tld in picked]
    seo = explorer._seo_rows(names) if names else {}
    fr = {}
    labels = sorted({label for _, label, tld in picked if tld == "fr"})
    for chunk in range(0, len(labels), 900):
        part = labels[chunk:chunk + 900]
        query = f"SELECT label, created_on, removed_on FROM fr_names WHERE label IN ({','.join('?' * len(part))})"
        fr.update({r["label"]: r for r in explorer.conn.execute(query, part)})
    out = []
    for (i, label, tld), name in zip(picked, names):
        code = data[tld][i]
        item = {
            "name": name,
            "status": (ABSENT_FR if tld == "fr" else ABSENT_ZONE) if code == ABSENT else LABELS.get(code, ""),
            "categories": u.categories(label),
            "natures": sorted(natures.get(label, ())),
            "ranks": {k: r[label] for k, r in ranks.items() if label in r},
            "taken_elsewhere": [t for t in data if t != tld and data[t][i] in TAKEN],
            "base": value.base(label),
        }
        row = fr.get(label) if tld == "fr" else None
        if row is not None:
            item["fr_history"] = {"created_on": row["created_on"], "removed_on": row["removed_on"]}
        s = seo.get(name)
        if s is not None:
            item["seo"] = {"pagerank": s["pr"], "archived_since": s["wb_first"], "archived_years": s["wb_years"]}
        out.append(item)
    return out


def save(conn, results: list, known) -> dict:
    """Enregistre les avis au bon format ; les autres sont rejetés avec la raison, sans bloquer le lot."""
    now = iso(utcnow())
    saved, rejected, names = 0, [], []
    for item in results[:MAX_RESULTS]:
        reason = check_any(item, known)
        if reason:
            name = item.get("name") if isinstance(item, dict) else None
            rejected.append({"name": str(name or ""), "reason": reason})
            continue
        if is_v5(item):
            # Notes et verdict calculés depuis les sous-notes : ils ne peuvent pas se contredire.
            notes = derive(item)
            row = {**item, "exploit": notes["exploit"], "resale": notes["resale"],
                   "comment": f"{notes['verdict']}. {item['comment']}"}
            grid, version = json.dumps({k: item[k] for k in CRITERIA_V5}), VERSION
        else:
            row, grid, version = item, None, 4  # avis à l'ancien format, arrivé pendant la transition
        # Un nom déjà noté en v5 qui revient : c'est le second passage, il ne repassera plus.
        again = conn.execute("SELECT 1 FROM domain_ai WHERE name = ? AND version >= ?", (row["name"], VERSION)).fetchone()
        conn.execute(
            """INSERT INTO domain_ai (name, exploit, resale, quick_low, quick_high, final_low, final_high,
                                      comment, rated_at, version, grid, reviewed_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(name) DO UPDATE SET exploit = excluded.exploit, resale = excluded.resale,
                 quick_low = excluded.quick_low, quick_high = excluded.quick_high,
                 final_low = excluded.final_low, final_high = excluded.final_high,
                 comment = excluded.comment, rated_at = excluded.rated_at, version = excluded.version,
                 grid = excluded.grid, reviewed_at = COALESCE(excluded.reviewed_at, domain_ai.reviewed_at)""",
            (*(row[k] for k in FIELDS), now, version, grid, now if again and version >= VERSION else None),
        )
        saved += 1
        names.append(item["name"])
    return {"saved": saved, "rejected": rejected, "names": names}


def known_name(explorer):
    """Un nom de l'univers, sur une extension suivie."""
    u = explorer.universe
    tlds = set(explorer.tlds())

    def exists(name: str) -> bool:
        label, _, tld = name.partition(".")
        return bool(label) and tld in tlds and u.index(label) is not None

    return exists


# --- Analyses demandées depuis la fiche -------------------------------------------------

def request(conn, name: str) -> dict:
    """Demande (ou redemande) l'analyse d'un nom : la tâche horaire la traite."""
    conn.execute(
        """INSERT INTO ai_requests (name, requested_at) VALUES (?, ?)
           ON CONFLICT(name) DO UPDATE SET requested_at = excluded.requested_at, done_at = NULL, seen_at = NULL""",
        (name, iso(utcnow())),
    )
    return request_state(conn, name)


def request_state(conn, name: str) -> dict | None:
    row = conn.execute("SELECT * FROM ai_requests WHERE name = ?", (name,)).fetchone()
    return dict(row) if row else None


def requests(conn) -> dict:
    """En attente, et traitées pas encore vues : la cloche de l'appli."""
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM ai_requests WHERE done_at IS NULL OR seen_at IS NULL ORDER BY requested_at DESC")]
    done = [r for r in rows if r["done_at"]]
    opinions = load(conn, [r["name"] for r in done])
    for r in done:
        r["comment"] = (opinions.get(r["name"]) or {}).get("comment", "")
    return {"pending": [r for r in rows if not r["done_at"]], "done": done}


def seen(conn, name: str) -> None:
    conn.execute("UPDATE ai_requests SET seen_at = ? WHERE name = ? AND done_at IS NOT NULL AND seen_at IS NULL",
                 (iso(utcnow()), name))


def requested(explorer) -> list[dict]:
    """Noms demandés à analyser, décrits comme ceux de la file de nuit quand ils y sont connus."""
    names = [r[0] for r in explorer.conn.execute(
        "SELECT name FROM ai_requests WHERE done_at IS NULL ORDER BY requested_at")]
    u = explorer.universe
    tlds = explorer.tlds()
    data = {t: explorer.status_bytes(t) for t in tlds}
    rdap = rdapinfo.load(explorer.conn, names)
    out = []
    for name in names:
        label, _, tld = name.partition(".")
        i = u.index(label)
        if i is not None and tld in data:
            item = _describe(explorer, [(i, label, tld)], data)[0]
        else:
            # Hors des listes ou extension sans zone : on dit ce qu'on sait, sans inventer.
            item = {"name": name, "status": "hors des listes suivies", "categories": [], "natures": [], "ranks": {},
                    "taken_elsewhere": [t for t in data if i is not None and data[t][i] in TAKEN], "base": value.base(label)}
        r = rdap.get(name)
        if r is not None:
            item["rdap"] = {k: r[k] for k in ("at", "available", "statuses", "registered_at", "expires_at")}
        item["requested"] = True
        out.append(item)
    return out


def finish(conn, names: list[str]) -> list[str]:
    """Marque traitées les demandes dont l'avis vient d'arriver ; renvoie ces noms."""
    now = iso(utcnow())
    done = []
    for name in names:
        cur = conn.execute("UPDATE ai_requests SET done_at = ? WHERE name = ? AND done_at IS NULL", (now, name))
        if cur.rowcount:
            done.append(name)
    return done

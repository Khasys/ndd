"""Avis IA sur les noms de domaine : le NAS publie la liste à noter, une tâche planifiée la note,
le NAS réimporte.

L'avis porte sur le nom complet (velo.fr, ciel.org) : l'extension change l'usage et le marché.
Notes sur 100 : exploitation (projet, marque, site) et revente. Deux fourchettes de prix en euros :
revente rapide (à un investisseur, en quelques semaines) et utilisateur final (le bon acheteur,
avec de la patience). Ce sont des avis, pas des cotes du marché.
"""

import hashlib
import heapq
import hmac
import json
import secrets
import threading
from datetime import timedelta

from . import gandi, rdapinfo, value, wordlists
from .checker import iso, utcnow

# Version de la consigne : la monter fait renoter tous les noms, petit à petit.
VERSION = 5  # 5 : grille détaillée (sous-notes), notes et verdict calculés, prix par paliers
MAX_PENDING = 5000
MAX_RESULTS = 5000

# Codes d'état de l'Explorer (explore.py) : libres ou bientôt libres d'abord, enregistrés en dernier.
FREE_SOON = (5, 4)        # libre vérifié, libération à venir
TAKEN_MIN_BASE = 40       # noms enregistrés notés seulement à partir de ce score de base
MAYBE_FREE = (2, 3, 9)    # supprimé non repris, jamais vu, libre selon le seul WHOIS
TAKEN = (1, 6, 4, 11)     # enregistré, suspendu, libération à venir, pris d'après Gandi (pris ailleurs = demande)
UNCHECKED = 10            # extension par Gandi seul, pas encore demandé : rien n'est connu, pas noté
# Nouvelles listes, en tête des noms probablement libres ou libres : termes stars, mots-clés et marques mêlés par
# rang, puis métier + ville.
FIRST_LISTS = (("premium", "keyword", "brand"), ("local",))

from .ai_format import (COMMENT_MAX, CRITERIA_V5, FIELDS, FIELDS_V5, FORMAT_V5, PRICE_STEPS, check,  # noqa: F401  (réexportés)
                        check_any, derive, is_v5)

FORMAT = FORMAT_V5

# Consigne remise avec la liste : la tâche de notation la suit telle quelle.
GUIDE = """Tu notes des noms de domaine pour un investisseur francophone qui achète des noms libres pour les exploiter ou les revendre.

Pour chaque nom : neuf sous-notes de 0 à 10, deux fourchettes de prix et une raison. Les notes sur 100 (exploitation, revente) et le verdict (À prendre, À surveiller, À laisser) sont calculés par l'appli à partir de tes sous-notes : ne les donne pas, mais vise juste, ils en découlent directement. « À prendre » demande une note de 60, un risque de 4 au plus et au moins 4 en acheteurs.

Sous-notes, entiers de 0 à 10, avec la même grille du premier au dernier nom :
- oral (prononciation) : 10 se dit d'une seule façon sans hésiter (velo, mai) ; 5 une hésitation (oye) ; 0 imprononçable (qsd). Un .fr ou un .com se juge dans la langue du mot (un mot français en français, un mot anglais en anglais) ; .io et .ai en anglais ; un nom qui se dit pareil dans les deux langues prend 1 point de plus.
- radio (entendu une fois, on sait l'écrire) : 10 une seule orthographe (velo) ; 5 une ambiguïté (lea ou léa, kado ou cado) ; 0 chiffres mêlés aux lettres, tiret ou plusieurs ambiguïtés.
- memo (mémorisation) : 10 deux à quatre lettres ou mot très courant ; 7 cinq à sept lettres claires ; 4 huit à douze lettres ; 2 plus long ou deux mots mal liés.
- sens : 10 mot très courant, clair et positif (velo, maison) ; 7 mot connu moins courant ou nom propre très connu ; 4 sens faible, rare ou technique ; 0 aucun sens (kalo, qsd).
- marque (potentiel de marque) : 10 court, évocateur, utilisable par beaucoup d'activités ; 7 invention élégante ou mot fort ; 4 descriptif plat ; 1 impossible à porter.
- marche (valeur commerciale) : 10 secteur qui paie avec intention d'achat (crédit, assurance, immobilier, avocat, voyage) ; 6 thème de consommation courant ; 3 loisir ou thème étroit ; 0 aucun marché.
- ext (adéquation à l'extension) : en .fr, 10 pour un mot français, 6 pour un mot anglais compris en France, 3 sans lien avec la France ; en .com, 10 pour un mot ou une expression qui a du sens, en français comme en anglais, 7 pour un nom inventé ; en .io et .ai, 10 pour un mot tech ; en .org, 10 pour une cause ou une association, 4 pour un mot commercial.
- acheteurs : 10 des centaines d'entreprises plausibles ; 5 quelques dizaines ; 1 un seul acheteur imaginable ; 0 aucun.
- risque : 0 aucun ; 3 léger (homonyme d'une petite marque) ; 5 marque connue dans un secteur ; 8 marque célèbre, sens vulgaire, négatif ou gênant dans une langue courante ; 10 injurieux ou interdit.

Prix en euros, uniquement ces valeurs : 0, 50, 100, 150, 200, 300, 500, 800, 1000, 1500, 2000, 3000, 5000, 8000, 10000, 15000, 20000, 30000, 50000, 80000, 100000, 150000, 200000, 300000, 500000, 1000000.
- final_low, final_high : prix pour un utilisateur final, avec de la patience (mois ou années).
- quick_low, quick_high : revente rapide à un investisseur, 40 % du prix final au plus ; 0 et 0 si personne ne l'achèterait vite.
- Bas inférieur ou égal au haut.
- Grille : le prix final suit la note de revente calculée (elle tient compte du marché et des acheteurs). Pour un .fr : 95 et plus, 20 000 à 300 000 ; 90, 10 000 à 150 000 ; 85, 8 000 à 80 000 ; 80, 5 000 à 30 000 ; 70, 3 000 à 15 000 ; 60, 1 500 à 8 000 ; 50, 1 000 à 5 000 ; 40, 500 à 2 000 ; 30, 200 à 1 000 ; 20, 100 à 800 ; 10, 50 à 300 ; en dessous, 0 à 100.
- Autres extensions, mêmes paliers multipliés par : .com 1,5 ; .io et .ai 0,8 ; .co 0,6 ; .org, .app et .dev 0,5 ; .net 0,4 ; .info, .biz et .xyz 0,25 ; autres 0,4.
- Un palier de tolérance au-dessus ou au-dessous est accepté. Dans le palier, monte si marche et acheteurs sont forts, descends sinon.
- Exception sans plafond de palier : noms de 1 ou 2 caractères (jusqu'à 3 en .com, marché porté par la rareté). Appuie-toi alors sur les repères publics ci-dessous. Les meilleurs mots du marché restent dans les paliers de 90 et 95 : les ventes au-delà sont l'exception.
- Repères publics, ventes exceptionnelles et non la norme : credit.fr 587 500 euros (2010), auto.fr 100 000 euros (2008), ev.fr 120 000 euros (2022). Prix médian des ventes sur Sedo en 2012, toutes extensions : 461 euros.

Raison (comment) : exactement deux phrases sur ce gabarit : « Raison. Acheteur type : qui. » Elle doit apprendre quelque chose que les sous-notes ne disent pas.
- raison : un fait précis sur ce nom, pas un résumé des notes. Par exemple l'usage concret (le site qu'on en ferait), ce qui fait payer dans ce secteur (panier, intention d'achat, saison, réglementation), une marque précise qui gêne, un nom voisin connu, ce qui le distingue des noms proches ;
- ne te contente pas de « long », « descriptif », « générique », « clair » ou d'un nombre de lettres : les sous-notes le disent déjà. Si c'est le seul défaut, dis ce qu'il coûte ou qui passerait outre ;
- acheteur type : un profil précis (« courtier en énergie pour les PME », pas « entreprise du secteur ») ;
- varie les formulations d'un nom à l'autre ;
- 180 caractères au plus, en français, sans emoji ni tiret long, sans verdict, sans notes ni prix.

Second passage : un nom qui porte « review » a déjà un avis de ta part, très éloigné de l'estimation calculée par l'appli (previous_note : ta meilleure note d'alors ; score_estimate : l'estimation). Renote-le à froid avec la même grille, sans te caler sur l'un ou l'autre chiffre : garde ton avis s'il tient, corrige-le s'il était faux.

Crédibilité avant tout :
- La plupart des noms valent peu. Des 9 et des 10 partout sont réservés aux meilleurs mots du marché.
- Le .com est l'extension la plus recherchée, y compris par les entreprises françaises (seloger.com, meilleurtaux.com, cdiscount.com) : un mot ou une expression française en .com n'est pas un défaut, ne le pénalise pas et ne le reproche pas dans la raison.
- Le statut du nom (libre, libération à venir, jamais enregistré, supprimé, enregistré) ne change pas sa valeur : juge le nom comme s'il était libre.
- Un nom qui porte « gandi » a été vérifié chez un registrar (kind, price : prix d'achat en euros). Un premium coûte cher à l'achat : la revente doit couvrir ce prix, sinon descends marche et acheteurs. Un nom indisponible (unavailable, reserved, restricted) ne vaut rien à l'achat.
- N'invente rien : un nom sans sens connu a sens 0, même s'il sonne bien.

Format : une liste JSON d'objets avec exactement ces clés : name, oral, radio, memo, sens, marque, marche, ext, acheteurs, risque, quick_low, quick_high, final_low, final_high, comment ; name est le nom complet reçu, tel quel ; un objet par nom, dans l'ordre reçu, sans texte autour.

Exemples de référence :
{"name": "velo.fr", "oral": 10, "radio": 10, "memo": 10, "sens": 10, "marque": 9, "marche": 8, "ext": 10, "acheteurs": 9, "risque": 0, "quick_low": 3000, "quick_high": 8000, "final_low": 15000, "final_high": 50000, "comment": "Le vélo électrique a fait du secteur un marché à gros panier, et le mot nu se tape sans réfléchir. Acheteur type : marketplace de vélos d'occasion ou enseigne de cycles."}
  (calculé : exploitation 95, revente 90, À prendre)
{"name": "femmes.fr", "oral": 9, "radio": 8, "memo": 8, "sens": 9, "marque": 7, "marche": 8, "ext": 9, "acheteurs": 8, "risque": 0, "quick_low": 1000, "quick_high": 3000, "final_low": 5000, "final_high": 15000, "comment": "Audience large qui attire la publicité santé, mode et beauté, comme les grands médias féminins. Acheteur type : groupe de presse féminine ou marque de lingerie."}
  (calculé : exploitation 75, revente 75, À prendre)
{"name": "mai.fr", "oral": 9, "radio": 7, "memo": 9, "sens": 7, "marque": 8, "marche": 6, "ext": 9, "acheteurs": 8, "risque": 0, "quick_low": 800, "quick_high": 2000, "final_low": 3000, "final_high": 10000, "comment": "Mois des ponts et des mariages, trois lettres rares en .fr qui vont à toute marque courte. Acheteur type : agence événementielle ou marque de mode."}
  (calculé : exploitation 70, revente 70, À prendre)
{"name": "kalo.fr", "oral": 9, "radio": 8, "memo": 9, "sens": 0, "marque": 7, "marche": 2, "ext": 6, "acheteurs": 4, "risque": 0, "quick_low": 100, "quick_high": 300, "final_low": 800, "final_high": 2000, "comment": "Se dit d'une traite et ne rappelle aucune marque connue : une page blanche pour une marque inventée. Acheteur type : créateur d'une marque de cosmétiques ou d'appli."}
  (calculé : exploitation 45, revente 40, À surveiller)
{"name": "lea.fr", "oral": 9, "radio": 6, "memo": 8, "sens": 5, "marque": 6, "marche": 3, "ext": 8, "acheteurs": 5, "risque": 0, "quick_low": 200, "quick_high": 500, "final_low": 1000, "final_high": 3000, "comment": "Prénom très donné, mais l'accent perdu (lea pour Léa) fait hésiter à l'écrit. Acheteur type : marque de cosmétiques ou de vêtements pour enfants."}
  (calculé : exploitation 55, revente 50, À surveiller)
{"name": "monassurance.fr", "oral": 8, "radio": 8, "memo": 6, "sens": 8, "marque": 4, "marche": 9, "ext": 9, "acheteurs": 6, "risque": 1, "quick_low": 500, "quick_high": 1500, "final_low": 3000, "final_high": 8000, "comment": "Un contrat signé rapporte gros au courtier, mais la formule « mon » plus un mot ne se protège pas comme marque. Acheteur type : courtier en ligne ou comparateur d'assurance."}
  (calculé : exploitation 55, revente 65, À prendre)
{"name": "forumdomotique.fr", "oral": 7, "radio": 8, "memo": 5, "sens": 8, "marque": 3, "marche": 5, "ext": 9, "acheteurs": 3, "risque": 0, "quick_low": 100, "quick_high": 300, "final_low": 500, "final_high": 2000, "comment": "Dit exactement ce qu'on y trouve, parfait pour une communauté, mais personne d'autre n'en voudra. Acheteur type : éditeur de site domotique qui lance un forum."}
  (calculé : exploitation 50, revente 45, À surveiller)
{"name": "premiere.fr", "oral": 9, "radio": 6, "memo": 7, "sens": 8, "marque": 6, "marche": 5, "ext": 8, "acheteurs": 5, "risque": 5, "quick_low": 150, "quick_high": 500, "final_low": 1000, "final_high": 3000, "comment": "Mot fort, mais le magazine Première peut s'y opposer. Acheteur type : organisateur d'événements."}
  (calculé : exploitation 50, revente 40, À surveiller)
{"name": "boulangerie.com", "oral": 9, "radio": 9, "memo": 7, "sens": 10, "marque": 6, "marche": 6, "ext": 10, "acheteurs": 7, "risque": 0, "quick_low": 1000, "quick_high": 3000, "final_low": 5000, "final_high": 15000, "comment": "Commerce que tout le monde fréquente, le .com porte une chaîne ou un site de commande national. Acheteur type : réseau de boulangeries franchisées ou appli de retrait en boutique."}
  (calculé : exploitation 75, revente 70, À prendre)
{"name": "comparateurenergie.com", "oral": 8, "radio": 8, "memo": 5, "sens": 8, "marque": 3, "marche": 8, "ext": 10, "acheteurs": 6, "risque": 0, "quick_low": 500, "quick_high": 1500, "final_low": 2000, "final_high": 8000, "comment": "Requête tapée à chaque hausse des tarifs, et chaque client signé rapporte une commission. Acheteur type : courtier en énergie pour particuliers."}
  (calculé : exploitation 55, revente 60, À prendre)
{"name": "stack.io", "oral": 9, "radio": 9, "memo": 9, "sens": 8, "marque": 7, "marche": 6, "ext": 9, "acheteurs": 6, "risque": 3, "quick_low": 500, "quick_high": 1500, "final_low": 3000, "final_high": 10000, "comment": "Évoque la pile logicielle (tech stack), vocabulaire quotidien des développeurs. Acheteur type : start-up d'outils pour développeurs."}
  (calculé : exploitation 65, revente 60, À prendre)
{"name": "ciel.org", "oral": 9, "radio": 8, "memo": 8, "sens": 8, "marque": 4, "marche": 2, "ext": 3, "acheteurs": 3, "risque": 0, "quick_low": 100, "quick_high": 300, "final_low": 800, "final_high": 3000, "comment": "Parle aux causes climat et astronomie, mais le .org ferme la porte aux acheteurs commerciaux. Acheteur type : association d'astronomie ou de qualité de l'air."}
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


# Pourquoi « À laisser » ou « À surveiller », en mots simples depuis la grille : le point faible qui pèse le plus.
WEAK_LABELS = {"sens": "sens faible", "marque": "peu de potentiel de marque", "marche": "marché étroit",
               "acheteurs": "peu d'acheteurs"}
RISK_STRONG = 8  # même seuil que derive : risque de 8 et plus = « À laisser »


def why(verdict: str, grid: dict | None) -> str:
    """Cause principale d'un verdict réservé : risque fort, sinon la sous-note la plus faible parmi
    sens, marque, marché, acheteurs. Vide pour « À prendre » et pour un avis sans grille (v4)."""
    if not grid or verdict not in ("À laisser", "À surveiller"):
        return ""
    if verdict == "À laisser" and grid.get("risque", 0) >= RISK_STRONG:
        return "risque fort (marque connue, sens négatif ou gênant)"
    weakest = min(WEAK_LABELS, key=lambda k: grid.get(k, 10))
    return WEAK_LABELS[weakest]


def public(row) -> dict:
    found = {k: row[k] for k in ("exploit", "resale", "quick_low", "quick_high", "final_low", "final_high",
                                 "comment", "rated_at")}
    keys = row.keys()
    grid = row["grid"] if "grid" in keys else None
    found["grid"] = json.loads(grid) if grid else None
    found["version"] = row["version"] if "version" in keys else None
    found["reviewed_at"] = row["reviewed_at"] if "reviewed_at" in keys else None
    found["why"] = why((found["comment"] or "").split(".")[0], found["grid"])
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
_eta_lock = threading.Lock()  # un seul recalcul de la file à la fois


# Second passage : chaque nuit, les avis v5 qui s'écartent le plus de l'estimation du score sont
# renotés à froid, une seule fois (reviewed_at), en tête de lot.
REVIEW_PER_NIGHT = 50
REVIEW_GAP = 25  # écart minimal, en points de note, entre l'avis et l'estimation


def to_review(explorer, limit: int = REVIEW_PER_NIGHT) -> list[tuple[str, int, int]]:
    """(nom, meilleure note de l'avis, estimation du score) des avis à revoir, les plus gros écarts d'abord."""
    from . import quality

    known = known_name(explorer)
    found = []
    for name, exploit, resale in explorer.conn.execute(
            "SELECT name, exploit, resale FROM domain_ai WHERE version >= ? AND reviewed_at IS NULL", (VERSION,)):
        if not known(name):
            continue  # demandé depuis la fiche, hors univers ou hors extension suivie : pas de second passage
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
    for name, best, estimate in review:  # noms de l'univers sur une extension suivie (to_review)
        label, _, tld = name.rpartition(".")
        head.append(((u.index(label), label, tld), {"previous_note": best, "score_estimate": estimate}))
    items = _describe(explorer, [p for p, _ in head] + picked, data)
    for item, (_, review_info) in zip(items, head):
        item["review"] = review_info
    return items


def queue_position(explorer, name: str) -> int | None:
    """Place d'un nom dans la file de notation (0 : premier noté la nuit prochaine), None au-delà
    de l'horizon ou hors file. File recalculée au plus une fois par heure, en arrière-plan : la fiche
    lit la dernière file connue (calculer la file prenait plusieurs secondes sur le NAS)."""
    import time

    cached = _eta_cache.get("positions")
    if cached is None:
        cached = _refresh_positions(explorer)
    elif cached[0] != explorer.generation or time.monotonic() - cached[1] > 3600:
        if _eta_lock.acquire(blocking=False):
            threading.Thread(target=_refresh_positions, args=(explorer, True), daemon=True).start()
    return cached[2].get(name)


def _refresh_positions(explorer, locked: bool = False):
    import time

    try:
        key = explorer.generation
        picked, _ = _queue(explorer, PER_NIGHT * HORIZON_NIGHTS)
        cached = (key, time.monotonic(), {f"{label}.{tld}": n for n, (_, label, tld) in enumerate(picked)})
        _eta_cache["positions"] = cached
        return cached
    finally:
        if locked:
            _eta_lock.release()


def _queue(explorer, limit: int):
    """1. libres ou bientôt libres, du meilleur score au moins bon (extension pondérée) ;
    2. probablement libres ou libres des nouvelles listes (termes stars, mots-clés et marques mêlés par rang, puis
    métier + ville), par palier d'extension ; 3. les autres probablement libres, puis 4. enregistrés, par score de
    base et extension. Ni les noms réservés ni ceux encore masqués (liste pas encore cherchée dans la zone), ni ceux
    d'une extension par Gandi seul pas encore demandés (un .com y entre dès que Gandi le dit libre)."""
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

    # 1. Classés par score complet (demande, historique, SEO compris), dans l'ordre d'un tri complet. Avec un .com
    # par Gandi seul, ils sont des dizaines de milliers : la borne du score (Explorer._bound, vingt fois moins chère)
    # range d'abord, et un nom n'est noté en entier que quand il peut sortir (aucune borne restante ne le dépasse).
    soon = []
    for tld in tlds:
        ctx = explorer._bonus_context(tld)
        weight = TLD_WEIGHT.get(tld, OTHER_TLD_WEIGHT)
        # Premium d'après Gandi : il s'achète (cher), noté avec les libres ; un premium probable non vérifié non.
        for i in _positions(data[tld], FREE_SOON) + explorer.gandi_premiums(tld):
            if u.good[i]:
                label = u.label(i)
                if f"{label}.{tld}" not in done:
                    soon.append((explorer._bound(tld, i, label, ctx) * weight, i, label, tld, ctx, weight))
    soon.sort(key=lambda x: -x[0])
    ready: list[tuple[float, str, str, int]] = []
    for bound, i, label, tld, ctx, weight in soon:
        while ready and -ready[0][0] > bound:
            _, lab, t, at = heapq.heappop(ready)
            if take(at, lab, t):
                return picked, data
        heapq.heappush(ready, (-explorer._value(tld, i, label, ctx)["score"] * weight, label, tld, i))
    while ready:
        _, lab, t, at = heapq.heappop(ready)
        if take(at, lab, t):
            return picked, data
    # 2 et 3. Trop nombreux pour tout classer : par paliers d'extension (.fr et .com, puis .org,
    # puis le reste), et dans chaque palier par score de base. Un nom de 1 ou 2 caractères jamais
    # vu n'est pas « probablement libre » : les registres réservent ces formats. On ne le note pas.
    from .explore import ABSENT, INVALID, PREMIUM, RESERVED  # import local : explore importe ce module

    tiers: dict[float, list[str]] = {}
    for tld in tlds:
        tiers.setdefault(TLD_WEIGHT.get(tld, OTHER_TLD_WEIGHT), []).append(tld)

    def wanted(i: int, tld: str, codes) -> str | None:
        """Libellé du nom s'il entre dans la file pour ces états, avec les filtres de toujours."""
        code = data[tld][i]
        if (codes is not None and code not in codes) or code in (INVALID, RESERVED, PREMIUM, UNCHECKED):
            return None
        label = u.label(i)
        if code == ABSENT and len(label) <= 2:
            return None
        if tld == "fr" and english_only(u, label):
            return None
        return label

    # 2. Nouvelles listes : leurs noms à saisir passent avant les autres probablement libres.
    ranks = wordlists.ranks()
    for keys in FIRST_LISTS:
        keys = [k for k in keys if ranks.get(k)]
        if not keys:
            continue
        order = u.frequency(",".join(keys))
        for weight in sorted(tiers, reverse=True):
            for i in order:
                if not u.good[i]:
                    continue
                for tld in tiers[weight]:
                    label = wanted(i, tld, (*MAYBE_FREE, FREE_SOON[0]))
                    if label is not None and take(i, label, tld):
                        return picked, data
    for group in (MAYBE_FREE, None):
        for weight in sorted(tiers, reverse=True):
            for score in range(70, -1, -1):
                if group is None and score < TAKEN_MIN_BASE:
                    break  # noms pris : seulement les meilleurs, la notation sert d'abord aux noms à saisir
                for i in u.buckets()[score]:
                    if not u.good[i]:
                        continue
                    for tld in tiers[weight]:
                        label = wanted(i, tld, group)
                        if label is not None and take(i, label, tld):
                            return picked, data
    return picked, data


# Formes de nom : pas des listes de mots (une forme ne dit rien de la langue).
SHAPES = {"letters", "digits", "mixed", "hyphen", "long"}


def english_only(u, label: str) -> bool:
    """Mot de la seule liste anglaise : en .fr, il vaut peu (consigne) et l'IA le laisse presque toujours.
    Hors de la file de masse ; un nom libre ou bientôt libre reste noté, quelle que soit sa langue."""
    lists = set(u.categories(label)) - SHAPES
    return lists == {"english"}


def _describe(explorer, picked, data) -> list[dict]:
    from .explore import ABSENT, LABELS, absent_label  # import local : explore importe ce module

    u = explorer.universe
    ranks = wordlists.ranks()
    natures = wordlists.natures()
    names = [f"{label}.{tld}" for _, label, tld in picked]
    seo = explorer._seo_rows(names) if names else {}
    # Réponse de Gandi (premium et son prix, libre, indisponible) : la notation en tient compte (consigne).
    checks = gandi.load(explorer.conn, names)
    since = {}
    for chunk in range(0, len(names), 900):
        part = names[chunk:chunk + 900]
        since.update(explorer.conn.execute(
            f"SELECT name, state_since FROM drops WHERE name IN ({','.join('?' * len(part))})", part).fetchall())
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
            "status": absent_label(tld) if code == ABSENT else LABELS.get(code, ""),
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
        found = gandi.summary(checks.get(name), since.get(name))
        if found is not None:
            item["gandi"] = found
        out.append(item)
    return out


def save(conn, results: list, known, status_of=None) -> dict:
    """Enregistre les avis au bon format ; les autres sont rejetés avec la raison, sans bloquer le lot.
    `status_of(name)` : code d'état de l'Explorer au moment de l'avis, gardé pour la mesure a posteriori."""
    now = iso(utcnow())
    saved, rejected, names = 0, [], []
    # Noms déjà notés avec la grille, avant ce lot : un avis à l'ancien format ne les remplace pas, et un
    # avis v5 qui revient est le second passage (il ne repassera plus), même en double dans le lot.
    detailed = {r[0] for r in conn.execute("SELECT name FROM domain_ai WHERE version >= ?", (VERSION,))}
    for item in results[:MAX_RESULTS]:
        reason = check_any(item, known)
        if not reason and not is_v5(item) and item["name"] in detailed:
            reason = "déjà noté avec la grille"
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
        again = row["name"] in detailed
        conn.execute(
            """INSERT INTO domain_ai (name, exploit, resale, quick_low, quick_high, final_low, final_high,
                                      comment, rated_at, version, grid, reviewed_at, status_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(name) DO UPDATE SET exploit = excluded.exploit, resale = excluded.resale,
                 quick_low = excluded.quick_low, quick_high = excluded.quick_high,
                 final_low = excluded.final_low, final_high = excluded.final_high,
                 comment = excluded.comment, rated_at = excluded.rated_at, version = excluded.version,
                 grid = excluded.grid, reviewed_at = COALESCE(excluded.reviewed_at, domain_ai.reviewed_at),
                 status_at = excluded.status_at""",
            (*(row[k] for k in FIELDS), now, version, grid, now if again and version >= VERSION else None,
             status_of(row["name"]) if status_of else None),
        )
        saved += 1
        names.append(item["name"])
    return {"saved": saved, "rejected": rejected, "names": names}


def status_lookup(explorer):
    """Code d'état de l'Explorer d'un nom (None hors de l'univers ou sur une extension sans données)."""
    u = explorer.universe
    data = {t: explorer.status_bytes(t) for t in explorer.tlds()}

    def status(name: str) -> int | None:
        label, _, tld = name.partition(".")
        i = u.index(label)
        return int(data[tld][i]) if tld in data and i is not None else None

    return status


# --- Mesure a posteriori : les noms notés libres sont-ils repris ? --------------------

OUTCOME_AGE = timedelta(days=30)
FREE_CODES = (*MAYBE_FREE, FREE_SOON[0])   # supprimé non repris, jamais vu, libre vérifié
TAKEN_NOW = (1, 6, 11)                     # enregistré, suspendu, pris d'après Gandi
RESALE_BANDS = (("0-39", 0, 39), ("40-59", 40, 59), ("60-79", 60, 79), ("80-100", 80, 100))


def update_outcomes(conn, explorer, now) -> int:
    """Tâche de nuit : pour chaque avis vieux de 30 jours au moins, donné quand le nom était libre ou
    probablement libre, l'état d'aujourd'hui. Enregistré par quelqu'un d'autre (ni surveillé ni au
    portefeuille) : repris, daté du premier passage qui l'a vu. Renvoie le nombre de noms revus."""
    rows = conn.execute(
        f"SELECT name, resale, comment, rated_at FROM domain_ai WHERE rated_at <= ? AND status_at IN ({','.join('?' * len(FREE_CODES))})",
        (iso(now - OUTCOME_AGE), *FREE_CODES)).fetchall()
    u = explorer.universe
    data = {t: explorer.status_bytes(t) for t in explorer.tlds()}
    mine = {r[0] for r in conn.execute("SELECT name FROM domains")} | {r[0] for r in conn.execute("SELECT name FROM portfolio")}
    before = dict(conn.execute("SELECT name, taken_at FROM ai_outcomes").fetchall())
    stamp = iso(now)
    count = 0
    conn.execute("BEGIN")
    try:
        for r in rows:
            label, _, tld = r["name"].partition(".")
            i = u.index(label)
            if tld not in data or i is None:
                continue
            taken_now = data[tld][i] in TAKEN_NOW and r["name"] not in mine
            earlier = before.get(r["name"])
            # Repris une fois depuis l'avis : ça reste un regret, même si le nom retombe ; renoté depuis : on repart.
            taken_at = earlier if earlier and earlier >= r["rated_at"] else (stamp if taken_now else None)
            conn.execute(
                """INSERT INTO ai_outcomes (name, verdict, resale, rated_at, free_at_rating, taken_at, checked_at)
                   VALUES (?, ?, ?, ?, 1, ?, ?)
                   ON CONFLICT(name) DO UPDATE SET verdict = excluded.verdict, resale = excluded.resale,
                     rated_at = excluded.rated_at, taken_at = excluded.taken_at, checked_at = excluded.checked_at""",
                (r["name"], r["comment"].split(".")[0], r["resale"], r["rated_at"], taken_at, stamp),
            )
            count += 1
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return count


def outcomes(conn) -> dict:
    """Résumé : par verdict et par tranche de note de revente, nombre de noms et part reprise."""
    rows = conn.execute("SELECT verdict, resale, taken_at, checked_at FROM ai_outcomes WHERE free_at_rating = 1").fetchall()

    def cell() -> dict:
        return {"count": 0, "taken": 0, "share": None}

    def band_of(resale: int) -> str:
        return next(key for key, low, high in RESALE_BANDS if low <= resale <= high)

    by_verdict, by_resale, cross = {}, {key: cell() for key, _, _ in RESALE_BANDS}, {}
    for r in rows:
        band = band_of(max(0, min(100, r["resale"])))
        for bucket in (by_verdict.setdefault(r["verdict"], cell()), by_resale[band],
                       cross.setdefault((r["verdict"], band), cell())):
            bucket["count"] += 1
            bucket["taken"] += r["taken_at"] is not None
    for bucket in (*by_verdict.values(), *by_resale.values(), *cross.values()):
        bucket["share"] = round(bucket["taken"] / bucket["count"], 2) if bucket["count"] else None
    return {"min_age_days": OUTCOME_AGE.days, "total": len(rows),
            "checked_at": max((r["checked_at"] for r in rows), default=None),
            "by_verdict": by_verdict, "by_resale": by_resale,
            "cross": [{"verdict": v, "band": b, **c} for (v, b), c in sorted(cross.items())]}


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
            found = gandi.summary(gandi.load(explorer.conn, [name]).get(name))
            if found is not None:
                item["gandi"] = found
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

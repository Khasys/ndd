"""Avis IA sur les noms de domaine : le NAS publie la liste à noter, une tâche planifiée la note,
le NAS réimporte.

L'avis porte sur le nom complet (velo.fr, ciel.org) : l'extension change l'usage et le marché.
Notes sur 100 : exploitation (projet, marque, site) et revente. Deux fourchettes de prix en euros :
revente rapide (à un investisseur, en quelques semaines) et utilisateur final (le bon acheteur,
avec de la patience). Ce sont des avis, pas des cotes du marché.
"""

import hashlib
import hmac
import secrets

from . import value, wordlists
from .checker import iso, utcnow

# Version de la consigne : la monter fait renoter tous les noms, petit à petit.
VERSION = 3
MAX_PENDING = 5000
MAX_RESULTS = 5000
MAX_PRICE = 10_000_000

# Codes d'état de l'Explorer (explore.py) : libres ou bientôt libres d'abord, enregistrés en dernier.
FREE_SOON = (5, 4)        # libre vérifié, libération à venir
MAYBE_FREE = (2, 3)       # supprimé non repris, jamais vu
TAKEN = (1, 6, 4)         # enregistré, suspendu, libération à venir (pris ailleurs = demande)

from .ai_format import COMMENT_MAX, FIELDS, FORMAT, PRICE_STEPS, check  # noqa: F401  (réexportés)

# Consigne remise avec la liste : la tâche de notation la suit telle quelle.
GUIDE = """Tu notes des noms de domaine pour un investisseur francophone qui achète des noms libres pour les exploiter ou les revendre.

Format, identique pour chaque nom, sans exception :
- un objet JSON avec exactement ces clés : name, exploit, resale, quick_low, quick_high, final_low, final_high, comment ;
- name : le nom complet reçu, tel quel ;
- exploit : potentiel d'exploitation (projet, marque, site, activité), entier de 0 à 100, multiple de 5 ;
- resale : potentiel de revente (facilité à trouver un acheteur), entier de 0 à 100, multiple de 5 ;
- quick_low, quick_high : revente rapide en euros, à un investisseur ou une place de marché en quelques semaines ;
- final_low, final_high : prix pour un utilisateur final, avec de la patience (mois ou années) ;
- prix : uniquement ces valeurs : 0, 50, 100, 150, 200, 300, 500, 800, 1000, 1500, 2000, 3000, 5000, 8000, 10000, 15000, 20000, 30000, 50000, 80000, 100000, 150000, 200000, 300000, 500000, 1000000 ; bas inférieur ou égal au haut ; revente rapide inférieure ou égale au prix final ;
- comment : exactement trois phrases sur ce gabarit : « Verdict. Raison. Acheteur type : qui. » ;
  verdict : « À prendre », « À surveiller » ou « À laisser », rien d'autre ;
  « À prendre » seulement si exploit ou resale vaut au moins 60 ; « À laisser » seulement si les deux valent 45 au plus ;
  raison : ce qui fait la valeur ou ce qui la freine (sens, longueur, demande, risque de marque) ;
  acheteur type : à qui le revendre ou qui l'exploiterait ;
  200 caractères au plus ; en français ; sans emoji ni tiret long ; sans répéter les notes ni les prix.

Crédibilité avant tout :
- La plupart des noms valent peu. Une note de revente au-dessus de 70 est rare et réservée aux noms très courts ou très recherchés. Beaucoup de noms ont une revente rapide à 0.
- Revente rapide : en général 10 à 25 % du prix utilisateur final. Si elle ne couvre pas les frais (moins de 100 euros), mets 0 et 0.
- Le .fr se revend moins facilement que le .com ; le .org vise les associations et les projets non commerciaux ; le .info vaut peu.
- Un risque de marque connue fait baisser la revente : mets-le dans la raison.
- N'invente rien : un nom sans sens connu le dit.

Exemples de référence :
{"name": "velo.fr", "exploit": 90, "resale": 90, "quick_low": 2000, "quick_high": 5000, "final_low": 10000, "final_high": 30000, "comment": "À prendre. Mot que tout le monde tape, rare en 4 lettres. Acheteur type : enseigne, marketplace ou start-up vélo."}
{"name": "femmes.fr", "exploit": 80, "resale": 75, "quick_low": 1000, "quick_high": 3000, "final_low": 5000, "final_high": 15000, "comment": "À prendre. Thème large et porteur, se lit bien au pluriel. Acheteur type : média, marque de mode ou de santé."}
{"name": "mai.fr", "exploit": 75, "resale": 80, "quick_low": 800, "quick_high": 2000, "final_low": 3000, "final_high": 8000, "comment": "À prendre. Trois lettres et un mot du calendrier, rare en .fr. Acheteur type : marque courte ou agence événementielle."}
{"name": "lea.fr", "exploit": 60, "resale": 55, "quick_low": 150, "quick_high": 500, "final_low": 1000, "final_high": 3000, "comment": "À surveiller. Prénom court très donné, plaît aux particuliers. Acheteur type : parent, marque cosmétique ou enfant."}
{"name": "premiere.fr", "exploit": 55, "resale": 50, "quick_low": 200, "quick_high": 800, "final_low": 2000, "final_high": 5000, "comment": "À surveiller. Mot fort, mais le magazine Première peut s'y opposer. Acheteur type : organisateur d'événements."}
{"name": "oye.fr", "exploit": 45, "resale": 40, "quick_low": 100, "quick_high": 300, "final_low": 500, "final_high": 2000, "comment": "À surveiller. Court et prononçable, sans sens courant en français. Acheteur type : créateur de marque inventée."}
{"name": "ciel.org", "exploit": 55, "resale": 40, "quick_low": 50, "quick_high": 200, "final_low": 500, "final_high": 2000, "comment": "À surveiller. Mot simple et poétique, mais le .org limite les acheteurs. Acheteur type : association ou projet culturel."}
{"name": "sommes.fr", "exploit": 30, "resale": 25, "quick_low": 0, "quick_high": 100, "final_low": 300, "final_high": 1000, "comment": "À laisser. Forme de « être » ou pluriel de « somme », sens flou. Acheteur type : rare, à la marge en finance."}
{"name": "qsd.fr", "exploit": 15, "resale": 25, "quick_low": 50, "quick_high": 200, "final_low": 200, "final_high": 800, "comment": "À laisser. Suite imprononçable sans sens, seule la brièveté compte. Acheteur type : entreprise au sigle QSD."}
{"name": "kylian.org", "exploit": 25, "resale": 15, "quick_low": 0, "quick_high": 0, "final_low": 50, "final_high": 200, "comment": "À laisser. Prénom courant, mais le .org ne colle pas à un usage perso. Acheteur type : association de ce nom."}

Réponds par une liste JSON de ces objets, un par nom reçu, dans le même ordre, sans texte autour."""


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
    return {k: row[k] for k in ("exploit", "resale", "quick_low", "quick_high", "final_low", "final_high",
                                "comment", "rated_at")}


def load(conn, names: list[str]) -> dict[str, dict]:
    found = {}
    for chunk in range(0, len(names), 900):
        part = names[chunk:chunk + 900]
        rows = conn.execute(f"SELECT * FROM domain_ai WHERE name IN ({','.join('?' * len(part))})", part).fetchall()
        found.update({r["name"]: public(r) for r in rows})
    return found


def stats(explorer) -> dict:
    rated = explorer.conn.execute("SELECT COUNT(*), MAX(rated_at) FROM domain_ai WHERE version >= ?", (VERSION,)).fetchone()
    total = sum(explorer.universe.good) * len(explorer.tlds())
    return {"rated": rated[0], "last": rated[1], "total": total, "pending": max(0, total - rated[0]),
            "token_set": bool(explorer.fr.store.get("ai_token_hash"))}


# Marché francophone : à valeur égale, un .fr ou un .com passe avant un .org, puis le reste.
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


def pending(explorer, limit: int) -> list[dict]:
    """Noms complets sans avis à jour, dans l'ordre où ils servent :
    1. libres ou bientôt libres, du meilleur score au moins bon (extension pondérée) ;
    2. probablement libres, puis 3. enregistrés, par score de base et extension."""
    u = explorer.universe
    u.buckets()  # construit u.good
    done = {r[0] for r in explorer.conn.execute("SELECT name FROM domain_ai WHERE version >= ?", (VERSION,))}
    tlds = sorted(explorer.tlds(), key=lambda t: -TLD_WEIGHT.get(t, OTHER_TLD_WEIGHT))
    data = {t: explorer.status_bytes(t) for t in tlds}
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
            return _describe(explorer, picked, data)
    # 2 et 3. Trop nombreux pour tout classer : score de base, puis extension.
    for group in (MAYBE_FREE, None):
        for score in range(70, -1, -1):
            for i in u.buckets()[score]:
                if not u.good[i]:
                    continue
                for tld in tlds:
                    if group is not None and data[tld][i] not in group:
                        continue
                    if take(i, u.label(i), tld):
                        return _describe(explorer, picked, data)
    return _describe(explorer, picked, data)


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
    saved, rejected = 0, []
    for item in results[:MAX_RESULTS]:
        reason = check(item, known)
        if reason:
            name = item.get("name") if isinstance(item, dict) else None
            rejected.append({"name": str(name or ""), "reason": reason})
            continue
        conn.execute(
            """INSERT INTO domain_ai (name, exploit, resale, quick_low, quick_high, final_low, final_high,
                                      comment, rated_at, version)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(name) DO UPDATE SET exploit = excluded.exploit, resale = excluded.resale,
                 quick_low = excluded.quick_low, quick_high = excluded.quick_high,
                 final_low = excluded.final_low, final_high = excluded.final_high,
                 comment = excluded.comment, rated_at = excluded.rated_at, version = excluded.version""",
            (*(item[k] for k in FIELDS), now, VERSION),
        )
        saved += 1
    return {"saved": saved, "rejected": rejected}


def known_name(explorer):
    """Un nom de l'univers, sur une extension suivie."""
    u = explorer.universe
    tlds = set(explorer.tlds())

    def exists(name: str) -> bool:
        label, _, tld = name.partition(".")
        return bool(label) and tld in tlds and u.index(label) is not None

    return exists

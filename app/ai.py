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
VERSION = 2
MAX_PENDING = 5000
MAX_RESULTS = 5000
MAX_COMMENT = 240
MAX_PRICE = 10_000_000

# Codes d'état de l'Explorer (explore.py) : libres ou bientôt libres d'abord, enregistrés en dernier.
FREE_SOON = (5, 4)        # libre vérifié, libération à venir
MAYBE_FREE = (2, 3)       # supprimé non repris, jamais vu
TAKEN = (1, 6, 4)         # enregistré, suspendu, libération à venir (pris ailleurs = demande)

# Consigne remise avec la liste : la tâche de notation la suit telle quelle.
GUIDE = """Tu notes des noms de domaine pour un investisseur francophone qui achète des noms libres pour les exploiter ou les revendre.

Pour chaque nom complet (mot + extension) :
- exploit : potentiel d'exploitation de 0 à 100 (projet, marque, site, activité) ;
- resale : potentiel de revente de 0 à 100 (facilité à trouver un acheteur) ;
- quick_low, quick_high : prix de revente rapide en euros, à un investisseur ou sur une place de marché en quelques semaines ;
- final_low, final_high : prix pour un utilisateur final, avec de la patience (mois ou années) ;
- comment : 200 caractères au plus, en français, sans emoji ni tiret long.

Crédibilité avant tout :
- La plupart des noms valent peu. Une note de revente au-dessus de 70 est rare et réservée aux noms très courts ou très recherchés. Beaucoup de noms ont une revente rapide à 0.
- Revente rapide : en général 10 à 25 % du prix utilisateur final. Sous 100 euros, elle ne couvre pas les frais : mets 0.
- Le .fr se revend moins facilement que le .com ; le .org vise les associations et les projets non commerciaux ; le .info vaut peu.
- Un risque de marque connue fait baisser la revente : dis-le.
- Ne recopie pas les notes dans le commentaire. Le commentaire doit suffire à comprendre la valeur : ce qu'est le nom, à qui il sert, ce qui le limite.
- N'invente rien : un nom sans sens connu le dit.

Exemples de référence (exploit, resale, rapide, utilisateur final, commentaire) :
- velo.fr : 90, 90, 2000-5000, 10000-30000. Mot courant et commercial : vente, location, réparation de vélos. Très recherché, rare en 4 lettres. Cible : enseignes, marketplaces, mobilité.
- mai.fr : 75, 80, 800-2000, 3000-8000. 3 lettres, mot du calendrier, facile à retenir. Usage flou mais rareté forte en .fr. Cible : marque courte, événementiel.
- lea.fr : 60, 55, 150-500, 1000-3000. Prénom très donné, 3 lettres. Intéresse surtout des particuliers et des marques prénom (cosmétique, enfant).
- premiere.fr : 55, 50, 200-800, 2000-6000. Mot fort mais risque de marque (magazine Première). Usage possible pour un événement ou un service « première fois ».
- oye.fr : 45, 40, 100-300, 500-2000. Court et prononçable, sans sens courant en français. Bonne base de marque inventée, valeur portée par la brièveté.
- ciel.org : 55, 40, 50-200, 500-2000. Mot simple et poétique ; le .org vise associations et projets culturels. Marché de revente étroit.
- sommes.fr : 30, 25, 0-100, 300-1000. Forme de « être » ou pluriel de « somme » : sens flou hors contexte. Intérêt limité, éventuellement finance.
- qsd.fr : 15, 25, 50-200, 200-800. Suite imprononçable sans sens. Seule valeur : 3 lettres en .fr, pour un sigle ou un collectionneur.
- kylian.org : 25, 15, 0-0, 50-200. Prénom, mais le .org ne colle pas à un usage personnel. Utile seulement à une association de ce nom.

Réponds par une liste JSON d'objets : name, exploit, resale, quick_low, quick_high, final_low, final_high, comment."""


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


def pending(explorer, limit: int) -> list[dict]:
    """Noms complets sans avis à jour : libres ou bientôt libres d'abord, puis probablement libres,
    puis enregistrés ; dans chaque groupe, les plus prometteurs d'abord (score de base)."""
    u = explorer.universe
    done = {r[0] for r in explorer.conn.execute("SELECT name FROM domain_ai WHERE version >= ?", (VERSION,))}
    tlds = explorer.tlds()
    data = {t: explorer.status_bytes(t) for t in tlds}
    picked: list[tuple[int, str, str]] = []
    seen = set()
    for group in (FREE_SOON, MAYBE_FREE, None):
        for score in range(70, -1, -1):
            for i in u.buckets()[score]:
                if not u.good[i]:
                    continue
                label = None
                for tld in tlds:
                    if group is not None and data[tld][i] not in group:
                        continue
                    label = label or u.label(i)
                    name = f"{label}.{tld}"
                    if name in done or name in seen:
                        continue
                    seen.add(name)
                    picked.append((i, label, tld))
                    if len(picked) >= limit:
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


def save(conn, results: list[dict], known) -> dict:
    """Enregistre les avis valides ; les autres sont rejetés avec la raison, sans bloquer le lot."""
    now = iso(utcnow())
    saved, rejected = 0, []
    for item in results[:MAX_RESULTS]:
        name = str(item.get("name", "")).strip().lower().rstrip(".")
        try:
            exploit, resale = int(item["exploit"]), int(item["resale"])
            prices = [int(item.get(k) or 0) for k in ("quick_low", "quick_high", "final_low", "final_high")]
        except (KeyError, TypeError, ValueError):
            rejected.append({"name": name, "reason": "notes ou prix non numériques"})
            continue
        quick_low, quick_high, final_low, final_high = prices
        comment = " ".join(str(item.get("comment", "")).split())
        if not known(name):
            reason = "nom inconnu"
        elif not (0 <= exploit <= 100 and 0 <= resale <= 100):
            reason = "notes hors de 0 à 100"
        elif not (0 <= quick_low <= quick_high <= MAX_PRICE and 0 <= final_low <= final_high <= MAX_PRICE):
            reason = "fourchette de prix invalide"
        elif quick_high > final_high:
            reason = "revente rapide plus chère que l'utilisateur final"
        elif not comment:
            reason = "commentaire vide"
        else:
            reason = None
        if reason:
            rejected.append({"name": name, "reason": reason})
            continue
        conn.execute(
            """INSERT INTO domain_ai (name, exploit, resale, quick_low, quick_high, final_low, final_high,
                                      comment, rated_at, version)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(name) DO UPDATE SET exploit = excluded.exploit, resale = excluded.resale,
                 quick_low = excluded.quick_low, quick_high = excluded.quick_high,
                 final_low = excluded.final_low, final_high = excluded.final_high,
                 comment = excluded.comment, rated_at = excluded.rated_at, version = excluded.version""",
            (name, exploit, resale, *prices, comment[:MAX_COMMENT], now, VERSION),
        )
        saved += 1
    return {"saved": saved, "rejected": rejected}


def known_name(explorer):
    """Un nom de l'univers, sur une extension suivie."""
    u = explorer.universe
    tlds = set(explorer.tlds())

    def check(name: str) -> bool:
        label, _, tld = name.partition(".")
        return bool(label) and tld in tlds and u.index(label) is not None

    return check

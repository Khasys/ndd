"""Avis IA sur les noms : le NAS publie la liste à noter, une tâche planifiée la note, le NAS réimporte.

L'avis porte sur le nom seul (« velo », pas « velo.fr ») : une fois pour toutes. Ce qui dépend de
l'extension (pris ailleurs, historique .fr, SEO) reste dans le score calculé de l'Explorer.

Notes sur 100 : exploitation (projet, marque, site) et revente. Fourchette de prix en euros,
indicative : un avis, pas une cote du marché.
"""

import hashlib
import hmac
import secrets

from . import value, wordlists
from .checker import iso, utcnow

# Version de la consigne de notation : la monter fait renoter tous les noms, petit à petit.
VERSION = 1
MAX_PENDING = 5000
MAX_RESULTS = 5000
MAX_COMMENT = 240
MAX_PRICE = 10_000_000

# Consigne remise avec la liste : la tâche de notation la suit telle quelle.
GUIDE = (
    "Pour chaque nom (sans extension, marché francophone) : exploit = potentiel d'exploitation "
    "(projet, marque, site, activité) de 0 à 100 ; resale = potentiel de revente à un tiers de 0 à 100 ; "
    "price_low et price_high = fourchette de prix de revente réaliste en euros (entiers, 0 si invendable) ; "
    "comment = une ou deux phrases, 200 caractères au plus, en français, sans emoji : "
    "ce que c'est, à qui ça sert, ce qui limite. Pas d'invention : un nom sans sens connu le dit. "
    "Repères : 1 à 3 lettres et mots très courants valent cher ; une suite imprononçable vaut peu ; "
    "un prénom ou un nom de famille courant intéresse surtout des particuliers ; une forme conjuguée vaut "
    "moins que le mot de base ; un mot commercial (métier, produit, ville) vaut plus qu'un mot abstrait."
)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def new_token(store) -> str:
    """Jeton d'accès de la tâche de notation : montré une fois, seul son empreinte est gardée."""
    token = "dw_" + secrets.token_urlsafe(32)
    store.set("ai_token_hash", hash_token(token))
    return token


def token_ok(store, header: str | None) -> bool:
    expected = store.get("ai_token_hash")
    if not expected or not header or not header.startswith("Bearer "):
        return False
    return hmac.compare_digest(hash_token(header[7:].strip()), expected)


def load(conn, labels: list[str]) -> dict[str, dict]:
    found = {}
    for chunk in range(0, len(labels), 900):
        part = labels[chunk:chunk + 900]
        rows = conn.execute(f"SELECT * FROM name_ai WHERE label IN ({','.join('?' * len(part))})", part).fetchall()
        found.update({r["label"]: public(r) for r in rows})
    return found


def public(row) -> dict:
    return {"exploit": row["exploit"], "resale": row["resale"], "price_low": row["price_low"],
            "price_high": row["price_high"], "comment": row["comment"], "rated_at": row["rated_at"]}


def stats(explorer) -> dict:
    conn = explorer.conn
    rated = conn.execute("SELECT COUNT(*), MAX(rated_at) FROM name_ai WHERE version >= ?", (VERSION,)).fetchone()
    u = explorer.universe
    total = int(u.good_int.bit_count()) if hasattr(u, "good_int") else sum(u.good)
    return {"rated": rated[0], "last": rated[1], "total": total, "pending": max(0, total - rated[0]),
            "token_set": bool(explorer.fr.store.get("ai_token_hash"))}


def pending(explorer, limit: int) -> list[dict]:
    """Les noms qui ont un sens et pas encore d'avis à jour, les plus prometteurs d'abord."""
    u = explorer.universe
    done = {r[0] for r in explorer.conn.execute("SELECT label FROM name_ai WHERE version >= ?", (VERSION,))}
    tlds = explorer.tlds()
    data = {t: explorer.status_bytes(t) for t in tlds}
    picked = []
    for score in range(70, -1, -1):
        for i in u.buckets()[score]:
            if not u.good[i]:
                continue
            label = u.label(i)
            if label in done:
                continue
            picked.append((i, label))
            if len(picked) >= limit:
                break
        if len(picked) >= limit:
            break
    ranks = wordlists.ranks()
    natures = wordlists.natures()
    out = []
    for i, label in picked:
        out.append({
            "label": label,
            "length": len(label),
            "categories": u.categories(label),
            "natures": sorted(natures.get(label, ())),
            "ranks": {k: r[label] for k, r in ranks.items() if label in r},
            "taken": [t for t in tlds if data[t][i] in (1, 6, 4)],
            "base": value.base(label),
        })
    return out


def save(conn, results: list[dict], known) -> dict:
    """Enregistre les avis valides ; les autres sont rejetés avec la raison, sans bloquer le lot."""
    now = iso(utcnow())
    saved, rejected = 0, []
    for item in results[:MAX_RESULTS]:
        label = str(item.get("label", "")).strip().lower()
        try:
            exploit, resale = int(item["exploit"]), int(item["resale"])
            low = int(item.get("price_low") or 0)
            high = int(item.get("price_high") or 0)
        except (KeyError, TypeError, ValueError):
            rejected.append({"label": label, "reason": "notes ou prix non numériques"})
            continue
        comment = " ".join(str(item.get("comment", "")).split())
        if not known(label):
            reason = "nom inconnu"
        elif not (0 <= exploit <= 100 and 0 <= resale <= 100):
            reason = "notes hors de 0 à 100"
        elif not (0 <= low <= high <= MAX_PRICE):
            reason = "fourchette de prix invalide"
        elif not comment:
            reason = "commentaire vide"
        else:
            reason = None
        if reason:
            rejected.append({"label": label, "reason": reason})
            continue
        conn.execute(
            """INSERT INTO name_ai (label, exploit, resale, price_low, price_high, comment, rated_at, version)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(label) DO UPDATE SET exploit = excluded.exploit, resale = excluded.resale,
                 price_low = excluded.price_low, price_high = excluded.price_high, comment = excluded.comment,
                 rated_at = excluded.rated_at, version = excluded.version""",
            (label, exploit, resale, low, high, comment[:MAX_COMMENT], now, VERSION),
        )
        saved += 1
    return {"saved": saved, "rejected": rejected}


def known_label(explorer):
    u = explorer.universe
    return lambda label: bool(label) and u.index(label) is not None

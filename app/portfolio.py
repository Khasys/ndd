"""Portefeuille : noms possédés, prix d'achat, renouvellements, mise en vente, ventes.

L'échéance vient du domaine surveillé du même nom (RDAP), sinon de la date saisie. Rappel Telegram
30 jours puis 7 jours avant chaque renouvellement. Les ventes et les offres reçues servent d'exemples
réels à la notation IA : c'est la seule calibration sur le marché dont on dispose.
"""

import json
import math
from datetime import datetime, timezone
from urllib.parse import quote

from . import ai, states
from .checker import iso, parse_iso

STATUSES = {"owned": "Possédé", "in_use": "En exploitation", "not_for_sale": "Pas à vendre",
            "for_sale": "En vente", "sold": "Vendu", "dropped": "Abandonné"}
KEPT = ("owned", "in_use", "not_for_sale", "for_sale")  # encore à toi : renouvellements à payer
REMIND_DAYS = (30, 7)
FIELDS = ("status", "bought_on", "buy_price", "renew_price", "expires_at", "asking_price", "best_offer",
          "sold_on", "sold_price", "notes")
# Places de marché, sans API : la page du nom (Dan, Afternic) ou la recherche (Sedo), le nom dans l'adresse.
# Formats relevés sur les sites ; Dan.com rejoint Afternic (GoDaddy) : son lien peut y renvoyer.
SALE_SITES = {"sedo": "https://sedo.com/search/?keyword={name}", "dan": "https://dan.com/buy-domain/{name}",
              "afternic": "https://www.afternic.com/domain/{name}"}
EXPORT_COLUMNS = ("nom", "statut", "achete_le", "prix_achat", "renouvellement", "echeance", "source_echeance",
                  "prix_demande", "meilleure_offre", "vendu_le", "prix_vente", "benefice", "verdict_ia",
                  "note_revente", "prix_final_bas", "prix_final_haut", "notes")


def sale_links(name: str) -> dict[str, str]:
    return {site: url.format(name=quote(name, safe="")) for site, url in SALE_SITES.items()}


def _expiry(row, domain, now: datetime) -> datetime | None:
    if domain is not None and domain["expires_at"]:
        return states.effective_expiry(domain["state"], parse_iso(domain["expires_at"]), now)
    # Date saisie (AAAA-MM-JJ) : minuit UTC ce jour-là.
    return datetime.fromisoformat(row["expires_at"][:10]).replace(tzinfo=timezone.utc) if row["expires_at"] else None


def items(conn, now: datetime) -> list[dict]:
    rows = conn.execute("""SELECT * FROM portfolio ORDER BY CASE status WHEN 'for_sale' THEN 0 WHEN 'in_use' THEN 1
                           WHEN 'owned' THEN 2 WHEN 'not_for_sale' THEN 3 WHEN 'sold' THEN 4 ELSE 5 END, name""").fetchall()
    names = [r["name"] for r in rows]
    domains = {r["name"]: r for r in conn.execute("SELECT * FROM domains")}
    opinions = ai.load(conn, names)
    out = []
    for r in rows:
        domain = domains.get(r["name"])
        expiry = _expiry(r, domain, now)
        item = {k: r[k] for k in ("id", "name", *FIELDS)}
        item.update(
            status_label=STATUSES.get(r["status"], r["status"]),
            expiry=iso(expiry) if expiry else None,
            expiry_source="RDAP" if domain is not None and domain["expires_at"] else ("saisie" if r["expires_at"] else None),
            domain_id=domain["id"] if domain is not None else None,
            ai=opinions.get(r["name"]),
            profit=(r["sold_price"] - (r["buy_price"] or 0)) if r["status"] == "sold" and r["sold_price"] is not None else None,
            sale_links=sale_links(r["name"]),
        )
        out.append(item)
    return out


def years(found: list[dict], now: datetime) -> list[dict]:
    """Par année : achats (investi), renouvellements, ventes et bénéfice. Un renouvellement par an après
    l'année d'achat, jusqu'à l'année de vente ou l'année en cours ; un nom abandonné ne compte que son achat
    (date d'abandon inconnue) ; sans date d'achat, pas de renouvellement attribué."""
    by_year: dict[int, dict] = {}

    def at(year: int) -> dict:
        return by_year.setdefault(year, {"year": year, "invested": 0, "renewals": 0, "sold": 0, "profit": 0})

    for item in found:
        bought = int(item["bought_on"][:4]) if item["bought_on"] else None
        if bought is not None:
            at(bought)["invested"] += item["buy_price"] or 0
        sold = int(item["sold_on"][:4]) if item["status"] == "sold" and item["sold_on"] else None
        if item["status"] == "sold":
            at(sold or now.year)["sold"] += item["sold_price"] or 0
            at(sold or now.year)["profit"] += item["profit"] or 0
        if bought is not None and item["renew_price"] and item["status"] != "dropped":
            for year in range(bought + 1, (sold or now.year) + 1):
                at(year)["renewals"] += item["renew_price"]
    return [by_year[y] for y in sorted(by_year)]


def export_csv(found: list[dict]) -> str:
    """Le portefeuille en CSV (point-virgule, BOM pour Excel) : une ligne par nom, les colonnes utiles."""
    lines = [";".join(EXPORT_COLUMNS)]
    for i in found:
        opinion = i.get("ai") or {}
        cells = [i["name"], i["status_label"], i["bought_on"], i["buy_price"], i["renew_price"], i["expiry"],
                 i["expiry_source"], i["asking_price"], i["best_offer"], i["sold_on"], i["sold_price"], i["profit"],
                 (opinion.get("comment") or "").split(".")[0], opinion.get("resale"), opinion.get("final_low"),
                 opinion.get("final_high"), i["notes"]]
        # Les notes libres ne cassent pas le fichier : point-virgule et retours à la ligne remplacés.
        lines.append(";".join("" if c is None else " ".join(str(c).replace(";", ",").split()) for c in cells))
    return "\ufeff" + "\n".join(lines) + "\n"


def totals(found: list[dict]) -> dict:
    kept = [i for i in found if i["status"] in KEPT]
    sold = [i for i in found if i["status"] == "sold"]
    return {
        "count": {k: sum(1 for i in found if i["status"] == k) for k in STATUSES},
        "kept": len(kept),
        "invested": sum(i["buy_price"] or 0 for i in found),
        "yearly_renewals": sum(i["renew_price"] or 0 for i in kept),
        "sold_total": sum(i["sold_price"] or 0 for i in sold),
        "profit": sum(i["profit"] or 0 for i in sold),
        "asking_total": sum(i["asking_price"] or 0 for i in kept if i["status"] == "for_sale"),
    }


def clean(payload: dict) -> dict:
    """Champs saisis, au bon type ; les montants en euros entiers, les dates en AAAA-MM-JJ."""
    out = {}
    for key in FIELDS:
        if key not in payload:
            continue
        value = payload[key]
        if key == "status":
            # Le statut ne se vide pas : NOT NULL en base (erreur 500 sinon).
            if not isinstance(value, str) or value not in STATUSES:
                raise ValueError("Statut inconnu.")
            out[key] = value
        elif value in ("", None):
            out[key] = None
        elif key.endswith("_price") or key == "best_offer":
            if isinstance(value, bool) or not isinstance(value, (int, float, str)):
                raise ValueError("Montant invalide.")
            try:
                number = float(value)
            except ValueError:
                raise ValueError("Montant invalide.") from None
            if not math.isfinite(number) or not 0 <= number <= 100_000_000:
                raise ValueError("Montant hors limites.")
            out[key] = int(number)
        elif key in ("bought_on", "sold_on", "expires_at"):
            out[key] = datetime.fromisoformat(str(value)[:10]).date().isoformat()
        else:
            out[key] = str(value)[:500]
    return out


def due_reminders(conn, now: datetime) -> list[tuple[int, str, str]]:
    """Rappels de renouvellement à envoyer (30 puis 7 jours avant) : (id, clés séparées par des virgules, texte).
    Marqués envoyés par mark_reminded, une fois le message parti : un échec Telegram ne les perd pas."""
    found = []
    for item in items(conn, now):
        if item["status"] not in KEPT or not item["expiry"]:
            continue
        expiry = parse_iso(item["expiry"])
        left = (expiry - now).days
        row = conn.execute("SELECT reminded FROM portfolio WHERE id = ?", (item["id"],)).fetchone()
        sent = set(json.loads(row["reminded"] or "[]"))
        # Toutes les clés dues d'un coup : un nom ajouté à J-5 reçoit un rappel, pas celui de J-30 puis celui de J-7.
        due = [f"{expiry.date().isoformat()}:{days}" for days in REMIND_DAYS if left <= days]
        due = [k for k in due if k not in sent]
        if left >= 0 and due:
            price = f", renouvellement {item['renew_price']} €" if item["renew_price"] else ""
            found.append((item["id"], ",".join(due), f"{item['name']} expire le {expiry.strftime('%d/%m/%Y')} (dans {left} j){price}."))
    return found


def mark_reminded(conn, item_id: int, key: str) -> None:
    row = conn.execute("SELECT reminded FROM portfolio WHERE id = ?", (item_id,)).fetchone()
    if row is None:
        return
    sent = set(json.loads(row["reminded"] or "[]")) | set(key.split(","))
    conn.execute("UPDATE portfolio SET reminded = ? WHERE id = ?", (json.dumps(sorted(sent)), item_id))


def reminders(conn, now: datetime) -> list[str]:
    """Rappels dus, marqués envoyés tout de suite (sans Telegram)."""
    texts = []
    for item_id, key, text in due_reminders(conn, now):
        mark_reminded(conn, item_id, key)
        texts.append(text)
    return texts


def market_examples(conn, limit: int = 15) -> list[dict]:
    """Ventes et offres réelles, pour la consigne de notation."""
    found = []
    for r in conn.execute("SELECT name, sold_price, best_offer, status FROM portfolio "
                          "WHERE sold_price IS NOT NULL OR best_offer IS NOT NULL ORDER BY COALESCE(sold_on, created_at) DESC"):
        if r["sold_price"] is not None:
            found.append({"name": r["name"], "price": r["sold_price"], "kind": "vendu"})
        elif r["best_offer"] is not None:
            found.append({"name": r["name"], "price": r["best_offer"], "kind": "offre reçue"})
        if len(found) >= limit:
            break
    return found


def guide_addendum(examples: list[dict]) -> str:
    if not examples:
        return ""
    lines = [f"- {e['name']} : {e['price']} euros ({e['kind']})" for e in examples]
    return ("\n\nPrix réels connus de cet investisseur (ventes et offres reçues), à prendre comme repères "
            "pour le prix utilisateur final de noms comparables :\n" + "\n".join(lines))

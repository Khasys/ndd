"""Prix et disponibilité chez Gandi : détecte les noms premium du registre et les noms exclus.

Le RDAP dit seulement si un nom est enregistré. Un nom inconnu du registre peut pourtant être
exclu (pm.fr) ou vendu au prix premium fixé par le registre. Le registrar le sait : on lui
demande, pour les seuls noms vus libres.

Premium : prix de création nettement au-dessus du prix normal de l'extension, mesuré sur un nom
au hasard de la même extension. Les noms revendus par des tiers sont enregistrés : hors sujet ici.
"""

import asyncio
import json
import logging
import random
import string
from datetime import timedelta

import httpx

from .checker import iso, utcnow

log = logging.getLogger(__name__)

API = "https://api.gandi.net/v5/domain/check"
PREMIUM_FACTOR = 2.0  # au-delà de deux fois le prix normal : premium
NORMAL_DAYS = 7       # prix normal d'une extension, relu chaque semaine
FRESH_DAYS = 7        # prix d'un nom, relu chaque semaine
BATCH = 60            # noms par passe (une passe par heure)
DELAY = 1.0           # secondes entre deux requêtes

FILTERS = {
    "std": ("Prix standard", "available = 1 AND premium = 0"),
    "premium": ("Premium du registre", "available = 1 AND premium = 1"),
    "no": ("Indisponible chez Gandi", "available = 0"),
}


def load(conn, names: list[str]) -> dict[str, dict]:
    found = {}
    for chunk in range(0, len(names), 900):
        part = names[chunk:chunk + 900]
        rows = conn.execute(f"SELECT * FROM name_price WHERE name IN ({','.join('?' * len(part))})", part)
        for r in rows:
            found[r["name"]] = {"available": None if r["available"] is None else bool(r["available"]),
                                "status": r["status"], "price": r["price"], "normal": r["normal"],
                                "currency": r["currency"], "premium": bool(r["premium"]),
                                "at": r["checked_at"], "error": r["error"]}
    return found


def parse(body: dict) -> dict:
    """Réponse de /v5/domain/check : statut et prix de création d'un an, toutes taxes comprises."""
    products = body.get("products") or []
    product = next((p for p in products if p.get("process", "create") == "create"), products[0] if products else {})
    status = str(product.get("status") or "")
    price = None
    for p in product.get("prices") or []:
        if p.get("duration_unit", "y") == "y" and int(p.get("min_duration", 1) or 1) <= 1:
            price = p.get("price_after_taxes", p.get("price_before_taxes"))
            break
    return {"status": status, "available": status == "available", "price": None if price is None else float(price),
            "currency": body.get("currency") or "EUR"}


class Gandi:
    def __init__(self, conn, store, explorer=None, http_factory=None):
        self.conn = conn
        self.store = store
        self.explorer = explorer
        self._http_factory = http_factory or (lambda: httpx.AsyncClient(timeout=20, headers={"User-Agent": "domain-watch"}))
        self.running = False

    @property
    def configured(self) -> bool:
        return bool(self.store.get("gandi_token"))

    async def _ask(self, http, fqdn: str) -> dict:
        token = self.store.get("gandi_token")
        params = {"name": fqdn, "processes": "create", "currency": "EUR"}
        # Jeton d'accès personnel (Bearer) ; ancienne clé d'API (Apikey) en second essai.
        for scheme in ("Bearer", "Apikey"):
            response = await http.get(API, params=params, headers={"Authorization": f"{scheme} {token}"})
            if response.status_code not in (401, 403):
                break
        if response.status_code in (401, 403):
            raise PermissionError("jeton Gandi refusé")
        response.raise_for_status()
        return response.json()

    async def _normal(self, http, tld: str) -> float | None:
        """Prix de création d'un nom quelconque de l'extension : la référence du « non premium »."""
        cached = json.loads(self.store.get(f"gandi_normal_{tld}") or "{}")
        if cached.get("at") and cached["at"] > iso(utcnow() - timedelta(days=NORMAL_DAYS)):
            return cached.get("price")
        label = "".join(random.choices(string.ascii_lowercase, k=12)) + "".join(random.choices(string.digits, k=3))
        found = parse(await self._ask(http, f"{label}.{tld}"))
        price = found["price"] if found["available"] else None
        self.store.set(f"gandi_normal_{tld}", json.dumps({"at": iso(utcnow()), "price": price}))
        return price

    async def check(self, name: str, http=None) -> dict:
        if not self.configured:
            raise RuntimeError("Gandi non configuré")
        if http is None:
            async with self._http_factory() as client:
                return await self.check(name, client)
        tld = name.rpartition(".")[2]
        row = {"name": name, "available": None, "status": None, "price": None, "normal": None,
               "currency": "EUR", "premium": 0, "error": None, "raw": None}
        try:
            body = await self._ask(http, name)
            found = parse(body)
            normal = await self._normal(http, tld)
            row.update(found, raw=json.dumps(body)[:4000], normal=normal)
            row["premium"] = int(bool(found["available"] and found["price"] and normal
                                      and found["price"] > normal * PREMIUM_FACTOR))
        except PermissionError:
            raise
        except (httpx.HTTPError, ValueError) as exc:
            row["error"] = exc.__class__.__name__
        row["available"] = None if row["available"] is None else int(row["available"])
        self.conn.execute(
            """INSERT INTO name_price (name, available, status, price, normal, currency, premium, checked_at, error, raw)
               VALUES (:name, :available, :status, :price, :normal, :currency, :premium, :at, :error, :raw)
               ON CONFLICT(name) DO UPDATE SET available = excluded.available, status = excluded.status,
                 price = excluded.price, normal = excluded.normal, currency = excluded.currency,
                 premium = excluded.premium, checked_at = excluded.checked_at, error = excluded.error,
                 raw = excluded.raw""",
            {**row, "at": iso(utcnow())},
        )
        if self.explorer is not None:
            self.explorer.price_changed()
        return load(self.conn, [name])[name]

    def pending(self, limit: int) -> list[str]:
        """Noms vus libres (vérifiés par le registre) sans prix récent, les mieux notés d'abord."""
        from . import states

        after = iso(utcnow() - timedelta(days=FRESH_DAYS))
        rows = self.conn.execute(
            """SELECT d.name FROM drops d LEFT JOIN name_price p ON p.name = d.name
               WHERE d.state = ? AND (p.name IS NULL OR p.checked_at < ?)
               ORDER BY d.available_since DESC LIMIT ?""",
            (states.AVAILABLE, after, limit),
        ).fetchall()
        return [r[0] for r in rows]

    async def run(self, limit: int = BATCH) -> dict:
        if self.running or not self.configured:
            return {"started": False}
        self.running = True
        done, error = 0, None
        try:
            async with self._http_factory() as http:
                for name in self.pending(limit):
                    await self.check(name, http)
                    done += 1
                    await asyncio.sleep(DELAY)
        except PermissionError as exc:
            error = str(exc)
        except Exception as exc:  # noqa: BLE001  le journal garde la cause, l'écran l'affiche
            log.warning("Gandi : %s", exc)
            error = exc.__class__.__name__
        finally:
            self.running = False
            self.store.set("gandi_status", json.dumps({"at": iso(utcnow()), "done": done, "error": error}))
        return {"done": done, "error": error}

    def status(self) -> dict:
        info = json.loads(self.store.get("gandi_status") or "{}")
        count = self.conn.execute("SELECT COUNT(*), SUM(premium), SUM(available = 0) FROM name_price").fetchone()
        return {**info, "configured": self.configured, "running": self.running,
                "checked": count[0] or 0, "premium": count[1] or 0, "unavailable": count[2] or 0}

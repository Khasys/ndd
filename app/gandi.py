"""Gandi : disponibilité et prix d'un nom, par l'API v5 (vérification /v5/domain/check).

Source d'information seulement : rien n'est acheté. Jeton d'accès personnel (PAT) créé dans le compte
Gandi, en lecture ; gardé dans les réglages, jamais renvoyé à l'interface. Limite de l'API : 1 000
requêtes par minute et par adresse IP.

Le bouton « Tester » de Paramètres interroge quelques noms et garde leurs réponses brutes (aucun secret
dedans) : elles servent à caler la lecture des états et des prix sur ce que Gandi répond vraiment.
"""

import json
import logging

import httpx

from .checker import iso, utcnow

log = logging.getLogger(__name__)

CHECK_URL = "https://api.gandi.net/v5/domain/check"
# Un nom réservé probable (.co), des noms courants, un premium probable, un nom inventé.
SAMPLES = ("menu.co", "voyage.fr", "maison.com", "ai.io", "chat.ai", "qzkwyxv.com")


class GandiError(Exception):
    pass


class Gandi:
    def __init__(self, store, http_factory=None):
        self.store = store
        self._http_factory = http_factory or (lambda: httpx.AsyncClient(timeout=20, headers={"User-Agent": "domain-watch"}))

    @property
    def key(self) -> str:
        return (self.store.get("gandi_pat") or "").strip()

    def set_key(self, key: str) -> None:
        self.store.set("gandi_pat", "".join(key.split()))

    async def check(self, name: str, http=None) -> dict:
        """Réponse brute de Gandi pour un nom (prix en euros, création seulement)."""
        if not self.key:
            raise GandiError("Jeton Gandi non configuré")
        client = http or self._http_factory()
        try:
            response = await client.get(CHECK_URL, params={"name": name, "currency": "EUR"},
                                        headers={"Authorization": f"Bearer {self.key}"})
        except httpx.HTTPError as exc:
            raise GandiError(f"Gandi injoignable ({exc.__class__.__name__})") from None
        finally:
            if http is None:
                await client.aclose()
        if response.status_code in (401, 403):
            raise GandiError("Jeton Gandi refusé (expiré, révoqué ou sans la permission de lecture des domaines)")
        if response.status_code == 429:
            raise GandiError("Limite de requêtes Gandi atteinte")
        if response.status_code != 200:
            raise GandiError(f"Réponse Gandi inattendue ({response.status_code})")
        try:
            return response.json()
        except ValueError:
            raise GandiError("Réponse Gandi illisible") from None

    async def test(self) -> dict:
        """Interroge les noms témoins ; garde les réponses brutes et un résumé lisible."""
        samples, summary = {}, []
        async with self._http_factory() as http:
            for name in SAMPLES:
                try:
                    raw = await self.check(name, http)
                except GandiError as exc:
                    samples[name] = {"error": str(exc)}
                    summary.append({"name": name, "error": str(exc)})
                    if "refusé" in str(exc):
                        break  # jeton refusé : inutile d'insister sur les autres noms
                    continue
                samples[name] = raw
                summary.append({"name": name, **brief(raw)})
        self.store.set("gandi_samples", json.dumps({"at": iso(utcnow()), "samples": samples}))
        ok = any("error" not in s for s in summary)
        self.store.set("gandi_error", "" if ok else (summary[0]["error"] if summary else ""))
        return {"ok": ok, "results": summary}

    def samples(self) -> dict | None:
        try:
            return json.loads(self.store.get("gandi_samples") or "null")
        except ValueError:
            return None

    def status(self) -> dict:
        found = self.samples() or {}
        return {"key_set": bool(self.key), "tested_at": found.get("at"), "error": self.store.get("gandi_error") or None}


def brief(raw: dict) -> dict:
    """État et prix de création d'une réponse : provisoire, la lecture complète viendra des réponses témoins."""
    product = next((p for p in raw.get("products", []) if p.get("process", "create") == "create"), None)
    if product is None:
        return {"status": "inconnu"}
    prices = product.get("prices") or []
    first = prices[0] if prices else {}
    return {"status": product.get("status") or "inconnu", "price": first.get("price_after_taxes"),
            "type": first.get("type") or "", "currency": raw.get("currency") or "EUR"}

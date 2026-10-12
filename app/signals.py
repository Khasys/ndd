"""Signaux d'abandon d'un domaine encore enregistré : DNS, site, certificat, mail, parking."""

import asyncio
import json
import ssl
from datetime import datetime, timedelta, timezone

import dns.asyncresolver
import dns.exception
import dns.resolver
import httpx

# Serveurs DNS de plateformes de parking ou de revente. Liste courte et volontairement prudente.
PARKING_NS = ("sedoparking.com", "parkingcrew.net", "bodis.com", "above.com", "dan.com", "afternic.com")
FOR_SALE = (
    "this domain is for sale",
    "this domain may be for sale",
    "buy this domain",
    "domain is for sale",
    "ce nom de domaine est à vendre",
    "ce domaine est à vendre",
)

LABELS = {
    "no_dns": "Ne résout pas (aucune adresse IP)",
    "web_down": "Site injoignable",
    "web_error": "Site en erreur",
    "tls_invalid": "Certificat HTTPS invalide ou expiré",
    "parking": "Page de parking ou domaine à vendre",
    "no_mx": "Aucun serveur de mail (MX)",
}


async def _resolve(name: str, rdtype: str, timeout: float) -> list[str]:
    resolver = dns.asyncresolver.Resolver()
    resolver.lifetime = timeout
    try:
        answer = await resolver.resolve(name, rdtype)
    except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer, dns.resolver.NoNameservers, dns.exception.Timeout):
        return []
    return [r.to_text() for r in answer]


async def _fetch(http: httpx.AsyncClient, url: str) -> tuple[int | None, str, str | None]:
    """(code HTTP, début de la page, type d'erreur)."""
    try:
        response = await http.get(url)
    except httpx.ConnectError as exc:
        cause = exc.__cause__ or exc.__context__
        if isinstance(cause, ssl.SSLError) or "certificate" in str(exc).lower() or "ssl" in str(exc).lower():
            return None, "", "tls"
        return None, "", "down"
    except httpx.HTTPError:
        return None, "", "down"
    return response.status_code, response.text[:20000].lower(), None


async def collect(name: str, nameservers: list[str], http: httpx.AsyncClient, timeout: float = 10) -> dict:
    addresses, mx = await asyncio.gather(
        _resolve(name, "A", timeout), _resolve(name, "MX", timeout)
    )
    if not addresses:
        addresses = await _resolve(name, "AAAA", timeout)

    found: list[str] = []
    web = "absent"
    if not addresses:
        found.append("no_dns")
    else:
        status, body, error = await _fetch(http, f"https://{name}/")
        if error == "tls":
            found.append("tls_invalid")
            status, body, error = await _fetch(http, f"http://{name}/")
        if error:
            found.append("web_down")
        elif status and status >= 400:
            found.append("web_error")
            web = f"HTTP {status}"
        else:
            web = f"HTTP {status}"
        if body and any(p in body for p in FOR_SALE):
            found.append("parking")

    if any(ns.endswith(p) for ns in nameservers for p in PARKING_NS) and "parking" not in found:
        found.append("parking")
    if not mx:
        found.append("no_mx")

    return {"signals": found, "web": web, "addresses": len(addresses), "mx": len(mx)}


def suspicious(result: dict | None) -> bool:
    """Assez d'indices pour signaler le domaine. Un seul indice (pas de MX) est trop courant."""
    if not result:
        return False
    found = set(result.get("signals", []))
    return "parking" in found or len(found - {"no_mx"}) >= 2 or {"no_dns"} <= found


REGISTERED = ("active", "expiring", "expired", "redemption", "pending_delete")


async def update_one(conn, row, http: httpx.AsyncClient) -> dict:
    result = await collect(row["name"], json.loads(row["nameservers"] or "[]"), http)
    conn.execute(
        "UPDATE domains SET signals = ?, signals_checked_at = ? WHERE id = ?",
        (json.dumps(result), datetime.now(timezone.utc).replace(microsecond=0).isoformat(), row["id"]),
    )
    return result


async def update_due(conn, limit: int = 20, max_age_hours: int = 24) -> int:
    """Rafraîchit les domaines enregistrés dont les signaux datent de plus d'un jour."""
    threshold = (datetime.now(timezone.utc) - timedelta(hours=max_age_hours)).isoformat()
    rows = conn.execute(
        f"""SELECT * FROM domains WHERE state IN ({",".join("?" * len(REGISTERED))})
            AND (signals_checked_at IS NULL OR signals_checked_at < ?)
            ORDER BY signals_checked_at IS NOT NULL, signals_checked_at LIMIT ?""",
        (*REGISTERED, threshold, limit),
    ).fetchall()
    if not rows:
        return 0
    # Requêtes vers les sites eux-mêmes : en-tête de navigateur, sinon certains renvoient 403.
    async with httpx.AsyncClient(
        timeout=10, follow_redirects=True, max_redirects=5,
        headers={"User-Agent": "Mozilla/5.0 (compatible; domain-watch)"},
    ) as http:
        for row in rows:
            await update_one(conn, row, http)
    return len(rows)

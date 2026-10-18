"""Garde la dernière réponse RDAP ou WHOIS de chaque nom, pour l'afficher sans nouvelle requête."""

import json

from .checker import iso, utcnow
from .rdap import whois_only_free


def save(conn, name: str, result) -> None:
    if result is None or result.error:
        return
    info = result.info
    conn.execute(
        """INSERT INTO name_rdap (name, at, source, available, statuses, registrar, registered_at,
                                  updated_at, expires_at, nameservers)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(name) DO UPDATE SET at = excluded.at, source = excluded.source,
             available = excluded.available, statuses = excluded.statuses, registrar = excluded.registrar,
             registered_at = excluded.registered_at, updated_at = excluded.updated_at,
             expires_at = excluded.expires_at, nameservers = excluded.nameservers""",
        (
            name, iso(utcnow()), result.source, 1 if result.available else 0,
            json.dumps(info.statuses) if info else None, info.registrar if info else None,
            iso(info.registered_at) if info else None, iso(info.updated_at) if info else None,
            iso(info.expires_at) if info else None, json.dumps(info.nameservers) if info else None,
        ),
    )


def load(conn, names: list[str]) -> dict[str, dict]:
    found = {}
    for chunk in range(0, len(names), 900):
        part = names[chunk:chunk + 900]
        for row in conn.execute(f"SELECT * FROM name_rdap WHERE name IN ({','.join('?' * len(part))})", part):
            found[row["name"]] = {
                "at": row["at"], "source": row["source"], "available": bool(row["available"]),
                "statuses": json.loads(row["statuses"] or "[]"), "registrar": row["registrar"],
                "registered_at": row["registered_at"], "updated_at": row["updated_at"],
                "expires_at": row["expires_at"], "nameservers": json.loads(row["nameservers"] or "[]"),
            }
    return found


def whois_only(rdap: dict | None) -> bool:
    """Dernière réponse gardée : libre d'après le seul WHOIS, donc à confirmer (voir LookupResult.unconfirmed)."""
    return bool(rdap) and whois_only_free(rdap.get("source"), rdap.get("available"))

import json
import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import states
from .checker import Checker, parse_iso

FIELDS = {"state": "État", "registrar": "Registrar", "nameservers": "DNS", "expires_at": "Expiration"}


def local_zone() -> ZoneInfo:
    try:
        return ZoneInfo(os.environ.get("TZ") or "UTC")
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def _day(value: datetime) -> str:
    """Date en heure locale (TZ du conteneur) : une échéance à 23 h UTC tombe le lendemain à Paris."""
    return value.astimezone(local_zone()).strftime("%d/%m/%Y")


def _value(field: str, value: str | None) -> str:
    if value in (None, "", "[]"):
        return "aucun"
    if field == "state":
        return states.LABELS.get(value, value)
    if field == "expires_at":
        return _day(parse_iso(value))
    if field == "nameservers":
        return ", ".join(json.loads(value))
    return value


def build(checker: Checker, now: datetime, app_url: str = "") -> str:
    conn = checker.conn
    since = (now - timedelta(days=7)).isoformat()
    changes = conn.execute(
        """SELECT d.name, h.field, h.old_value, h.new_value FROM history h
           JOIN domains d ON d.id = h.domain_id WHERE h.at >= ? ORDER BY h.at""",
        (since,),
    ).fetchall()
    rows = conn.execute("SELECT * FROM domains ORDER BY name").fetchall()

    expiring, releases, errors = [], [], []
    for row in rows:
        if row["check_error"]:
            errors.append(row["name"])
        expiry = states.effective_expiry(row["state"], parse_iso(row["expires_at"]), now)
        if expiry and row["state"] != states.AVAILABLE and now <= expiry <= now + timedelta(days=30):
            expiring.append((expiry, row["name"]))
        release = checker.release_estimate(row, now)
        if release and release <= now + timedelta(days=30):
            releases.append((release, row["name"]))

    lines = ["Résumé de la semaine"]
    if changes:
        lines.append("\nChangements :")
        lines += [
            f"{c['name']} : {FIELDS.get(c['field'], c['field'])} {_value(c['field'], c['old_value'])} → {_value(c['field'], c['new_value'])}"
            for c in changes
        ]
    if expiring:
        lines.append("\nExpirent dans les 30 jours :")
        lines += [f"{name} : {_day(d)}" for d, name in sorted(expiring)]
    if releases:
        lines.append("\nLibérations estimées dans les 30 jours :")
        lines += [f"{name} : ~{_day(d)}" for d, name in sorted(releases)]
    if errors:
        lines.append(f"\nEn erreur de check : {', '.join(errors)}")
    if len(lines) == 1:
        lines.append(f"Rien de nouveau. {len(rows)} domaines surveillés.")
    if app_url:
        lines.append(f"\n{app_url}")
    text = "\n".join(lines)
    return text if len(text) < 4000 else text[:3990] + "\n[tronqué]"

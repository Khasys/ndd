from datetime import datetime, timedelta

from . import states
from .checker import Checker, parse_iso
from .rdap import tld_of


MAX_LINE = 75  # octets par ligne, RFC 5545 : au-delà, pliage sur une ligne qui commence par une espace


def fold(line: str) -> list[str]:
    """Plie une ligne à 75 octets, sans couper un caractère UTF-8 ; la suite commence par une espace."""
    data = line.encode("utf-8")
    if len(data) <= MAX_LINE:
        return [line]
    parts, room = [], MAX_LINE
    while data:
        cut = min(room, len(data))
        while 0 < cut < len(data) and (data[cut] & 0xC0) == 0x80:
            cut -= 1  # octet de continuation : on recule au début du caractère
        parts.append((" " if parts else "") + data[:cut].decode("utf-8"))
        data, room = data[cut:], MAX_LINE - 1
    return parts


def _escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def _event(uid: str, now: datetime, start: str, end: str, summary: str, description: str) -> list[str]:
    return [
        "BEGIN:VEVENT",
        f"UID:{uid}@domain-watch",
        f"DTSTAMP:{now.strftime('%Y%m%dT%H%M%SZ')}",
        start,
        end,
        f"SUMMARY:{_escape(summary)}",
        f"DESCRIPTION:{_escape(description)}",
        "BEGIN:VALARM",
        "TRIGGER:-P1D",
        "ACTION:DISPLAY",
        f"DESCRIPTION:{_escape(summary)}",
        "END:VALARM",
        "END:VEVENT",
    ]


def _all_day(day: datetime) -> tuple[str, str]:
    return f"DTSTART;VALUE=DATE:{day.strftime('%Y%m%d')}", f"DTEND;VALUE=DATE:{(day + timedelta(days=1)).strftime('%Y%m%d')}"


def build(checker: Checker, now: datetime, app_url: str = "") -> str:
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//domain-watch//FR",
        "CALSCALE:GREGORIAN",
        "X-WR-CALNAME:domain-watch",
    ]
    link = f"\n{app_url}" if app_url else ""
    for row in checker.conn.execute("SELECT * FROM domains ORDER BY name").fetchall():
        name = row["name"]
        expiry = states.effective_expiry(row["state"], parse_iso(row["expires_at"]), now)
        if expiry and row["state"] != states.AVAILABLE:
            lines += _event(
                f"{name}-expiration", now, *_all_day(expiry), f"Expiration {name}",
                f"État : {states.LABELS.get(row['state'], 'inconnu')}{link}",
            )
        release = checker.release_estimate(row, now)
        if release:
            if states.drop_time(tld_of(name), checker.delays):
                start = f"DTSTART:{release.strftime('%Y%m%dT%H%M%SZ')}"
                end = f"DTEND:{(release + timedelta(hours=1)).strftime('%Y%m%dT%H%M%SZ')}"
            else:
                start, end = _all_day(release)
            lines += _event(
                f"{name}-liberation", now, start, end, f"Libération estimée {name}",
                f"Estimation calculée depuis le statut et les délais du registre, pas une date garantie.{link}",
            )
    lines.append("END:VCALENDAR")
    # RFC 5545 : lignes en CRLF, pliées à 75 octets.
    return "\r\n".join(part for line in lines for part in fold(line)) + "\r\n"

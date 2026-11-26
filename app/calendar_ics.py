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


# Libération à l'heure connue (lot de l'Afnic, passage de Verisign) : les mêmes rappels que Telegram.
TIMED_ALARMS = ("-P1D", "-PT3H", "-PT1H")


def _event(uid: str, now: datetime, start: str, end: str, summary: str, description: str,
           alarms: tuple[str, ...] = ("-P1D",)) -> list[str]:
    lines = [
        "BEGIN:VEVENT",
        f"UID:{uid}@domain-watch",
        f"DTSTAMP:{now.strftime('%Y%m%dT%H%M%SZ')}",
        start,
        end,
        f"SUMMARY:{_escape(summary)}",
        f"DESCRIPTION:{_escape(description)}",
    ]
    for trigger in alarms:
        lines += ["BEGIN:VALARM", f"TRIGGER:{trigger}", "ACTION:DISPLAY", f"DESCRIPTION:{_escape(summary)}", "END:VALARM"]
    return lines + ["END:VEVENT"]


def _all_day(day: datetime) -> tuple[str, str]:
    return f"DTSTART;VALUE=DATE:{day.strftime('%Y%m%d')}", f"DTEND;VALUE=DATE:{(day + timedelta(days=1)).strftime('%Y%m%d')}"


def _head() -> list[str]:
    return ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//domain-watch//FR", "CALSCALE:GREGORIAN",
            "X-WR-CALNAME:domain-watch"]


def _close(lines: list[str]) -> str:
    lines.append("END:VCALENDAR")
    # RFC 5545 : lignes en CRLF, pliées à 75 octets.
    return "\r\n".join(part for line in lines for part in fold(line)) + "\r\n"


def _release_event(checker: Checker, row, now: datetime, app_url: str) -> list[str]:
    """La libération estimée d'un nom suivi : à l'heure du registre quand elle est connue (lot de l'Afnic, passage de
    Verisign), avec rappels 24 h, 3 h et 1 h avant ; sinon le jour, avec rappel la veille. Rien sans estimation."""
    from .notify import RESTORE_NOTE, release_lines  # import local : notify importe des modules lourds
    name = row["name"]
    release = checker.release_estimate(row, now)
    if not release:
        return []
    link = f"\n{app_url}/?fiche={name}" if app_url else ""
    if states.has_drop_time(tld_of(name), checker.delays):
        when, what = release_lines(name, release, now, checker.delays)
        return _event(
            f"{name}-liberation", now, f"DTSTART:{release.strftime('%Y%m%dT%H%M%SZ')}",
            f"DTEND:{(release + timedelta(minutes=30)).strftime('%Y%m%dT%H%M%SZ')}", f"Libération {name}",
            f"{what[0].upper()}{what[1:]}{RESTORE_NOTE.get(row['state'], '')}. Estimation tirée de l'état du "
            f"registre, pas une date garantie.{link}", TIMED_ALARMS)
    return _event(f"{name}-liberation", now, *_all_day(release), f"Libération estimée {name}",
                  f"Estimation calculée depuis le statut et les délais du registre, pas une date garantie.{link}")


def build(checker: Checker, now: datetime, app_url: str = "") -> str:
    lines = _head()
    link = f"\n{app_url}" if app_url else ""
    for row in checker.conn.execute("SELECT * FROM domains ORDER BY name").fetchall():
        name = row["name"]
        expiry = states.effective_expiry(row["state"], parse_iso(row["expires_at"]), now)
        if expiry and row["state"] != states.AVAILABLE:
            lines += _event(
                f"{name}-expiration", now, *_all_day(expiry), f"Expiration {name}",
                f"État : {states.LABELS.get(row['state'], 'inconnu')}{link}",
            )
        lines += _release_event(checker, row, now, app_url)
    return _close(lines)


def build_one(checker: Checker, name: str, now: datetime, app_url: str = "") -> str | None:
    """Le seul événement de libération d'un nom suivi, pour le lien « Ajouter au calendrier » des rappels Telegram :
    ouvert sur iPhone, il propose d'ajouter l'événement et ses rappels. None : nom pas suivi ou sans estimation."""
    row = checker.conn.execute("SELECT * FROM domains WHERE name = ?", (name,)).fetchone()
    event = _release_event(checker, row, now, app_url) if row else []
    return _close(_head() + event) if event else None

import asyncio
import re
from datetime import datetime, timezone

from .rdap import DomainInfo

IANA_WHOIS = "whois.iana.org"
MAX_RESPONSE = 256 * 1024

# Chaque registre a son format. On ne lit que des lignes « clé: valeur » dont la clé est connue ;
# l'ordre compte : la première clé trouvée l'emporte.
EXPIRY_KEYS = [
    "registry expiry date", "expiry date", "expiration date", "registrar registration expiration date",
    "expire date", "expires on", "expires", "expire", "paid-till", "renewal date", "valid until",
]
CREATED_KEYS = [
    "creation date", "created on", "created", "registered on", "registration date", "registered date", "registered",
    "domain registration date",
]
UPDATED_KEYS = ["updated date", "last updated", "last update", "last-update", "last modified", "modified", "changed"]
REGISTRAR_KEYS = ["registrar", "sponsoring registrar", "registrar name", "registrar organization", "registrar organisation"]
NS_KEYS = ["name server", "nameserver", "nameservers", "name servers", "nserver", "name servers name"]
STATUS_KEYS = ["domain status", "status", "state", "registration status"]

# Ne conclure « libre » que sur une formule explicite du registre, et seulement si la réponse
# ne contient aucune donnée d'enregistrement.
NOT_FOUND = [
    "no match for", "not found", "no data found", "no entries found", "no object found",
    "does not exist", "no matching record", "is available for registration", "not registered",
    "has not been registered", "status: free", "status: available", "no information available",
    "nothing found", "do not have an entry", "no match",
]
RATE_LIMITED = ["limit exceeded", "quota exceeded", "too many", "try again later", "rate limit", "excessive"]

_BRACKET = re.compile(r"^\s*\[([^\]]{1,60})\]\s+(.+?)\s*$")
_LINE = re.compile(r"^\s*([A-Za-z][A-Za-z0-9 ._/-]{1,60}?)\s*:\s*(.*?)\s*$")
_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


class WhoisError(Exception):
    pass


def parse_date(value: str) -> datetime | None:
    value = value.strip().split(" (")[0].strip()
    if not value:
        return None
    iso = re.match(r"^(\d{4})[-./](\d{2})[-./](\d{2})(?:[T ](\d{2}):(\d{2})(?::(\d{2}))?)?", value)
    if iso:
        y, mo, d, h, mi, s = (int(x) if x else 0 for x in iso.groups())
        return datetime(y, mo, d, h, mi, s, tzinfo=timezone.utc)
    # 24-Feb-2027, 24 Feb 2027
    named = re.match(r"^(\d{1,2})[- ]([A-Za-z]{3})[a-z]*[- ](\d{4})", value)
    if named and named.group(2).lower() in _MONTHS:
        return datetime(int(named.group(3)), _MONTHS[named.group(2).lower()], int(named.group(1)), tzinfo=timezone.utc)
    # 31.03.2027 : jour en premier. Le format 03/31/2027 est ambigu, on ne le lit pas.
    dotted = re.match(r"^(\d{2})\.(\d{2})\.(\d{4})", value)
    if dotted:
        return datetime(int(dotted.group(3)), int(dotted.group(2)), int(dotted.group(1)), tzinfo=timezone.utc)
    return None


def _fields(text: str) -> dict[str, list[str]]:
    """Lit trois formats : « clé: valeur », « [clé] valeur » (.jp), et les blocs
    « Clé: » suivis de lignes indentées (.eu, .gg), où « Registrar: / Name: X »
    devient « registrar name ».
    """
    fields: dict[str, list[str]] = {}
    section = None
    for line in text.splitlines():
        if not line.strip():
            section = None
            continue
        if line.lstrip().startswith(("%", "#", ">>>")):
            continue
        indented = line[:1] in (" ", "\t")
        bracket = _BRACKET.match(line)
        match = _LINE.match(line)
        if bracket:
            key, value = bracket.group(1), bracket.group(2)
        elif indented and section:
            if match and match.group(2):
                key, value = f"{section} {match.group(1).strip().lower()}", match.group(2)
            else:
                key, value = section, line.strip()
        elif match:
            key, value = match.group(1), match.group(2)
            if not value:
                section = key.strip().lower()
                continue
            section = None
        else:
            continue
        fields.setdefault(key.strip().lower(), []).append(value.strip())
    return fields


def _first(fields: dict[str, list[str]], keys: list[str]) -> str | None:
    for key in keys:
        if fields.get(key):
            return fields[key][0]
    return None


def _date(fields: dict[str, list[str]], keys: list[str]) -> datetime | None:
    for key in keys:
        for value in fields.get(key, []):
            parsed = parse_date(value)
            if parsed:
                return parsed
    return None


def _short_lines(text: str) -> list[str]:
    return [" ".join(line.split()).lower() for line in text.splitlines() if len(line) <= 160]


def rate_limited(text: str) -> bool:
    """Formule de limite de requêtes dans une ligne courte (voir parse_whois)."""
    return any(p in line for line in _short_lines(text) for p in RATE_LIMITED)


def parse_whois(text: str) -> DomainInfo | None:
    """DomainInfo si le domaine est enregistré, None s'il est explicitement libre.

    Lève WhoisError si la réponse n'est pas interprétable : mieux vaut une erreur de check
    qu'un faux « disponible ».
    """
    # Formules cherchées ligne par ligne, espaces normalisés, et seulement dans les lignes courtes :
    # les conditions d'utilisation, en longs paragraphes, citent « too many queries » ou « not found ».
    short_lines = _short_lines(text)
    fields = _fields(text)
    info = DomainInfo()
    info.expires_at = _date(fields, EXPIRY_KEYS)
    info.registered_at = _date(fields, CREATED_KEYS)
    info.updated_at = _date(fields, UPDATED_KEYS)
    info.last_changed_at = info.updated_at
    registrar = _first(fields, REGISTRAR_KEYS)
    info.registrar = registrar if registrar and not registrar.lower().startswith("http") else None
    info.nameservers = sorted({
        v.split()[0].lower().rstrip(".") for k in NS_KEYS for v in fields.get(k, []) if v.split()
    })
    statuses = []
    for key in STATUS_KEYS:
        for value in fields.get(key, []):
            # « clientHold https://icann.org/epp#clientHold » : le code seul. Sinon la valeur entière
            # (« NOT AVAILABLE », « REGISTERED, DELEGATED »).
            token = value.split()[0] if "http" in value else value.strip().rstrip(",")
            if token and token not in statuses:
                statuses.append(token)
        if statuses:
            break
    info.statuses = statuses

    free_status = {s.lower() for s in statuses} <= {"free", "available"}
    registered = info.expires_at or info.registered_at or info.registrar or info.nameservers or not free_status
    if registered:
        return info
    if any(p in line for line in short_lines for p in NOT_FOUND):
        return None
    if rate_limited(text):
        raise WhoisError("Limite de requêtes WHOIS du registre atteinte")
    raise WhoisError("Réponse WHOIS non reconnue")


def referral(text: str) -> str | None:
    for key in ("whois", "refer"):
        values = _fields(text).get(key)
        if values:
            return values[0].split()[0].lower()
    return None


async def query(server: str, name: str, timeout: float) -> str:
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_connection(server, 43), timeout)
    except (OSError, asyncio.TimeoutError) as exc:
        raise WhoisError(f"Serveur WHOIS {server} injoignable ({exc.__class__.__name__})") from None
    try:
        writer.write(f"{name}\r\n".encode("idna" if not name.isascii() else "ascii"))
        await writer.drain()
        data = b""
        async with asyncio.timeout(timeout):
            while len(data) < MAX_RESPONSE:
                chunk = await reader.read(8192)
                if not chunk:
                    break
                data += chunk
    except (OSError, asyncio.TimeoutError) as exc:
        raise WhoisError(f"Serveur WHOIS {server} muet ({exc.__class__.__name__})") from None
    finally:
        writer.close()
    return data.decode("utf-8", errors="replace")

import asyncio
import email.utils
import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from publicsuffixlist import PublicSuffixList

log = logging.getLogger(__name__)

BOOTSTRAP_URL = "https://data.iana.org/rdap/dns.json"
BOOTSTRAP_TTL = 24 * 3600

# Registres qui ont un RDAP fonctionnel mais absent du bootstrap IANA. Le bootstrap reste
# prioritaire. N'ajouter une entrée qu'après avoir vérifié un domaine enregistré (200) et un
# domaine libre (404) : un serveur qui ne connaît pas le TLD répond aussi 404.
EXTRA_SERVERS = {
    # Vérifié le 2026-10-12 : notion.so -> 200, nom aléatoire -> 404.
    "so": "https://rdap.nic.so/",
}

# Section ICANN seule : les suffixes privés (github.io...) n'ont pas de registre RDAP.
_PSL = PublicSuffixList(only_icann=True)
_LABEL = re.compile(r"^(?!-)[a-z0-9-]{1,63}(?<!-)$")


class InvalidDomain(ValueError):
    pass


def clean_domain(raw: str) -> str:
    value = raw.strip().lower()
    value = re.sub(r"^[a-z][a-z0-9+.-]*://", "", value)
    value = re.split(r"[/?#]", value, maxsplit=1)[0]
    value = value.rsplit("@", 1)[-1]
    value = value.split(":", 1)[0].strip(".")
    if value.startswith("www.") and value.count(".") >= 2:
        value = value[4:]
    if not value:
        raise InvalidDomain("Nom de domaine vide.")
    try:
        value = value.encode("idna").decode("ascii")
    except UnicodeError:
        raise InvalidDomain(f"Nom de domaine invalide : {raw.strip()}") from None
    labels = value.split(".")
    if len(labels) < 2 or not all(_LABEL.match(label) for label in labels) or len(value) > 253:
        raise InvalidDomain(f"Nom de domaine invalide : {raw.strip()}")
    if labels[-1].isdigit():
        raise InvalidDomain(f"Nom de domaine invalide : {raw.strip()}")
    registrable = _PSL.privatesuffix(value)
    if registrable is None:
        raise InvalidDomain(f"{value} est une extension, pas un nom de domaine.")
    if registrable != value:
        raise InvalidDomain(f"{value} est un sous-domaine. Surveiller {registrable} à la place.")
    return value


def tld_of(domain: str) -> str:
    return domain.rsplit(".", 1)[-1]


@dataclass
class DomainInfo:
    statuses: list[str] = field(default_factory=list)
    registered_at: datetime | None = None
    updated_at: datetime | None = None
    expires_at: datetime | None = None
    last_changed_at: datetime | None = None
    registrar: str | None = None
    nameservers: list[str] = field(default_factory=list)


@dataclass
class LookupResult:
    found: bool = False
    available: bool = False
    info: DomainInfo | None = None
    error: str | None = None
    source: str = "rdap"


def _parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _vcard_name(entity: dict) -> str | None:
    vcard = entity.get("vcardArray")
    if not isinstance(vcard, list) or len(vcard) < 2:
        return None
    for prop in vcard[1]:
        if isinstance(prop, list) and len(prop) >= 4 and prop[0] == "fn" and prop[3]:
            return str(prop[3]).strip()
    return None


def _find_registrar(entities: list) -> str | None:
    for entity in entities or []:
        if "registrar" in (entity.get("roles") or []):
            name = _vcard_name(entity)
            if name:
                return name
            for public_id in entity.get("publicIds") or []:
                if public_id.get("identifier"):
                    return f"IANA {public_id['identifier']}"
            if entity.get("handle"):
                return str(entity["handle"])
        nested = _find_registrar(entity.get("entities") or [])
        if nested:
            return nested
    return None


def parse_domain(data: dict) -> DomainInfo:
    info = DomainInfo()
    info.statuses = [str(s) for s in data.get("status") or []]
    for event in data.get("events") or []:
        action = (event.get("eventAction") or "").lower()
        date = _parse_date(event.get("eventDate"))
        if action == "registration":
            info.registered_at = date
        elif action == "expiration":
            info.expires_at = date
        elif action == "last changed":
            info.updated_at = date
            info.last_changed_at = date
    info.registrar = _find_registrar(data.get("entities") or [])
    info.nameservers = sorted(
        {
            ns["ldhName"].lower().rstrip(".")
            for ns in data.get("nameservers") or []
            if ns.get("ldhName")
        }
    )
    return info


def _retry_after(response: httpx.Response) -> float | None:
    header = response.headers.get("Retry-After")
    if not header:
        return None
    if header.strip().isdigit():
        return float(header)
    try:
        when = email.utils.parsedate_to_datetime(header)
    except (TypeError, ValueError):
        return None
    return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())


class RdapClient:
    def __init__(
        self,
        cache_path: Path,
        min_delay: float,
        timeout: float,
        retries: int,
        backoff: float,
        max_retry_after: float,
        transport: httpx.AsyncBaseTransport | None = None,
        whois_min_delay: float = 5.0,
        whois_query=None,
    ):
        self.cache_path = cache_path
        self.whois_min_delay = whois_min_delay
        self.timeout = timeout
        self.min_delay = min_delay
        self.retries = retries
        self.backoff = backoff
        self.max_retry_after = max_retry_after
        self.http = httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=True,
            headers={"Accept": "application/rdap+json, application/json", "User-Agent": "domain-watch"},
            transport=transport,
        )
        self._bootstrap: dict[str, str] | None = None
        self._bootstrap_at = 0.0
        self._bootstrap_lock = asyncio.Lock()
        self._host_locks: dict[str, asyncio.Lock] = {}
        self._host_next: dict[str, float] = {}
        self._whois_servers: dict[str, str | None] = {}
        self._whois_query = whois_query

    async def close(self) -> None:
        await self.http.aclose()

    @staticmethod
    def _index(data: dict) -> dict[str, str]:
        index = {}
        for tlds, urls in data.get("services", []):
            url = next((u for u in urls if u.startswith("https://")), urls[0] if urls else None)
            if not url:
                continue
            for tld in tlds:
                index[tld.lower()] = url
        return index

    async def _load_bootstrap(self) -> dict[str, str]:
        async with self._bootstrap_lock:
            if self._bootstrap is not None and time.time() - self._bootstrap_at < BOOTSTRAP_TTL:
                return self._bootstrap
            cached_at = self.cache_path.stat().st_mtime if self.cache_path.exists() else 0.0
            if time.time() - cached_at < BOOTSTRAP_TTL:
                self._bootstrap = self._index(json.loads(self.cache_path.read_text()))
                self._bootstrap_at = cached_at
                return self._bootstrap
            try:
                response = await self.http.get(BOOTSTRAP_URL)
                response.raise_for_status()
                data = response.json()
                self.cache_path.parent.mkdir(parents=True, exist_ok=True)
                self.cache_path.write_text(json.dumps(data))
                self._bootstrap = self._index(data)
                self._bootstrap_at = time.time()
            except (httpx.HTTPError, ValueError) as exc:
                # Un bootstrap périmé vaut mieux que pas de bootstrap : les serveurs RDAP changent rarement.
                if self.cache_path.exists():
                    log.warning("Bootstrap IANA injoignable (%s), cache périmé utilisé", exc)
                    self._bootstrap = self._index(json.loads(self.cache_path.read_text()))
                    self._bootstrap_at = time.time() - BOOTSTRAP_TTL + 3600
                else:
                    raise
            return self._bootstrap

    async def server_for(self, domain: str) -> str | None:
        tld = tld_of(domain)
        return (await self._load_bootstrap()).get(tld) or EXTRA_SERVERS.get(tld)

    async def _wait_turn(self, host: str) -> None:
        delay = self._host_next.get(host, 0.0) - time.monotonic()
        if delay > 0:
            await asyncio.sleep(delay)

    def _push_next(self, host: str, seconds: float) -> None:
        self._host_next[host] = max(self._host_next.get(host, 0.0), time.monotonic() + seconds)

    async def lookup(self, domain: str) -> LookupResult:
        try:
            base = await self.server_for(domain)
        except (httpx.HTTPError, ValueError) as exc:
            return LookupResult(error=f"Bootstrap IANA indisponible : {exc.__class__.__name__}")
        if not base:
            return await self._lookup_whois(domain)
        url = f"{base.rstrip('/')}/domain/{domain}"
        host = urlsplit(url).netloc
        lock = self._host_locks.setdefault(host, asyncio.Lock())

        # Le verrou par serveur sérialise toutes les requêtes vers un même registre,
        # attentes de retry comprises : le mode rapide ne peut pas dépasser la cadence.
        async with lock:
            last_error = "Erreur inconnue"
            for attempt in range(self.retries + 1):
                await self._wait_turn(host)
                wait = self.backoff * (2**attempt)
                try:
                    response = await self.http.get(url)
                except httpx.HTTPError as exc:
                    last_error = f"Erreur réseau : {exc.__class__.__name__}"
                    self._push_next(host, max(self.min_delay, wait))
                    continue
                self._push_next(host, self.min_delay)

                if response.status_code == 404:
                    return LookupResult(available=True)
                if response.status_code == 200:
                    try:
                        data = response.json()
                    except ValueError:
                        last_error = "Réponse RDAP illisible"
                        continue
                    return LookupResult(found=True, info=parse_domain(data))
                if response.status_code == 429 or response.status_code >= 500:
                    retry_after = _retry_after(response)
                    if retry_after is not None and retry_after > self.max_retry_after:
                        self._push_next(host, retry_after)
                        return LookupResult(
                            error=f"Limite de requêtes du registre atteinte, reprise dans {int(retry_after)} s"
                        )
                    last_error = (
                        "Limite de requêtes du registre atteinte (429)"
                        if response.status_code == 429
                        else f"Serveur RDAP en erreur ({response.status_code})"
                    )
                    self._push_next(host, retry_after if retry_after is not None else wait)
                    continue
                return LookupResult(error=f"Réponse RDAP inattendue ({response.status_code})")
            return LookupResult(error=last_error)

    async def _whois(self, server: str, name: str) -> str:
        from . import whois

        query = self._whois_query or whois.query
        lock = self._host_locks.setdefault(f"whois:{server}", asyncio.Lock())
        async with lock:
            last: Exception | None = None
            for attempt in range(self.retries + 1):
                await self._wait_turn(f"whois:{server}")
                try:
                    text = await query(server, name, self.timeout)
                except whois.WhoisError as exc:
                    last = exc
                    self._push_next(f"whois:{server}", max(self.whois_min_delay, self.backoff * (2**attempt)))
                    continue
                self._push_next(f"whois:{server}", self.whois_min_delay)
                return text
            raise last

    async def whois_server(self, tld: str) -> str | None:
        from . import whois

        if tld not in self._whois_servers:
            self._whois_servers[tld] = whois.referral(await self._whois(whois.IANA_WHOIS, tld))
        return self._whois_servers[tld]

    async def _lookup_whois(self, domain: str) -> LookupResult:
        from . import whois

        tld = tld_of(domain)
        try:
            server = await self.whois_server(tld)
            if not server:
                return LookupResult(error=f"Ni RDAP ni WHOIS publié pour .{tld}", source="whois")
            info = whois.parse_whois(await self._whois(server, domain))
        except whois.WhoisError as exc:
            return LookupResult(error=str(exc), source="whois")
        if info is None:
            return LookupResult(available=True, source="whois")
        return LookupResult(found=True, info=info, source="whois")

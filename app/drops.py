"""Noms de 4 caractères qui quittent les zones ICANN (CZDS) : suivi jusqu'à leur libération.

Chaque jour, la zone d'une extension est lue en flux. Les noms de 4 caractères présents
sont notés dans une carte de bits (37^4 cases, 234 Ko). Un nom présent la veille et
absent ce jour a quitté la zone : rédemption, suppression en cours ou suspension.
Une vérification RDAP dit lequel, et l'estimation de libération suit les délais sourcés.
"""

import asyncio
import json
import logging
import re
import time
import zlib
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import AsyncIterator, Iterable

import httpx

from . import digest, states
from .checker import iso, parse_iso, utcnow
from .store import Store

log = logging.getLogger(__name__)

AUTH_URL = "https://account-api.icann.org/api/authenticate"
LINKS_URL = "https://czds-api.icann.org/czds/downloads/links"

CHARSET = "abcdefghijklmnopqrstuvwxyz0123456789-"
BASE = len(CHARSET)
CELLS = BASE**4
BITMAP_BYTES = (CELLS + 7) // 8
_INDEX = {c: i for i, c in enumerate(CHARSET)}

KINDS = {"letters": "Lettres", "digits": "Chiffres", "mixed": "Lettres et chiffres", "hyphen": "Avec tiret"}

# États propres à cet écran, en plus de ceux de states.
SUSPENDED = "suspended"  # hors zone mais ni en rédemption ni en suppression (hold, litige)
TRACKED = (states.EXPIRED, states.REDEMPTION, states.PENDING_DELETE)

KEEP_AVAILABLE_DAYS = 7
# Au-delà de cette part de noms disparus en un jour, la zone est jugée anormale et ignorée.
MAX_DAILY_REMOVED_SHARE = 0.05
KEEP_SUSPENDED_DAYS = 45


class CzdsError(Exception):
    pass


# --- Noms et carte de bits -------------------------------------------------------------


def encode(label: str) -> int:
    a, b, c, d = (_INDEX[ch] for ch in label)
    return ((a * BASE + b) * BASE + c) * BASE + d


def decode(index: int) -> str:
    chars = []
    for _ in range(4):
        index, rest = divmod(index, BASE)
        chars.append(CHARSET[rest])
    return "".join(reversed(chars))


def valid_label(label: str) -> bool:
    return len(label) == 4 and all(c in _INDEX for c in label) and label[0] != "-" and label[-1] != "-"


def kind_of(label: str) -> str:
    if "-" in label:
        return "hyphen"
    if label.isalpha():
        return "letters"
    if label.isdigit():
        return "digits"
    return "mixed"


def _line_pattern(tld: str) -> re.Pattern:
    # Première colonne d'un enregistrement : « abcd.com. ». Majuscules tolérées.
    return re.compile(rb"^([a-z0-9-]{4})\." + re.escape(tld.encode()) + rb"\.?[ \t]", re.MULTILINE | re.IGNORECASE)


class ZoneScanner:
    """Lit une zone gzip morceau par morceau et remplit la carte de bits des noms de 4 caractères."""

    def __init__(self, tld: str):
        self.pattern = _line_pattern(tld)
        self.bits = bytearray(BITMAP_BYTES)
        self._inflate = zlib.decompressobj(16 + zlib.MAX_WBITS)
        self._rest = b""
        self.bytes_in = 0

    def feed(self, chunk: bytes) -> None:
        self.bytes_in += len(chunk)
        data = self._inflate.decompress(chunk)
        # Un gzip peut enchaîner plusieurs membres.
        while self._inflate.eof and self._inflate.unused_data:
            tail = self._inflate.unused_data
            self._inflate = zlib.decompressobj(16 + zlib.MAX_WBITS)
            data += self._inflate.decompress(tail)
        self._scan(data, final=False)

    def finish(self) -> bytearray:
        data = self._inflate.flush()
        if not self._inflate.eof:
            # Fin de flux gzip absente : téléchargement coupé. Comparer une zone tronquée
            # ferait passer des milliers de noms pour supprimés.
            raise zlib.error("zone tronquée")
        self._scan(data, final=True)
        return self.bits

    def _scan(self, data: bytes, final: bool) -> None:
        buffer = self._rest + data
        if final:
            cut = len(buffer)
        else:
            cut = buffer.rfind(b"\n") + 1
        complete, self._rest = buffer[:cut], buffer[cut:]
        if final and complete and not complete.endswith(b"\n"):
            complete += b"\n"
        for match in self.pattern.finditer(complete):
            label = match.group(1).decode().lower()
            if label[0] != "-" and label[-1] != "-":
                index = encode(label)
                self.bits[index >> 3] |= 1 << (index & 7)


def scan_bytes(chunks: Iterable[bytes], tld: str) -> bytearray:
    scanner = ZoneScanner(tld)
    for chunk in chunks:
        scanner.feed(chunk)
    return scanner.finish()


def count(bits: bytes) -> int:
    return sum(bin(b).count("1") for b in bits)


def diff(previous: bytes, current: bytes) -> tuple[list[str], list[str]]:
    """(noms sortis de la zone, noms entrés dans la zone)."""
    removed, added = [], []
    for i, (p, c) in enumerate(zip(previous, current)):
        if p == c:
            continue
        for bit in range(8):
            mask = 1 << bit
            if p & mask and not c & mask:
                removed.append(decode(i * 8 + bit))
            elif c & mask and not p & mask:
                added.append(decode(i * 8 + bit))
    return removed, added


# --- Accès CZDS ------------------------------------------------------------------------


class Czds:
    """Client minimal de l'API CZDS d'ICANN : jeton, liste des zones autorisées, téléchargement."""

    TOKEN_TTL = 23 * 3600

    def __init__(self, http: httpx.AsyncClient, username: str, password: str):
        self.http = http
        self.username = username
        self.password = password
        self._token: tuple[str, float] | None = None

    async def token(self, force: bool = False) -> str:
        if not force and self._token and self._token[1] > time.monotonic():
            return self._token[0]
        try:
            response = await self.http.post(
                AUTH_URL,
                json={"username": self.username, "password": self.password},
                headers={"Accept": "application/json"},
            )
        except httpx.HTTPError as exc:
            raise CzdsError(f"ICANN injoignable ({exc.__class__.__name__})") from None
        if response.status_code == 401:
            raise CzdsError("Identifiant ou mot de passe ICANN refusé.")
        if response.status_code != 200:
            raise CzdsError(f"Connexion ICANN refusée (HTTP {response.status_code}).")
        try:
            value = response.json()["accessToken"]
        except (ValueError, KeyError):
            raise CzdsError("Réponse ICANN illisible.") from None
        self._token = (value, time.monotonic() + self.TOKEN_TTL)
        return value

    async def _get(self, url: str, stream: bool = False) -> httpx.Response:
        for attempt in range(2):
            headers = {"Authorization": f"Bearer {await self.token(force=attempt > 0)}"}
            request = self.http.build_request("GET", url, headers=headers)
            try:
                response = await self.http.send(request, stream=stream)
            except httpx.HTTPError as exc:
                raise CzdsError(f"CZDS injoignable ({exc.__class__.__name__})") from None
            if response.status_code == 401 and attempt == 0:
                await response.aclose()
                continue
            return response
        return response

    async def links(self) -> dict[str, str]:
        """Zones autorisées pour ce compte : extension vers URL de téléchargement."""
        response = await self._get(LINKS_URL)
        if response.status_code != 200:
            raise CzdsError(f"Liste des zones CZDS indisponible (HTTP {response.status_code}).")
        found = {}
        for url in response.json():
            name = str(url).rstrip("/").rsplit("/", 1)[-1]
            if name.endswith(".zone"):
                found[name[: -len(".zone")].lower()] = url
        return dict(sorted(found.items()))

    async def chunks(self, url: str) -> AsyncIterator[bytes]:
        response = await self._get(url, stream=True)
        try:
            if response.status_code != 200:
                raise CzdsError(f"Téléchargement refusé (HTTP {response.status_code}).")
            async for chunk in response.aiter_bytes(1 << 20):
                yield chunk
        finally:
            await response.aclose()


# --- Suivi quotidien -------------------------------------------------------------------


@dataclass
class ScanResult:
    tld: str
    names: int = 0
    removed: int = 0
    added: int = 0
    first: bool = False
    error: str | None = None


class DropWatcher:
    def __init__(self, conn, store: Store, data_dir: Path, rdap, delays: dict, http_factory=None):
        self.conn = conn
        self.store = store
        self.dir = data_dir / "zones"
        self.rdap = rdap
        self.delays = delays
        self._http_factory = http_factory or (
            lambda: httpx.AsyncClient(timeout=httpx.Timeout(60, read=300), follow_redirects=True)
        )
        self.running = False
        self._czds: Czds | None = None

    # Réglages

    @property
    def username(self) -> str:
        return self.store.get("czds_username").strip()

    @property
    def configured(self) -> bool:
        return bool(self.username and self.store.get("czds_password"))

    def approved(self) -> list[str]:
        return json.loads(self.store.get("czds_approved") or "[]")

    def enabled(self) -> list[str]:
        raw = self.store.get("czds_enabled")
        return json.loads(raw) if raw else self.approved()

    def status(self, tld: str) -> dict:
        return json.loads(self.store.get(f"zone_{tld}") or "{}")

    def last_run(self) -> str | None:
        return self.store.get("czds_last_run") or None

    def _client(self, http: httpx.AsyncClient) -> Czds:
        return Czds(http, self.username, self.store.get("czds_password"))

    async def refresh_approved(self) -> list[str]:
        async with self._http_factory() as http:
            links = await self._client(http).links()
        self.store.set("czds_approved", json.dumps(list(links)))
        return list(links)

    # Lecture des zones

    def _bitmap_path(self, tld: str) -> Path:
        return self.dir / f"{tld}.bits"

    def _load(self, tld: str) -> bytes | None:
        path = self._bitmap_path(tld)
        if path.exists() and path.stat().st_size == BITMAP_BYTES:
            return path.read_bytes()
        return None

    def _save(self, tld: str, bits: bytes) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self._bitmap_path(tld).with_suffix(".tmp")
        tmp.write_bytes(bits)
        tmp.replace(self._bitmap_path(tld))

    async def scan_zone(self, czds: Czds, tld: str, url: str) -> ScanResult:
        result = ScanResult(tld)
        started = time.monotonic()
        scanner = ZoneScanner(tld)
        try:
            async for chunk in czds.chunks(url):
                # Décompression et recherche dans un thread : l'interface reste réactive
                # pendant les minutes que prend une grosse zone comme .com.
                await asyncio.to_thread(scanner.feed, chunk)
            bits = await asyncio.to_thread(scanner.finish)
        except CzdsError as exc:
            result.error = str(exc)
        except zlib.error:
            result.error = "Fichier de zone corrompu ou incomplet."
        except httpx.HTTPError as exc:
            result.error = f"Téléchargement interrompu ({exc.__class__.__name__})."
        if result.error:
            self._set_status(tld, result, started, scanner.bytes_in)
            return result

        previous = self._load(tld)
        result.names = count(bits)
        if previous is None:
            result.first = True
        else:
            removed, added = diff(previous, bits)
            before = count(previous)
            if before >= 1000 and len(removed) > before * MAX_DAILY_REMOVED_SHARE:
                result.error = f"Écart anormal ({len(removed)} noms disparus sur {before}) : zone ignorée ce jour."
                self._set_status(tld, result, started, scanner.bytes_in)
                return result
            result.removed, result.added = len(removed), len(added)
            self.record(tld, removed, added, utcnow())
        self._save(tld, bits)
        self._set_status(tld, result, started, scanner.bytes_in)
        return result

    def _set_status(self, tld: str, result: ScanResult, started: float, size: int) -> None:
        previous = self.status(tld)
        value = {
            "at": iso(utcnow()),
            "names": result.names or previous.get("names"),
            "removed": result.removed,
            "added": result.added,
            "first": result.first,
            "error": result.error,
            "seconds": round(time.monotonic() - started),
            "megabytes": round(size / 1e6),
        }
        if result.error:
            value["ok_at"] = previous.get("ok_at")
        else:
            value["ok_at"] = value["at"]
        self.store.set(f"zone_{tld}", json.dumps(value))

    def record(self, tld: str, removed: list[str], added: list[str], now: datetime) -> None:
        now = now.replace(microsecond=0)
        self.conn.execute("BEGIN")
        try:
            for label in removed:
                name = f"{label}.{tld}"
                self.conn.execute(
                    """INSERT INTO drops (name, tld, label, kind, removed_on, next_check_at)
                       VALUES (?, ?, ?, ?, ?, ?)
                       ON CONFLICT(name) DO UPDATE SET removed_on = excluded.removed_on, state = NULL,
                         state_since = NULL, release_at = NULL, available_since = NULL, error = NULL,
                         next_check_at = excluded.next_check_at""",
                    (name, tld, label, kind_of(label), now.date().isoformat(), iso(now)),
                )
            for label in added:
                # Revenu dans la zone : renouvelé, restauré ou réenregistré. Plus rien à attendre.
                self.conn.execute("DELETE FROM drops WHERE name = ?", (f"{label}.{tld}",))
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

    async def run(self) -> list[ScanResult]:
        if self.running or not self.configured:
            return []
        self.running = True
        results = []
        try:
            async with self._http_factory() as http:
                czds = self._client(http)
                try:
                    links = await czds.links()
                except CzdsError as exc:
                    self.store.set("czds_error", str(exc))
                    return []
                self.store.set("czds_error", "")
                self.store.set("czds_approved", json.dumps(list(links)))
                for tld in self.enabled():
                    if tld in links:
                        result = await self.scan_zone(czds, tld, links[tld])
                        log.info(
                            "Zone .%s : %s noms de 4 caractères, %s sortis, %s entrés%s",
                            tld, result.names, result.removed, result.added,
                            f", erreur : {result.error}" if result.error else "",
                        )
                        results.append(result)
            self.store.set("czds_last_run", datetime.now(digest.local_zone()).date().isoformat())
            self.purge(utcnow())
        finally:
            self.running = False
        return results

    def due(self, now_local: datetime) -> bool:
        hour = int(self.store.get("czds_hour", "6"))
        return self.configured and now_local.hour >= hour and self.last_run() != now_local.date().isoformat()

    # Vérification RDAP des noms sortis

    def _next(self, state: str, now: datetime, release: datetime | None) -> datetime:
        if state == states.PENDING_DELETE and release:
            return max(now + timedelta(hours=1), min(release + timedelta(minutes=30), now + timedelta(hours=12)))
        if state == states.AVAILABLE:
            return now + timedelta(hours=12)
        if state == SUSPENDED:
            return now + timedelta(days=3)
        return now + timedelta(hours=24)

    async def check_due(self, limit: int = 20) -> int:
        now = utcnow()
        rows = self.conn.execute(
            "SELECT * FROM drops WHERE next_check_at <= ? ORDER BY next_check_at LIMIT ?", (iso(now), limit)
        ).fetchall()
        for row in rows:
            await self.check(row)
        return len(rows)

    async def check(self, row) -> None:
        result = await self.rdap.lookup(row["name"])
        now = utcnow()
        if result.error:
            self.conn.execute(
                "UPDATE drops SET error = ?, checked_at = ?, next_check_at = ? WHERE id = ?",
                (result.error, iso(now), iso(now + timedelta(hours=2)), row["id"]),
            )
            return
        expires = None
        if result.available:
            state = states.AVAILABLE
        else:
            info = result.info
            expires = info.expires_at
            state = states.compute_state(info.statuses, info.expires_at, now, 0)
            if state not in TRACKED:
                state = SUSPENDED
        since = parse_iso(row["state_since"])
        if state != row["state"]:
            since = now
            if not result.available and result.info.last_changed_at and state in (states.REDEMPTION, states.PENDING_DELETE):
                since = min(result.info.last_changed_at, now)
        release = self.release(state, row["tld"], expires, since, now)
        available_since = row["available_since"]
        if state == states.AVAILABLE and not available_since:
            available_since = iso(now)
        elif state != states.AVAILABLE:
            available_since = None
        self.conn.execute(
            """UPDATE drops SET state = ?, state_since = ?, release_at = ?, available_since = ?,
               error = NULL, checked_at = ?, next_check_at = ? WHERE id = ?""",
            (state, iso(since), iso(release), available_since, iso(now), iso(self._next(state, now, release)), row["id"]),
        )

    def release(self, state, tld, expires, since, now) -> datetime | None:
        value = states.estimate_release(state, tld, expires, since, now, self.delays)
        drop = states.drop_time(tld, self.delays)
        if value and drop:
            value = value.replace(hour=drop[0], minute=drop[1], second=0, microsecond=0)
        return value

    def purge(self, now: datetime) -> None:
        self.conn.execute(
            "DELETE FROM drops WHERE state = ? AND available_since < ?",
            (states.AVAILABLE, iso(now - timedelta(days=KEEP_AVAILABLE_DAYS))),
        )
        self.conn.execute(
            "DELETE FROM drops WHERE state = ? AND removed_on < ?",
            (SUSPENDED, (now - timedelta(days=KEEP_SUSPENDED_DAYS)).date().isoformat()),
        )

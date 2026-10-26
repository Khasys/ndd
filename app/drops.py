"""Noms qui quittent les zones ICANN (CZDS) : suivi jusqu'à leur libération.

Noms suivis : tous ceux de 1 à 4 caractères, plus les mots et noms de famille des listes
de app/data. Chaque jour, la zone d'une extension est lue en flux. Les noms courts présents
sont notés dans une carte de bits (240 Ko), les noms des listes dans un fichier texte.
Un nom présent la veille et absent ce jour a quitté la zone : rédemption, suppression en
cours ou suspension. Une vérification RDAP dit lequel, et l'estimation de libération suit
les délais sourcés.
"""

import asyncio
import json
import multiprocessing
import os
import logging
import re
import time
import zlib
from collections import deque
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import AsyncIterator, Iterable

import httpx

from . import digest, rdapinfo, registrations, states, wordlists
from .checker import iso, parse_iso, utcnow
from .store import Store

log = logging.getLogger(__name__)

AUTH_URL = "https://account-api.icann.org/api/authenticate"
LINKS_URL = "https://czds-api.icann.org/czds/downloads/links"

CHARSET = "abcdefghijklmnopqrstuvwxyz0123456789-"
BASE = len(CHARSET)
MAX_SHORT = 4
# Les noms de longueur 1, puis 2, 3 et 4 occupent des plages successives de la carte.
OFFSETS = [0]
for _length in range(1, MAX_SHORT + 1):
    OFFSETS.append(OFFSETS[-1] + BASE**_length)
CELLS = OFFSETS[-1]
BITMAP_BYTES = (CELLS + 7) // 8
_INDEX = {c: i for i, c in enumerate(CHARSET)}

KINDS = {
    "letters": "Lettres", "digits": "Chiffres", "mixed": "Lettres et chiffres", "hyphen": "Avec tiret",
    **wordlists.SHOWN,
}

# États propres à cet écran, en plus de ceux de states.
SUSPENDED = "suspended"  # hors zone mais ni en rédemption ni en suppression (hold, litige)
TAKEN = "taken"  # renouvelé, restauré ou repris : plus rien à attendre, gardé pour l'historique
# Libre d'après le seul WHOIS (extension sans RDAP, ou RDAP en panne) : un WHOIS « inconnu » ne connaît pas
# les noms réservés ou premium du registre (.co, .io, .ai). Libre à confirmer chez un registrar : ni
# « Libéré » dans la Veille et Aujourd'hui, ni alerte « libre », pas de date de libération constatée.
UNCONFIRMED = "unconfirmed"
TRACKED = (states.EXPIRED, states.REDEMPTION, states.PENDING_DELETE)

# removed_on d'une ligne créée par une vérification à la main : aucune sortie constatée (colonne NOT NULL).
MANUAL = ""
# Origines discrètes : vérifiées sans sortie de zone (revérification après un refus d'achat, passe des absents de la
# zone, absent.py). Hors Veille, alertes et quotidien : ce n'est pas une libération constatée. QUIET_SQL : la condition.
QUIET = ("gandi", "rdap")
QUIET_SQL = "COALESCE(origin, '') NOT IN ('gandi', 'rdap')"
# Un nom libre reste affiché ce nombre de jours (réglable), puis seulement dans l'Explorer.
DEFAULT_KEEP_DAYS = 30
# Au-delà de cette part de noms disparus en un jour, la zone est jugée anormale et ignorée.
MAX_DAILY_REMOVED_SHARE = 0.05


class CzdsError(Exception):
    pass


class CzdsAuthError(CzdsError):
    """Identifiants refusés : réessayer dans la foulée n'y changera rien."""


# --- Noms et carte de bits -------------------------------------------------------------


def encode(label: str) -> int:
    value = 0
    for ch in label:
        value = value * BASE + _INDEX[ch]
    return OFFSETS[len(label) - 1] + value


def decode(index: int) -> str:
    length = next(n for n in range(1, MAX_SHORT + 1) if index < OFFSETS[n])
    index -= OFFSETS[length - 1]
    chars = []
    for _ in range(length):
        index, rest = divmod(index, BASE)
        chars.append(CHARSET[rest])
    return "".join(reversed(chars))


def valid_label(label: str) -> bool:
    return (
        1 <= len(label) <= MAX_SHORT and all(c in _INDEX for c in label)
        and label[0] != "-" and label[-1] != "-"
    )


REMOVAL_SLACK_DAYS = 7


def redemption_start(since: datetime, state: str, tld: str, removed_on: str | None, now: datetime) -> datetime:
    """Une rédemption commence quand le registrar supprime le nom, qui sort alors de la zone (ou du DNS) : jamais
    avant ce jour. La dernière modification RDAP peut être bien plus ancienne (renouvellement automatique du .biz,
    45 jours de grâce). Le .fr est à part : sa date de retrait vient du fichier de l'Afnic, pas d'une zone."""
    if state not in (states.REDEMPTION, states.PENDING_DELETE) or tld == "fr" or not removed_on or removed_on == MANUAL:
        return since
    try:
        removed = datetime.fromisoformat(removed_on).replace(tzinfo=timezone.utc)
    except ValueError:
        return since
    # La zone est lue une fois par jour, parfois avec quelques jours de retard (CZDS en panne) : une modification RDAP
    # de la semaine qui précède est la suppression, plus précise que le jour de lecture. Bien avant (les grâces
    # durent 30 à 45 jours), c'est autre chose : renouvellement automatique, transfert.
    if since is None or removed - since > timedelta(days=REMOVAL_SLACK_DAYS):
        return min(removed, now)
    return since


def kind_of(label: str) -> str:
    """Type d'un nom court ; « long » au-delà de 4 caractères."""
    if len(label) > MAX_SHORT:
        return "long"
    if "-" in label:
        return "hyphen"
    if label.isalpha():
        return "letters"
    if label.isdigit():
        return "digits"
    return "mixed"


# --- Recherche dans les morceaux de zone ----------------------------------------------
# Fonctions de module : elles tournent aussi dans des processus séparés, un par cœur.

_UPPER = re.compile(rb"[A-Z]")
_patterns_cache: dict[str, tuple[re.Pattern, re.Pattern]] = {}
_worker_watched: frozenset[bytes] = frozenset()


def _patterns(tld: str) -> tuple[re.Pattern, re.Pattern]:
    if tld not in _patterns_cache:
        # Première colonne d'un enregistrement, en début de ligne : « abcd.com. ».
        suffix = rb"\." + re.escape(tld.lower().encode()) + rb"\.?[ \t]"
        _patterns_cache[tld] = (
            re.compile(rb"\n([a-z0-9-]{1,4})" + suffix),
            re.compile(rb"\n([a-z0-9-]{5,63})" + suffix),
        )
    return _patterns_cache[tld]


def parse_lines(lines: bytes, tld: str, watched: frozenset[bytes]) -> tuple[set[bytes], set[bytes]]:
    """(noms courts, noms des listes) présents dans des lignes complètes de zone."""
    text = b"\n" + lines
    # Les registres publient en minuscules ; la conversion, coûteuse, n'a lieu qu'en cas de besoin.
    if _UPPER.search(text):
        text = text.lower()
    short, long = _patterns(tld)
    found = set(short.findall(text))
    words = watched.intersection(long.findall(text)) if watched else set()
    return found, set(words)


def _init_worker(watched: frozenset[bytes]) -> None:
    global _worker_watched
    _worker_watched = watched


def _parse_in_worker(lines: bytes, tld: str) -> tuple[set[bytes], set[bytes]]:
    return parse_lines(lines, tld, _worker_watched)


def default_workers() -> int:
    """Cœurs pour la recherche : tous sauf un, laissé au NAS et à la décompression, 4 au plus."""
    return max(1, min(4, (os.cpu_count() or 1) - 1))


class ZoneScanner:
    """Lit une zone gzip morceau par morceau : carte des noms courts, ensemble des noms des listes.

    La décompression reste dans le thread appelant ; la recherche part dans des processus
    séparés quand workers > 1, ce qui répartit le gros du calcul sur plusieurs cœurs.
    """

    def __init__(self, tld: str, watched: frozenset[bytes] | None = None, workers: int = 1):
        self.tld = tld
        self.watched = wordlists.watched() if watched is None else watched
        self.bits = bytearray(BITMAP_BYTES)
        self.words: set[str] = set()
        self._inflate = zlib.decompressobj(16 + zlib.MAX_WBITS)
        self._rest = b""
        self.bytes_in = 0
        self._pending: deque = deque()
        self._pool = None
        if workers > 1:
            self._pool = ProcessPoolExecutor(
                workers, mp_context=multiprocessing.get_context("spawn"),
                initializer=_init_worker, initargs=(self.watched,),
            )
            self._limit = workers * 2

    def feed(self, chunk: bytes) -> None:
        self.bytes_in += len(chunk)
        data = self._inflate.decompress(chunk)
        # Un gzip peut enchaîner plusieurs membres.
        while self._inflate.eof and self._inflate.unused_data:
            tail = self._inflate.unused_data
            self._inflate = zlib.decompressobj(16 + zlib.MAX_WBITS)
            data += self._inflate.decompress(tail)
        buffer = self._rest + data
        cut = buffer.rfind(b"\n") + 1
        self._rest = buffer[cut:]
        if cut:
            self._submit(buffer[:cut])

    def finish(self) -> tuple[bytearray, set[str]]:
        try:
            data = self._inflate.flush()
            if not self._inflate.eof:
                # Fin de flux gzip absente : téléchargement coupé. Comparer une zone tronquée
                # ferait passer des milliers de noms pour supprimés.
                raise zlib.error("zone tronquée")
            tail = self._rest + data
            if tail:
                self._submit(tail if tail.endswith(b"\n") else tail + b"\n")
            while self._pending:
                self._merge(self._pending.popleft().result())
            return self.bits, self.words
        finally:
            self.close()

    def close(self) -> None:
        if self._pool:
            self._pool.shutdown(cancel_futures=True)
            self._pool = None

    def _submit(self, lines: bytes) -> None:
        if not self._pool:
            self._merge(parse_lines(lines, self.tld, self.watched))
            return
        self._pending.append(self._pool.submit(_parse_in_worker, lines, self.tld))
        # Mémoire bornée : on n'avance pas plus de deux morceaux par processus.
        while len(self._pending) > self._limit:
            self._merge(self._pending.popleft().result())

    def _merge(self, result: tuple[set[bytes], set[bytes]]) -> None:
        short, words = result
        for label in short:
            if label[:1] != b"-" and label[-1:] != b"-":
                index = encode(label.decode())
                self.bits[index >> 3] |= 1 << (index & 7)
        for label in words:
            self.words.add(label.decode())


def scan_bytes(chunks: Iterable[bytes], tld: str, watched: frozenset[bytes] | None = frozenset()) -> bytearray:
    """Carte des noms courts seule (tests et mesures)."""
    scanner = ZoneScanner(tld, watched)
    for chunk in chunks:
        scanner.feed(chunk)
    return scanner.finish()[0]


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
            raise CzdsAuthError("Identifiant ou mot de passe ICANN refusé.")
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
        try:
            urls = response.json()
        except ValueError:
            urls = None
        if not isinstance(urls, list):
            raise CzdsError("Liste des zones CZDS illisible.")
        found = {}
        for url in urls:
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
    # Temps passé à attendre des données du réseau, et temps de calcul (décompression, recherche).
    wait: float = 0.0
    compute: float = 0.0
    workers: int = 1


KEEP_SCANS = 14


async def _prefetch(chunks: AsyncIterator[bytes], queue: asyncio.Queue) -> None:
    """Télécharge en avance pendant que le calcul tourne. None marque la fin, une exception l'échec."""
    try:
        async for chunk in chunks:
            await queue.put(chunk)
        await queue.put(None)
    except Exception as exc:  # transmise au lecteur, qui la relève
        await queue.put(exc)


class DropWatcher:
    on_change = None  # appelé avec le nom après chaque vérification (Explorer)
    on_fr_taken = None  # .fr de nouveau à quelqu'un : retour dans les dates anniversaire (frdata)
    on_freed = None  # nom tout juste vu libre au registre : son prix est demandé sans attendre la nuit (main)

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
        self.workers = default_workers()

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
        self.store.set("czds_retry_at", "")
        return list(links)

    # Lecture des zones

    def _path(self, tld: str, kind: str) -> Path:
        return self.dir / f"{tld}.{kind}"

    def _load(self, tld: str) -> bytes | None:
        path = self._path(tld, "short")
        if path.exists() and path.stat().st_size == BITMAP_BYTES:
            return path.read_bytes()
        return None

    def _load_words(self, tld: str) -> set[str] | None:
        path = self._path(tld, "words")
        return set(path.read_text().split()) if path.exists() else None

    def covered(self, tld: str) -> set[str]:
        """Listes que la dernière lecture de la zone a cherchées. Lecture d'avant ce suivi :
        les quatre listes d'origine. Une liste ajoutée depuis attend la prochaine lecture."""
        raw = self.store.get(f"zone_lists_{tld}")
        if not raw:
            return set(wordlists.LEGACY)
        seen = json.loads(raw)
        return {key for key, fp in wordlists.fingerprints().items() if seen.get(key) == fp}

    def _write(self, path: Path, data: bytes) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_bytes(data)
        tmp.replace(path)

    def _save(self, tld: str, bits: bytes, words: set[str], watched=None) -> None:
        self._write(self._path(tld, "short"), bytes(bits))
        self._write(self._path(tld, "words"), "\n".join(sorted(words)).encode())
        # Noms cherchés à cette lecture : un nom ajouté depuis aux listes attend la suivante.
        searched = sorted(w.decode() if isinstance(w, bytes) else w for w in (watched or wordlists.watched()))
        self._write(self._path(tld, "searched"), "\n".join(searched).encode())
        self.store.set(f"zone_lists_{tld}", json.dumps(wordlists.fingerprints()))

    def searched(self, tld: str) -> set[str] | None:
        """Noms des listes cherchés à la dernière lecture de la zone ; None pour une lecture plus ancienne."""
        path = self._path(tld, "searched")
        return set(path.read_text().split()) if path.exists() else None

    async def scan_zone(self, czds: Czds, tld: str, url: str) -> ScanResult:
        result = ScanResult(tld)
        started = time.monotonic()
        while True:
            scanner = ZoneScanner(tld, workers=self.workers)
            result.workers, result.wait, result.compute = self.workers, 0.0, 0.0
            # Le téléchargement continue pendant le calcul : 8 morceaux d'1 Mo d'avance au plus.
            queue: asyncio.Queue = asyncio.Queue(maxsize=8)
            fetcher = asyncio.create_task(_prefetch(czds.chunks(url), queue))
            try:
                while True:
                    waited = time.monotonic()
                    chunk = await queue.get()
                    began = time.monotonic()
                    result.wait += began - waited
                    if chunk is None:
                        break
                    if isinstance(chunk, Exception):
                        raise chunk
                    # Décompression dans un thread, recherche dans des processus séparés :
                    # l'interface reste réactive pendant les minutes que prend .com.
                    await asyncio.to_thread(scanner.feed, chunk)
                    result.compute += time.monotonic() - began
                began = time.monotonic()
                bits, words = await asyncio.to_thread(scanner.finish)
                result.compute += time.monotonic() - began
            except BrokenProcessPool:
                if self.workers == 1:
                    raise
                # Processus de recherche impossibles à lancer ici : on continue sur un seul cœur.
                log.warning("Recherche parallèle indisponible, lecture de .%s sur un seul processus", tld)
                self.workers = 1
                continue
            except CzdsError as exc:
                result.error = str(exc)
            except zlib.error:
                result.error = "Fichier de zone corrompu ou incomplet."
            except httpx.HTTPError as exc:
                result.error = f"Téléchargement interrompu ({exc.__class__.__name__})."
            finally:
                fetcher.cancel()
                scanner.close()
            break
        if result.error:
            self._set_status(tld, result, started, scanner.bytes_in)
            return result

        previous, previous_words = self._load(tld), self._load_words(tld)
        searched_before = self.searched(tld)  # avant _save : les noms cherchés à la lecture précédente
        result.names = count(bits) + len(words)
        if previous is None:
            result.first = True
        else:
            removed, added = diff(previous, bits)
            removed_words, added_words = [], []
            if previous_words is not None:
                # Limité à la liste actuelle : un mot retiré des listes n'est pas un nom supprimé.
                current_list = {w.decode() for w in scanner.watched}
                removed_words = sorted((previous_words & current_list) - words)
                added_words = sorted(words - previous_words)
            # Écart accepté une fois dans Paramètres : cette lecture passe, puis la garde revient.
            accepted = self._take_gap_acceptance(tld)
            for label, gone, before, found in (
                ("noms courts", removed, count(previous), count(bits)),
                ("noms des listes", removed_words, len(previous_words or ()), len(words)),
            ):
                if before >= 1000 and len(gone) > before * MAX_DAILY_REMOVED_SHARE:
                    if accepted:
                        log.warning("Zone .%s : écart accepté (%s %s disparus sur %s)", tld, len(gone), label, before)
                        continue
                    result.error = f"Écart anormal ({len(gone)} {label} disparus sur {before}) : zone ignorée ce jour."
                    self._set_gap_refused(tld, {"expected": before, "found": found, "at": iso(utcnow())})
                    self._set_status(tld, result, started, scanner.bytes_in)
                    return result
            self._set_gap_refused(tld, None)
            # Journal des enregistrements (statistiques) : sans les noms d'une liste ajoutée entre deux lectures.
            registrations.journal_zone(self.conn, tld, added_words, searched_before, added, utcnow())
            removed += removed_words
            added += added_words
            result.removed, result.added = len(removed), len(added)
            self.record(tld, removed, added, utcnow())
        self._save(tld, bits, words, scanner.watched)
        self._set_status(tld, result, started, scanner.bytes_in)
        return result

    # Écart de zone refusé (plus de 5 % de noms disparus) : état pour l'interface, et acceptation une fois.

    def gap_refused(self) -> dict:
        return json.loads(self.store.get("czds_gap_refused") or "{}")

    def _set_gap_refused(self, tld: str, info: dict | None) -> None:
        refused = self.gap_refused()
        if info is None:
            if tld not in refused:
                return
            refused.pop(tld)
        else:
            refused[tld] = info
        self.store.set("czds_gap_refused", json.dumps(refused))

    def gap_accepted(self) -> list[str]:
        return json.loads(self.store.get("czds_accept_gap") or "[]")

    def accept_gap(self, tld: str) -> list[str]:
        """La prochaine lecture de cette extension passe malgré l'écart, une fois."""
        accepted = self.gap_accepted()
        if tld not in accepted:
            accepted.append(tld)
            self.store.set("czds_accept_gap", json.dumps(accepted))
        return accepted

    def _take_gap_acceptance(self, tld: str) -> bool:
        accepted = self.gap_accepted()
        if tld not in accepted:
            return False
        self.store.set("czds_accept_gap", json.dumps([t for t in accepted if t != tld]))
        return True

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
        self.conn.execute(
            """INSERT INTO zone_scans (tld, at, megabytes, seconds, wait_seconds, compute_seconds, workers,
               names, removed, added, error) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (tld, value["at"], round(size / 1e6, 1), round(time.monotonic() - started, 1), round(result.wait, 1),
             round(result.compute, 1), result.workers, result.names or None, result.removed, result.added, result.error),
        )
        self.conn.execute(
            """DELETE FROM zone_scans WHERE tld = ? AND id NOT IN
               (SELECT id FROM zone_scans WHERE tld = ? ORDER BY at DESC, id DESC LIMIT ?)""",
            (tld, tld, KEEP_SCANS),
        )

    def history(self) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM zone_scans ORDER BY at DESC, id DESC LIMIT 100").fetchall()
        return [dict(r) for r in rows]

    def record(self, tld: str, removed: list[str], added: list[str], now: datetime, removed_on=None) -> None:
        now = now.replace(microsecond=0)
        removed_day = (removed_on or now.date()).isoformat()
        own_transaction = not self.conn.in_transaction
        if own_transaction:
            self.conn.execute("BEGIN")
        try:
            for label in removed:
                name = f"{label}.{tld}"
                self.conn.execute(
                    """INSERT INTO drops (name, tld, label, kind, lists, removed_on, next_check_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(name) DO UPDATE SET removed_on = excluded.removed_on, state = NULL, origin = NULL,
                         state_since = NULL, release_at = NULL, available_since = NULL, error = NULL,
                         next_check_at = excluded.next_check_at, lists = excluded.lists""",
                    (name, tld, label, kind_of(label), ",".join(wordlists.categories(label)),
                     removed_day, iso(now)),
                )
            for label in added:
                # Revenu dans la zone : renouvelé, restauré ou réenregistré. Plus rien à attendre.
                self.conn.execute(
                    "UPDATE drops SET state = ?, state_since = ?, next_check_at = NULL, release_at = NULL WHERE name = ?",
                    (TAKEN, iso(now), f"{label}.{tld}"),
                )
            if own_transaction:
                self.conn.execute("COMMIT")
        except Exception:
            if own_transaction:
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
                    if isinstance(exc, CzdsAuthError):
                        # Pas de nouvel essai avant une heure (ICANN peut bloquer le compte), ou tout
                        # de suite après un nouveau mot de passe (refresh_approved).
                        self.store.set("czds_retry_at", iso(utcnow() + timedelta(hours=1)))
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
        finally:
            self.running = False
        return results

    def due(self, now_local: datetime) -> bool:
        hour = int(self.store.get("czds_hour", "6"))
        retry_at = parse_iso(self.store.get("czds_retry_at"))
        if retry_at and retry_at > utcnow():
            return False
        return self.configured and now_local.hour >= hour and self.last_run() != now_local.date().isoformat()

    # Vérification RDAP des noms sortis

    @property
    def keep_days(self) -> int:
        return int(self.store.get("drops_keep_days") or DEFAULT_KEEP_DAYS)

    def _next(self, state: str, now: datetime, release: datetime | None) -> datetime | None:
        """Le moins de requêtes possible : on regarde quand quelque chose doit changer."""
        if state in (states.AVAILABLE, TAKEN, UNCONFIRMED, states.RENEWED):
            # Plus rien à attendre. Bouton « Revérifier » ou « Surveiller » pour la suite. Renouvelé (.fr
            # seulement) : le suivi des dates anniversaire de l'Afnic reprend la main.
            return None
        if state in (states.REDEMPTION, states.PENDING_DELETE):
            if release:
                target = release + timedelta(minutes=30)
                if target > now + timedelta(hours=1):
                    return target  # Une seule vérification, à la libération estimée.
                return now + timedelta(hours=6)  # Estimation dépassée : on regarde jusqu'à la libération.
            return now + timedelta(hours=24)  # Délais inconnus pour cette extension.
        if state == states.EXPIRED:
            # Le registrar peut supprimer le nom avant la fin de la période de grâce.
            return min(release, now + timedelta(days=7)) if release and release > now else now + timedelta(days=7)
        if state == SUSPENDED:
            return now + timedelta(days=7)
        return now + timedelta(hours=24)

    async def check_due(self, limit: int = 20) -> int:
        # Le .fr passe par la file de l'Afnic (frdata), sous son propre plafond.
        now = utcnow()
        rows = self.conn.execute(
            "SELECT * FROM drops WHERE tld != 'fr' AND next_check_at <= ? ORDER BY next_check_at LIMIT ?",
            (iso(now), limit),
        ).fetchall()
        for row in rows:
            await self.check(row)
        return len(rows)

    async def check(self, row, result=None):
        if result is None:
            result = await self.rdap.lookup(row["name"])
            rdapinfo.save(self.conn, row["name"], result)
        self.apply(row, result, utcnow())
        return result

    def apply(self, row, result, now: datetime) -> str | None:
        """Enregistre le résultat RDAP d'un nom de l'écran Suppressions. Renvoie son état."""
        if result.error:
            self.conn.execute(
                "UPDATE drops SET error = ?, checked_at = ?, next_check_at = ? WHERE id = ?",
                (result.error, iso(now), iso(now + timedelta(hours=2)), row["id"]),
            )
            return None
        expires = None
        if result.available:
            state = UNCONFIRMED if result.unconfirmed else states.AVAILABLE
        else:
            info = result.info
            expires = info.expires_at
            state = states.compute_state(info.statuses, info.expires_at, now, 0, row["tld"])
            if state == states.EXPIRED and row["tld"] == "fr":
                # Le .fr n'a pas de délai de grâce : un nom non renouvelé passe en rédemption.
                # Échéance dépassée sans rédemption = renouvellement pas encore visible à l'Afnic.
                state = TAKEN
            if state not in TRACKED and state != states.RENEWED:
                # Hors zone sans être supprimé : suspendu (hold). Sinon il est de nouveau à quelqu'un.
                held = any("hold" in s.replace(" ", "").lower() for s in info.statuses)
                state = SUSPENDED if held else TAKEN
        since = parse_iso(row["state_since"])
        # Première vérification d'une ligne posée par Explorer.recheck (indisponible chez Gandi) : l'état date
        # de la lecture de Gandi, qui reste valable (pris : rien ne change ; libre au registre : réservé).
        if state != row["state"] and not (row["state"] is None and since and state not in TRACKED):
            since = now
            changed = None if result.available else result.info.last_changed_at
            # Date de dernière modification RDAP : début de la rédemption, sauf si elle précède l'échéance
            # (modification ancienne, sans rapport) ; on garde alors le moment où on voit l'état.
            if changed and state in (states.REDEMPTION, states.PENDING_DELETE) and (
                    result.info.expires_at is None or changed >= result.info.expires_at):
                since = min(changed, now)
            since = redemption_start(since, state, row["tld"], row["removed_on"], now)
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
        if (state in (states.AVAILABLE, UNCONFIRMED) and state != row["state"] and self.on_freed
                and row["origin"] != "gandi" and not (row["origin"] == "rdap" and row["state"] is None)):
            # Pas pour une revérification après un refus d'achat : Gandi vient de répondre pour ce nom. Ni pour la
            # première réponse de la passe des absents (des milliers de noms) : la passe Gandi de nuit les prend en tête.
            self.on_freed(row["name"])
        if state in (TAKEN, states.RENEWED) and row["tld"] == "fr" and self.on_fr_taken:
            # Repris ou renouvelé : de nouveau à quelqu'un, le suivi repart sur les dates anniversaire.
            self.on_fr_taken(row["label"], result)
        if self.on_change:
            self.on_change(row["name"])
        return state

    def row(self, name: str):
        return self.conn.execute("SELECT * FROM drops WHERE name = ?", (name,)).fetchone()

    def _repair_redemptions(self, now: datetime) -> int:
        """Rédemptions datées avant la sortie de zone (dernière modification RDAP du .biz, 45 jours de grâce) : la
        date de libération estimée était passée, ces noms restaient dans « libération sous 24 h » (27/10/2026)."""
        rows = self.conn.execute(
            "SELECT * FROM drops WHERE state IN (?, ?) AND tld != 'fr' AND removed_on IS NOT NULL AND removed_on != ? "
            "AND state_since < removed_on", (states.REDEMPTION, states.PENDING_DELETE, MANUAL)).fetchall()
        fixed = 0
        for row in rows:
            before = parse_iso(row["state_since"])
            since = redemption_start(before, row["state"], row["tld"], row["removed_on"], now)
            if since == before:
                continue
            fixed += 1
            release = self.release(row["state"], row["tld"], None, since, now)
            self.conn.execute("UPDATE drops SET state_since = ?, release_at = ?, next_check_at = ? WHERE id = ?",
                              (iso(since), iso(release), iso(self._next(row["state"], now, release)), row["id"]))
        return fixed

    def ensure(self, name: str, now: datetime, origin: str = "manual"):
        """La ligne d'un nom, créée au besoin : vérification manuelle depuis l'Explorer (origin manual), ou
        revérification d'un nom refusé à l'achat (gandi, Explorer.recheck : hors Veille et Aujourd'hui). Aucune
        sortie de zone ni du DNS n'a été constatée : removed_on vide (MANUAL), pas la date du jour, sinon la fiche
        afficherait « Sorti du DNS » ou « Sorti de la zone » ce jour-là."""
        found = self.row(name)
        if found is None:
            label, tld = name.split(".", 1)
            self.record(tld, [label], [], now)
            self.conn.execute("UPDATE drops SET next_check_at = NULL, removed_on = ?, origin = ? WHERE name = ?",
                              (MANUAL, origin, name))
            found = self.row(name)
        elif origin == "manual" and found["origin"] in QUIET:
            # Vérifié à la main ensuite : le nom entre dans la Veille comme les autres vérifications manuelles.
            self.conn.execute("UPDATE drops SET origin = 'manual' WHERE name = ?", (name,))
            found = self.row(name)
        return found

    def repair(self) -> dict:
        """Au démarrage, idempotent : corrige ce que les versions précédentes ont écrit.
        - Libres sur la seule foi d'un WHOIS « inconnu » (dernière réponse WHOIS, libre) : libres à confirmer,
          sans date de libération constatée.
        - .co, .ai, .io jamais vus dans le DNS : la date de sortie posée par une vérification à la main n'en est pas une.
        - Libres que Gandi a dits indisponibles avant la revérification immédiate (Explorer.recheck, 19/10/2026), jamais
          revus au registre depuis : revus maintenant, sinon la réponse de Gandi reste sans effet jusqu'à sa passe suivante."""
        from .ccdns import TLDS as cc  # import local : les processus de lecture des zones importent ce module
        from .gandi import BLOCKED

        whois = self.conn.execute(
            """UPDATE drops SET state = ?, state_since = COALESCE(available_since, state_since), available_since = NULL,
                 next_check_at = NULL
               WHERE state = ? AND name IN (SELECT name FROM name_rdap WHERE available = 1 AND source LIKE 'whois%')""",
            (UNCONFIRMED, states.AVAILABLE)).rowcount
        removed = self.conn.execute(
            f"""UPDATE drops SET removed_on = ? WHERE tld IN ({','.join('?' * len(cc))}) AND removed_on != ?
                AND NOT EXISTS (SELECT 1 FROM cc_dns c WHERE c.name = drops.name AND c.seen_at IS NOT NULL)""",
            (MANUAL, *cc, MANUAL)).rowcount
        blocked = self.conn.execute(
            f"""UPDATE drops SET next_check_at = ? WHERE state IN (?, ?) AND next_check_at IS NULL
                AND EXISTS (SELECT 1 FROM gandi_checks g WHERE g.name = drops.name AND g.kind IN ({','.join('?' * len(BLOCKED))})
                            AND (drops.checked_at IS NULL OR drops.checked_at < g.checked_at))""",
            (iso(utcnow()), states.AVAILABLE, UNCONFIRMED, *BLOCKED)).rowcount
        redated = self._repair_redemptions(utcnow())
        if whois or removed or blocked or redated:
            log.info("Veille : %s noms libres selon le seul WHOIS passés « à confirmer », %s fausses sorties du DNS effacées, "
                     "%s libres refusés par Gandi revus au registre, %s rédemptions redatées à la sortie de zone",
                     whois, removed, blocked, redated)
        return {"unconfirmed": whois, "removed_on": removed, "recheck": blocked, "redemption": redated}

    def release(self, state, tld, expires, since, now) -> datetime | None:
        value = states.estimate_release(state, tld, expires, since, now, self.delays)
        drop = states.drop_time(tld, self.delays)
        if value and drop:
            value = value.replace(hour=drop[0], minute=drop[1], second=0, microsecond=0)
        return value

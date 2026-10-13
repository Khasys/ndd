"""Le .fr, absent de CZDS : open data mensuel de l'Afnic, puis suivi RDAP piloté par l'expiration.

1. Import du fichier mensuel de l'Afnic (liste des noms en .fr existants ou ayant existé, avec
   leur date de suppression). Seuls les noms suivis par l'écran Suppressions sont gardés : noms
   de 1 à 4 caractères, mots, noms de famille, prénoms.
2. Balayage RDAP lent des noms enregistrés, pour connaître leur date d'expiration.
3. Revérification autour de cette date : renouvelé, on attend l'année suivante ; en rédemption
   ou libre, le nom passe dans la table drops, suivie comme les autres extensions.

Le fichier ne donne pas de date d'expiration : c'est le RDAP qui la fournit.
"""

import asyncio
import csv
import io
import json
import logging
import re
import unicodedata
import zipfile
from datetime import date, datetime, timedelta
from pathlib import Path

import httpx

from . import drops, states, wordlists
from .checker import iso, utcnow
from .store import Store

log = logging.getLogger(__name__)

DATASET_API = "https://www.data.gouv.fr/api/1/datasets/open-data-du-fr-1/"
RESOURCE_HINT = "nomsdedomaine"
# Une requête RDAP toutes les 5 s au plus vers l'Afnic, pour ce balayage.
SWEEP_BATCH = 60
SWEEP_EVERY_MINUTES = 5
RECENT_REMOVAL_DAYS = 45


class FrDataError(Exception):
    pass


def _fold(text: str) -> str:
    text = unicodedata.normalize("NFKD", text.strip().lower())
    return "".join(c for c in text if not unicodedata.combining(c))


def find_columns(header: list[str]) -> dict[str, int]:
    """Repère les colonnes utiles d'après leur intitulé : nom de domaine, création, suppression."""
    found: dict[str, int] = {}
    for i, raw in enumerate(header):
        name = _fold(raw)
        if "domain" in name and "name" not in found and "sous" not in name:
            found["name"] = i
        elif ("creation" in name or "enregistrement" in name) and "created" not in found:
            found["created"] = i
        elif ("retrait" in name or "suppression" in name or "deletion" in name) and "removed" not in found:
            found["removed"] = i
    if "name" not in found:
        raise FrDataError(f"Colonne du nom de domaine introuvable. Colonnes lues : {', '.join(header[:20])}")
    return found


_DATE_FORMATS = ("%d-%m-%Y", "%d/%m/%Y", "%Y-%m-%d", "%Y/%m/%d", "%d-%m-%y")


def parse_date(value: str) -> date | None:
    value = value.strip()[:10]
    if not value:
        return None
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    return None


def watched_label(label: str) -> bool:
    return drops.valid_label(label) or bool(wordlists.categories(label))


def read_rows(text_lines, watched=watched_label) -> tuple[dict[str, tuple[date | None, date | None]], dict]:
    """Lit le CSV ligne à ligne. Garde les noms suivis : nom vers (création, suppression)."""
    sample = next(text_lines)
    delimiter = ";" if sample.count(";") >= sample.count(",") else ","
    header = next(csv.reader([sample], delimiter=delimiter))
    columns = find_columns(header)
    kept: dict[str, tuple[date | None, date | None]] = {}
    rows = 0
    for row in csv.reader(text_lines, delimiter=delimiter):
        rows += 1
        try:
            name = row[columns["name"]].strip().lower().rstrip(".")
        except IndexError:
            continue
        if not name.endswith(".fr"):
            continue
        label = name[:-3]
        if "." in label or not watched(label):
            continue
        created = parse_date(row[columns["created"]]) if "created" in columns and len(row) > columns["created"] else None
        removed = parse_date(row[columns["removed"]]) if "removed" in columns and len(row) > columns["removed"] else None
        previous = kept.get(label)
        # Un nom supprimé puis recréé apparaît plusieurs fois : la ligne la plus récente fait foi.
        if previous is None or (created or date.min) >= (previous[0] or date.min):
            kept[label] = (created, removed)
    stats = {"rows": rows, "kept": len(kept), "columns": {k: header[v] for k, v in columns.items()}}
    return kept, stats


def read_zip(path: Path, watched=watched_label):
    with zipfile.ZipFile(path) as archive:
        members = [m for m in archive.namelist() if m.lower().endswith((".csv", ".txt"))]
        if not members:
            raise FrDataError("Aucun fichier CSV dans l'archive Afnic.")
        member = max(members, key=lambda m: archive.getinfo(m).file_size)
        with archive.open(member) as raw:
            # Latin-1 lit n'importe quel octet : noms (Punycode) et dates sont en ASCII de toute façon.
            lines = io.TextIOWrapper(raw, encoding="latin-1", newline="")
            return read_rows(iter(lines), watched)


class FrWatcher:
    def __init__(self, conn, store: Store, data_dir: Path, rdap, drops_watcher: drops.DropWatcher, http_factory=None):
        self.conn = conn
        self.store = store
        self.dir = data_dir / "zones"
        self.rdap = rdap
        self.drops = drops_watcher
        self._http_factory = http_factory or (
            lambda: httpx.AsyncClient(timeout=httpx.Timeout(60, read=300), follow_redirects=True)
        )
        self.running = False

    @property
    def enabled(self) -> bool:
        return self.store.get("fr_enabled") == "1"

    def status(self) -> dict:
        info = json.loads(self.store.get("fr_status") or "{}")
        counts = self.conn.execute(
            """SELECT COUNT(*) AS total,
                      SUM(removed_on IS NULL) AS registered,
                      SUM(removed_on IS NULL AND expires_at IS NOT NULL) AS dated
               FROM fr_names"""
        ).fetchone()
        info.update(total=counts["total"], registered=counts["registered"] or 0, dated=counts["dated"] or 0)
        return info

    async def resource_url(self, http: httpx.AsyncClient) -> tuple[str, str | None]:
        custom = self.store.get("fr_url").strip()
        if custom:
            return custom, None
        try:
            response = await http.get(DATASET_API)
            data = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise FrDataError(f"data.gouv.fr injoignable ({exc.__class__.__name__}).") from None
        for resource in data.get("resources", []):
            haystack = f"{resource.get('title', '')} {resource.get('url', '')}".lower().replace("-", "").replace("_", "")
            if RESOURCE_HINT in haystack:
                return resource["url"], resource.get("last_modified")
        raise FrDataError("Fichier des noms de domaine introuvable dans le jeu de données Afnic.")

    async def import_file(self) -> dict:
        """Télécharge le fichier s'il a changé, l'importe, et met à jour fr_names."""
        if self.running:
            return {}
        self.running = True
        started = utcnow()
        try:
            async with self._http_factory() as http:
                url, modified = await self.resource_url(http)
                if modified and modified == json.loads(self.store.get("fr_status") or "{}").get("file_date"):
                    return self.status()
                self.dir.mkdir(parents=True, exist_ok=True)
                target = self.dir / "afnic.zip"
                tmp = target.with_suffix(".tmp")
                try:
                    async with http.stream("GET", url) as response:
                        if response.status_code != 200:
                            raise FrDataError(f"Téléchargement Afnic refusé (HTTP {response.status_code}).")
                        with tmp.open("wb") as out:
                            async for chunk in response.aiter_bytes(1 << 20):
                                out.write(chunk)
                except httpx.HTTPError as exc:
                    raise FrDataError(f"Téléchargement Afnic interrompu ({exc.__class__.__name__}).") from None
                tmp.replace(target)
            kept, stats = await asyncio.to_thread(read_zip, target)
            target.unlink(missing_ok=True)
            self._apply(kept, utcnow())
            self.store.set("fr_status", json.dumps({
                "at": iso(utcnow()), "file_date": modified, "rows": stats["rows"], "kept": stats["kept"],
                "columns": stats["columns"], "error": None,
                "seconds": round((utcnow() - started).total_seconds()),
            }))
        except (FrDataError, zipfile.BadZipFile, OSError) as exc:
            previous = json.loads(self.store.get("fr_status") or "{}")
            previous.update(at=iso(utcnow()), error=str(exc) if isinstance(exc, FrDataError) else f"Fichier Afnic illisible ({exc.__class__.__name__}).")
            self.store.set("fr_status", json.dumps(previous))
        finally:
            self.running = False
        return self.status()

    def _apply(self, kept: dict, now: datetime) -> None:
        recent = (now - timedelta(days=RECENT_REMOVAL_DAYS)).date()
        self.conn.execute("BEGIN")
        try:
            for label, (created, removed) in kept.items():
                self.conn.execute(
                    """INSERT INTO fr_names (label, created_on, removed_on, next_check_at)
                       VALUES (?, ?, ?, ?)
                       ON CONFLICT(label) DO UPDATE SET created_on = excluded.created_on,
                         removed_on = excluded.removed_on,
                         next_check_at = CASE WHEN fr_names.created_on IS NOT excluded.created_on
                           THEN excluded.next_check_at ELSE fr_names.next_check_at END""",
                    (label, created and created.isoformat(), removed and removed.isoformat(),
                     None if removed else iso(now)),
                )
                # Supprimé récemment d'après l'Afnic : vérification RDAP, libre ou repris.
                if removed and removed >= recent:
                    self.drops.record("fr", [label], [], now, removed_on=removed)
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

    def due(self, now_local: datetime) -> bool:
        """Une fois par semaine : l'Afnic publie vers le milieu du mois, la date exacte varie."""
        if not self.enabled or self.running:
            return False
        last = json.loads(self.store.get("fr_status") or "{}").get("at")
        return not last or datetime.fromisoformat(last) < utcnow() - timedelta(days=7)

    async def sweep(self, limit: int = SWEEP_BATCH) -> int:
        """Vérifie en RDAP les noms .fr enregistrés dont c'est le tour."""
        if not self.enabled:
            return 0
        now = utcnow()
        rows = self.conn.execute(
            """SELECT * FROM fr_names WHERE removed_on IS NULL AND next_check_at IS NOT NULL
               AND next_check_at <= ? ORDER BY expires_at IS NOT NULL, next_check_at LIMIT ?""",
            (iso(now), limit),
        ).fetchall()
        for row in rows:
            await self._check(row)
        return len(rows)

    async def _check(self, row) -> None:
        name = f"{row['label']}.fr"
        result = await self.rdap.lookup(name)
        now = utcnow()
        if result.error:
            self._schedule(row["label"], row["expires_at"], now + timedelta(days=1), now)
            return
        if result.available:
            self.drops.record("fr", [row["label"]], [], now)
            self._schedule(row["label"], None, now + timedelta(days=30), now)
            return
        info = result.info
        state = states.compute_state(info.statuses, info.expires_at, now, 0)
        if state in (states.REDEMPTION, states.PENDING_DELETE, states.EXPIRED):
            self.drops.record("fr", [row["label"]], [], now)
            self._schedule(row["label"], iso(info.expires_at), now + timedelta(days=7), now)
            return
        expires = info.expires_at
        if expires and expires > now:
            # Renouvelé ou actif : prochaine vérification le lendemain de l'échéance.
            following = expires + timedelta(days=1)
        else:
            following = now + timedelta(days=7)
        self._schedule(row["label"], iso(expires), following, now)

    def _schedule(self, label: str, expires: str | None, following: datetime, now: datetime) -> None:
        self.conn.execute(
            "UPDATE fr_names SET expires_at = ?, checked_at = ?, next_check_at = ? WHERE label = ?",
            (expires, iso(now), iso(following), label),
        )

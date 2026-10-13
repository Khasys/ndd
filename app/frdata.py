"""Le .fr, absent de CZDS : open data mensuel de l'Afnic, puis une vérification RDAP par an et par nom.

1. Import du fichier mensuel de l'Afnic (liste des noms en .fr existants ou ayant existé, avec
   leurs dates de création et de suppression). Seuls les noms suivis par l'écran Suppressions sont
   gardés : noms de 1 à 4 caractères, mots, noms de famille, prénoms.
2. Un nom .fr expire à la date anniversaire de sa création. Il est vérifié en RDAP le lendemain :
   renouvelé, la vraie échéance lue en RDAP fixe la vérification suivante, un an plus tard ;
   en rédemption ou libre, le nom passe dans la table drops, suivie comme les autres extensions.

Pas de balayage : quelques centaines de requêtes par jour au plus, plafonnées, avec une pause de
24 h si l'Afnic signale trop de requêtes.
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
# Si le jeu de données a été renommé ou remplacé, on le cherche par mots-clés.
SEARCH_API = "https://www.data.gouv.fr/api/1/datasets/?q=afnic&page_size=50"
RESOURCE_HINTS = ("nomsdedomaine", "nomdedomaine", "enpointfr")
# Adresse directe chez l'Afnic, un fichier par mois : on essaie le mois en cours puis les précédents.
AFNIC_MONTHLY = "https://www.afnic.fr/wp-media/ftp/documentsOpenData/{month}_OPENDATA_A-NomsDeDomaineEnPointFr.zip"
MONTHS_BACK = 4
MONTH_IN_URL = re.compile(r"(?<!\d)(20\d{2}(?:0[1-9]|1[0-2]))(?=_OPENDATA)")
# Plafond : 3 requêtes toutes les 5 minutes, soit 864 par jour au plus, même avec du retard.
SWEEP_BATCH = 3
SWEEP_EVERY_MINUTES = 5
PAUSE_AFTER_LIMIT = timedelta(hours=24)
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


def next_anniversary(created: date, after: date) -> date:
    """Prochaine date anniversaire de création strictement après « after » (29 février : 28)."""
    for year in (after.year, after.year + 1):
        try:
            candidate = created.replace(year=year)
        except ValueError:
            candidate = date(year, 2, 28)
        if candidate > after:
            return candidate
    raise AssertionError("inatteignable")


def _check_time(day: date) -> datetime:
    # Le lendemain de l'échéance, en milieu de journée UTC : le registrar a eu le temps d'agir.
    return datetime(day.year, day.month, day.day, 12, tzinfo=utcnow().tzinfo) + timedelta(days=1)


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


def _squash(text: str) -> str:
    return _fold(text).replace("-", "").replace("_", "").replace(" ", "")


def pick_resource(datasets: list[dict]) -> dict | None:
    """Le fichier des noms en .fr le plus récent parmi les ressources des jeux de données."""
    matches = []
    for dataset in datasets:
        for resource in dataset.get("resources") or []:
            if not resource.get("url"):
                continue
            haystack = _squash(f"{resource.get('title', '')} {resource['url']}")
            if any(hint in haystack for hint in RESOURCE_HINTS):
                matches.append(resource)
    if not matches:
        return None
    return max(matches, key=lambda r: r.get("last_modified") or "")


def recent_months(today: date, count: int = MONTHS_BACK) -> list[str]:
    """Les mois au format AAAAMM, du plus récent au plus ancien."""
    year, month = today.year, today.month
    months = []
    for _ in range(count):
        months.append(f"{year}{month:02d}")
        year, month = (year, month - 1) if month > 1 else (year - 1, 12)
    return months


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
        week = iso(utcnow() + timedelta(days=7))
        counts = self.conn.execute(
            """SELECT COUNT(*) AS total,
                      SUM(removed_on IS NULL) AS registered,
                      SUM(removed_on IS NULL AND next_check_at IS NOT NULL AND next_check_at <= ?) AS week
               FROM fr_names""",
            (week,),
        ).fetchone()
        info.update(total=counts["total"], registered=counts["registered"] or 0, week=counts["week"] or 0,
                    paused_until=self.store.get("fr_paused_until") or None)
        return info

    async def _get_json(self, http: httpx.AsyncClient, url: str) -> dict | None:
        try:
            response = await http.get(url, follow_redirects=True)
        except httpx.HTTPError as exc:
            raise FrDataError(f"data.gouv.fr injoignable ({exc.__class__.__name__}).") from None
        if response.status_code != 200:
            return None
        try:
            return response.json()
        except ValueError:
            return None

    async def _exists(self, http: httpx.AsyncClient, url: str) -> bool:
        try:
            async with http.stream("GET", url, follow_redirects=True) as response:
                return response.status_code == 200
        except httpx.HTTPError:
            return False

    async def _latest_monthly(self, http: httpx.AsyncClient, template: str) -> tuple[str, str] | None:
        for month in recent_months(utcnow().date()):
            url = template.format(month=month)
            if await self._exists(http, url):
                return url, month
        return None

    async def resource_url(self, http: httpx.AsyncClient) -> tuple[str, str | None]:
        custom = self.store.get("fr_url").strip()
        if custom and MONTH_IN_URL.search(custom):
            # Adresse d'un mois précis : on la suit de mois en mois.
            template = MONTH_IN_URL.sub("{month}", custom.replace("{", "{{").replace("}", "}}"), count=1)
            return await self._latest_monthly(http, template) or (custom, None)
        if custom:
            return custom, None
        found = await self._latest_monthly(http, AFNIC_MONTHLY)
        if found:
            return found
        datasets = []
        direct = await self._get_json(http, DATASET_API)
        if direct and direct.get("resources"):
            datasets.append(direct)
        found = pick_resource(datasets)
        if not found:
            search = await self._get_json(http, SEARCH_API)
            datasets += (search or {}).get("data") or []
            found = pick_resource(datasets)
        if found:
            return found["url"], found.get("last_modified")
        seen = [r.get("title") or r.get("url", "") for d in datasets for r in d.get("resources") or []]
        detail = f" Fichiers vus : {', '.join(seen[:8])}." if seen else " Aucun fichier vu sur data.gouv.fr."
        raise FrDataError("Fichier des noms de domaine introuvable dans le jeu de données Afnic." + detail
                          + " Colle son adresse dans Paramètres, Extension .fr, Adresse du fichier.")

    async def import_file(self) -> dict:
        """Télécharge le fichier s'il a changé, l'importe, et met à jour fr_names."""
        if self.running:
            return {}
        self.running = True
        started = utcnow()
        target = self.dir / "afnic.zip"
        tmp = target.with_suffix(".tmp")
        try:
            async with self._http_factory() as http:
                url, modified = await self.resource_url(http)
                if modified and modified == json.loads(self.store.get("fr_status") or "{}").get("file_date"):
                    return self.status()
                self.dir.mkdir(parents=True, exist_ok=True)
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
            # Le zip ne sert qu'à l'import : il ne reste jamais sur le disque, même après une erreur.
            for leftover in (tmp, target):
                leftover.unlink(missing_ok=True)
            self.running = False
        return self.status()

    def _apply(self, kept: dict, now: datetime) -> None:
        recent = (now - timedelta(days=RECENT_REMOVAL_DAYS)).date()
        today = now.date()
        self.conn.execute("BEGIN")
        try:
            for label, (created, removed) in kept.items():
                # Enregistré : vérification le lendemain du prochain anniversaire de création.
                # Sans date de création, le nom n'est pas suivi : on ne sait pas quand regarder.
                following = None
                if not removed and created:
                    following = iso(_check_time(next_anniversary(created, today - timedelta(days=1))))
                self.conn.execute(
                    """INSERT INTO fr_names (label, created_on, removed_on, next_check_at)
                       VALUES (?, ?, ?, ?)
                       ON CONFLICT(label) DO UPDATE SET created_on = excluded.created_on,
                         removed_on = excluded.removed_on,
                         next_check_at = CASE
                           WHEN excluded.removed_on IS NOT NULL THEN NULL
                           WHEN fr_names.created_on IS NOT excluded.created_on
                             OR fr_names.next_check_at IS NULL THEN excluded.next_check_at
                           ELSE fr_names.next_check_at END""",
                    (label, created and created.isoformat(), removed and removed.isoformat(), following),
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
        paused = self.store.get("fr_paused_until")
        if paused and datetime.fromisoformat(paused) > now:
            return 0
        rows = self.conn.execute(
            """SELECT * FROM fr_names WHERE removed_on IS NULL AND next_check_at IS NOT NULL
               AND next_check_at <= ? ORDER BY next_check_at LIMIT ?""",
            (iso(now), limit),
        ).fetchall()
        done = 0
        for row in rows:
            done += 1
            if not await self._check(row):
                break
        return done

    async def _check(self, row) -> bool:
        """False si l'Afnic limite les requêtes : le balayage s'arrête et se met en pause."""
        name = f"{row['label']}.fr"
        result = await self.rdap.lookup(name)
        now = utcnow()
        if result.error:
            if "Limite" in result.error or "429" in result.error:
                self.store.set("fr_paused_until", iso(now + PAUSE_AFTER_LIMIT))
                log.warning("Afnic : trop de requêtes, vérifications .fr en pause 24 h")
                return False
            self._schedule(row["label"], row["expires_at"], now + timedelta(days=1), now)
            return True
        if result.available:
            self.drops.record("fr", [row["label"]], [], now)
            # Libre : le suivi passe à l'écran Suppressions. Le prochain import dira s'il est repris.
            self._schedule(row["label"], None, None, now)
            return True
        info = result.info
        state = states.compute_state(info.statuses, info.expires_at, now, 0)
        if state in (states.REDEMPTION, states.PENDING_DELETE, states.EXPIRED):
            self.drops.record("fr", [row["label"]], [], now)
            self._schedule(row["label"], iso(info.expires_at), None, now)
            return True
        expires = info.expires_at
        if expires and expires > now:
            # Renouvelé : la vraie échéance, lue en RDAP, fixe la vérification suivante.
            following = _check_time(expires.date())
        elif row["created_on"]:
            following = _check_time(next_anniversary(date.fromisoformat(row["created_on"]), now.date()))
        else:
            following = now + timedelta(days=30)
        self._schedule(row["label"], iso(expires), following, now)
        return True

    def _schedule(self, label: str, expires: str | None, following: datetime | None, now: datetime) -> None:
        self.conn.execute(
            "UPDATE fr_names SET expires_at = ?, checked_at = ?, next_check_at = ? WHERE label = ?",
            (expires, iso(now), iso(following), label),
        )

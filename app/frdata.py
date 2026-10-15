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
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import httpx

from . import digest, drops, rdapinfo, states, wordlists
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
# Plafond commun à toutes les requêtes vers l'Afnic, par minute, réglable dans Paramètres.
# L'Afnic ne publie pas de limite : on reste prudent et on recule au premier signal.
DEFAULT_RATE = 2
MAX_RATE = 20
# Signal de limite (429) : pause d'une heure, doublée à chaque nouveau signal, 24 h au plus.
FIRST_PAUSE = timedelta(hours=1)
MAX_PAUSE = timedelta(hours=24)
# Premier import : faute de fichier précédent, les suppressions de ces derniers jours sont vérifiées.
BACKFILL_DAYS = 90
KEEP_REQUEST_LOG_DAYS = 30
# À augmenter quand la lecture du fichier change : force un nouvel import du même mois.
READER_VERSION = 3
# Échéance .fr dépassée sans rédemption : délai avant de regarder de nouveau.
RENEWAL_LAG_DAYS = 3


def lists_version() -> str:
    """Empreinte des listes de noms suivis : si elles changent, le fichier est relu."""
    import hashlib

    names = sorted(w.decode() for w in wordlists.watched())
    return hashlib.sha256("\n".join(names).encode()).hexdigest()[:16]


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
    stats = {"rows": rows, "kept": len(kept), "columns": {k: header[v] for k, v in columns.items()},
             "dated": sum(1 for created, _ in kept.values() if created)}
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
            encoding = _encoding(raw.read(1 << 16))
        with archive.open(member) as raw:
            # Noms (Punycode) et dates sont en ASCII : seul l'en-tête dépend de l'encodage.
            lines = io.TextIOWrapper(raw, encoding=encoding, errors="replace", newline="")
            return read_rows(iter(lines), watched)


def _encoding(head: bytes) -> str:
    """UTF-8 si le début du fichier en est (« Date de création » lisible), sinon latin-1."""
    try:
        head.decode("utf-8")
    except UnicodeDecodeError as exc:
        # Un caractère coupé par la fin de l'échantillon ne compte pas.
        if exc.start < len(head) - 4:
            return "latin-1"
    return "utf-8-sig"


class FrWatcher:
    on_change = None  # appelé avec le nom quand son état change (Explorer)

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
        self.current: str | None = None  # nom en cours de vérification, pour l'affichage en direct
        drops_watcher.on_fr_taken = lambda label, result: self._sync_name(label, result, drops.TAKEN)

    @property
    def enabled(self) -> bool:
        return self.store.get("fr_enabled") == "1"

    @property
    def rate(self) -> int:
        try:
            return max(1, min(MAX_RATE, int(self.store.get("fr_rate") or DEFAULT_RATE)))
        except ValueError:
            return DEFAULT_RATE

    def paused_until(self, now: datetime | None = None) -> datetime | None:
        value = self.store.get("fr_paused_until")
        if value and datetime.fromisoformat(value) > (now or utcnow()):
            return datetime.fromisoformat(value)
        return None

    def status(self) -> dict:
        info = json.loads(self.store.get("fr_status") or "{}")
        week = iso(utcnow() + timedelta(days=7))
        counts = self.conn.execute(
            """SELECT COUNT(*) AS total,
                      SUM(removed_on IS NULL) AS registered,
                      SUM(removed_on IS NULL AND next_check_at IS NOT NULL AND next_check_at <= ?) AS week,
                      SUM(removed_on IS NULL AND next_check_at IS NOT NULL) AS scheduled,
                      MIN(CASE WHEN removed_on IS NULL THEN next_check_at END) AS next_at
               FROM fr_names""",
            (week,),
        ).fetchone()
        paused = self.paused_until()
        info.update(total=counts["total"], registered=counts["registered"] or 0, week=counts["week"] or 0,
                    scheduled=counts["scheduled"] or 0, next_at=counts["next_at"],
                    paused_until=iso(paused), rate=self.rate, work=self.work())
        return info

    def _due_between(self, start: datetime, end: datetime) -> dict:
        names = self.conn.execute(
            """SELECT COUNT(*) FROM fr_names WHERE removed_on IS NULL AND next_check_at IS NOT NULL
               AND next_check_at >= ? AND next_check_at < ?""",
            (iso(start), iso(end)),
        ).fetchone()[0]
        removed = self.conn.execute(
            "SELECT COUNT(*) FROM drops WHERE tld = 'fr' AND next_check_at >= ? AND next_check_at < ?",
            (iso(start), iso(end)),
        ).fetchone()[0]
        return {"anniversaries": names, "removals": removed}

    def work(self) -> dict:
        """Travail du jour et du lendemain, en heure locale, et ce qui se passe en ce moment."""
        now = utcnow()
        midnight = datetime.now(digest.local_zone()).replace(hour=0, minute=0, second=0, microsecond=0)
        today, tomorrow, after = (midnight.astimezone(timezone.utc) + timedelta(days=d) for d in (0, 1, 2))
        origin = datetime(1970, 1, 1, tzinfo=timezone.utc)
        # Une vérification faite est replanifiée plus loin : ce qui est prévu avant minuit reste à faire.
        left = self._due_between(origin, tomorrow)
        due_now = sum(self._due_between(origin, now).values())
        done = self.conn.execute(
            "SELECT source, COUNT(*) AS n FROM fr_requests WHERE at >= ? GROUP BY source", (iso(today),)
        ).fetchall()
        recent = self.conn.execute(
            "SELECT at, name, source, outcome FROM fr_requests ORDER BY id DESC LIMIT 8"
        ).fetchall()
        return {
            "left_today": left,
            "tomorrow": self._due_between(tomorrow, after),
            "done_today": {r["source"]: r["n"] for r in done},
            "due_now": due_now,
            "current": self.current,
            "recent": [dict(r) for r in recent],
        }

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
                previous = json.loads(self.store.get("fr_status") or "{}")
                if (modified and modified == previous.get("file_date") and previous.get("reader") == READER_VERSION
                        and previous.get("lists") == lists_version()):
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
                "columns": stats["columns"], "dated": stats["dated"], "reader": READER_VERSION, "lists": lists_version(), "error": None,
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
        today = now.date()
        before = dict(self.conn.execute("SELECT label, removed_on FROM fr_names").fetchall())
        # Premier import (ou import d'avant ce suivi) : pas de fichier précédent pour comparer.
        first = not before or self.store.get("fr_backfill") != "1"
        since = (now - timedelta(days=BACKFILL_DAYS)).date()
        followed = {r[0] for r in self.conn.execute(
            "SELECT label FROM drops WHERE tld = 'fr' AND state IS NOT NULL AND state != ?", (drops.TAKEN,))}
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
                if not removed or label in followed:
                    continue
                # Nouvelle suppression depuis le fichier précédent, quelle que soit sa date :
                # le fichier arrive avec plusieurs semaines de retard.
                # Un nom tout juste ajouté aux listes n'a pas d'état précédent : seule une suppression
                # récente compte, sinon des milliers de vieilles suppressions partiraient en vérification.
                known = label in before
                new = removed >= since if first or not known else before[label] != removed.isoformat()
                if new:
                    self.drops.record("fr", [label], [], now, removed_on=removed)
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        self.store.set("fr_backfill", "1")

    def due(self, now_local: datetime) -> bool:
        """Une fois par semaine : l'Afnic publie vers le milieu du mois, la date exacte varie."""
        if not self.enabled or self.running:
            return False
        status = json.loads(self.store.get("fr_status") or "{}")
        if status.get("file_date") and (status.get("reader") != READER_VERSION or status.get("lists") != lists_version()):
            return True  # Lecture corrigée depuis le dernier import : on relit le fichier.
        last = status.get("at")
        return not last or datetime.fromisoformat(last) < utcnow() - timedelta(days=7)

    # File unique des requêtes vers l'Afnic

    def _next_job(self, now: datetime):
        """La vérification la plus en retard, suppressions et dates anniversaire confondues."""
        removal = self.conn.execute(
            "SELECT * FROM drops WHERE tld = 'fr' AND next_check_at <= ? ORDER BY next_check_at LIMIT 1",
            (iso(now),),
        ).fetchone()
        anniversary = self.conn.execute(
            """SELECT * FROM fr_names WHERE removed_on IS NULL AND next_check_at IS NOT NULL
               AND next_check_at <= ? ORDER BY next_check_at LIMIT 1""",
            (iso(now),),
        ).fetchone()
        if removal and (not anniversary or removal["next_check_at"] <= anniversary["next_check_at"]):
            return "suppression", removal
        if anniversary:
            return "anniversaire", anniversary
        return None

    def _used_last_minute(self, now: datetime) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) FROM fr_requests WHERE at > ?", (iso(now - timedelta(minutes=1)),)
        ).fetchone()[0]

    async def tick(self) -> int:
        """Appelée chaque minute : autant de vérifications que le plafond le permet."""
        if not self.enabled:
            return 0
        now = utcnow()
        if self.paused_until(now):
            return 0
        self.conn.execute(
            "DELETE FROM fr_requests WHERE at < ?", (iso(now - timedelta(days=KEEP_REQUEST_LOG_DAYS)),)
        )
        done = 0
        for _ in range(max(0, self.rate - self._used_last_minute(now))):
            job = self._next_job(utcnow())
            if job is None:
                break
            source, row = job
            done += 1
            if source == "suppression":
                ok = await self._check_removal(row)
            else:
                ok = await self._check(row)
            if not ok:
                break
        return done

    # Conservée pour les appels existants : une passe de la file.
    sweep = tick

    async def lookup(self, name: str, source: str):
        """Requête RDAP vers l'Afnic, journalisée. Renvoie None si l'Afnic limite (pause posée)."""
        self.current = name
        try:
            result = await self.rdap.lookup(name)
        finally:
            self.current = None
        now = utcnow()
        rdapinfo.save(self.conn, name, result)
        limited = bool(result.error) and ("Limite" in result.error or "429" in result.error)
        outcome = "limite" if limited else "erreur" if result.error else "libre" if result.available else "enregistré"
        if not result.error and not result.available:
            state = states.compute_state(result.info.statuses, result.info.expires_at, now, 0)
            if state in drops.TRACKED:
                outcome = states.LABELS[state].lower()
        self.conn.execute(
            "INSERT INTO fr_requests (at, name, source, outcome) VALUES (?, ?, ?, ?)",
            (iso(now.replace(microsecond=0)), name, source, outcome),
        )
        if limited:
            self._pause(result.error, now)
            return None
        self.store.set("fr_limit_streak", "0")
        return result

    def _pause(self, error: str, now: datetime) -> None:
        streak = int(self.store.get("fr_limit_streak") or 0) + 1
        self.store.set("fr_limit_streak", str(streak))
        pause = min(MAX_PAUSE, FIRST_PAUSE * (2 ** (streak - 1)))
        asked = re.search(r"reprise dans (\d+) s", error)
        if asked:
            pause = max(pause, timedelta(seconds=int(asked.group(1))))
        self.store.set("fr_paused_until", iso(now + pause))
        log.warning("Afnic : trop de requêtes, vérifications .fr en pause %s", pause)

    async def _check_removal(self, row) -> bool:
        result = await self.lookup(row["name"], "suppression")
        if result is None:
            return False
        state = self.drops.apply(row, result, utcnow())
        self._sync_name(row["label"], result, state)
        return True

    def _sync_name(self, label: str, result, state: str | None) -> None:
        """Un nom de l'écran Suppressions repris : il retourne dans les dates anniversaire."""
        if state != drops.TAKEN or result.error or result.available:
            return
        expires = result.info.expires_at
        now = utcnow()
        # Échéance passée : renouvellement pas encore visible, on regarde de nouveau dans 3 jours.
        following = _check_time(expires.date()) if expires and expires > now else now + timedelta(days=RENEWAL_LAG_DAYS)
        self.conn.execute(
            """INSERT INTO fr_names (label, removed_on, expires_at, checked_at, next_check_at)
               VALUES (?, NULL, ?, ?, ?)
               ON CONFLICT(label) DO UPDATE SET removed_on = NULL, expires_at = excluded.expires_at,
                 checked_at = excluded.checked_at, next_check_at = excluded.next_check_at""",
            (label, iso(expires), iso(now), iso(following)),
        )
        if self.on_change:
            self.on_change(f"{label}.fr")

    async def _check(self, row) -> bool:
        """False si l'Afnic limite les requêtes : la file s'arrête et se met en pause."""
        name = f"{row['label']}.fr"
        result = await self.lookup(name, "anniversaire")
        now = utcnow()
        if result is None:
            return False
        if result.error:
            self._schedule(row["label"], row["expires_at"], now + timedelta(days=1), now)
            return True
        if result.available:
            self._hand_over(row["label"], result, now)
            # Libre : le suivi passe à l'écran Suppressions. Le prochain import dira s'il est repris.
            self._schedule(row["label"], None, None, now)
            return True
        info = result.info
        state = states.compute_state(info.statuses, info.expires_at, now, 0)
        if state == states.EXPIRED:
            # Pas de délai de grâce en .fr : sans rédemption, le renouvellement n'est pas encore
            # visible (le registrar renouvelle parfois à l'échéance). On revient dans 3 jours.
            self._schedule(row["label"], iso(info.expires_at), now + timedelta(days=RENEWAL_LAG_DAYS), now)
            return True
        if state in drops.TRACKED:
            self._hand_over(row["label"], result, now)
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

    def _hand_over(self, label: str, result, now: datetime) -> None:
        """Passe le nom à l'écran Suppressions avec le résultat déjà lu : pas de seconde requête."""
        self.drops.record("fr", [label], [], now)
        self.drops.apply(self.drops.row(f"{label}.fr"), result, now)

    def _schedule(self, label: str, expires: str | None, following: datetime | None, now: datetime) -> None:
        self.conn.execute(
            "UPDATE fr_names SET expires_at = ?, checked_at = ?, next_check_at = ? WHERE label = ?",
            (expires, iso(now), iso(following), label),
        )

"""Prénoms récents : le fichier national des prénoms de l'Insee, une édition par an.

L'Insee publie les naissances d'une année l'année suivante (parfois deux ans après) : l'année en
cours n'existe jamais. Le fichier est téléchargé par le NAS une fois par mois, trouvé par l'API de
data.gouv.fr (ou à l'adresse saisie dans Paramètres), lu en flux puis effacé. Seul reste, dans le
dossier de données, le classement des 2 000 prénoms les plus donnés des trois années les plus
récentes : il sert au Top, au tri « Les plus donnés en … » et à une raison sur la carte. Il ne touche
ni aux listes suivies (app/data) ni au score.
"""

import asyncio
import csv
import io
import json
import logging
import re
import unicodedata
import zipfile
from datetime import datetime, timedelta
from pathlib import Path

import httpx

from . import drops, wordlists
from .checker import iso, utcnow
from .frdata import _encoding

log = logging.getLogger(__name__)

SEARCH_API = "https://www.data.gouv.fr/api/1/datasets/?q=fichier+des+prenoms&page_size=20"
FILE_NAME = "prenoms_recents.json"
# Clé de la liste dans le Top (à côté de « Prénoms », l'historique 1900-2018).
TOP_KEY = "firstname_recent"
YEARS_KEPT = 3
TOP_KEPT = 2000
# À augmenter quand la lecture du fichier change : force un nouveau téléchargement.
READER_VERSION = 1
REFRESH_DAYS = 30
RETRY_DAYS = 1
# Intitulés connus : ancien format (sexe;preusuel;annais;nombre), récent (sexe;prenom;periode;valeur).
COLUMNS = {"name": ("preusuel", "prenom", "prenoms", "prenom_usuel"),
           "year": ("annais", "periode", "annee", "annee_naissance"),
           "count": ("nombre", "valeur", "nb", "effectif")}
# Colonne du sexe, facultative : filles et garçons sont additionnés, une ligne « ensemble » éventuelle
# est écartée pour ne pas compter deux fois.
SEX_COLUMNS = ("sexe", "sex")
ALL_SEXES = frozenset(("0", "t", "_t", "ensemble", "total", "tous"))
_VALID = re.compile(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?")


class PrenomsError(Exception):
    pass


def _fold(text: str) -> str:
    text = text.strip().strip('"').lstrip("﻿").lower().replace("œ", "oe").replace("æ", "ae")
    text = unicodedata.normalize("NFKD", text)
    return "".join(c for c in text if not unicodedata.combining(c))


def normalize(raw: str) -> str | None:
    """Comme prenoms.txt : minuscules, sans accents, tiret gardé (jean-pierre), espaces et apostrophes retirés."""
    text = re.sub(r"[\s'’]", "", _fold(raw))
    if not 2 <= len(text) <= 63 or "--" in text or not _VALID.fullmatch(text):
        return None
    return text


def find_columns(header: list[str]) -> dict[str, int]:
    """Repère prénom, année et nombre d'après leur intitulé, pas leur position."""
    folded = [_fold(h).replace(" ", "_") for h in header]
    found = {}
    for key, names in COLUMNS.items():
        for i, name in enumerate(folded):
            if name in names:
                found[key] = i
                break
    sex = next((i for i, name in enumerate(folded) if name in SEX_COLUMNS), None)
    if sex is not None:
        found["sex"] = sex
    missing = [k for k in COLUMNS if k not in found]
    if missing:
        raise PrenomsError("Colonnes du fichier des prénoms non reconnues (prénom, année et nombre attendus). "
                           f"Colonnes lues : {', '.join(h.strip() for h in header[:20]) or 'aucune'}.")
    return found


def read_rows(lines) -> tuple[dict[int, dict[str, int]], dict]:
    """Lit le CSV ligne à ligne. Filles et garçons additionnés, seules les années récentes gardées
    en mémoire : les autres sont écartées au fil de la lecture."""
    try:
        sample = next(lines)
    except StopIteration:
        raise PrenomsError("Fichier des prénoms vide.") from None
    delimiter = max((";", ",", "\t"), key=sample.count)
    header = next(csv.reader([sample], delimiter=delimiter))
    columns = find_columns(header)
    years: dict[int, dict[str, int]] = {}
    rows = 0
    for row in csv.reader(lines, delimiter=delimiter):
        rows += 1
        try:
            raw_year, raw_name, raw_count = row[columns["year"]], row[columns["name"]], row[columns["count"]]
        except IndexError:
            continue
        raw_year = raw_year.strip()
        # « XXXX » : prénoms rares sans année ; « _PRENOMS_RARES » : leur regroupement.
        if len(raw_year) != 4 or not raw_year.isdigit() or raw_name.strip().startswith("_"):
            continue
        if "sex" in columns and len(row) > columns["sex"] and _fold(row[columns["sex"]]) in ALL_SEXES:
            continue
        year = int(raw_year)
        if years and year < max(years) - (YEARS_KEPT - 1):
            continue
        name = normalize(raw_name)
        try:
            count = int(float(raw_count.strip().replace(",", ".")))
        except ValueError:
            continue
        if not name or count <= 0:
            continue
        if year not in years:
            years[year] = {}
            for old in [y for y in years if y < max(years) - (YEARS_KEPT - 1)]:
                del years[old]
        bucket = years[year]
        bucket[name] = bucket.get(name, 0) + count
    if not years:
        raise PrenomsError(f"Aucune année lue dans le fichier des prénoms ({rows} lignes, colonnes {', '.join(header[:8])}).")
    stats = {"rows": rows, "columns": {k: header[v].strip() for k, v in columns.items()}}
    return years, stats


def read_file(path: Path) -> tuple[dict[int, dict[str, int]], dict]:
    """Zip (le plus gros CSV de l'archive) ou CSV direct."""
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            members = [m for m in archive.namelist() if m.lower().endswith((".csv", ".txt"))]
            if not members:
                raise PrenomsError("Aucun fichier CSV dans l'archive des prénoms.")
            member = max(members, key=lambda m: archive.getinfo(m).file_size)
            with archive.open(member) as raw:
                encoding = _encoding(raw.read(1 << 16))
            with archive.open(member) as raw:
                return read_rows(iter(io.TextIOWrapper(raw, encoding=encoding, errors="replace", newline="")))
    with path.open("rb") as raw:
        encoding = _encoding(raw.read(1 << 16))
    with path.open(encoding=encoding, errors="replace", newline="") as text:
        return read_rows(iter(text))


def ranking(years: dict[int, dict[str, int]]) -> dict[str, list]:
    """Les TOP_KEPT plus donnés de chaque année gardée : [[prénom, nombre], …], du plus donné au moins."""
    return {str(year): [[n, c] for n, c in sorted(names.items(), key=lambda p: (-p[1], p[0]))[:TOP_KEPT]]
            for year, names in sorted(years.items(), reverse=True)[:YEARS_KEPT]}


def _squash(text: str) -> str:
    return re.sub(r"[\s_\-.]", "", _fold(text))


def pick_resource(datasets: list[dict]) -> dict | None:
    """Fichier national le plus récent (zip ou csv) des jeux de données « prénoms » de l'Insee."""
    matches = []
    for dataset in datasets:
        org = _fold(" ".join(str((dataset.get("organization") or {}).get(k) or "") for k in ("name", "acronym", "slug")))
        if "insee" not in org and "statistique" not in org:
            continue
        if "prenom" not in _fold(f"{dataset.get('title', '')} {dataset.get('slug', '')}"):
            continue
        for resource in dataset.get("resources") or []:
            url = resource.get("url") or ""
            kind = (resource.get("format") or "").lower()
            if not url or not (kind in ("zip", "csv") or url.lower().split("?")[0].endswith((".zip", ".csv"))):
                continue
            haystack = _squash(f"{resource.get('title', '')} {url.rsplit('/', 1)[-1]}")
            if not ("nat" in haystack or "france" in haystack) or any(h in haystack for h in ("dpt", "depart", "region")):
                continue
            matches.append(resource)
    if not matches:
        return None
    return max(matches, key=lambda r: r.get("last_modified") or r.get("created_at") or "")


def _nth(rank: int) -> str:
    return "1er" if rank == 1 else f"{rank}e"


def reason(rank: int, year: int) -> str:
    return f"{_nth(rank)} prénom donné en {year}"


def tracked(label: str) -> bool:
    """Dans l'univers de l'Explorer : nom court valide, ou nom d'une liste suivie."""
    return drops.valid_label(label) or bool(wordlists.categories(label))


class RecentNames:
    def __init__(self, store, data_dir: Path, http_factory=None):
        self.store = store
        self.path = Path(data_dir) / FILE_NAME
        self._http_factory = http_factory or (
            lambda: httpx.AsyncClient(timeout=httpx.Timeout(60, read=300), follow_redirects=True,
                                      headers={"User-Agent": "domain-watch"}))
        self.running = False
        self.version = 0  # change à chaque chargement : l'Explorer refait ses positions
        self._data: dict | None = None
        self._ranks: dict[str, tuple[int, int]] = {}
        self._loaded = False

    # --- Lecture ---------------------------------------------------------------------

    def data(self) -> dict | None:
        """Classements gardés, lus une fois puis remplacés en mémoire à chaque mise à jour."""
        if not self._loaded:
            self._loaded = True
            try:
                self._set(json.loads(self.path.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                self._set(None)
        return self._data

    def _set(self, data: dict | None) -> None:
        if data and str(data.get("latest")) not in (data.get("years") or {}):
            data = None
        self._data = data
        self._ranks = ({name: (rank, count) for rank, (name, count) in enumerate(data["years"][str(data["latest"])], 1)}
                       if data else {})
        self.version += 1

    def reload(self) -> None:
        self._loaded = False
        self.data()

    @property
    def latest(self) -> int | None:
        data = self.data()
        return int(data["latest"]) if data else None

    def ranking(self, year: int | None = None) -> list[tuple[str, int]]:
        data = self.data()
        if not data:
            return []
        return [tuple(p) for p in data["years"].get(str(year or data["latest"]), [])]

    def rank_of(self, label: str) -> tuple[int, int] | None:
        """(rang, nombre) du prénom l'année la plus récente, ou None."""
        self.data()
        return self._ranks.get(label)

    def status(self) -> dict:
        info = json.loads(self.store.get("prenoms_status") or "{}")
        data = self.data()
        names = self.ranking()
        missing = [n for n, _ in names if not tracked(n)]
        return {
            "running": self.running, "at": info.get("at"), "error": info.get("error"),
            "file_date": (data or {}).get("file_date"), "source": (data or {}).get("url"),
            "latest": self.latest, "years": sorted((int(y) for y in (data or {}).get("years", {})), reverse=True),
            "names": len(names), "tracked": len(names) - len(missing), "untracked": len(missing),
            "untracked_examples": missing[:5], "columns": (data or {}).get("columns"),
            "url": self.store.get("prenoms_url"),
        }

    def due(self) -> bool:
        if self.running:
            return False
        info = json.loads(self.store.get("prenoms_status") or "{}")
        last = info.get("at")
        if not last or info.get("reader") != READER_VERSION:
            return True
        days = RETRY_DAYS if info.get("error") else REFRESH_DAYS
        return utcnow() - datetime.fromisoformat(last) >= timedelta(days=days)

    # --- Téléchargement --------------------------------------------------------------

    async def resource_url(self, http: httpx.AsyncClient) -> tuple[str, str | None]:
        custom = self.store.get("prenoms_url").strip()
        if custom:
            return custom, None
        try:
            response = await http.get(SEARCH_API)
        except httpx.HTTPError as exc:
            raise PrenomsError(f"data.gouv.fr injoignable ({exc.__class__.__name__}).") from None
        datasets = []
        if response.status_code == 200:
            try:
                datasets = response.json().get("data") or []
            except ValueError:
                datasets = []
        found = pick_resource(datasets)
        if found:
            return found["url"], found.get("last_modified")
        seen = [r.get("title") or r.get("url", "") for d in datasets for r in d.get("resources") or []]
        detail = f" Fichiers vus : {', '.join(seen[:8])}." if seen else " Aucun fichier vu sur data.gouv.fr."
        raise PrenomsError("Fichier national des prénoms introuvable sur data.gouv.fr." + detail
                           + " Coller son adresse dans Paramètres, Prénoms récents, Adresse du fichier.")

    async def refresh(self) -> dict:
        if self.running:
            return self.status()
        self.running = True
        started = utcnow()
        tmp = self.path.with_suffix(".download")
        previous = json.loads(self.store.get("prenoms_status") or "{}")
        try:
            async with self._http_factory() as http:
                url, modified = await self.resource_url(http)
                data = self.data()
                if (modified and data and modified == data.get("file_date") and data.get("url") == url
                        and data.get("reader") == READER_VERSION):
                    # Même édition que la dernière fois : rien à télécharger.
                    self.store.set("prenoms_status", json.dumps({**previous, "at": iso(utcnow()), "error": None}))
                    return self.status()
                self.path.parent.mkdir(parents=True, exist_ok=True)
                try:
                    async with http.stream("GET", url) as response:
                        if response.status_code != 200:
                            raise PrenomsError(f"Téléchargement du fichier des prénoms refusé (HTTP {response.status_code}).")
                        modified = modified or response.headers.get("last-modified")
                        with tmp.open("wb") as out:
                            async for chunk in response.aiter_bytes(1 << 20):
                                out.write(chunk)
                except httpx.HTTPError as exc:
                    raise PrenomsError(f"Téléchargement du fichier des prénoms interrompu ({exc.__class__.__name__}).") from None
            # Quelques centaines de milliers de lignes : lues dans un fil, la boucle reste libre.
            years, stats = await asyncio.to_thread(read_file, tmp)
            kept = ranking(years)
            latest = max(int(y) for y in kept)
            data = {"file_date": modified, "url": url, "latest": latest, "years": kept,
                    "rows": stats["rows"], "columns": stats["columns"], "reader": READER_VERSION}
            part = self.path.with_suffix(".tmp")
            part.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            part.replace(self.path)
            self._set(data)
            self._loaded = True
            self.store.set("prenoms_status", json.dumps({
                "at": iso(utcnow()), "error": None, "reader": READER_VERSION,
                "seconds": round((utcnow() - started).total_seconds()),
            }))
            log.info("Prénoms récents : année %s, %s prénoms", latest, len(kept[str(latest)]))
        except (PrenomsError, zipfile.BadZipFile, OSError) as exc:
            message = str(exc) if isinstance(exc, PrenomsError) else f"Fichier des prénoms illisible ({exc.__class__.__name__})."
            # Le classement précédent reste en place ; nouvel essai le lendemain.
            self.store.set("prenoms_status", json.dumps({**previous, "at": iso(utcnow()), "error": message,
                                                         "reader": READER_VERSION}))
        finally:
            tmp.unlink(missing_ok=True)
            self.running = False
        return self.status()

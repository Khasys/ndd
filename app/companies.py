"""Entreprises : présence en ligne des grandes entreprises françaises, pour un audit (pas pour revendre).

Les 5 000 plus grandes entreprises actives viennent de l'API de recherche d'entreprises de l'État
(Annuaire des Entreprises : Insee Sirene, INPI), relue tous les mois par le NAS : les unités légales des
grandes entreprises et des ETI, rangées par leur propre tranche d'effectif, la plus grande d'abord, puis par
dernier chiffre d'affaires publié. La catégorie (GE, ETI) est celle du groupe entier : toute filiale d'un
grand groupe est « GE », d'où plus de 10 000 unités légales « GE » ; elle sert de filtre, pas de rang.
Administrations, associations et organismes (formes juridiques 1, 2, 7, 8, 9) sont écartés. L'Insee ne
publie l'effectif que par tranches.

Pour chacune, les noms tirés de sa raison sociale, de son sigle, de ses noms commerciaux (nom commercial et
enseignes du siège) et des noms ajoutés à la main, en .fr et en .com : enregistrés ou libres. Le .com par le
RDAP de Verisign ; le .fr par le fichier mensuel de l'Afnic, le RDAP .fr de l'appli n'ayant que quelques
requêtes par minute. Un nom enregistré l'est peut-être par un tiers : le titulaire est masqué.

À part du reste de l'appli : ces noms n'entrent ni dans les listes, ni dans la notation IA, ni dans les
pépites, la Veille ou les alertes. Rien n'est acheté : l'écran constate, l'audit se fait à la main.
"""

import asyncio
import json
import logging
import re
import time
import unicodedata
from datetime import date, datetime, timedelta
from pathlib import Path

import httpx

from .checker import iso, parse_iso, utcnow
from .rdap import parse_domain

log = logging.getLogger(__name__)

API = "https://recherche-entreprises.api.gouv.fr/search"
USER_AGENT = "domain-watch (audit de presence en ligne des entreprises)"
API_RATE = 4.0       # requêtes par seconde : l'API en accepte 7 au plus par adresse
PER_PAGE = 25        # le plus que l'API rende par page
MAX_PAGES = 400      # 10 000 résultats par recherche : les ETI sont lues tranche par tranche
TOP = 5000
REFRESH_DAYS = 30
TLDS = ("fr", "com")
MAX_NAMES = 8        # noms vérifiés par entreprise, raison sociale d'abord
COM_RDAP = "https://rdap.verisign.com/com/v1"
COM_RATE = 4.0       # Verisign en accepte bien plus (25 par seconde pour la passe des absents)
RECHECK_DAYS = 30
SOON_DAYS = 60       # échéance proche : à revoir chaque semaine, et signalée
COM_BATCH = 2000     # noms .com par passage de la tâche
CATEGORIES = ("GE", "ETI", "PME")
# Premier chiffre de la catégorie juridique Insee : 3 droit étranger, 4 public soumis au droit commercial (EPIC),
# 5 sociétés commerciales, 6 autres personnes morales de droit privé (coopératives, sociétés civiles).
BUSINESS = ("3", "4", "5", "6")
# Codes Insee, de la plus grande à la plus petite (libellés : app/labels/tranches-effectifs.json de l'API).
HEADCOUNT = {
    "53": "10 000 salariés et plus", "52": "5 000 à 9 999 salariés", "51": "2 000 à 4 999 salariés",
    "42": "1 000 à 1 999 salariés", "41": "500 à 999 salariés", "32": "250 à 499 salariés",
    "31": "200 à 249 salariés", "22": "100 à 199 salariés", "21": "50 à 99 salariés", "12": "20 à 49 salariés",
    "11": "10 à 19 salariés", "03": "6 à 9 salariés", "02": "3 à 5 salariés", "01": "1 ou 2 salariés",
    "00": "0 salarié", "NN": "Non employeuse",
}
TRANCHES = list(HEADCOUNT)
# Formes juridiques en tête ou en fin de raison sociale : pas dans le nom de domaine.
LEGAL_FORMS = {"sa", "sas", "sasu", "sarl", "eurl", "snc", "sca", "scs", "se", "scop", "sci", "gie", "sem", "selarl",
               "selas", "ste", "societe anonyme", "ltd", "gmbh", "inc", "bv", "nv", "ag", "spa", "srl", "plc"}
_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")


class CompanyError(Exception):
    pass


def fold(text: str) -> str:
    """Minuscules sans accents, pour la recherche et les noms de domaine."""
    return unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode().lower()


def labels_of(text: str) -> list[str]:
    """Noms de domaine d'un nom d'entreprise : mots collés, et avec des tirets s'il y en a plusieurs.
    L'Oréal : loreal ; Société Générale : societegenerale, societe-generale. Un nom masqué par l'Insee
    (« NON DIFFUSIBLE ») n'en donne aucun."""
    if "diffusible" in fold(text):
        return []
    words = re.findall(r"[a-z0-9]+", fold(text).replace("&", " et "))
    while words and words[-1] in LEGAL_FORMS:
        words.pop()
    while words and words[0] in LEGAL_FORMS:
        words.pop(0)
    if len(words) >= 2 and " ".join(words[-2:]) in LEGAL_FORMS:
        words = words[:-2]
    found = []
    for label in ("".join(words), "-".join(words)):
        if _LABEL.fullmatch(label) and label not in found:
            found.append(label)
    return found


def first_word(text: str) -> str | None:
    """Premier mot d'une raison sociale quand c'est une marque (STELLANTIS AUTO : stellantis) : ni un mot courant
    (SOCIETE, ELECTRICITE, SAFRAN), qui ferait croire à un trou, ni moins de trois lettres."""
    from . import wordlists

    if "diffusible" in fold(text):
        return None
    words = [w for w in re.findall(r"[a-z0-9]+", fold(text)) if w not in LEGAL_FORMS]
    if len(words) < 2 or len(words[0]) < 3 or not words[0].isalpha():
        return None
    lists = wordlists.lists()
    if any(words[0] in lists.get(key, ()) for key in ("word", "form", "english")):
        return None
    return words[0]


def names_of(company: dict) -> list[tuple[str, str]]:
    """(libellé, origine) : raison sociale (et son premier mot s'il est une marque), sigle, noms commerciaux,
    noms ajoutés ; MAX_NAMES au plus, plus les noms ajoutés."""
    sources = [(company.get("name"), "raison sociale"), (first_word(company.get("name") or ""), "raison sociale (1er mot)"),
               (company.get("sigle"), "sigle")]
    sources += [(c, "nom commercial") for c in company.get("commercial") or []]
    sources += [(c, "ajouté") for c in company.get("extra") or []]
    found: dict[str, str] = {}
    for text, origin in sources:
        for label in labels_of(text or ""):
            found.setdefault(label, origin)
    added = [(label, origin) for label, origin in found.items() if origin == "ajouté"]
    automatic = [(label, origin) for label, origin in found.items() if origin != "ajouté"]
    return automatic[:MAX_NAMES] + added  # les noms ajoutés à la main sont toujours vérifiés


def parse(result: dict) -> dict:
    """Une entreprise de l'API (modèle UniteLegaleResponse) : ce que l'écran montre."""
    siege = result.get("siege") or {}
    finances = result.get("finances") or {}
    years = [y for y, f in finances.items() if isinstance(f, dict) and f.get("ca") is not None]
    year = max(years) if years else None
    commercial: list[str] = []
    for name in [siege.get("nom_commercial"), *(siege.get("liste_enseignes") or [])]:
        name = (name or "").strip()
        if name and fold(name) not in {fold(c) for c in commercial} and fold(name) != fold(result.get("nom_raison_sociale")):
            commercial.append(name)
    return {
        "siren": result["siren"], "name": (result.get("nom_raison_sociale") or result.get("nom_complet") or "").strip(),
        "sigle": (result.get("sigle") or "").strip() or None, "commercial": commercial,
        "category": result.get("categorie_entreprise"), "headcount": result.get("tranche_effectif_salarie"),
        "headcount_year": result.get("annee_tranche_effectif_salarie"),
        "revenue": finances[year]["ca"] if year else None, "revenue_year": year,
        "net_income": finances[year].get("resultat_net") if year else None,
        "naf": result.get("activite_principale"), "city": siege.get("libelle_commune"),
        "postcode": siege.get("code_postal"), "siret": siege.get("siret"), "created_on": result.get("date_creation"),
    }


def rank_key(company: dict) -> tuple:
    tranche = TRANCHES.index(company["headcount"]) if company.get("headcount") in TRANCHES else len(TRANCHES)
    revenue = company.get("revenue")
    return tranche, -(revenue if revenue is not None else -1), company.get("name") or ""


def is_business(result: dict) -> bool:
    return (result.get("nature_juridique") or "")[:1] in BUSINESS


class Directory:
    """L'API de recherche d'entreprises, au rythme qu'elle accepte (429 : on attend Retry-After)."""

    def __init__(self, http_factory=None, sleep=None, clock=None, rate: float = API_RATE):
        self._http_factory = http_factory or (lambda: httpx.AsyncClient(
            timeout=20, headers={"User-Agent": USER_AGENT, "Accept": "application/json"}))
        self.sleep = sleep or asyncio.sleep
        self.clock = clock or time.monotonic
        self.rate = rate
        self._next_at = 0.0
        self._lock = asyncio.Lock()

    async def search(self, http, params: dict) -> dict:
        for attempt in range(5):
            async with self._lock:
                wait = self._next_at - self.clock()
                if wait > 0:
                    await self.sleep(wait)
                self._next_at = max(self._next_at, self.clock()) + 1 / self.rate
            try:
                response = await http.get(API, params=params)
            except httpx.HTTPError as exc:
                error = f"Annuaire des Entreprises injoignable ({exc.__class__.__name__})"
                await self.sleep(2 ** attempt)
                continue
            if response.status_code == 200:
                return response.json()
            error = f"Annuaire des Entreprises : réponse {response.status_code}"
            if response.status_code == 429 or response.status_code >= 500:
                try:
                    pause = float(response.headers.get("retry-after") or 0)
                except ValueError:
                    pause = 0
                await self.sleep(max(pause, 2 ** attempt))
                continue
            try:
                detail = response.json().get("erreur") or response.json().get("detail")
            except ValueError:
                detail = None
            raise CompanyError(f"{error}{f' ({detail})' if detail else ''}")
        raise CompanyError(error)

    async def top(self, limit: int = TOP) -> list[dict]:
        """Les plus grandes entreprises actives, rangées (rank_key) : GE et ETI, tranche d'effectif par tranche,
        jusqu'à en avoir assez ; la dernière tranche est lue en entier, pour la ranger par chiffre d'affaires.
        Au NAS le 27/10/2026 : 89, 170, 874, 1 490 puis 2 962 unités de 10 000 salariés et plus à 500 à 999."""
        found: dict[str, dict] = {}
        base = {"etat_administratif": "A", "per_page": PER_PAGE, "minimal": "true", "include": "siege,finances"}
        async with self._http_factory() as http:
            async def sweep(**filters) -> None:
                page = 1
                while page <= MAX_PAGES:
                    data = await self.search(http, {**base, **filters, "page": page})
                    for result in data.get("results") or []:
                        if (result.get("siren") and result.get("categorie_entreprise") in ("GE", "ETI")
                                and is_business(result)):
                            found.setdefault(result["siren"], parse(result))
                    if page >= (data.get("total_pages") or 0):
                        break
                    page += 1

            for tranche in TRANCHES:
                if len(found) >= limit:
                    break
                await sweep(categorie_entreprise="GE,ETI", tranche_effectif_salarie=tranche)
        return sorted(found.values(), key=rank_key)[:limit]

    async def find(self, q: str, limit: int = 8) -> list[dict]:
        """Recherche libre (raison sociale, nom commercial, SIREN ou SIRET), pour une entreprise hors du top."""
        params = {"q": q, "per_page": min(limit, PER_PAGE), "minimal": "true", "include": "siege,finances"}
        async with self._http_factory() as http:
            data = await self.search(http, params)
        return [parse(r) for r in data.get("results") or [] if r.get("siren")]


# --- Base -----------------------------------------------------------------------------------------

COLUMNS = ("siren", "name", "sigle", "category", "headcount", "headcount_year", "revenue", "revenue_year",
           "net_income", "naf", "city", "postcode", "siret", "created_on")


def _row(row) -> dict:
    company = {k: row[k] for k in COLUMNS}
    company.update(commercial=json.loads(row["commercial"] or "[]"), extra=json.loads(row["extra"] or "[]"),
                   rank=row["rank"], added_at=row["added_at"])
    return company


def _folded(company: dict) -> str:
    return " ".join(fold(t) for t in [company.get("name"), company.get("sigle"), *(company.get("commercial") or []),
                                      *(company.get("extra") or [])] if t)


def save_top(conn, companies: list[dict], now: datetime) -> dict:
    """Le nouveau top : les entreprises sorties du top disparaissent, sauf celles ajoutées à la main."""
    stamp = iso(now)
    conn.execute("BEGIN")
    try:
        conn.execute("UPDATE companies SET rank = NULL")
        for position, company in enumerate(companies, 1):
            conn.execute(
                f"""INSERT INTO companies ({', '.join(COLUMNS)}, commercial, rank, updated_at, folded)
                    VALUES ({', '.join('?' * len(COLUMNS))}, ?, ?, ?, ?)
                    ON CONFLICT(siren) DO UPDATE SET {', '.join(f'{c} = excluded.{c}' for c in COLUMNS[1:])},
                      commercial = excluded.commercial, rank = excluded.rank, updated_at = excluded.updated_at,
                      folded = excluded.folded""",
                (*(company.get(c) for c in COLUMNS), json.dumps(company["commercial"], ensure_ascii=False), position,
                 stamp, _folded(company)))
        gone = conn.execute("DELETE FROM companies WHERE rank IS NULL AND added_at IS NULL").rowcount
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    _forget_unused(conn)
    return {"kept": len(companies), "gone": gone}


def add(conn, company: dict, now: datetime) -> None:
    """Entreprise hors du top, ajoutée depuis la recherche : gardée jusqu'à ce qu'on la retire."""
    conn.execute(
        f"""INSERT INTO companies ({', '.join(COLUMNS)}, commercial, added_at, updated_at, folded)
            VALUES ({', '.join('?' * len(COLUMNS))}, ?, ?, ?, ?)
            ON CONFLICT(siren) DO UPDATE SET added_at = COALESCE(companies.added_at, excluded.added_at)""",
        (*(company.get(c) for c in COLUMNS), json.dumps(company["commercial"], ensure_ascii=False), iso(now), iso(now),
         _folded(company)))


def remove(conn, siren: str) -> bool:
    """Retire une entreprise ajoutée à la main ; une entreprise du top revient à la relecture suivante."""
    done = conn.execute("DELETE FROM companies WHERE siren = ? AND rank IS NULL", (siren,)).rowcount
    if not done:
        conn.execute("UPDATE companies SET added_at = NULL WHERE siren = ?", (siren,))
    _forget_unused(conn)
    return bool(done)


def set_extra(conn, siren: str, names: list[str]) -> dict | None:
    row = conn.execute("SELECT * FROM companies WHERE siren = ?", (siren,)).fetchone()
    if row is None:
        return None
    company = _row(row)
    company["extra"] = [n.strip() for n in names if n and n.strip() and labels_of(n)][:10]
    conn.execute("UPDATE companies SET extra = ?, folded = ? WHERE siren = ?",
                 (json.dumps(company["extra"], ensure_ascii=False), _folded(company), siren))
    _forget_unused(conn)
    return company


def wanted(conn) -> dict[str, tuple[str, str]]:
    """Tous les noms à vérifier : domaine -> (libellé, extension)."""
    found = {}
    for row in conn.execute("SELECT * FROM companies"):
        for label, _ in names_of(_row(row)):
            for tld in TLDS:
                found[f"{label}.{tld}"] = (label, tld)
    return found


def _forget_unused(conn) -> None:
    keep = wanted(conn)
    gone = [r[0] for r in conn.execute("SELECT name FROM company_domains") if r[0] not in keep]
    conn.executemany("DELETE FROM company_domains WHERE name = ?", [(n,) for n in gone])


# --- Lecture pour l'écran ---------------------------------------------------------------------------

def _domains(conn) -> dict[str, dict]:
    return {r["name"]: dict(r) for r in conn.execute("SELECT * FROM company_domains")}


def describe(company: dict, domains: dict[str, dict], today: date) -> dict:
    """L'entreprise avec ses noms .fr et .com, leur état, et ses trous (noms libres, échéance proche)."""
    out = {**company, "headcount_label": HEADCOUNT.get(company.get("headcount") or "", None), "domains": []}
    soon = (today + timedelta(days=SOON_DAYS)).isoformat()
    free = expiring = 0
    for label, origin in names_of(company):
        for tld in TLDS:
            name = f"{label}.{tld}"
            row = domains.get(name) or {}
            entry = {"name": name, "label": label, "tld": tld, "origin": origin, "state": row.get("state"),
                     "created_on": row.get("created_on"), "expires_on": row.get("expires_on"),
                     "registrar": row.get("registrar"), "source": row.get("source"), "checked_at": row.get("checked_at")}
            entry["soon"] = bool(entry["state"] == "taken" and entry["expires_on"] and entry["expires_on"] <= soon)
            free += entry["state"] == "free"
            expiring += entry["soon"]
            out["domains"].append(entry)
    out["gaps"] = {"free": free, "expiring": expiring}
    return out


def listing(conn, q: str = "", gaps: str = "", category: str = "", limit: int = 100, offset: int = 0,
            today: date | None = None) -> dict:
    today = today or utcnow().date()
    where, args = ["1"], []
    q = fold(q).strip()
    if q:
        if q.isdigit():
            where.append("(siren LIKE ? OR siret LIKE ?)")
            args += [f"{q[:9]}%", f"{q}%"]
        else:
            where.append("folded LIKE ?")
            args.append(f"%{q}%")
    if category in CATEGORIES:
        where.append("category = ?")
        args.append(category)
    rows = conn.execute(f"SELECT * FROM companies WHERE {' AND '.join(where)} "
                        "ORDER BY rank IS NULL, rank, name", args).fetchall()
    domains = _domains(conn)
    items = [describe(_row(r), domains, today) for r in rows]
    if gaps == "free":
        items = [i for i in items if i["gaps"]["free"]]
    elif gaps == "expiring":
        items = [i for i in items if i["gaps"]["expiring"]]
    return {"total": len(items), "items": items[offset:offset + limit]}


def suggest_local(conn, q: str, limit: int = 8) -> list[dict]:
    q = fold(q).strip()
    if len(q) < 2:
        return []
    if q.isdigit():
        rows = conn.execute("SELECT * FROM companies WHERE siren LIKE ? OR siret LIKE ? ORDER BY rank IS NULL, rank LIMIT ?",
                            (f"{q[:9]}%", f"{q}%", limit)).fetchall()
    else:
        # Début de mot d'abord (« soc » : Société Générale avant Assoc…), puis le reste.
        rows = conn.execute(
            "SELECT * FROM companies WHERE folded LIKE ? ORDER BY NOT (folded LIKE ? OR folded LIKE ?), rank IS NULL, rank "
            "LIMIT ?", (f"%{q}%", f"{q}%", f"% {q}%", limit)).fetchall()
    return [_short(_row(r), True) for r in rows]


def _short(company: dict, listed: bool) -> dict:
    return {"siren": company["siren"], "name": company["name"], "sigle": company.get("sigle"),
            "commercial": company.get("commercial") or [], "city": company.get("city"),
            "category": company.get("category"), "headcount_label": HEADCOUNT.get(company.get("headcount") or ""),
            "rank": company.get("rank"), "listed": listed}


def status(conn, store) -> dict:
    counts = dict(conn.execute("SELECT COALESCE(state, 'unchecked'), COUNT(*) FROM company_domains GROUP BY 1").fetchall())
    total = len(wanted(conn))
    return {**json.loads(store.get("companies_status") or "{}"),
            "companies": conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0],
            "added": conn.execute("SELECT COUNT(*) FROM companies WHERE added_at IS NOT NULL").fetchone()[0],
            "names": total, "taken": counts.get("taken", 0), "free": counts.get("free", 0),
            "unchecked": total - counts.get("taken", 0) - counts.get("free", 0),
            "fr_scan": json.loads(store.get("companies_fr") or "{}")}


# --- Vérification des noms --------------------------------------------------------------------------

def due_com(conn, now: datetime, limit: int = COM_BATCH) -> list[str]:
    """Noms .com jamais vus, vus il y a plus de 30 jours, ou à échéance proche vus il y a plus d'une semaine."""
    rows = {r["name"]: r for r in conn.execute("SELECT name, state, expires_on, checked_at FROM company_domains WHERE tld = 'com'")}
    old, week, soon = iso(now - timedelta(days=RECHECK_DAYS)), iso(now - timedelta(days=7)), (now + timedelta(days=SOON_DAYS)).date().isoformat()
    due = []
    for name, (_, tld) in wanted(conn).items():
        if tld != "com":
            continue
        row = rows.get(name)
        if (row is None or not row["checked_at"] or row["checked_at"] < old
                or (row["state"] == "taken" and row["expires_on"] and row["expires_on"] <= soon and row["checked_at"] < week)):
            due.append(name)
    return due[:limit]


def save_checks(conn, results: list[dict]) -> None:
    conn.execute("BEGIN")
    try:
        conn.executemany(
            """INSERT INTO company_domains (name, label, tld, state, created_on, expires_on, registrar, source, checked_at, error)
               VALUES (:name, :label, :tld, :state, :created_on, :expires_on, :registrar, :source, :checked_at, :error)
               ON CONFLICT(name) DO UPDATE SET state = COALESCE(excluded.state, company_domains.state),
                 created_on = COALESCE(excluded.created_on, company_domains.created_on),
                 expires_on = COALESCE(excluded.expires_on, company_domains.expires_on),
                 registrar = COALESCE(excluded.registrar, company_domains.registrar),
                 source = excluded.source, checked_at = excluded.checked_at, error = excluded.error""",
            results)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def _day(value) -> str | None:
    return value.date().isoformat() if value else None


class Audit:
    """Relecture du top (tous les mois), passe .com (RDAP Verisign) et lecture du fichier .fr de l'Afnic."""

    def __init__(self, conn, store, data_dir: Path, directory: Directory | None = None, http_factory=None,
                 sleep=None, clock=None):
        self.conn, self.store, self.dir = conn, store, Path(data_dir)
        self.directory = directory or Directory()
        self._http_factory = http_factory or (lambda: httpx.AsyncClient(
            timeout=20, follow_redirects=True,
            headers={"Accept": "application/rdap+json, application/json", "User-Agent": USER_AGENT}))
        self.sleep = sleep or asyncio.sleep
        self.clock = clock or time.monotonic
        self.running = False
        self.current = ""

    def _status(self, **changes) -> None:
        found = json.loads(self.store.get("companies_status") or "{}")
        found.update(changes)
        self.store.set("companies_status", json.dumps(found, ensure_ascii=False))

    def due(self, now: datetime) -> bool:
        done = parse_iso(json.loads(self.store.get("companies_status") or "{}").get("ok_at"))
        tried = parse_iso(json.loads(self.store.get("companies_status") or "{}").get("at"))
        if tried and now - tried < timedelta(hours=6):
            return False  # erreur récente : pas de nouvel essai tout de suite
        return done is None or now - done >= timedelta(days=REFRESH_DAYS)

    async def refresh(self, off) -> dict:
        """Relit le top depuis l'API ; l'écriture se fait hors de la boucle (off)."""
        started = utcnow()
        self.current = "liste des entreprises"
        try:
            top = await self.directory.top()
            if not top:
                raise CompanyError("Annuaire des Entreprises : aucune entreprise rendue")
            saved = await off(save_top, self.conn, top, utcnow())
            self._status(at=iso(utcnow()), ok_at=iso(utcnow()), error=None, seconds=round((utcnow() - started).total_seconds()),
                         **saved)
            return saved
        except CompanyError as exc:
            self._status(at=iso(utcnow()), error=str(exc))
            log.warning("Entreprises : %s", exc)
            return {"error": str(exc)}
        finally:
            self.current = ""

    async def check_com(self, off, limit: int = COM_BATCH) -> dict:
        """Les noms .com à revoir, au registre : 404 libre, 200 enregistré (création, échéance, registrar)."""
        names = await off(due_com, self.conn, utcnow(), limit)
        done, failed, last_error, results, pause_end, next_at = 0, 0, None, [], 0.0, 0.0
        targets = await off(wanted, self.conn)
        async with self._http_factory() as http:
            for name in names:
                wait = max(pause_end, next_at) - self.clock()
                if wait > 0:
                    await self.sleep(wait)
                next_at = self.clock() + 1 / COM_RATE
                self.current = name
                label, tld = targets.get(name, (name.rsplit(".", 1)[0], "com"))
                entry = {"name": name, "label": label, "tld": tld, "state": None, "created_on": None, "expires_on": None,
                         "registrar": None, "source": "rdap", "checked_at": iso(utcnow()), "error": None}
                # Sans réponse nette (réseau, refus, réponse illisible), rien n'est écrit : le nom est redemandé au
                # passage suivant au lieu d'attendre 30 jours.
                try:
                    response = await http.get(f"{COM_RDAP}/domain/{name}")
                except httpx.HTTPError as exc:
                    failed, last_error = failed + 1, f"Erreur réseau : {exc.__class__.__name__}"
                    continue
                if response.status_code == 404:
                    entry["state"] = "free"
                elif response.status_code == 200:
                    try:
                        info = parse_domain(response.json())
                    except ValueError:
                        failed, last_error = failed + 1, "Réponse RDAP illisible"
                        continue
                    entry.update(state="taken", created_on=_day(info.registered_at), expires_on=_day(info.expires_at),
                                 registrar=info.registrar)
                else:
                    failed, last_error = failed + 1, f"Réponse {response.status_code} du registre"
                    if response.status_code in (403, 429) or response.status_code >= 500:
                        try:
                            pause = float(response.headers.get("retry-after") or 60)
                        except ValueError:
                            pause = 60
                        pause_end = self.clock() + min(pause, 3600)
                        log.warning("Entreprises : Verisign refuse (%s), pause %d s", last_error, pause)
                        if response.status_code == 403:
                            break  # refus franc : on reprend au passage suivant
                    continue
                results.append(entry)
                done += 1
                if len(results) >= 200:
                    await off(save_checks, self.conn, results)
                    results = []
        if results:
            await off(save_checks, self.conn, results)
        self.current = ""
        self._status(com_at=iso(utcnow()), com_checked=done, com_failed=failed, com_error=last_error)
        return {"asked": len(names), "checked": done, "failed": failed}

    def fr_due(self, file_date: str | None, now: datetime) -> bool:
        """Fichier de l'Afnic plus récent que la dernière lecture (import .fr de l'appli), ou noms .fr pas encore
        cherchés dedans (entreprise ou nom ajouté) ; six heures au moins entre deux lectures pour ces derniers."""
        last = json.loads(self.store.get("companies_fr") or "{}")
        at = parse_iso(last.get("at"))
        if at and now - at < timedelta(hours=6):
            return False
        if file_date and file_date != last.get("file_date"):
            return True
        seen = {r[0] for r in self.conn.execute("SELECT name FROM company_domains WHERE tld = 'fr' AND state IS NOT NULL")}
        return any(tld == "fr" and name not in seen for name, (_, tld) in wanted(self.conn).items())

    async def check_fr(self, off, fr_watcher) -> dict:
        """Lit le fichier mensuel de l'Afnic pour les seuls noms .fr des entreprises (le RDAP .fr de l'appli est
        plafonné). Un nom absent du fichier, ou retiré, est libre d'après l'Afnic à la date du fichier."""
        from . import frdata

        targets = await off(wanted, self.conn)
        labels = {label for label, tld in targets.values() if tld == "fr"}
        if not labels:
            return {}
        self.current = "fichier .fr de l'Afnic"
        path = self.dir / "companies-afnic.zip"
        try:
            async with fr_watcher._http_factory() as http:
                url, modified = await fr_watcher.resource_url(http)
                async with http.stream("GET", url) as response:
                    if response.status_code != 200:
                        raise CompanyError(f"Téléchargement Afnic refusé (HTTP {response.status_code})")
                    with path.open("wb") as out:
                        async for chunk in response.aiter_bytes(1 << 20):
                            out.write(chunk)
            found, _ = await asyncio.to_thread(frdata.read_zip, path, lambda label: label in labels)
            stamp = iso(utcnow())
            results = []
            for label in sorted(labels):
                created, removed = found.get(label, (None, None))
                taken = label in found and removed is None
                results.append({"name": f"{label}.fr", "label": label, "tld": "fr", "state": "taken" if taken else "free",
                                "created_on": created.isoformat() if created else None, "expires_on": None,
                                "registrar": None, "source": f"afnic {modified or ''}".strip(), "checked_at": stamp,
                                "error": None})
            await off(save_checks, self.conn, results)
            self.store.set("companies_fr", json.dumps({"at": stamp, "file_date": modified, "names": len(labels),
                                                       "error": None}))
            return {"names": len(labels), "taken": sum(r["state"] == "taken" for r in results)}
        except (CompanyError, httpx.HTTPError, frdata.FrDataError, OSError, ValueError) as exc:
            last = json.loads(self.store.get("companies_fr") or "{}")
            last.update(at=iso(utcnow()), error=str(exc) or exc.__class__.__name__)
            self.store.set("companies_fr", json.dumps(last))
            log.warning("Entreprises, fichier .fr : %s", exc)
            return {"error": str(exc)}
        finally:
            path.unlink(missing_ok=True)
            self.current = ""

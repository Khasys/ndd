import asyncio
import hmac
import html
import secrets
import time
import json
import math
import logging
import re
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode

import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from . import ai, bot, calendar_ics, ccdns, db, morning, digest, drops, explore, frdata, frdns, rdapinfo, reserved, seo, signals, states, updates, value, wordlists
from .auth import (
    COOKIE_NAME,
    MIN_PASSWORD_LENGTH,
    LoginGuard,
    Sessions,
    compose_escape,
    hash_password,
    load_or_create_secret,
    verify_credentials,
)
from .checker import Checker, DuplicateDomain, iso, parse_iso, utcnow
from .config import VERSION, load_settings
from .rdap import InvalidDomain, RdapClient, clean_domain, tld_of
from .store import Store
from .telegram import Telegram, TelegramError

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("domain-watch")
STARTED_AT = datetime.now(timezone.utc)


class _Recent(logging.Handler):
    """Derniers avertissements et erreurs du journal, gardés en mémoire pour le diagnostic."""

    def __init__(self, size: int = 50):
        super().__init__(logging.WARNING)
        self.records: list[dict] = []
        self.size = size

    def emit(self, record: logging.LogRecord) -> None:
        # Rien qui touche à l'authentification : ces lignes citent parfois le début d'un réglage.
        if "AUTH_" in str(record.msg):
            return
        try:
            self.records.append({"at": iso(datetime.fromtimestamp(record.created, timezone.utc)),
                                 "level": record.levelname, "logger": record.name, "message": record.getMessage()[:500]})
            del self.records[:-self.size]
        except Exception:  # noqa: BLE001  un journal ne doit jamais casser l'appli
            pass


RECENT = _Recent()
logging.getLogger().addHandler(RECENT)

STATIC = Path(__file__).parent / "static"
PWA_PATHS = {"/manifest.webmanifest", "/sw.js", "/offline"}
AI_PATHS = {"/api/ai/pending", "/api/ai/results"}
# Lecture seule avec le même jeton : de quoi observer et analyser, jamais modifier ni lire un réglage.
READ_PATHS = re.compile(r"^/api/(meta|diag|domains|domains/\d+/history|drops|explore(/[a-z]+)?|ai/(status|rated))$")
PUBLIC_PATHS = {"/login", "/health", "/favicon.svg", "/logo", *PWA_PATHS}
SETUP_PATHS = {"/setup", "/health", "/favicon.svg", "/logo"}
BCRYPT_HASH = re.compile(r"\$2[aby]\$\d{2}\$[./A-Za-z0-9]{53}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = load_settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    conn = db.connect(settings.db_path)
    db.migrate(conn, settings.backup_dir)

    http = httpx.AsyncClient(timeout=15)
    rdap = RdapClient(
        cache_path=settings.data_dir / "rdap-bootstrap.json",
        min_delay=settings.rdap_min_delay,
        timeout=settings.rdap_timeout,
        retries=settings.rdap_retries,
        backoff=settings.rdap_backoff,
        max_retry_after=settings.rdap_max_retry_after,
        whois_min_delay=settings.whois_min_delay,
    )
    store = Store(conn, {"telegram_token": settings.telegram_token, "telegram_chat_id": settings.telegram_chat_id})
    telegram = Telegram(store, http)
    checker = Checker(conn, settings, rdap, http, notify=telegram.send)
    checker.link_base = lambda: settings.app_url or store.get("public_url") or ""
    telegram.handler = bot.build_handler(checker, VERSION)

    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_job(checker.run_due, "interval", seconds=30, max_instances=1, coalesce=True)

    app.state.latest_version = None
    app.state.version_checked_at = None

    async def refresh_latest() -> None:
        if not settings.update_repository:
            return
        found = await updates.latest_version(http, settings.update_repository)
        if found:
            app.state.latest_version = found
            app.state.version_checked_at = iso(utcnow())

    async def send_digest() -> None:
        now_local = datetime.now(digest.local_zone())
        if telegram.configured and digest.due(store, now_local):
            if await telegram.send(digest.build(checker, utcnow(), settings.app_url or store.get("public_url") or "")) is None:
                store.set("digest_last_sent", now_local.date().isoformat())

    async def send_morning() -> None:
        now_local = datetime.now(digest.local_zone())
        if telegram.configured and morning.due(store, now_local):
            text = await _morning_text(app.state)
            # Rien à signaler : pas de message, mais le jour compte comme fait.
            if text is None or await telegram.send(text) is None:
                store.set("morning_last_sent", now_local.date().isoformat())

    async def check_health() -> None:
        if not telegram.configured:
            return
        text = morning.health_message(store, await _off(morning.problems, app.state, utcnow()))
        if text:
            await telegram.send(text)

    async def refresh_signals() -> None:
        await signals.update_due(conn, limit=20)

    # Chargées au démarrage : un fichier de liste absent de l'image doit se voir tout de suite,
    # pas au milieu de la lecture de nuit.
    sizes = {k: len(v) for k, v in wordlists.lists().items()}
    log.info("Listes de noms : %s", ", ".join(f"{k} {n}" for k, n in sizes.items()))
    watcher = drops.DropWatcher(conn, store, settings.data_dir, rdap, checker.delays)

    async def scan_zones() -> None:
        if watcher.due(datetime.now(digest.local_zone())):
            await watcher.run()
            app.state.explorer.warm()  # nouvelle zone : l'Explorer se prépare avant qu'on l'ouvre

    async def check_drops() -> None:
        await watcher.check_due(limit=20)

    fr = frdata.FrWatcher(conn, store, settings.data_dir, rdap, watcher)

    async def import_fr() -> None:
        if fr.due(datetime.now(digest.local_zone())):
            await fr.import_file()
            app.state.explorer.warm()

    async def tick_fr() -> None:
        await fr.tick()

    async def check_fr_dns() -> None:
        if fr_dns.due(datetime.now(digest.local_zone())):
            await fr_dns.run()

    def cc_job(tld):
        async def check() -> None:
            checker = app.state.cc_dns[tld]
            if checker.due(datetime.now(digest.local_zone())):
                await checker.run()
        return check

    async def refresh_reserved() -> None:
        if app.state.reserved.due():
            await app.state.reserved.refresh()
            app.state.explorer.invalidate()
            app.state.explorer.warm()

    now = datetime.now(timezone.utc)
    scheduler.add_job(refresh_latest, "interval", hours=24, next_run_time=now, max_instances=1, id="latest")
    scheduler.add_job(send_digest, "interval", minutes=15, max_instances=1)
    scheduler.add_job(send_morning, "interval", minutes=15, next_run_time=now + timedelta(minutes=5), max_instances=1)
    scheduler.add_job(check_health, "interval", hours=1, next_run_time=now + timedelta(minutes=20), max_instances=1)
    scheduler.add_job(refresh_signals, "interval", hours=1, next_run_time=now + timedelta(minutes=2), max_instances=1)
    scheduler.add_job(scan_zones, "interval", minutes=15, next_run_time=now + timedelta(minutes=1), max_instances=1)
    scheduler.add_job(check_drops, "interval", minutes=5, next_run_time=now + timedelta(minutes=3), max_instances=1)
    scheduler.add_job(import_fr, "interval", hours=6, next_run_time=now + timedelta(minutes=4), max_instances=1)
    scheduler.add_job(refresh_reserved, "interval", hours=6, next_run_time=now + timedelta(minutes=2), max_instances=1)
    # File .fr : chaque minute, autant de requêtes que le plafond réglé le permet.
    scheduler.add_job(tick_fr, "interval", minutes=1, next_run_time=now + timedelta(minutes=6), max_instances=1)
    # DNS .fr : une passe par jour, lancée à minuit, étalée sur la journée.
    scheduler.add_job(check_fr_dns, "interval", minutes=10, next_run_time=now + timedelta(minutes=8), max_instances=1)
    # DNS .co, .ai, .io : même passe du jour, une par extension en parallèle.
    for n, tld in enumerate(ccdns.TLDS):
        scheduler.add_job(cc_job(tld), "interval", minutes=10, next_run_time=now + timedelta(minutes=9 + n),
                          max_instances=1, id=f"check_{tld}_dns")
    scheduler.start()
    telegram.start()

    app.state.refresh_latest = refresh_latest
    app.state.scheduler = scheduler
    app.state.drops = watcher
    app.state.fr = fr
    app.state.reserved = reserved.Reserved(conn, store)
    app.state.explorer = explore.Explorer(conn, watcher, fr, app.state.reserved)
    fr_dns = frdns.FrDns(conn, store, app.state.explorer, digest.local_zone(), notify=telegram.send)
    app.state.fr_dns = fr_dns
    # .co, .ai, .io : une passe par extension, en même temps que le .fr (registres différents).
    app.state.cc_dns = {t: ccdns.CcDns(t, conn, store, app.state.explorer, digest.local_zone(), notify=telegram.send)
                        for t in ccdns.TLDS}
    app.state.seo = seo.Seo(conn, store)
    # Explorer : univers et classement préparés hors requête (thread), puis états et carte en mémoire.
    async def warm_explorer() -> None:
        await asyncio.get_running_loop().run_in_executor(None, lambda: explore.shared_universe().buckets())
        app.state.explorer.warm()

    app.state.explorer_warm = asyncio.create_task(warm_explorer())
    app.state.store = store
    app.state.telegram = telegram
    app.state.settings = settings
    app.state.conn = conn
    app.state.checker = checker
    app.state.http = http
    secret = settings.secret_key or load_or_create_secret(settings.data_dir / "secret_key")
    app.state.sessions = Sessions(secret, settings.session_max_age)
    app.state.guard = LoginGuard(settings.login_max_failures, settings.login_lock_seconds)
    log.info("domain-watch %s démarré", VERSION)
    if settings.setup_mode:
        log.warning("AUTH_USERNAME ou AUTH_PASSWORD_HASH absent : seule la page /setup est accessible")
    elif not BCRYPT_HASH.fullmatch(settings.password_hash):
        log.error(
            "AUTH_PASSWORD_HASH n'est pas un hash bcrypt valide (%d caractères, début %r) : aucune connexion possible",
            len(settings.password_hash),
            settings.password_hash[:7],
        )
    else:
        log.info("Connexion attendue pour l'identifiant %r", settings.username)
    try:
        yield
    finally:
        scheduler.shutdown(wait=False)
        await telegram.stop()
        await rdap.close()
        await http.aclose()
        conn.close()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


SLOW_REQUEST = 1.0  # secondes : au-delà, la requête est notée au journal (lu par /api/diag)


@app.middleware("http")
async def log_slow_requests(request: Request, call_next):
    started = time.monotonic()
    response = await call_next(request)
    spent = time.monotonic() - started
    if spent > SLOW_REQUEST and request.url.path.startswith("/api/"):
        log.warning("Requête lente : %s %s en %.1f s", request.method, request.url.path, spent)
    return response


def _remember_public_url(request: Request) -> None:
    """Adresse publique de l'appli, vue du navigateur : sert aux liens des messages Telegram."""
    host = request.headers.get("x-forwarded-host") or request.headers.get("host")
    if host:
        scheme = request.headers.get("x-forwarded-proto") or request.url.scheme
        url = f"{scheme}://{host}"
        if request.app.state.store.get("public_url") != url:
            request.app.state.store.set("public_url", url)


def _public_url(state) -> str:
    return state.settings.app_url or state.store.get("public_url") or ""


async def _morning_text(state) -> str | None:
    min_score = int(state.store.get("morning_min", "40"))
    status = {tld: c.status() for tld, c in state.cc_dns.items()}
    return await _off(morning.build, state.conn, state.explorer, status, _public_url(state), utcnow(), min_score)


@app.middleware("http")
async def require_session(request: Request, call_next):
    path = request.url.path
    state = request.app.state
    if state.settings.setup_mode:
        if path in SETUP_PATHS or path.startswith("/static/"):
            return await call_next(request)
        if path.startswith("/api/"):
            return JSONResponse({"detail": "Configuration initiale à terminer."}, status_code=503)
        return RedirectResponse("/setup", status_code=303)
    if path in PUBLIC_PATHS or path.startswith(("/static/", "/calendar/")):
        return await call_next(request)
    if state.sessions.valid(request.cookies.get(COOKIE_NAME), state.settings.username):
        if path == "/" and not state.settings.app_url:
            _remember_public_url(request)
        return await call_next(request)
    # Tâche de notation IA : jeton d'accès, limité à l'échange de la liste et des avis.
    if path in AI_PATHS and ai.token_ok(state.store, request.headers.get("authorization")):
        return await call_next(request)
    if (request.method == "GET" and READ_PATHS.match(path)
            and ai.token_ok(state.store, request.headers.get("authorization"))):
        return await call_next(request)
    if path.startswith("/api/"):
        return JSONResponse({"detail": "Session expirée."}, status_code=401)
    # Lien ouvert sans session (notification Telegram) : retour à la même page après connexion.
    if path == "/" and request.url.query:
        return RedirectResponse(f"/login?{urlencode({'next': '/?' + request.url.query})}", status_code=303)
    return RedirectResponse("/login", status_code=303)


THEMES = {"system": ("", "light dark"), "light": (' data-theme="light"', "light"), "dark": (' data-theme="dark"', "dark")}
LOGO_TYPES = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp", "image/svg+xml": "svg"}
LOGO_MAX_BYTES = 300 * 1024
DEFAULT_NAME = "domain-watch"
LOGO_HEIGHT = (20, 120, 32)  # min, max, défaut, en pixels


def _logo_height() -> int:
    low, high, default = LOGO_HEIGHT
    try:
        return min(high, max(low, int(app.state.store.get("logo_height") or default)))
    except ValueError:
        return default


def _logo_path() -> Path | None:
    for ext in LOGO_TYPES.values():
        path = app.state.settings.data_dir / f"logo.{ext}"
        if path.exists():
            return path
    return None


def _brand(name: str) -> str:
    logo = _logo_path()
    img = f'<img class="logo" src="/logo?v={int(logo.stat().st_mtime)}" alt="">' if logo else ""
    return f'<span class="brand">{img}<span class="appname">{html.escape(name)}</span></span>'


def _page(name: str, **values: str) -> HTMLResponse:
    store = app.state.store
    app_name = store.get("app_name") or DEFAULT_NAME
    theme_attr, color_scheme = THEMES.get(store.get("theme", "system"), THEMES["system"])
    content = (STATIC / name).read_text()
    # Fragments HTML construits ici, insérés tels quels.
    # Taille du logo posée dès le rendu : pas de saut à l'affichage.
    theme_attr += f' style="--logo-h: {_logo_height()}px"'
    raw = {"THEME_ATTR": theme_attr, "BRAND": _brand(app_name)}
    for key, value in raw.items():
        content = content.replace("{{" + key + "}}", value)
    for key, value in {"VERSION": VERSION, "APP_NAME": app_name, "COLOR_SCHEME": color_scheme, **values}.items():
        content = content.replace("{{" + key + "}}", html.escape(value))
    return HTMLResponse(content, headers={"Cache-Control": "no-store"})


@app.get("/health")
async def health(request: Request):
    request.app.state.conn.execute("SELECT 1").fetchone()
    return {"status": "ok", "version": VERSION}


@app.get("/logo")
async def logo():
    path = _logo_path()
    if path is None:
        raise HTTPException(404)
    media = next(t for t, ext in LOGO_TYPES.items() if path.suffix == f".{ext}")
    # Un SVG ouvert directement pourrait exécuter du script : la CSP l'en empêche.
    return Response(
        path.read_bytes(),
        media_type=media,
        headers={"Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'", "X-Content-Type-Options": "nosniff"},
    )


@app.get("/favicon.svg")
async def favicon():
    return Response((STATIC / "favicon.svg").read_bytes(), media_type="image/svg+xml")


def _missing(settings) -> str:
    names = [n for n, v in (("AUTH_USERNAME", settings.username), ("AUTH_PASSWORD_HASH", settings.password_hash)) if not v]
    return " et ".join(names) + (" non définis" if len(names) > 1 else " non défini")


@app.get("/setup", response_class=HTMLResponse)
async def setup_page(request: Request):
    settings = request.app.state.settings
    if not settings.setup_mode:
        return RedirectResponse("/", status_code=303)
    return _page("setup.html", MISSING=_missing(settings), ERROR="")


@app.post("/setup", response_class=HTMLResponse)
async def setup_hash(request: Request, password: str = Form(""), confirm: str = Form("")):
    settings = request.app.state.settings
    if not settings.setup_mode:
        return RedirectResponse("/", status_code=303)
    if len(password) < MIN_PASSWORD_LENGTH:
        return _page("setup.html", MISSING=_missing(settings), ERROR=f"{MIN_PASSWORD_LENGTH} caractères minimum.")
    if password != confirm:
        return _page("setup.html", MISSING=_missing(settings), ERROR="Les deux saisies diffèrent.")
    hashed = await run_in_threadpool(hash_password, password)
    return _page("setup-done.html", HASH=compose_escape(hashed))


@app.get("/login", response_class=HTMLResponse)
async def login_page(next: str = ""):
    return _page("login.html", ERROR="", NEXT=_safe_next(next))


def _safe_next(target: str) -> str:
    """Page de retour après connexion : chemin local seulement, jamais un autre site."""
    return target if target.startswith("/") and not target.startswith(("//", "/\\")) and "\n" not in target else "/"


@app.post("/login")
async def login(request: Request, username: str = Form(""), password: str = Form(""), next: str = Form("/")):
    state = request.app.state
    client = request.client.host if request.client else "unknown"
    locked = state.guard.locked_for(client)
    if locked:
        minutes = (locked + 59) // 60
        return _page("login.html", ERROR=f"Trop d'échecs. Nouvel essai possible dans {minutes} min.", NEXT=_safe_next(next))
    ok = await run_in_threadpool(
        verify_credentials, username, password, state.settings.username, state.settings.password_hash
    )
    if not ok:
        state.guard.fail(client)
        # Le journal n'est lisible que par l'admin du NAS : y dire quelle partie échoue aide au diagnostic.
        user_ok = username.strip().casefold() == state.settings.username.casefold()
        log.info(
            "Échec de connexion depuis %s : %s",
            client,
            "mot de passe incorrect" if user_ok else f"identifiant {username.strip()!r} inconnu",
        )
        if state.guard.locked_for(client):
            minutes = (state.settings.login_lock_seconds + 59) // 60
            return _page("login.html", ERROR=f"Trop d'échecs. Nouvel essai possible dans {minutes} min.", NEXT=_safe_next(next))
        return _page("login.html", ERROR="Identifiant ou mot de passe incorrect.", NEXT=_safe_next(next))
    state.guard.success(client)
    response = RedirectResponse(_safe_next(next), status_code=303)
    _set_session(response, state)
    return response


def _set_session(response: Response, state) -> None:
    response.set_cookie(
        COOKIE_NAME,
        state.sessions.create(state.settings.username),
        max_age=state.settings.session_max_age,
        httponly=True,
        # « lax » : la session suit un lien ouvert depuis Telegram ; les envois (POST, PUT) d'un autre site
        # restent sans cookie.
        samesite="lax",
        secure=state.settings.cookie_secure,
    )


@app.post("/logout")
async def logout():
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(COOKIE_NAME)
    return response


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    response = _page("index.html")
    # Session glissante : l'appli installée sur le téléphone ne redemande pas le mot de passe
    # tant qu'elle est ouverte au moins une fois par période SESSION_MAX_AGE.
    _set_session(response, request.app.state)
    return response


@app.get("/manifest.webmanifest")
async def manifest():
    store = app.state.store
    name = store.get("app_name") or DEFAULT_NAME
    icons = [
        {"src": f"/static/icons/icon-{size}.png?v={VERSION}", "sizes": f"{size}x{size}", "type": "image/png", "purpose": "any"}
        for size in (192, 512)
    ] + [{"src": f"/static/icons/maskable-512.png?v={VERSION}", "sizes": "512x512", "type": "image/png", "purpose": "maskable"}]
    body = {
        "name": name,
        "short_name": name[:12],
        "description": "Surveillance de noms de domaine",
        "lang": "fr",
        "id": "/",
        "start_url": "/",
        "scope": "/",
        "display": "standalone",
        "background_color": "#14171c",
        "theme_color": "#1d5c66",
        "icons": icons,
    }
    return JSONResponse(body, media_type="application/manifest+json", headers={"Cache-Control": "no-cache"})


@app.get("/sw.js")
async def service_worker():
    content = (STATIC / "sw.js").read_text().replace("{{VERSION}}", VERSION)
    # Servi depuis la racine pour couvrir toute l'appli.
    return Response(content, media_type="text/javascript", headers={"Cache-Control": "no-cache"})


@app.get("/offline", response_class=HTMLResponse)
async def offline():
    return _page("offline.html")


def _serialize(row: sqlite3.Row, checker: Checker) -> dict:
    now = utcnow()
    release = checker.release_estimate(row, now)
    effective = states.effective_expiry(row["state"], parse_iso(row["expires_at"]), now)
    found_signals = json.loads(row["signals"]) if row["signals"] else {}
    return {
        "id": row["id"],
        "name": row["name"],
        "unicode_name": _unicode(row["name"]),
        "state": row["state"],
        "state_since": row["state_since"],
        "check_error": row["check_error"],
        "epp_statuses": json.loads(row["epp_statuses"]),
        "registered_at": row["registered_at"],
        "updated_at": row["updated_at"],
        "expires_at": row["expires_at"],
        "effective_expires_at": iso(effective),
        "registrar": row["registrar"],
        "nameservers": json.loads(row["nameservers"]),
        "added_at": row["added_at"],
        "last_checked_at": row["last_checked_at"],
        "last_success_at": row["last_success_at"],
        "next_check_at": row["next_check_at"],
        "source": row["source"],
        "release_at": iso(release),
        "release_is_upper_bound": row["state"] in (states.EXPIRING, states.EXPIRED) and release is not None,
        "release_has_time": bool(release and states.drop_time(tld_of(row["name"]), checker.delays)),
        "tags": json.loads(row["tags"]),
        "signals": [{"key": k, "label": signals.LABELS[k]} for k in found_signals.get("signals", [])],
        "signals_web": found_signals.get("web"),
        "signals_checked_at": row["signals_checked_at"],
        "abandoned": signals.suspicious(found_signals),
    }


def _unicode(name: str) -> str:
    try:
        return name.encode("ascii").decode("idna")
    except UnicodeError:
        return name


@app.get("/api/meta")
async def meta(request: Request):
    settings = request.app.state.settings
    return {
        "version": VERSION,
        "intervals": settings.intervals,
        "expiring_days": settings.expiring_days,
        "telegram_configured": request.app.state.telegram.configured,
        "bulk_running": request.app.state.checker.bulk_running,
        "update_available": updates.newer(VERSION, request.app.state.latest_version),
        "latest_version": request.app.state.latest_version,
        "version_checked_at": request.app.state.version_checked_at,
        "version_next_check": _next_version_check(request.app),
        # Premiers pas : ce qui est déjà branché, pour l'accueil.
        "setup": {
            "telegram": request.app.state.telegram.configured,
            "czds": request.app.state.drops.configured,
            "fr": request.app.state.fr.enabled,
            "ai": bool(request.app.state.store.get("ai_token_hash")),
        },
    }


def _next_version_check(app_: FastAPI) -> str | None:
    job = app_.state.scheduler.get_job("latest") if hasattr(app_.state, "scheduler") else None
    if job is None or job.next_run_time is None:
        return None
    return iso(job.next_run_time.astimezone(timezone.utc))


@app.get("/api/domains")
async def list_domains(request: Request):
    checker = request.app.state.checker
    rows = request.app.state.conn.execute("SELECT * FROM domains ORDER BY name").fetchall()
    items = [_serialize(row, checker) for row in rows]
    # Score et avis IA, mêmes calculs que l'Explorer : filtres et tri communs à tous les écrans.
    values = request.app.state.explorer.value_of([d["name"] for d in items])
    for d in items:
        v = values.get(d["name"]) or {}
        d.update(score=v.get("score", 0), reasons=v.get("reasons", []), parts=v.get("parts", {}), ai=v.get("ai"))
    return items


class NewDomain(BaseModel):
    name: str


@app.post("/api/domains", status_code=201)
async def add_domain(request: Request, payload: NewDomain):
    checker = request.app.state.checker
    try:
        row = await checker.add(payload.name)
    except InvalidDomain as exc:
        raise HTTPException(422, str(exc)) from None
    except DuplicateDomain as exc:
        raise HTTPException(409, str(exc)) from None
    return _serialize(row, checker)


def _get_row(request: Request, domain_id: int) -> sqlite3.Row:
    row = request.app.state.conn.execute("SELECT * FROM domains WHERE id = ?", (domain_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "Domaine introuvable.")
    return row


@app.delete("/api/domains/{domain_id}", status_code=204)
async def delete_domain(request: Request, domain_id: int):
    _get_row(request, domain_id)
    request.app.state.conn.execute("DELETE FROM domains WHERE id = ?", (domain_id,))
    return Response(status_code=204)


@app.post("/api/domains/{domain_id}/check")
async def check_domain(request: Request, domain_id: int):
    _get_row(request, domain_id)
    checker = request.app.state.checker
    await checker.check(domain_id)
    return _serialize(_get_row(request, domain_id), checker)


@app.get("/api/domains/{domain_id}/history")
async def domain_history(request: Request, domain_id: int):
    _get_row(request, domain_id)
    rows = request.app.state.conn.execute(
        "SELECT at, field, old_value, new_value FROM history WHERE domain_id = ? ORDER BY at DESC, id DESC",
        (domain_id,),
    ).fetchall()
    return [dict(row) for row in rows]


@app.post("/api/check-all", status_code=202)
async def check_all(request: Request):
    started = request.app.state.checker.check_all()
    return {"started": started}


@app.post("/api/version/check")
async def check_version(request: Request):
    await request.app.state.refresh_latest()
    # Vérification manuelle : la prochaine automatique repart pour 24 h.
    job = request.app.state.scheduler.get_job("latest")
    if job is not None:
        job.modify(next_run_time=utcnow() + timedelta(hours=24))
    return await meta(request)


@app.post("/api/alerts/test")
async def test_alert(request: Request):
    state = request.app.state
    settings = state.settings
    lines = ["domain-watch : alerte test", f"Version {VERSION}"]
    if _public_url(state):
        lines.append(_public_url(state))
    error = await state.telegram.send("\n".join(lines))
    if error:
        raise HTTPException(502, error)
    return {"sent": True}


class Tags(BaseModel):
    tags: list[str]


@app.put("/api/domains/{domain_id}/tags")
async def set_tags(request: Request, domain_id: int, payload: Tags):
    _get_row(request, domain_id)
    tags: list[str] = []
    for tag in payload.tags:
        tag = " ".join(tag.split()).lower()[:30]
        if tag and tag not in tags:
            tags.append(tag)
    request.app.state.conn.execute("UPDATE domains SET tags = ? WHERE id = ?", (json.dumps(tags), domain_id))
    return _serialize(_get_row(request, domain_id), request.app.state.checker)


@app.post("/api/domains/{domain_id}/signals")
async def refresh_domain_signals(request: Request, domain_id: int):
    row = _get_row(request, domain_id)
    if row["state"] not in signals.REGISTERED:
        raise HTTPException(409, "Signaux disponibles seulement pour un domaine enregistré.")
    async with httpx.AsyncClient(
        timeout=10, follow_redirects=True, max_redirects=5,
        headers={"User-Agent": "Mozilla/5.0 (compatible; domain-watch)"},
    ) as http:
        if await signals.update_one(request.app.state.conn, row, http) is None:
            raise HTTPException(503, "Résolveur DNS du NAS injoignable, analyse impossible pour l'instant.")
    return _serialize(_get_row(request, domain_id), request.app.state.checker)


def _calendar_url(request: Request) -> str | None:
    token = request.app.state.store.get("calendar_token")
    if not token:
        return None
    base = request.app.state.settings.app_url or str(request.base_url).rstrip("/")
    return f"{base}/calendar/{token}.ics"


@app.get("/api/settings")
async def get_settings(request: Request):
    state = request.app.state
    store, telegram = state.store, state.telegram
    token = telegram.token
    if token and not telegram.bot_name:
        try:
            await telegram.check_token()
        except (TelegramError, httpx.HTTPError):
            pass
    return {
        "telegram": {
            "token_set": bool(token),
            "bot_name": telegram.bot_name,
            "chat_set": bool(telegram.chat_id),
            "pairing": telegram.pairing_active,
        },
        "digest": {
            "enabled": store.get("digest_enabled", "1") == "1",
            "day": int(store.get("digest_day", "0")),
            "hour": int(store.get("digest_hour", "9")),
            "last_sent": store.get("digest_last_sent") or None,
        },
        "morning": {
            "enabled": store.get("morning_enabled", "1") == "1",
            "hour": int(store.get("morning_hour", "8")),
            "min_score": int(store.get("morning_min", "40")),
            "last_sent": store.get("morning_last_sent") or None,
        },
        "calendar_url": _calendar_url(request),
        "czds": _czds_settings(state.drops),
        "fr": {"enabled": state.fr.enabled, "url": state.store.get("fr_url"), "rate": state.fr.rate,
               "dns": state.store.get("fr_dns_enabled", "1") == "1"},
        "drops": {"keep_days": state.drops.keep_days, "min_score": _drops_min_score(state.store)},
        "premium": {"devalue": state.explorer.premium_devalue(), "prices": state.explorer.premium_prices(),
                    "off": sorted(state.explorer.premium_tlds_off()),
                    "tlds": [t for t in state.explorer.tlds() if t != "fr"]},
        "seo": {"key_set": bool(state.seo.key)},
        "reserved": state.reserved.status(),
        "weights": {"values": value.weights_from(store.get("score_weights")), "labels": value.CRITERIA},
        "order_url": _order_url(store),
        "appearance": {
            "app_name": store.get("app_name") or DEFAULT_NAME,
            "theme": store.get("theme", "system"),
            "logo": _logo_path() is not None,
            "logo_height": _logo_height(),
        },
    }


def _czds_settings(watcher: drops.DropWatcher) -> dict:
    store = watcher.store
    return {
        "username": watcher.username,
        "password_set": bool(store.get("czds_password")),
        "approved": watcher.approved(),
        "enabled": watcher.enabled(),
        "hour": int(store.get("czds_hour", "6")),
        "error": store.get("czds_error") or None,
    }


class CzdsAccount(BaseModel):
    username: str
    password: str = ""


@app.put("/api/settings/czds")
async def set_czds(request: Request, payload: CzdsAccount):
    watcher = request.app.state.drops
    store = watcher.store
    username = payload.username.strip()
    if not username:
        raise HTTPException(422, "Identifiant ICANN manquant.")
    if not payload.password and not store.get("czds_password"):
        raise HTTPException(422, "Mot de passe ICANN manquant.")
    previous = (store.get("czds_username"), store.get("czds_password"))
    store.set("czds_username", username)
    if payload.password:
        store.set("czds_password", payload.password)
    try:
        approved = await watcher.refresh_approved()
    except drops.CzdsError as exc:
        store.set("czds_username", previous[0])
        store.set("czds_password", previous[1])
        raise HTTPException(422, str(exc)) from None
    store.set("czds_error", "")
    if not approved:
        return {**_czds_settings(watcher), "notice": "Compte accepté, mais aucune zone n'est encore autorisée."}
    return _czds_settings(watcher)


@app.delete("/api/settings/czds", status_code=204)
async def delete_czds(request: Request):
    store = request.app.state.drops.store
    for key in ("czds_username", "czds_password", "czds_approved", "czds_enabled", "czds_error"):
        store.set(key, "")
    return Response(status_code=204)


class CzdsZones(BaseModel):
    enabled: list[str]
    hour: int = 6


@app.put("/api/settings/czds/zones")
async def set_czds_zones(request: Request, payload: CzdsZones):
    watcher = request.app.state.drops
    if not 0 <= payload.hour <= 23:
        raise HTTPException(422, "Heure invalide.")
    approved = set(watcher.approved())
    watcher.store.set("czds_enabled", json.dumps(sorted(t for t in payload.enabled if t in approved)))
    watcher.store.set("czds_hour", str(payload.hour))
    return _czds_settings(watcher)


class FrSettings(BaseModel):
    enabled: bool
    url: str = ""
    rate: int | None = None
    dns: bool | None = None


@app.put("/api/settings/fr")
async def set_fr(request: Request, payload: FrSettings):
    store = request.app.state.store
    url = payload.url.strip()
    if url and not url.startswith("https://"):
        raise HTTPException(422, "Adresse du fichier : https:// attendu.")
    if payload.rate is not None and not 1 <= payload.rate <= frdata.MAX_RATE:
        raise HTTPException(422, f"Requêtes par minute : entre 1 et {frdata.MAX_RATE}.")
    store.set("fr_enabled", "1" if payload.enabled else "0")
    store.set("fr_url", url)
    if payload.rate is not None:
        store.set("fr_rate", str(payload.rate))
    if payload.dns is not None:
        store.set("fr_dns_enabled", "1" if payload.dns else "0")
    return {"enabled": payload.enabled, "url": url, "rate": request.app.state.fr.rate,
            "dns": store.get("fr_dns_enabled", "1") == "1"}


class DropsSettings(BaseModel):
    keep_days: int
    min_score: int = 20


def _drops_min_score(store) -> int:
    """Score minimum appliqué à l'ouverture de l'écran Suppressions (bouton « Tout voir » pour l'ignorer)."""
    try:
        return int(store.get("drops_min_score", "20"))
    except ValueError:
        return 20


@app.put("/api/settings/drops")
async def set_drops(request: Request, payload: DropsSettings):
    if not 1 <= payload.keep_days <= 365:
        raise HTTPException(422, "Durée d'affichage : entre 1 et 365 jours.")
    if not 0 <= payload.min_score <= 100:
        raise HTTPException(422, "Score minimum : entre 0 et 100.")
    request.app.state.store.set("drops_keep_days", str(payload.keep_days))
    request.app.state.store.set("drops_min_score", str(payload.min_score))
    return {"keep_days": payload.keep_days, "min_score": payload.min_score}


class PremiumSettings(BaseModel):
    devalue: bool
    prices: dict[str, dict[str, int | None]]
    off: list[str] | None = None  # extensions sans premium chez le registre


@app.put("/api/settings/premium")
async def set_premium(request: Request, payload: PremiumSettings):
    """Premiums probables : dévaluation du score (case), prix saisis par extension pour 2 et 3 caractères."""
    clean = {}
    for tld, by_len in payload.prices.items():
        tld = tld.strip().lower().lstrip(".")
        if not re.fullmatch(r"[a-z]{2,20}", tld):
            continue
        kept = {k: int(v) for k, v in by_len.items() if k in ("2", "3") and v and 0 < int(v) <= 10_000_000}
        if kept:
            clean[tld] = kept
    store = request.app.state.store
    store.set("premium_devalue", "1" if payload.devalue else "0")
    store.set("premium_prices", json.dumps(clean))
    explorer = request.app.state.explorer
    if payload.off is not None:
        off = sorted({t.strip().lower().lstrip(".") for t in payload.off if re.fullmatch(r"\.?[a-z]{2,20}", t.strip().lower())})
        if set(off) != explorer.premium_tlds_off():
            store.set("premium_off", json.dumps(off))
            explorer.invalidate()  # l'état « premium probable » change : états recalculés
            explorer.warm()
    else:
        explorer.refresh_settings()
    return {"devalue": payload.devalue, "prices": clean, "off": sorted(explorer.premium_tlds_off())}


@app.post("/api/drops/fr/import", status_code=202)
async def import_fr_now(request: Request):
    fr = request.app.state.fr
    if not fr.enabled:
        raise HTTPException(409, "Suivi du .fr désactivé.")
    if fr.running:
        return {"started": False}
    async def run() -> None:
        await fr.import_file()
        request.app.state.explorer.warm()

    request.app.state.fr_task = asyncio.create_task(run())
    return {"started": True}


@app.get("/api/drops")
async def list_drops(request: Request):
    watcher = request.app.state.drops
    conn = request.app.state.conn
    # Un nom libre reste affiché keep_days jours ; il reste en base et dans l'Explorer.
    shown_since = iso(utcnow() - timedelta(days=watcher.keep_days))
    rows = conn.execute(
        """SELECT * FROM drops WHERE state IN (?, ?, ?) OR (state = ? AND available_since >= ?)
           ORDER BY release_at IS NULL, release_at, name""",
        (*drops.TRACKED, states.AVAILABLE, shown_since),
    ).fetchall()
    pending = conn.execute("SELECT COUNT(*) FROM drops WHERE state IS NULL").fetchone()[0]
    reserved_names = request.app.state.reserved.names()
    hide_reserved = request.app.state.reserved.mode == "hide"
    held = request.app.state.reserved
    rows = [r for r in rows if not (hide_reserved and (r["name"] in reserved_names or held.short_reserved(r["name"])))]
    suspended = dict(conn.execute("SELECT tld, COUNT(*) FROM drops WHERE state = ? GROUP BY tld", (drops.SUSPENDED,)).fetchall())
    watched = {r[0] for r in conn.execute("SELECT name FROM domains")}
    # Rafraîchissement de l'état seul (écran ouvert) : pas de liste, quelques Ko au lieu de plusieurs Mo.
    # Le détail RDAP n'est pas dans la liste : la fiche le charge.
    with_items = request.query_params.get("items") != "0"
    prices = request.app.state.explorer.premium_prices()
    premium_off = request.app.state.explorer.premium_tlds_off()
    values = await _off(request.app.state.explorer.value_of, [r["name"] for r in rows]) if with_items else {}
    return {
        "configured": watcher.configured or request.app.state.fr.enabled,
        "czds": watcher.configured,
        "running": watcher.running,
        "last_run": watcher.last_run(),
        "error": watcher.store.get("czds_error") or None,
        "zones": [{"tld": t, **watcher.status(t)} for t in watcher.enabled()],
        "history": watcher.history(),
        "fr": {"enabled": request.app.state.fr.enabled, "running": request.app.state.fr.running, **request.app.state.fr.status(),
               "dns": request.app.state.fr_dns.status()},
        "cc_dns": {tld: c.status() for tld, c in request.app.state.cc_dns.items()},
        "workers": watcher.workers,
        "pending_checks": pending,
        "suspended": suspended,
        "keep_days": watcher.keep_days,
        "min_score": _drops_min_score(request.app.state.store),
        "kinds": drops.KINDS,
        "natures": explore.NATURES,
        "items": [
            {
                "name": r["name"],
                "tld": r["tld"],
                "kind": r["kind"],
                "categories": ([r["kind"]] if r["kind"] != "long" else []) + [c for c in r["lists"].split(",") if c],
                "state": r["state"],
                "removed_on": r["removed_on"],
                "release_at": r["release_at"],
                "available_since": r["available_since"],
                "checked_at": r["checked_at"],
                "next_check_at": r["next_check_at"],
                "watched": r["name"] in watched,
                "reserved": r["name"] in reserved_names,
                "state_since": r["state_since"],
                # 3 caractères libres hors .fr : souvent premium chez le registre (voir explore.PREMIUM).
                "premium": (r["tld"] != "fr" and r["tld"] not in premium_off
                            and len(r["name"]) - len(r["tld"]) - 1 == explore.PREMIUM_LENGTH),
                "premium_price": prices.get(r["tld"], {}).get(str(len(r["name"]) - len(r["tld"]) - 1)),
                **values[r["name"]],
            }
            for r in rows
        ] if with_items else None,
    }


class NameCheck(BaseModel):
    name: str


async def _verify(request: Request, name: str) -> dict:
    try:
        name = clean_domain(name)
    except InvalidDomain as exc:
        raise HTTPException(422, str(exc)) from None
    try:
        return await request.app.state.explorer.verify(name)
    except explore.VerifyError as exc:
        raise HTTPException(409, str(exc)) from None


@app.post("/api/drops/check")
async def recheck_drop(request: Request, payload: NameCheck):
    return await _verify(request, payload.name)


# --- Explorer ---------------------------------------------------------------------------


def _explore_params(request: Request) -> dict:
    p = request.query_params
    explorer = request.app.state.explorer
    tlds = explorer.tlds()
    # Une ou plusieurs extensions, séparées par des virgules, dans l'ordre de la liste connue.
    # Une extension inconnue (zone retirée, vue ancienne) est ignorée si une autre est connue ;
    # aucune connue : 404, et l'écran revient à l'extension par défaut.
    asked = {t.strip().lstrip(".").lower() for t in (p.get("tld") or "").split(",") if t.strip()}
    chosen = [t for t in tlds if t in asked] if asked else tlds[:1]
    if not chosen:
        raise HTTPException(404, "Extension sans données : importer le .fr ou lire les zones d'abord.")
    tld = chosen[0]
    try:
        length = int(p["len"]) if p.get("len") else None
        cursor = int(p.get("cursor", "-1"))
        max_rank = int(p["rank"]) if p.get("rank") else None
        min_score = int(p["min"]) if p.get("min") else None
    except ValueError:
        raise HTTPException(422, "Paramètre numérique invalide.") from None
    if p.get("cat") and p["cat"] not in explore.KINDS:
        raise HTTPException(422, "Catégorie inconnue.")
    if p.get("nat") and p["nat"] not in explore.NATURES:
        raise HTTPException(422, "Nature inconnue.")
    taken = p.get("taken", "")
    # « Pris en .X » avec .X dans la sélection, ou inconnu : sans objet.
    if taken and taken != "any" and (taken not in tlds or taken in chosen):
        taken = ""
    mode = p.get("mode", "prefix")
    if mode not in ("prefix", "contains", "exact"):
        raise HTTPException(422, "Mode de recherche inconnu.")
    if p.get("status") and p["status"] not in explore.STATUS_FILTERS:
        raise HTTPException(422, "État inconnu.")
    return {"tld": tld, "tlds": chosen, "q": p.get("q", ""), "mode": mode, "status": p.get("status", ""),
            "category": p.get("cat", ""), "length": length, "cursor": cursor, "nature": p.get("nat", ""),
            "max_rank": max_rank, "min_score": min_score, "taken": taken, "junk": p.get("junk") == "1",
            "seo": p.get("seo", "") if p.get("seo", "") in explore.SEO_FILTERS else "",
            "ai": _ai_param(p),
            "lang": p.get("lang", "") if p.get("lang", "") in wordlists.LANGS and p.get("cat", "") not in wordlists.NO_LANG else "",
            "premium": p.get("premium") == "1"}


def _ai_param(p) -> str:
    """Filtre avis IA : ai_v=take,watch et, pour chaque champ, ai_<champ>_min / ai_<champ>_max."""
    verdicts = [v for v in (p.get("ai_v") or "").split(",") if v]
    if any(v not in explore.AI_VERDICTS for v in verdicts):
        raise HTTPException(422, "Verdict inconnu.")
    ranges = {}
    for field in explore.AI_FIELDS:
        bounds = []
        for side in ("min", "max"):
            raw = (p.get(f"ai_{field}_{side}") or "").strip()
            try:
                bounds.append(int(raw) if raw else None)
            except ValueError:
                raise HTTPException(422, "Paramètre numérique invalide.") from None
        ranges[field] = tuple(bounds)
    return explore.ai_filter(verdicts, ranges)


def _limit(request: Request, most: int) -> int:
    try:
        limit = int(request.query_params.get("limit") or 50)
    except ValueError:
        raise HTTPException(422, "Paramètre numérique invalide.") from None
    if limit < 1:
        raise HTTPException(422, "Paramètre numérique invalide.")
    return min(limit, most)


def _facets(explorer, params: dict) -> dict:
    """Compteurs de la sélection, additionnés sur les extensions choisies."""
    rest = {k: v for k, v in params.items() if k not in ("tld", "tlds", "cursor", "q")}
    total: dict[str, int] = {}
    for tld in params["tlds"]:
        for key, count in explorer.facets(tld, **rest).items():
            total[key] = total.get(key, 0) + count
    return total


def _explore_meta(explorer, tld: str, chosen: list[str] | None = None) -> dict:
    chosen = chosen or [tld]
    return {
        "tld": ",".join(chosen),
        "tlds": explorer.tlds(),
        "kinds": explore.KINDS,
        "natures": explore.NATURES,
        "seo_filters": {k: v[0] for k, v in explore.SEO_FILTERS.items()},
        "ai_verdicts": explore.AI_VERDICTS,
        "langs": wordlists.LANGS,
        "ai_fields": explore.AI_FIELDS,
        "statuses": explore.STATUS_NAMES,
        "absent_label": explore.ABSENT_FR if chosen == ["fr"] else explore.ABSENT_ZONE,
    }


# Calculs de l'Explorer hors de la boucle d'événements : un parcours de plusieurs secondes
# (nouvelle extension, filtres larges) ne bloque plus le reste de l'appli ni la connexion.
# Un seul fil : les calculs de l'écran passent l'un après l'autre, comme avant.
_EXPLORER_POOL = ThreadPoolExecutor(max_workers=1, thread_name_prefix="explorer")


async def _off(fn, *args):
    return await asyncio.get_running_loop().run_in_executor(_EXPLORER_POOL, fn, *args)


@app.get("/api/explore")
async def explore_search(request: Request):
    """Liste par ordre des noms (et export) : la vue « par nom »."""
    explorer = request.app.state.explorer
    params = _explore_params(request)
    chosen = params.pop("tlds")
    limit = _limit(request, explore.MAX_LIMIT)

    def run():
        if len(chosen) == 1:
            result = explorer.search(**params, limit=limit)
        else:
            # Plusieurs extensions : une page par extension, sans curseur commun.
            rest = {k: v for k, v in params.items() if k != "tld"}
            items = [i for tld in chosen for i in explorer.search(tld, **rest, limit=limit)["items"]]
            result = {"items": items[:limit], "cursor": None}
        params["tlds"] = chosen
        return {**result, **_explore_meta(explorer, params["tld"], chosen),
                "counts": _facets(explorer, params)}

    return await _off(run)


@app.get("/api/explore/rank")
async def explore_rank(request: Request):
    """Les meilleures affaires d'abord, avec les compteurs de la sélection."""
    explorer = request.app.state.explorer
    params = _explore_params(request)
    params.pop("cursor")
    params["status"] = params["status"] or "available"
    limit = _limit(request, explore.MAX_LIMIT * 5)
    sort = request.query_params.get("sort", "score")
    if sort not in ("score", "freq", "length", "seo", "ai", "release"):
        raise HTTPException(422, "Tri inconnu.")
    chosen = params["tlds"]
    rest = {k: v for k, v in params.items() if k not in ("tld", "tlds")}
    return await _off(_rank, explorer, params, chosen, rest, limit, sort)


def _rank(explorer, params, chosen, rest, limit, sort):
    if len(chosen) == 1:
        result = explorer.rank(chosen[0], **rest, limit=limit, sort=sort)
    else:
        # Plusieurs extensions : les meilleurs de chacune, fusionnés dans le même ordre.
        items = [item for tld in chosen for item in explorer.rank(tld, **rest, limit=limit, sort=sort)["items"]]
        if sort == "seo":
            rows = explorer._seo_rows([i["name"] for i in items])
            for item in items:
                row = rows.get(item["name"])
                item["_pr"], item["_wb"] = (row["pr"], row["wb_years"]) if row else (0, 0)
        items.sort(key=explorer.merge_key(sort, params["category"]))
        for item in items:
            item.pop("_pr", None)
            item.pop("_wb", None)
        result = {"items": items[:limit]}
    return {**result, **_explore_meta(explorer, params["tld"], chosen),
            "counts": _facets(explorer, params)}


TOP_SIZES = (10, 50, 100, 200, 500, 1000)
# État court pour le tableau Top : pris ou pas, d'un coup d'œil.
TOP_STATES = {explore.REGISTERED: "pris", explore.SUSPENDED: "pris", explore.GONE: "libre ?", explore.ABSENT: "libre ?",
              explore.COMING: "à venir", explore.FREE: "libre", explore.RESERVED: "réservé", explore.INVALID: "pas lu", explore.PREMIUM: "premium ?"}


@app.get("/api/explore/top")
async def explore_top(request: Request):
    """Les N premiers d'une liste, pris ou non, avec leur état sur chaque extension."""
    explorer = request.app.state.explorer
    category = request.query_params.get("cat", "sector")
    if category not in wordlists.LABELS:
        raise HTTPException(422, "Liste inconnue.")
    try:
        size = int(request.query_params.get("n", "50"))
    except ValueError:
        raise HTTPException(422, "Taille invalide.") from None
    if size not in TOP_SIZES:
        raise HTTPException(422, "Taille invalide.")
    return await _off(_top, explorer, category, size, request.query_params.get("tld", ""),
                      request.query_params.get("lang", ""))


def _top(explorer, category, size, wanted, lang):
    u = explorer.universe
    known = explorer.tlds()
    # Extensions choisies dans l'Explorer (toutes si aucune), langue du filtre.
    chosen = [t for t in wanted.split(",") if t in known]
    tlds = chosen or known
    data = {t: explorer.status_bytes(t) for t in tlds}
    order = u.by_frequency[category]
    if lang in wordlists.LANGS and category not in wordlists.NO_LANG:
        names = wordlists.languages()[lang]
        order = [i for i in order if u.label(i) in names]
    order = list(order[:size])
    opinions = ai.load(explorer.conn, [f"{u.label(i)}.{t}" for i in order for t in tlds])
    items = []
    for rank, i in enumerate(order, 1):
        label = u.label(i)
        cells = {t: {"code": data[t][i], "state": TOP_STATES.get(data[t][i], ""), "ai": opinions.get(f"{label}.{t}")}
                 for t in tlds}
        items.append({"rank": rank, "label": label, "cells": cells})
    return {"category": category, "size": size, "tlds": tlds, "items": items,
            "lists": wordlists.LABELS, "sizes": TOP_SIZES}


@app.get("/api/explore/map")
async def explore_map(request: Request):
    return await _off(request.app.state.explorer.map)


class SeoNames(BaseModel):
    names: list[str]


@app.post("/api/explore/seo")
async def explore_seo(request: Request, payload: SeoNames):
    analyzer = request.app.state.seo
    names = [n.strip().lower() for n in payload.names if n.strip()][:seo.MAX_NAMES]
    if not names:
        raise HTTPException(422, "Aucun nom à analyser.")
    task = getattr(request.app.state, "seo_task", None)
    if analyzer.running or (task is not None and not task.done()):
        raise HTTPException(409, "Une analyse est déjà en cours.")
    explorer = request.app.state.explorer
    if len(names) == 1:
        rows = await analyzer.analyze(names)
        explorer.touch(names[0])  # le SEO entre dans le score
        return {"done": True, "rows": {k: dict(v) for k, v in rows.items()}}

    async def analyze() -> None:
        try:
            await analyzer.analyze(names)
        finally:
            for name in names:
                explorer.touch(name)

    request.app.state.seo_task = asyncio.create_task(analyze())
    return {"done": False, "total": len(names)}


@app.get("/api/explore/seo")
async def explore_seo_status(request: Request):
    analyzer = request.app.state.seo
    return {"running": analyzer.running, **analyzer.progress, "key_set": bool(analyzer.key)}


# Lien « Réserver » : {domain} est remplacé par le nom. Registrar : Gandi, modifiable dans Paramètres.
DEFAULT_ORDER_URL = "https://shop.gandi.net/fr/domain/suggest?search={domain}"
# Ancien défaut (OVH) : s'il a été enregistré tel quel, on passe au nouveau.
OLD_ORDER_URL = "https://www.ovhcloud.com/fr/order/webcloud/?#/webCloud/domain/select?selection=~(domain~'{domain}')"


class Weights(BaseModel):
    values: dict[str, float]


@app.put("/api/settings/weights")
async def set_weights(request: Request, payload: Weights):
    raw = {k: float(payload.values.get(k, 1.0)) for k in value.CRITERIA}
    if any(math.isnan(v) or math.isinf(v) for v in raw.values()):
        raise HTTPException(422, "Poids invalide.")
    clean = {k: max(0.0, min(2.0, v)) for k, v in raw.items()}
    request.app.state.store.set("score_weights", json.dumps(clean))
    request.app.state.explorer.refresh_settings()
    return {"values": clean, "labels": value.CRITERIA}


def _order_url(store) -> str:
    saved = store.get("order_url")
    return saved if saved and saved != OLD_ORDER_URL else DEFAULT_ORDER_URL


class OrderUrl(BaseModel):
    url: str


@app.put("/api/settings/order-url")
async def set_order_url(request: Request, payload: OrderUrl):
    url = payload.url.strip()
    if url and (not url.startswith("https://") or "{domain}" not in url):
        raise HTTPException(422, "Adresse https:// contenant {domain} attendue.")
    request.app.state.store.set("order_url", url)
    return {"order_url": url or DEFAULT_ORDER_URL}


class Views(BaseModel):
    views: list[dict]


@app.get("/api/ai/pending")
async def ai_pending(request: Request):
    """Liste à noter, avec la consigne : la tâche planifiée la récupère chaque nuit."""
    explorer = request.app.state.explorer
    if request.query_params.get("requested") == "1":
        # Tâche horaire : seulement les analyses demandées depuis une fiche (souvent aucune).
        names = await _off(ai.requested, explorer)
    else:
        names = ai.pending(explorer, _limit(request, ai.MAX_PENDING))
    return {"version": ai.VERSION, "guide": ai.GUIDE, "format": ai.FORMAT, "names": names}


class AiResults(BaseModel):
    results: list[dict]


@app.post("/api/ai/results")
async def ai_results(request: Request, payload: AiResults):
    if len(payload.results) > ai.MAX_RESULTS:
        raise HTTPException(422, f"{ai.MAX_RESULTS} avis au plus par envoi.")
    explorer = request.app.state.explorer
    conn = request.app.state.conn
    known = ai.known_name(explorer)
    asked = {r["name"] for r in ai.requests(conn)["pending"]}
    saved = ai.save(conn, payload.results, lambda name: name in asked or known(name))
    explorer.ai_changed()  # compteurs filtrés sur l'avis
    for name in ai.finish(conn, saved.pop("names")):
        await _notify_ai_done(request, name)
    return saved


async def _notify_ai_done(request: Request, name: str) -> None:
    """Analyse demandée prête : message Telegram avec le lien direct vers la fiche."""
    opinion = ai.load(request.app.state.conn, [name]).get(name) or {}
    base = _public_url(request.app.state)
    link = f"{base}/?{urlencode({'fiche': name, 'ai': '1'})}" if base else ""
    text = "\n".join(x for x in (f"Analyse IA prête : {name}", opinion.get("comment", ""), link) if x)
    try:
        await request.app.state.telegram.send(text)
    except Exception as exc:  # noqa: BLE001  la notification dans l'appli reste
        log.warning("Analyse IA : message Telegram non envoyé (%s)", exc)


class AiAsk(BaseModel):
    name: str


@app.post("/api/ai/request")
async def ai_request(request: Request, payload: AiAsk):
    """Demande d'analyse IA d'un nom, traitée par la tâche horaire."""
    try:
        name = clean_domain(payload.name)
    except InvalidDomain as exc:
        raise HTTPException(422, str(exc)) from None
    if not request.app.state.settings.app_url:
        _remember_public_url(request)
    return ai.request(request.app.state.conn, name)


@app.get("/api/ai/requests")
async def ai_requests(request: Request):
    return ai.requests(request.app.state.conn)


@app.post("/api/ai/requests/seen")
async def ai_request_seen(request: Request, payload: AiAsk):
    ai.seen(request.app.state.conn, payload.name.strip().lower())
    return ai.requests(request.app.state.conn)


@app.get("/api/ai/status")
async def ai_status(request: Request):
    return ai.stats(request.app.state.explorer)


@app.get("/api/ai/rated")
async def ai_rated(request: Request):
    """Avis IA enregistrés, meilleure revente rapide d'abord ; verdict et extension en option."""
    conn = request.app.state.conn
    limit = _limit(request, 5000)
    where, args = ["1"], []
    verdict = request.query_params.get("verdict", "")
    if verdict in explore.AI_VERDICTS:
        where.append("comment LIKE ?")
        args.append(explore.AI_VERDICTS[verdict] + ".%")
    tld = request.query_params.get("tld", "")
    if tld:
        where.append("name LIKE ?")
        args.append(f"%.{tld}")
    rows = conn.execute(f"SELECT * FROM domain_ai WHERE {' AND '.join(where)} ORDER BY quick_high DESC, resale DESC, name "
                        "LIMIT ?", (*args, limit)).fetchall()
    return [{"name": r["name"], **ai.public(r)} for r in rows]


@app.get("/api/diag")
async def diag(request: Request):
    """État de l'appli en une lecture : pour suivre les nuits et analyser, sans aucun secret."""
    state = request.app.state
    memory = None
    try:
        for line in Path("/proc/self/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                memory = int(line.split()[1]) // 1024
    except OSError:
        pass
    conn = state.conn
    watcher = state.drops
    explorer = state.explorer
    drops_by_state = dict(conn.execute("SELECT COALESCE(state, 'à vérifier'), COUNT(*) FROM drops GROUP BY 1").fetchall())
    return {
        "version": VERSION,
        "started_at": iso(STARTED_AT),
        "now": iso(utcnow()),
        "memory_mb": memory,
        "zones": {t: watcher.status(t) for t in watcher.enabled()} if watcher.configured else {},
        "zone_last_run": watcher.last_run(),
        "czds_error": state.store.get("czds_error") or None,
        "fr": {"enabled": state.fr.enabled, **state.fr.status()},
        "fr_dns": state.fr_dns.status(),
        "cc_dns": {tld: c.status() for tld, c in state.cc_dns.items()},
        "ai": ai.stats(explorer),
        "reserved": state.reserved.status(),
        "lists": {k: len(v) for k, v in wordlists.lists().items()},
        "domains": dict(conn.execute("SELECT COALESCE(state, 'inconnu'), COUNT(*) FROM domains GROUP BY 1").fetchall()),
        "drops": drops_by_state,
        "map": explorer.map(),
        "jobs": [{"id": j.id, "name": j.name, "next": iso(j.next_run_time) if j.next_run_time else None}
                 for j in state.scheduler.get_jobs()],
        "recent_errors": list(RECENT.records),
    }


@app.post("/api/settings/ai-token")
async def ai_token(request: Request):
    """Nouveau jeton pour la tâche de notation ; l'ancien cesse de marcher. Montré une seule fois."""
    return {"token": ai.new_token(request.app.state.store)}


@app.get("/api/explore/views")
async def get_views(request: Request):
    return json.loads(request.app.state.store.get("explore_views") or "[]")


@app.put("/api/explore/views")
async def set_views(request: Request, payload: Views):
    if len(payload.views) > 30:
        raise HTTPException(422, "30 vues au plus.")
    keep = []
    for view in payload.views:
        name = str(view.get("name", "")).strip()[:60]
        filters = view.get("filters")
        if not name or not isinstance(filters, dict) or len(filters) > 20:
            continue
        keep = [v for v in keep if v["name"] != name]  # même nom : la dernière l'emporte
        # La liste des extensions peut être longue (« Toutes ») : pas de coupure pour elle.
        clean = {str(k)[:20]: ("" if v is None else str(v))[:400 if k == "tld" else 40]
                 for k, v in filters.items() if v is None or isinstance(v, (str, int, float, bool))}
        keep.append({"name": name, "filters": clean})
    request.app.state.store.set("explore_views", json.dumps(keep))
    return keep


class ReservedSettings(BaseModel):
    mode: str


@app.put("/api/settings/reserved")
async def set_reserved_mode(request: Request, payload: ReservedSettings):
    if payload.mode not in ("hide", "show"):
        raise HTTPException(422, "Mode inconnu.")
    request.app.state.store.set("reserved_mode", payload.mode)
    request.app.state.explorer.refresh_settings()
    return request.app.state.reserved.status()


@app.post("/api/reserved/refresh", status_code=202)
async def refresh_reserved_now(request: Request):
    lists = request.app.state.reserved
    if not lists.running:
        async def refresh() -> None:
            await lists.refresh()
            request.app.state.explorer.invalidate()
            request.app.state.explorer.warm()

        request.app.state.reserved_task = asyncio.create_task(refresh())
    return {"started": not lists.running}


class NameFlag(BaseModel):
    name: str
    reserved: bool | None


@app.post("/api/reserved/flag")
async def flag_reserved(request: Request, payload: NameFlag):
    try:
        name = clean_domain(payload.name)
    except InvalidDomain as exc:
        raise HTTPException(422, str(exc)) from None
    request.app.state.reserved.flag(name, payload.reserved)
    request.app.state.explorer.touch(name)
    return {"name": name, "reserved": name in request.app.state.reserved.names()}


class SeoKey(BaseModel):
    key: str


@app.put("/api/settings/seo")
async def set_seo_key(request: Request, payload: SeoKey):
    request.app.state.store.set("opr_key", payload.key.strip())
    return {"key_set": bool(payload.key.strip())}


@app.get("/api/explore/export")
async def explore_export(request: Request):
    params = _explore_params(request)
    params["cursor"] = -1
    chosen = params.pop("tlds")
    params.pop("tld")
    explorer = request.app.state.explorer
    items = await _off(_export_items, explorer, params, chosen)
    lines = ["nom;etat;categories;cree_le;supprime_le;echeance;verifie_le"]
    for item in items[:explore.EXPORT_LIMIT]:
        lines.append(";".join([
            item["name"], item["status_label"], ",".join(item["categories"]),
            item["created_on"] or "", item["removed_on"] or "", item["expires_at"] or "", item["checked_at"] or "",
        ]))
    filename = f"explorer-{'-'.join(chosen)}.csv"
    # BOM : Excel lit alors les accents correctement.
    return Response("\ufeff" + "\n".join(lines) + "\n", media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


def _export_items(explorer, params, chosen):
    items = []
    for tld in chosen:
        found = explorer.search(tld, **params, limit=explore.EXPORT_LIMIT)["items"]
        if params["min_score"]:
            # Même règle que la liste affichée : le score minimum s'applique aussi à l'export.
            scores = explorer.value_of([i["name"] for i in found])
            found = [i for i in found if scores.get(i["name"], {}).get("score", 0) >= params["min_score"]]
        items += found
    return items


@app.get("/api/explore/name")
async def explore_name(request: Request):
    return await _off(request.app.state.explorer.card, request.query_params.get("label", ""))


@app.get("/api/explore/fiche")
async def explore_fiche(request: Request):
    try:
        name = clean_domain(request.query_params.get("name", ""))
    except InvalidDomain as exc:
        raise HTTPException(422, str(exc)) from None
    explorer = request.app.state.explorer
    found = await _off(explorer.fiche, name)
    found["ai_eta"] = await _ai_eta(explorer, found)
    found["ai_request"] = ai.request_state(request.app.state.conn, name)
    if found.get("tld") in ccdns.TLDS:
        found["cc_dns"] = ccdns.load_names(request.app.state.conn, [name]).get(name)
    if found.get("tld") == "fr":
        found["dns_eta"] = await _dns_eta(request.app.state.fr_dns, found)
    return found


async def _dns_eta(fr_dns, fiche: dict) -> dict:
    """Prochaine interrogation DNS d'un .fr : la nuit prochaine, ou pourquoi pas."""
    if not fr_dns.status().get("enabled"):
        return {"queued": False, "why": "désactivée"}
    position = await asyncio.get_running_loop().run_in_executor(None, fr_dns.position, fiche["label"])
    if position is not None:
        return {"queued": True, "position": position + 1, "nightly": frdns.NIGHTLY}
    item = fiche.get("item") or {}
    why = "déjà enregistré d'après l'Afnic" if item.get("status") == explore.REGISTERED and not fiche.get("dns") else \
        "vu récemment, revu plus tard" if fiche.get("dns") else "hors des noms suivis ou peu prioritaire"
    return {"queued": False, "why": why}


async def _ai_eta(explorer, fiche: dict) -> dict | None:
    """Nuit prévue pour l'avis IA d'un nom pas encore noté, d'après sa place dans la file."""
    if fiche.get("tld") not in ai.AI_TLDS or fiche.get("ai"):
        return None
    position = await asyncio.get_running_loop().run_in_executor(None, ai.queue_position, explorer, fiche["name"])
    if position is None:
        return {"position": None, "horizon": ai.HORIZON_NIGHTS}
    zone = digest.local_zone()
    now = datetime.now(zone)
    first = now.replace(hour=5, minute=0, second=0, microsecond=0)
    if now >= first:
        first += timedelta(days=1)
    night = first + timedelta(days=position // ai.PER_NIGHT)
    return {"position": position + 1, "night": night.date().isoformat(), "per_night": ai.PER_NIGHT}


@app.post("/api/explore/check")
async def explore_check(request: Request, payload: NameCheck):
    return await _verify(request, payload.name)


@app.post("/api/drops/scan", status_code=202)
async def scan_drops(request: Request):
    watcher = request.app.state.drops
    if not watcher.configured:
        raise HTTPException(409, "Compte CZDS non configuré.")
    if watcher.running:
        return {"started": False}
    # Référence gardée : une tâche sans référence peut être collectée en cours de route.
    async def run() -> None:
        await watcher.run()
        request.app.state.explorer.warm()

    request.app.state.scan_task = asyncio.create_task(run())
    return {"started": True}


class TelegramToken(BaseModel):
    token: str


@app.put("/api/settings/telegram")
async def set_telegram_token(request: Request, payload: TelegramToken):
    state = request.app.state
    token = "".join(payload.token.split())
    previous = state.telegram.token
    state.store.set("telegram_token", token)
    try:
        name = await state.telegram.check_token()
    except (TelegramError, httpx.HTTPError) as exc:
        if previous:
            state.store.set("telegram_token", previous)
        else:
            state.store.delete("telegram_token")
        raise HTTPException(422, f"Jeton refusé par Telegram : {exc}") from None
    if token != previous:
        # Nouveau bot : l'ancien chat associé ne le connaît pas.
        state.store.set("telegram_chat_id", "")
    return {"bot_name": name}


@app.delete("/api/settings/telegram", status_code=204)
async def delete_telegram(request: Request):
    store = request.app.state.store
    store.set("telegram_token", "")
    store.set("telegram_chat_id", "")
    request.app.state.telegram.bot_name = None
    return Response(status_code=204)


@app.post("/api/settings/telegram/pair")
async def pair_telegram(request: Request):
    telegram = request.app.state.telegram
    if not telegram.token:
        raise HTTPException(409, "Enregistrer d'abord le jeton du bot.")
    if not telegram.bot_name:
        try:
            await telegram.check_token()
        except (TelegramError, httpx.HTTPError) as exc:
            raise HTTPException(502, f"Telegram injoignable : {exc}") from None
    return {"code": telegram.start_pairing(), "bot_name": telegram.bot_name}


class DigestSettings(BaseModel):
    enabled: bool
    day: int
    hour: int


@app.put("/api/settings/digest")
async def set_digest(request: Request, payload: DigestSettings):
    if not 0 <= payload.day <= 6 or not 0 <= payload.hour <= 23:
        raise HTTPException(422, "Jour ou heure invalide.")
    store = request.app.state.store
    store.set("digest_enabled", "1" if payload.enabled else "0")
    store.set("digest_day", str(payload.day))
    store.set("digest_hour", str(payload.hour))
    return await get_settings(request)


class MorningSettings(BaseModel):
    enabled: bool
    hour: int
    min_score: int


@app.put("/api/settings/morning")
async def set_morning(request: Request, payload: MorningSettings):
    if not 0 <= payload.hour <= 23 or not 0 <= payload.min_score <= 100:
        raise HTTPException(422, "Heure ou score invalide.")
    store = request.app.state.store
    store.set("morning_enabled", "1" if payload.enabled else "0")
    store.set("morning_hour", str(payload.hour))
    store.set("morning_min", str(payload.min_score))
    return await get_settings(request)


@app.post("/api/morning/send")
async def send_morning_now(request: Request):
    text = await _morning_text(request.app.state)
    if text is None:
        return {"sent": False, "detail": "Rien à signaler sur les dernières 24 h."}
    error = await request.app.state.telegram.send(text)
    if error:
        raise HTTPException(502, error)
    return {"sent": True}


@app.post("/api/digest/send")
async def send_digest_now(request: Request):
    state = request.app.state
    error = await state.telegram.send(digest.build(state.checker, utcnow(), _public_url(state)))
    if error:
        raise HTTPException(502, error)
    return {"sent": True}


@app.post("/api/settings/calendar")
async def regenerate_calendar(request: Request):
    request.app.state.store.set("calendar_token", secrets.token_urlsafe(24))
    return {"calendar_url": _calendar_url(request)}


@app.get("/calendar/{token}.ics")
async def calendar(request: Request, token: str):
    expected = request.app.state.store.get("calendar_token")
    # Lien secret, sans session : les applis de calendrier ne savent pas se connecter.
    if not expected or not hmac.compare_digest(token.encode(), expected.encode()):
        raise HTTPException(404)
    body = calendar_ics.build(request.app.state.checker, utcnow(), request.app.state.settings.app_url)
    return Response(body, media_type="text/calendar; charset=utf-8")


class Appearance(BaseModel):
    app_name: str
    theme: str
    logo_height: int | None = None


@app.put("/api/settings/appearance")
async def set_appearance(request: Request, payload: Appearance):
    if payload.theme not in THEMES:
        raise HTTPException(422, "Thème inconnu.")
    name = " ".join(payload.app_name.split())[:40]
    store = request.app.state.store
    store.set("app_name", name or DEFAULT_NAME)
    store.set("theme", payload.theme)
    if payload.logo_height is not None:
        low, high, _ = LOGO_HEIGHT
        store.set("logo_height", str(min(high, max(low, payload.logo_height))))
    return await get_settings(request)


@app.post("/api/settings/logo")
async def upload_logo(request: Request, file: UploadFile):
    ext = LOGO_TYPES.get(file.content_type or "")
    if not ext:
        raise HTTPException(422, "Format accepté : PNG, JPEG, WebP ou SVG.")
    data = await file.read(LOGO_MAX_BYTES + 1)
    if len(data) > LOGO_MAX_BYTES:
        raise HTTPException(422, "Logo trop lourd : 300 Ko maximum.")
    old = _logo_path()
    if old:
        old.unlink()
    (request.app.state.settings.data_dir / f"logo.{ext}").write_bytes(data)
    return await get_settings(request)


@app.delete("/api/settings/logo")
async def delete_logo(request: Request):
    old = _logo_path()
    if old:
        old.unlink()
    return await get_settings(request)

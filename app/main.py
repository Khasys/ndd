import asyncio
import hmac
import html
import secrets
import json
import logging
import re
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from . import bot, calendar_ics, db, digest, drops, explore, frdata, rdapinfo, reserved, seo, signals, states, updates, value, wordlists
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

STATIC = Path(__file__).parent / "static"
PWA_PATHS = {"/manifest.webmanifest", "/sw.js", "/offline"}
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
            if await telegram.send(digest.build(checker, utcnow(), settings.app_url)) is None:
                store.set("digest_last_sent", now_local.date().isoformat())

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

    async def check_drops() -> None:
        await watcher.check_due(limit=20)

    fr = frdata.FrWatcher(conn, store, settings.data_dir, rdap, watcher)

    async def import_fr() -> None:
        if fr.due(datetime.now(digest.local_zone())):
            await fr.import_file()

    async def tick_fr() -> None:
        await fr.tick()

    async def refresh_reserved() -> None:
        if app.state.reserved.due():
            await app.state.reserved.refresh()
            app.state.explorer.invalidate()

    now = datetime.now(timezone.utc)
    scheduler.add_job(refresh_latest, "interval", hours=24, next_run_time=now, max_instances=1, id="latest")
    scheduler.add_job(send_digest, "interval", minutes=15, max_instances=1)
    scheduler.add_job(refresh_signals, "interval", hours=1, next_run_time=now + timedelta(minutes=2), max_instances=1)
    scheduler.add_job(scan_zones, "interval", minutes=15, next_run_time=now + timedelta(minutes=1), max_instances=1)
    scheduler.add_job(check_drops, "interval", minutes=5, next_run_time=now + timedelta(minutes=3), max_instances=1)
    scheduler.add_job(import_fr, "interval", hours=6, next_run_time=now + timedelta(minutes=4), max_instances=1)
    scheduler.add_job(refresh_reserved, "interval", hours=6, next_run_time=now + timedelta(minutes=2), max_instances=1)
    # File .fr : chaque minute, autant de requêtes que le plafond réglé le permet.
    scheduler.add_job(tick_fr, "interval", minutes=1, next_run_time=now + timedelta(minutes=6), max_instances=1)
    scheduler.start()
    telegram.start()

    app.state.refresh_latest = refresh_latest
    app.state.scheduler = scheduler
    app.state.drops = watcher
    app.state.fr = fr
    app.state.reserved = reserved.Reserved(conn, store)
    app.state.explorer = explore.Explorer(conn, watcher, fr, app.state.reserved)
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
        return await call_next(request)
    if path.startswith("/api/"):
        return JSONResponse({"detail": "Session expirée."}, status_code=401)
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
async def login_page():
    return _page("login.html", ERROR="")


@app.post("/login")
async def login(request: Request, username: str = Form(""), password: str = Form("")):
    state = request.app.state
    client = request.client.host if request.client else "unknown"
    locked = state.guard.locked_for(client)
    if locked:
        minutes = (locked + 59) // 60
        return _page("login.html", ERROR=f"Trop d'échecs. Nouvel essai possible dans {minutes} min.")
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
            return _page("login.html", ERROR=f"Trop d'échecs. Nouvel essai possible dans {minutes} min.")
        return _page("login.html", ERROR="Identifiant ou mot de passe incorrect.")
    state.guard.success(client)
    response = RedirectResponse("/", status_code=303)
    _set_session(response, state)
    return response


def _set_session(response: Response, state) -> None:
    response.set_cookie(
        COOKIE_NAME,
        state.sessions.create(state.settings.username),
        max_age=state.settings.session_max_age,
        httponly=True,
        samesite="strict",
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
    return [_serialize(row, checker) for row in rows]


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
    if settings.app_url:
        lines.append(settings.app_url)
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
        "calendar_url": _calendar_url(request),
        "czds": _czds_settings(state.drops),
        "fr": {"enabled": state.fr.enabled, "url": state.store.get("fr_url"), "rate": state.fr.rate},
        "drops": {"keep_days": state.drops.keep_days},
        "seo": {"key_set": bool(state.seo.key)},
        "reserved": state.reserved.status(),
        "weights": {"values": value.weights_from(store.get("score_weights")), "labels": value.CRITERIA},
        "order_url": store.get("order_url") or DEFAULT_ORDER_URL,
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
    previous = (store.get("czds_username"), store.get("czds_password"))
    store.set("czds_username", username)
    if payload.password:
        store.set("czds_password", payload.password)
    if not store.get("czds_password"):
        raise HTTPException(422, "Mot de passe ICANN manquant.")
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


@app.put("/api/settings/fr")
async def set_fr(request: Request, payload: FrSettings):
    store = request.app.state.store
    url = payload.url.strip()
    if url and not url.startswith("https://"):
        raise HTTPException(422, "Adresse du fichier : https:// attendu.")
    store.set("fr_enabled", "1" if payload.enabled else "0")
    store.set("fr_url", url)
    if payload.rate is not None:
        if not 1 <= payload.rate <= frdata.MAX_RATE:
            raise HTTPException(422, f"Requêtes par minute : entre 1 et {frdata.MAX_RATE}.")
        store.set("fr_rate", str(payload.rate))
    return {"enabled": payload.enabled, "url": url, "rate": request.app.state.fr.rate}


class DropsSettings(BaseModel):
    keep_days: int


@app.put("/api/settings/drops")
async def set_drops(request: Request, payload: DropsSettings):
    if not 1 <= payload.keep_days <= 365:
        raise HTTPException(422, "Durée d'affichage : entre 1 et 365 jours.")
    request.app.state.store.set("drops_keep_days", str(payload.keep_days))
    return {"keep_days": payload.keep_days}


@app.post("/api/drops/fr/import", status_code=202)
async def import_fr_now(request: Request):
    fr = request.app.state.fr
    if not fr.enabled:
        raise HTTPException(409, "Suivi du .fr désactivé.")
    if fr.running:
        return {"started": False}
    request.app.state.fr_task = asyncio.create_task(fr.import_file())
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
    rows = [r for r in rows if not (hide_reserved and r["name"] in reserved_names)]
    suspended = dict(conn.execute("SELECT tld, COUNT(*) FROM drops WHERE state = ? GROUP BY tld", (drops.SUSPENDED,)).fetchall())
    watched = {r[0] for r in conn.execute("SELECT name FROM domains")}
    details = rdapinfo.load(conn, [r["name"] for r in rows])
    return {
        "configured": watcher.configured or request.app.state.fr.enabled,
        "czds": watcher.configured,
        "running": watcher.running,
        "last_run": watcher.last_run(),
        "error": watcher.store.get("czds_error") or None,
        "zones": [{"tld": t, **watcher.status(t)} for t in watcher.enabled()],
        "history": watcher.history(),
        "fr": {"enabled": request.app.state.fr.enabled, "running": request.app.state.fr.running, **request.app.state.fr.status()},
        "workers": watcher.workers,
        "pending_checks": pending,
        "suspended": suspended,
        "keep_days": watcher.keep_days,
        "kinds": drops.KINDS,
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
                "rdap": details.get(r["name"]),
            }
            for r in rows
        ],
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
    tld = p.get("tld") or (tlds[0] if tlds else "")
    if tld not in tlds:
        raise HTTPException(404, "Extension sans données : importer le .fr ou lire les zones d'abord.")
    try:
        length = int(p["len"]) if p.get("len") else None
        cursor = int(p.get("cursor", "-1"))
        max_rank = int(p["rank"]) if p.get("rank") else None
        min_score = int(p["min"]) if p.get("min") else None
    except ValueError:
        raise HTTPException(422, "Paramètre numérique invalide.") from None
    if p.get("nat") and p["nat"] not in explore.NATURES:
        raise HTTPException(422, "Nature inconnue.")
    taken = p.get("taken", "")
    if taken and taken != "any" and taken not in tlds:
        raise HTTPException(422, "Extension inconnue.")
    mode = p.get("mode", "prefix")
    if mode not in ("prefix", "contains", "exact"):
        raise HTTPException(422, "Mode de recherche inconnu.")
    if p.get("status") and p["status"] not in explore.STATUS_FILTERS:
        raise HTTPException(422, "État inconnu.")
    return {"tld": tld, "q": p.get("q", ""), "mode": mode, "status": p.get("status", ""),
            "category": p.get("cat", ""), "length": length, "cursor": cursor, "nature": p.get("nat", ""),
            "max_rank": max_rank, "min_score": min_score, "taken": taken, "junk": p.get("junk") == "1"}


def _facets(explorer, params: dict) -> dict:
    return explorer.facets(params["tld"], **{k: v for k, v in params.items() if k not in ("tld", "cursor", "q")})


def _explore_meta(explorer, tld: str) -> dict:
    return {
        "tld": tld,
        "tlds": explorer.tlds(),
        "kinds": explore.KINDS,
        "natures": explore.NATURES,
        "statuses": explore.STATUS_NAMES,
        "absent_label": explore.ABSENT_FR if tld == "fr" else explore.ABSENT_ZONE,
    }


@app.get("/api/explore")
async def explore_search(request: Request):
    """Liste par ordre des noms (et export) : la vue « par nom »."""
    explorer = request.app.state.explorer
    params = _explore_params(request)
    limit = min(int(request.query_params.get("limit", "50") or 50), explore.MAX_LIMIT)
    result = explorer.search(**params, limit=limit)
    return {**result, **_explore_meta(explorer, params["tld"]),
            "counts": _facets(explorer, params)}


@app.get("/api/explore/rank")
async def explore_rank(request: Request):
    """Les meilleures affaires d'abord, avec les compteurs de la sélection."""
    explorer = request.app.state.explorer
    params = _explore_params(request)
    params.pop("cursor")
    params["status"] = params["status"] or "available"
    limit = min(int(request.query_params.get("limit", "50") or 50), explore.MAX_LIMIT * 5)
    sort = request.query_params.get("sort", "score")
    if sort not in ("score", "freq", "length"):
        raise HTTPException(422, "Tri inconnu.")
    result = explorer.rank(**params, limit=limit, sort=sort)
    return {**result, **_explore_meta(explorer, params["tld"]),
            "counts": _facets(explorer, params)}


@app.get("/api/explore/map")
async def explore_map(request: Request):
    return request.app.state.explorer.map()


class SeoNames(BaseModel):
    names: list[str]


@app.post("/api/explore/seo")
async def explore_seo(request: Request, payload: SeoNames):
    analyzer = request.app.state.seo
    names = [n.strip().lower() for n in payload.names if n.strip()][:seo.MAX_NAMES]
    if not names:
        raise HTTPException(422, "Aucun nom à analyser.")
    if analyzer.running:
        raise HTTPException(409, "Une analyse est déjà en cours.")
    if len(names) == 1:
        rows = await analyzer.analyze(names)
        return {"done": True, "rows": {k: dict(v) for k, v in rows.items()}}
    request.app.state.seo_task = asyncio.create_task(analyzer.analyze(names))
    return {"done": False, "total": len(names)}


@app.get("/api/explore/seo")
async def explore_seo_status(request: Request):
    analyzer = request.app.state.seo
    return {"running": analyzer.running, **analyzer.progress, "key_set": bool(analyzer.key)}


# Lien « Réserver » : {domain} est remplacé par le nom. Format à vérifier chez OVH, modifiable.
DEFAULT_ORDER_URL = "https://www.ovhcloud.com/fr/order/webcloud/?#/webCloud/domain/select?selection=~(domain~'{domain}')"


class Weights(BaseModel):
    values: dict[str, float]


@app.put("/api/settings/weights")
async def set_weights(request: Request, payload: Weights):
    clean = {k: max(0.0, min(2.0, float(payload.values.get(k, 1.0)))) for k in value.CRITERIA}
    request.app.state.store.set("score_weights", json.dumps(clean))
    return {"values": clean, "labels": value.CRITERIA}


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


@app.get("/api/explore/views")
async def get_views(request: Request):
    return json.loads(request.app.state.store.get("explore_views") or "[]")


@app.put("/api/explore/views")
async def set_views(request: Request, payload: Views):
    keep = []
    for view in payload.views[:30]:
        name = str(view.get("name", "")).strip()[:60]
        if name and isinstance(view.get("filters"), dict):
            keep.append({"name": name, "filters": {k: str(v)[:40] for k, v in view["filters"].items()}})
    request.app.state.store.set("explore_views", json.dumps(keep))
    return keep


class ReservedSettings(BaseModel):
    mode: str


@app.put("/api/settings/reserved")
async def set_reserved_mode(request: Request, payload: ReservedSettings):
    if payload.mode not in ("hide", "show"):
        raise HTTPException(422, "Mode inconnu.")
    request.app.state.store.set("reserved_mode", payload.mode)
    return request.app.state.reserved.status()


@app.post("/api/reserved/refresh", status_code=202)
async def refresh_reserved_now(request: Request):
    lists = request.app.state.reserved
    if not lists.running:
        async def refresh() -> None:
            await lists.refresh()
            request.app.state.explorer.invalidate()

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
    request.app.state.explorer.invalidate()
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
    result = request.app.state.explorer.search(**params, limit=explore.EXPORT_LIMIT)
    lines = ["nom;etat;categories;cree_le;supprime_le;echeance;verifie_le"]
    for item in result["items"]:
        lines.append(";".join([
            item["name"], item["status_label"], ",".join(item["categories"]),
            item["created_on"] or "", item["removed_on"] or "", item["expires_at"] or "", item["checked_at"] or "",
        ]))
    filename = f"explorer-{params['tld']}.csv"
    return Response("\n".join(lines) + "\n", media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@app.get("/api/explore/name")
async def explore_name(request: Request):
    return request.app.state.explorer.card(request.query_params.get("label", ""))


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
    request.app.state.scan_task = asyncio.create_task(watcher.run())
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


@app.post("/api/digest/send")
async def send_digest_now(request: Request):
    state = request.app.state
    error = await state.telegram.send(digest.build(state.checker, utcnow(), state.settings.app_url))
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

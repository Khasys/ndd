import html
import json
import logging
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from . import alerts, db, states
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
from .checker import Checker, iso, parse_iso, utcnow
from .config import VERSION, load_settings
from .rdap import InvalidDomain, RdapClient, clean_domain

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("domain-watch")

STATIC = Path(__file__).parent / "static"
PUBLIC_PATHS = {"/login", "/health", "/favicon.svg"}
SETUP_PATHS = {"/setup", "/health", "/favicon.svg"}


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
    )
    checker = Checker(conn, settings, rdap, http)

    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_job(checker.run_due, "interval", seconds=30, max_instances=1, coalesce=True)
    scheduler.start()

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
    try:
        yield
    finally:
        scheduler.shutdown(wait=False)
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
    if path in PUBLIC_PATHS or path.startswith("/static/"):
        return await call_next(request)
    if state.sessions.valid(request.cookies.get(COOKIE_NAME), state.settings.username):
        return await call_next(request)
    if path.startswith("/api/"):
        return JSONResponse({"detail": "Session expirée."}, status_code=401)
    return RedirectResponse("/login", status_code=303)


def _page(name: str, **values: str) -> HTMLResponse:
    content = (STATIC / name).read_text()
    for key, value in {"VERSION": VERSION, **values}.items():
        content = content.replace("{{" + key + "}}", html.escape(value))
    return HTMLResponse(content, headers={"Cache-Control": "no-store"})


@app.get("/health")
async def health(request: Request):
    request.app.state.conn.execute("SELECT 1").fetchone()
    return {"status": "ok", "version": VERSION}


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
        if state.guard.locked_for(client):
            minutes = (state.settings.login_lock_seconds + 59) // 60
            return _page("login.html", ERROR=f"Trop d'échecs. Nouvel essai possible dans {minutes} min.")
        return _page("login.html", ERROR="Identifiant ou mot de passe incorrect.")
    state.guard.success(client)
    response = RedirectResponse("/", status_code=303)
    response.set_cookie(
        COOKIE_NAME,
        state.sessions.create(state.settings.username),
        max_age=state.settings.session_max_age,
        httponly=True,
        samesite="strict",
        secure=state.settings.cookie_secure,
    )
    return response


@app.post("/logout")
async def logout():
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(COOKIE_NAME)
    return response


@app.get("/", response_class=HTMLResponse)
async def index():
    return _page("index.html")


def _serialize(row: sqlite3.Row, checker: Checker) -> dict:
    now = utcnow()
    release = checker.release_estimate(row, now)
    effective = states.effective_expiry(row["state"], parse_iso(row["expires_at"]), now)
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
        "release_at": iso(release),
        "release_is_upper_bound": row["state"] in (states.EXPIRING, states.EXPIRED) and release is not None,
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
        "telegram_configured": bool(settings.telegram_token and settings.telegram_chat_id),
        "bulk_running": request.app.state.checker.bulk_running,
    }


@app.get("/api/domains")
async def list_domains(request: Request):
    checker = request.app.state.checker
    rows = request.app.state.conn.execute("SELECT * FROM domains ORDER BY name").fetchall()
    return [_serialize(row, checker) for row in rows]


class NewDomain(BaseModel):
    name: str


@app.post("/api/domains", status_code=201)
async def add_domain(request: Request, payload: NewDomain):
    conn = request.app.state.conn
    checker = request.app.state.checker
    try:
        name = clean_domain(payload.name)
    except InvalidDomain as exc:
        raise HTTPException(422, str(exc)) from None
    try:
        cur = conn.execute("INSERT INTO domains (name, added_at) VALUES (?, ?)", (name, iso(utcnow())))
    except sqlite3.IntegrityError:
        raise HTTPException(409, f"{name} est déjà surveillé.") from None
    await checker.check(cur.lastrowid)
    row = conn.execute("SELECT * FROM domains WHERE id = ?", (cur.lastrowid,)).fetchone()
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


@app.post("/api/alerts/test")
async def test_alert(request: Request):
    state = request.app.state
    settings = state.settings
    lines = ["domain-watch : alerte test", f"Version {VERSION}"]
    if settings.app_url:
        lines.append(settings.app_url)
    error = await alerts.send_telegram(state.http, settings.telegram_token, settings.telegram_chat_id, "\n".join(lines))
    if error:
        raise HTTPException(502, error)
    return {"sent": True}

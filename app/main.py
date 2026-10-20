import asyncio
import functools
import hashlib
import hmac
import html
import ipaddress
import secrets
import time
import traceback
import json
import math
import logging
import re
import sqlite3
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode

import httpx
from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_MISSED
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI, Form, HTTPException, Request, UploadFile
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from . import ai, bot, mcp, tokens, calendar_ics, ccdns, db, decisions, market, morning, notify, pepites, portfolio, digest, drops, explore, frdata, frdns, gandi, prenoms, reserved, seo, signals, states, updates, value, wordlists
from .auth import (
    COOKIE_NAME,
    LoginGuard,
    Sessions,
    compose_escape,
    hash_password,
    load_or_create_secret,
    password_problem,
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
# Nom tout juste libre : son prix est demandé à Gandi après ce délai (secondes), sauf réponse récente.
FREED_GANDI_DELAY = 60


# Jetons possibles dans un message d'erreur (URL de bot Telegram, en-têtes, paramètres) : masqués,
# le diagnostic est lisible avec le jeton en lecture seule.
_SECRETS = re.compile(r"(bot)\d+:[\w-]+|(Bearer\s+)\S+|((?:token|key|password|secret)=)[^&\s]+|(/mcp/)[^/?\s\"]+", re.I)


def _redact(text: str) -> str:
    return _SECRETS.sub(lambda m: (m.group(1) or m.group(2) or m.group(3) or m.group(4)) + "<masqué>", text)


def _exception_line(exc: BaseException) -> str:
    """Type, message et dernière ligne de l'appli dans la pile : de quoi savoir où ça casse."""
    where = ""
    for frame in traceback.extract_tb(exc.__traceback__):
        if "/app/" in frame.filename:
            where = f" ({Path(frame.filename).name}:{frame.lineno})"
    return f"{exc.__class__.__name__}: {exc}{where}"


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
            message = record.getMessage()
            if record.exc_info and record.exc_info[1] is not None:
                message += " : " + _exception_line(record.exc_info[1])
            self.records.append({"at": iso(datetime.fromtimestamp(record.created, timezone.utc)),
                                 "level": record.levelname, "logger": record.name, "message": _redact(message)[:500]})
            del self.records[:-self.size]
        except Exception:  # noqa: BLE001  un journal ne doit jamais casser l'appli
            pass


RECENT = _Recent()
logging.getLogger().addHandler(RECENT)


class _MaskMcp(logging.Filter):
    """Journal d'accès d'uvicorn : l'adresse du connecteur MCP contient son jeton, masqué avant écriture."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple) and any(isinstance(a, str) and "/mcp/" in a for a in record.args):
            record.args = tuple(_redact(a) if isinstance(a, str) else a for a in record.args)
        return True


logging.getLogger("uvicorn.access").addFilter(_MaskMcp())

STATIC = Path(__file__).parent / "static"
PWA_PATHS = {"/manifest.webmanifest", "/sw.js", "/offline"}
AI_PATHS = {"/api/ai/pending", "/api/ai/results"}
# Lecture seule avec le même jeton : de quoi observer et analyser, jamais modifier ni lire un réglage.
# Explorer : les écrans seulement (liste, classement, carte, fiches, export), pas les vues enregistrées.
READ_PATHS = re.compile(r"^/api/(meta|diag|today|domains|domains/\d+/history|drops|explore(/(rank|top|map|name|fiche|export))?|ai/(status|rated|outcomes))$")
PUBLIC_PATHS = {"/login", "/health", "/favicon.svg", "/logo", *PWA_PATHS}
SETUP_PATHS = {"/setup", "/health", "/favicon.svg", "/logo"}
BCRYPT_HASH = re.compile(r"\$2[aby]\$\d{2}\$[./A-Za-z0-9]{53}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = load_settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    wordlists.configure(settings.data_dir)  # « Mes ajouts » vivent dans le dossier de données
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
    rdap.whois_store = store  # serveurs WHOIS gardés en base, 7 jours
    telegram = Telegram(store, http)

    async def notify_domain(text: str) -> str | None:
        """Changement d'un domaine suivi : alerte immédiate, sauf type décoché (Paramètres, carte Alertes)."""
        return await telegram.send(text) if notify.enabled(store, "domain_change") else None

    async def note_health(text: str) -> None:
        """Passe DNS ou Gandi arrêtée : dite une fois dans la rubrique Santé du quotidien, plus de message à part."""
        morning.note(store, text, utcnow())

    checker = Checker(conn, settings, rdap, http, notify=notify_domain)
    checker.link_base = lambda: settings.app_url or store.get("public_url") or ""
    telegram.handler = bot.build_handler(checker, VERSION)

    # La boucle est parfois occupée quelques secondes (import .fr, listes réservées) : une exécution
    # en retard est rattrapée au lieu d'être sautée (tolérance par défaut d'APScheduler : 1 s).
    scheduler = AsyncIOScheduler(timezone="UTC", job_defaults={"misfire_grace_time": 300, "coalesce": True})
    scheduler.add_job(checker.run_due, "interval", seconds=30, max_instances=1, coalesce=True)
    # Exécutions manquées ou en erreur : les 20 dernières, pour /api/diag et l'alerte de panne (morning.problems).
    events = {"missed": deque(maxlen=20), "errors": deque(maxlen=20)}

    def on_job_event(event) -> None:
        job = scheduler.get_job(event.job_id)
        entry = {"at": iso(utcnow()), "job": job.name.rsplit(".", 1)[-1] if job else event.job_id,
                 "scheduled": iso(event.scheduled_run_time.astimezone(timezone.utc).replace(microsecond=0)) if event.scheduled_run_time else None}
        if event.code == EVENT_JOB_ERROR:
            entry["error"] = _redact(_exception_line(event.exception)) if event.exception else None
            events["errors"].append(entry)
        else:
            events["missed"].append(entry)

    scheduler.add_listener(on_job_event, EVENT_JOB_MISSED | EVENT_JOB_ERROR)
    app.state.scheduler_events = events

    app.state.latest_version = None
    app.state.version_checked_at = None

    async def refresh_latest() -> None:
        if not settings.update_repository:
            return
        found = await updates.latest_version(http, settings.update_repository)
        if found:
            app.state.latest_version = found
            app.state.version_checked_at = iso(utcnow())

    async def send_daily() -> None:
        """Quotidien (notify.daily) : un message à l'heure réglée. Noté fait une fois parti : un redémarrage ou une
        exécution rattrapée ne le renvoie pas. Rien à dire : le jour compte aussi comme fait."""
        now_local = datetime.now(digest.local_zone())
        if not telegram.configured or not notify.daily_due(store, now_local):
            return
        await _off(morning.record_health, store, await _off(morning.problems, app.state, utcnow()), utcnow())
        messages, marks = await _daily(app.state, now_local)
        sent = 0
        for text in messages:
            if await telegram.send(text) is not None:
                break
            sent += 1
        # Premier message refusé : rien n'est parti, nouvel essai au passage suivant. Refus plus loin : le début est
        # parti, le jour est noté pour ne pas le renvoyer en double.
        if sent or not messages:
            notify.commit_daily(app.state.explorer, store, marks, utcnow())

    async def check_health() -> None:
        """Toutes les 10 minutes : les pannes apparues ou réglées sont notées pour la rubrique Santé du quotidien ;
        les bloquantes partent au passage suivant de send_alerts."""
        await _off(morning.record_health, store, await _off(morning.problems, app.state, utcnow()), utcnow())

    async def refresh_signals() -> None:
        await signals.update_due(conn, limit=20)

    alert_lock = asyncio.Lock()

    async def send_alerts() -> None:
        """Immédiat (notify.pepite_tick), chaque minute : les pépites libres du passage, dans un seul message. Le verrou
        et les marques en base empêchent tout envoi en double (redémarrage, rattrapage d'une exécution manquée)."""
        async with alert_lock:
            state, now = app.state, utcnow()
            text, names = await _off(notify.pepite_tick, state.explorer, store, now, _public_url(state), _order_url(store),
                                     telegram.configured, bool(state.gandi.key))
            if text and await telegram.send(text) is None:
                pepites.mark_alerted(conn, names, now)
            text, events = notify.urgent_tick(store, telegram.configured)
            if text and await telegram.send(text) is None:
                morning.mark_urgent_sent(store, events)

    async def update_outcomes() -> None:
        """Nuit : les avis d'il y a 30 jours sur des noms libres, revus à l'état d'aujourd'hui (ai_outcomes)."""
        count = await _off(ai.update_outcomes, conn, app.state.explorer, utcnow())
        log.info("Mesure a posteriori des avis IA : %s noms revus", count)

    # Chargées au démarrage : un fichier de liste absent de l'image doit se voir tout de suite,
    # pas au milieu de la lecture de nuit.
    sizes = {k: len(v) for k, v in wordlists.lists().items()}
    log.info("Listes de noms : %s", ", ".join(f"{k} {n}" for k, n in sizes.items()))
    watcher = drops.DropWatcher(conn, store, settings.data_dir, rdap, checker.delays)

    async def scan_zones() -> None:
        if watcher.due(datetime.now(digest.local_zone())):
            await watcher.run()
            await _off(app.state.explorer.warm)  # nouvelle zone : l'Explorer se prépare avant qu'on l'ouvre

    async def check_drops() -> None:
        await watcher.check_due(limit=20)

    fr = frdata.FrWatcher(conn, store, settings.data_dir, rdap, watcher)
    checker.fr = fr  # les .fr de l'écran Domaines passent par la file Afnic

    async def import_fr() -> None:
        if fr.due(datetime.now(digest.local_zone())):
            await fr.import_file()
            await _off(app.state.explorer.warm)

    async def tick_fr() -> None:
        await fr.tick()

    # Une passe DNS dure des heures : elle tourne à part, la tâche planifiée rend la main tout de suite
    # (sinon chaque déclenchement suivant est « sauté » et remplit le journal).
    passes: set[asyncio.Task] = set()

    def start_pass(checker) -> None:
        if checker.running or not checker.due(datetime.now(digest.local_zone())):
            return
        task = asyncio.create_task(checker.run())
        passes.add(task)

        def done(t: asyncio.Task) -> None:
            passes.discard(t)
            if not t.cancelled() and t.exception() is not None:
                log.error("Passe DNS .%s interrompue", checker.tld, exc_info=t.exception())
            elif not t.cancelled() and checker.tld in app.state.cc_dns and t.result().get("checked"):
                # .co, .ai, .io, .com sans zone : nouvelles données de l'Explorer, préparées avant qu'on l'ouvre.
                app.state.explorer_warm = asyncio.create_task(_off(app.state.explorer.warm))
        task.add_done_callback(done)

    async def check_fr_dns() -> None:
        start_pass(fr_dns)

    def cc_job(tld):
        async def check() -> None:
            start_pass(app.state.cc_dns[tld])
        return check

    async def refresh_gandi_grid() -> None:
        state = app.state
        tlds = sorted({*state.gandi_pass.tlds(), *state.gandi_live.tlds()})
        if tlds and state.gandi.key:
            if await state.gandi.refresh_grid(tlds):
                state.explorer.invalidate()  # prix normal changé : reporté sur les libres de l'extension
        # Prix d'achat de noms repris depuis (zone lue, registre) : effacés, ils ne valent plus.
        forgotten = await _off(state.explorer.forget_taken_prices)
        if forgotten:
            log.info("Prix d'achat effacés : %s noms enregistrés depuis la réponse de Gandi", forgotten)

    async def check_gandi() -> None:
        """Passe Gandi : de 3 h 30 à 4 h 45, après les lectures et les passes DNS, avant la notation IA de 5 h."""
        night = app.state.gandi_pass
        if not night.running and night.due(datetime.now(digest.local_zone())):
            start_gandi()

    def start_gandi(manual: bool = False) -> bool:
        night = app.state.gandi_pass
        if night.running:
            return False
        task = asyncio.create_task(night.run(manual=manual))
        passes.add(task)

        def done(t: asyncio.Task) -> None:
            passes.discard(t)
            if not t.cancelled() and t.exception() is not None:
                log.error("Passe Gandi interrompue", exc_info=t.exception())
        task.add_done_callback(done)
        return True

    async def check_gandi_live() -> None:
        """Passe continue des extensions par Gandi seul (.com sans zone) : relancée si elle s'est arrêtée."""
        if app.state.gandi_live.due():
            start_gandi_live()

    def start_gandi_live() -> bool:
        live = app.state.gandi_live
        if live.running:
            return False
        task = asyncio.create_task(live.run())
        passes.add(task)

        def done(t: asyncio.Task) -> None:
            passes.discard(t)
            if not t.cancelled() and t.exception() is not None:
                log.error("Passe Gandi continue interrompue", exc_info=t.exception())
        task.add_done_callback(done)
        return True

    async def refresh_market() -> None:
        # Rapports DNJournal (toutes les deux semaines) : un passage par jour, rattrapage de l'historique compris.
        if app.state.market.due(utcnow()):
            await app.state.market.refresh()

    async def refresh_prenoms() -> None:
        # Une fois par mois : l'Insee publie une édition par an, la date exacte varie.
        if app.state.prenoms.due():
            await app.state.prenoms.refresh()

    async def refresh_reserved() -> None:
        if app.state.reserved.due():
            await app.state.reserved.refresh()
            app.state.explorer.invalidate()
            await _off(app.state.explorer.warm)

    now = datetime.now(timezone.utc)
    scheduler.add_job(refresh_latest, "interval", hours=24, next_run_time=now, max_instances=1, id="latest")
    # Messages Telegram en file de rejeu (429, panne, réseau) : renvoyés quand leur heure est venue.
    scheduler.add_job(telegram.flush, "interval", minutes=5, next_run_time=now + timedelta(minutes=7), max_instances=1, id="telegram_flush")
    scheduler.add_job(send_daily, "interval", minutes=5, next_run_time=now + timedelta(minutes=5), max_instances=1, id="send_daily")
    scheduler.add_job(check_health, "interval", minutes=10, next_run_time=now + timedelta(minutes=20), max_instances=1)
    scheduler.add_job(refresh_signals, "interval", hours=1, next_run_time=now + timedelta(minutes=2), max_instances=1)
    scheduler.add_job(send_alerts, "interval", minutes=1, next_run_time=now + timedelta(minutes=7), max_instances=1, id="send_alerts")
    scheduler.add_job(update_outcomes, "cron", hour=4, minute=40, max_instances=1, id="ai_outcomes")
    scheduler.add_job(scan_zones, "interval", minutes=15, next_run_time=now + timedelta(minutes=1), max_instances=1)
    scheduler.add_job(check_drops, "interval", minutes=5, next_run_time=now + timedelta(minutes=3), max_instances=1)
    scheduler.add_job(import_fr, "interval", hours=6, next_run_time=now + timedelta(minutes=4), max_instances=1)
    scheduler.add_job(refresh_reserved, "interval", hours=6, next_run_time=now + timedelta(minutes=2), max_instances=1)
    scheduler.add_job(refresh_prenoms, "interval", hours=6, next_run_time=now + timedelta(minutes=11), max_instances=1,
                      id="prenoms")
    scheduler.add_job(refresh_market, "interval", hours=6, next_run_time=now + timedelta(minutes=13), max_instances=1,
                      id="market")
    # File .fr : chaque minute, autant de requêtes que le plafond réglé le permet.
    scheduler.add_job(tick_fr, "interval", minutes=1, next_run_time=now + timedelta(minutes=6), max_instances=1)
    # DNS .fr : une passe par jour, lancée à minuit, étalée sur la journée.
    scheduler.add_job(check_fr_dns, "interval", minutes=10, next_run_time=now + timedelta(minutes=8), max_instances=1)
    # DNS .co, .ai, .io (et .com tant qu'il est sans zone) : même passe du jour, une par extension en parallèle.
    for n, tld in enumerate((*ccdns.TLDS, "com")):
        scheduler.add_job(cc_job(tld), "interval", minutes=10, next_run_time=now + timedelta(minutes=9 + n),
                          max_instances=1, id=f"check_{tld}_dns", name=f"check_{tld}_dns")
    # Gandi : une passe par nuit, dans sa fenêtre ; la tâche rend la main tout de suite (passe à part).
    scheduler.add_job(check_gandi, "interval", minutes=5, next_run_time=now + timedelta(minutes=13), max_instances=1,
                      id="check_gandi", name="check_gandi")
    # Grille de prix Gandi : un appel par extension et par jour (prix normal ; les libres ne sont plus redemandés).
    scheduler.add_job(refresh_gandi_grid, "interval", hours=1, next_run_time=now + timedelta(minutes=4), max_instances=1,
                      id="gandi_grid", name="gandi_grid")
    # Extensions par Gandi seul : passe continue, jour et nuit ; relancée toutes les 5 minutes si elle s'est arrêtée.
    scheduler.add_job(check_gandi_live, "interval", minutes=5, next_run_time=now + timedelta(minutes=3), max_instances=1,
                      id="check_gandi_live", name="check_gandi_live")
    scheduler.start()
    telegram.start()

    app.state.refresh_latest = refresh_latest
    app.state.send_alerts = send_alerts
    app.state.send_daily = send_daily
    app.state.check_health = check_health
    app.state.update_outcomes = update_outcomes
    app.state.scheduler = scheduler
    app.state.drops = watcher
    app.state.fr = fr
    app.state.reserved = reserved.Reserved(conn, store)
    app.state.explorer = explore.Explorer(conn, watcher, fr, app.state.reserved)
    app.state.prenoms = prenoms.RecentNames(store, settings.data_dir)
    app.state.market = market.Market(conn, store)
    app.state.explorer.market = app.state.market  # ventes comparables : file IA et fiche
    app.state.explorer.recent = app.state.prenoms
    fr_dns = frdns.FrDns(conn, store, app.state.explorer, digest.local_zone(), notify=note_health)
    app.state.fr_dns = fr_dns
    # .co, .ai, .io : une passe par extension, en même temps que le .fr (registres différents).
    app.state.cc_dns = {t: ccdns.CcDns(t, conn, store, app.state.explorer, digest.local_zone(), notify=note_health)
                        for t in ccdns.TLDS}
    # .com sans zone (en attendant l'accès CZDS) : délégué, pris ; absent, demandé à Gandi en premier. Passe
    # inactive dès que la zone est lue (ComDns.ready).
    app.state.cc_dns["com"] = ccdns.ComDns(conn, store, app.state.explorer, digest.local_zone(), notify=note_health)
    # Libres sur la seule foi d'un WHOIS, fausses sorties du DNS : corrigés une fois, sans migration.
    if any(watcher.repair().values()):
        app.state.explorer.invalidate()
    # Noms sortis du DNS avant que la Veille ne les suive : repris au démarrage (puis à chaque passe).
    for cc in app.state.cc_dns.values():
        if cc.resume_lost():
            app.state.explorer.invalidate(cc.tld)
    app.state.seo = seo.Seo(conn, store)
    app.state.gandi = gandi.Gandi(store, conn=conn)
    # Une réponse de Gandi change l'état d'un nom (premium, libre, indisponible) : l'Explorer le corrige en place.
    app.state.gandi.on_change = app.state.explorer.touch
    app.state.gandi.on_blocked = app.state.explorer.recheck
    app.state.explorer.gandi = app.state.gandi
    app.state.gandi_pass = gandi.NightPass(app.state.gandi, conn, store, app.state.explorer, digest.local_zone(),
                                           notify=note_health)
    app.state.start_gandi = start_gandi
    app.state.gandi_live = gandi.LivePass(app.state.gandi, conn, store, app.state.explorer, digest.local_zone(),
                                          notify=note_health)
    app.state.start_gandi_live = start_gandi_live

    async def gandi_freed(name: str) -> None:
        """Nom tout juste libre : son prix (standard, premium, indisponible) sans attendre la passe de nuit. Une
        minute d'attente : Vérifier interroge déjà Gandi, une réponse de moins de dix minutes suffit."""
        await asyncio.sleep(FREED_GANDI_DELAY)
        client = app.state.gandi
        last = gandi.load(conn, [name]).get(name)
        if not client.key or (last and last["checked_at"] > iso(utcnow() - timedelta(minutes=10))):
            return
        try:
            await client.refresh(name)
        except gandi.GandiError as exc:
            log.info("%s : prix non demandé (%s)", name, exc)

    def on_freed(name: str) -> None:
        try:
            task = asyncio.get_running_loop().create_task(gandi_freed(name))
        except RuntimeError:
            return  # hors de la boucle (fil de calcul) : la passe de nuit s'en charge
        passes.add(task)
        task.add_done_callback(passes.discard)

    watcher.on_freed = on_freed

    async def gandi_domain(name: str, forced: bool) -> None:
        """Domaine suivi non pris, vérifié : Gandi aussi, une fois par jour au plus sauf à la main ou à un changement d'état."""
        client = app.state.gandi
        if not client.key:
            return
        if not forced:
            last = gandi.load(conn, [name]).get(name)
            if last and last["checked_at"] > iso(utcnow() - timedelta(hours=20)):
                return
        try:
            await client.refresh(name)
        except gandi.GandiError as exc:
            log.info("%s : Gandi : %s", name, exc)

    checker.gandi = gandi_domain
    checker.on_result = app.state.explorer.sync_registry
    # Explorer : univers et classement préparés hors requête (thread), puis états et carte en mémoire.
    async def warm_explorer() -> None:
        await asyncio.get_running_loop().run_in_executor(None, lambda: explore.shared_universe().buckets())
        await _off(app.state.explorer.warm)
        # Files de notation IA et de la passe DNS .fr : la fiche y lit la place d'un nom ; prêtes avant qu'on l'ouvre.
        await _off(ai.queue_position, app.state.explorer, "")
        if fr_dns.ready():
            await _off(fr_dns.position, "")

    app.state.explorer_warm = asyncio.create_task(warm_explorer())
    # Règle de calcul des notes IA changée : avis gardés recalculés depuis leurs sous-notes (une fois).
    rederived = ai.rederive(conn, store)
    if rederived:
        log.info("Avis IA recalculés : %s", rederived)
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


def _json_default(value):
    """Ce que json ne sait pas écrire (dates, Row) : la conversion de FastAPI, appelée pour ces valeurs seules."""
    return jsonable_encoder(value)


class FastJSON(JSONResponse):
    def render(self, content) -> bytes:
        return json.dumps(content, ensure_ascii=False, allow_nan=False, separators=(",", ":"), default=_json_default).encode("utf-8")


def fast_json(endpoint):
    """Route à grosse réponse (Veille, Explorer, fiche) : JSON écrit directement. La conversion générique de FastAPI
    parcourt chaque valeur et coûtait plus que le calcul (0,4 s pour 3 600 noms ici, bien plus sur le NAS).
    Avec une empreinte (ETag) : réponse identique à celle que le navigateur a déjà, 304 sans corps, et l'écran
    n'a ni à relire plusieurs Mo ni à se redessiner."""
    @functools.wraps(endpoint)
    async def wrapper(request: Request):
        result = await endpoint(request)
        if isinstance(result, Response):
            return result
        response = FastJSON(result)
        tag = '"' + hashlib.blake2b(response.body, digest_size=12).hexdigest() + '"'
        if request.headers.get("if-none-match") == tag:
            return Response(status_code=304, headers={"ETag": tag, "Cache-Control": "no-cache"})
        response.headers["ETag"] = tag
        response.headers["Cache-Control"] = "no-cache"
        return response
    return wrapper


SLOW_REQUEST = 1.0  # secondes : au-delà, la requête est notée au journal (lu par /api/diag)


@app.middleware("http")
async def log_slow_requests(request: Request, call_next):
    started = time.monotonic()
    response = await call_next(request)
    spent = time.monotonic() - started
    if spent > SLOW_REQUEST and request.url.path.startswith("/api/"):
        log.warning("Requête lente : %s %s en %.1f s", request.method, request.url.path, spent)
    return response


@app.exception_handler(RequestValidationError)
async def invalid_input(request: Request, exc: RequestValidationError):
    """422 sans recopier l'entrée : un nombre hors limites (1e400) n'est pas encodable en JSON, ce serait un 500."""
    errors = [{k: v for k, v in error.items() if k != "input"} for error in exc.errors()]
    return JSONResponse({"detail": jsonable_encoder(errors)}, status_code=422)


def _remember_public_url(request: Request) -> None:
    """Adresse publique de l'appli, vue du navigateur : sert aux liens des messages Telegram."""
    host = request.headers.get("x-forwarded-host") or request.headers.get("host")
    if host:
        scheme = request.headers.get("x-forwarded-proto") or request.url.scheme
        url = f"{scheme}://{host}"
        if request.app.state.store.get("public_url") != url:
            request.app.state.store.set("public_url", url)


def _cc_status(state) -> dict:
    """Passes DNS sans zone, pour l'écran et le quotidien : .co, .ai, .io toujours ; .com seulement tant qu'il est sans
    zone (ou si sa passe tourne encore)."""
    return {tld: c.status() for tld, c in state.cc_dns.items() if tld in ccdns.TLDS or c.running or c.ready()}


def _public_url(state) -> str:
    return state.settings.app_url or state.store.get("public_url") or ""


async def _daily(state, now_local: datetime) -> tuple[list[str], dict]:
    status = _cc_status(state)
    return await _off(notify.daily, state.explorer, state.store, state.checker, status, _public_url(state), utcnow(),
                      now_local, lambda filters: _view_params(state.explorer, filters))


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
    # Connecteur MCP : son jeton est vérifié par la route elle-même (adresse secrète ou en-tête).
    if path in PUBLIC_PATHS or path == "/mcp" or path.startswith(("/static/", "/calendar/", "/mcp/")):
        return await call_next(request)
    if state.sessions.valid(request.cookies.get(COOKIE_NAME), state.settings.username):
        if path == "/" and not state.settings.app_url:
            _remember_public_url(request)
        return await call_next(request)
    # Tâche de notation IA : jeton d'accès, limité à l'échange de la liste et des avis.
    if path in AI_PATHS and ai.token_ok(state.conn, request.headers.get("authorization")):
        return await call_next(request)
    if (request.method == "GET" and READ_PATHS.match(path)
            and tokens.check(state.conn, ("ai", "read"), tokens.bearer(request.headers.get("authorization")))):
        return await call_next(request)
    if path.startswith("/api/"):
        return JSONResponse({"detail": "Session expirée."}, status_code=401)
    # Lien ouvert sans session (notification Telegram) : retour à la même page après connexion.
    if path == "/" and request.url.query:
        return RedirectResponse(f"/login?{urlencode({'next': '/?' + request.url.query})}", status_code=303)
    return RedirectResponse("/login", status_code=303)


# Pages et API : jamais dans un cadre d'un autre site, pas de devinette de type, adresse non transmise ailleurs.
# La CSP s'arrête à frame-ancestors : le reste bloquerait Alpine (inline) et le CDN.
SECURITY_HEADERS = {
    "Content-Security-Policy": "frame-ancestors 'none'",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
    "X-Frame-Options": "DENY",
}


# Déclaré en dernier : le plus extérieur, il couvre aussi les réponses courtes des autres middlewares (401, 503).
@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    for key, value in SECURITY_HEADERS.items():
        response.headers.setdefault(key, value)  # /logo garde sa propre CSP
    return response


# Réponses compressées : la Veille pèse plusieurs Mo en JSON (3 600 noms), dix fois moins en gzip, et le NAS
# envoie à Cloudflare sans compression sinon. Ajouté après les autres middlewares : le plus extérieur.
app.add_middleware(GZipMiddleware, minimum_size=1024, compresslevel=5)


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
    problem = password_problem(password)
    if problem:
        return _page("setup.html", MISSING=_missing(settings), ERROR=problem)
    if password != confirm:
        return _page("setup.html", MISSING=_missing(settings), ERROR="Les deux saisies diffèrent.")
    hashed = await run_in_threadpool(hash_password, password)
    return _page("setup-done.html", HASH=compose_escape(hashed))


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, next: str = ""):
    state = request.app.state
    # Déjà connecté (lien Telegram, page gardée ouverte) : droit à l'appli.
    if state.sessions.valid(request.cookies.get(COOKIE_NAME), state.settings.username):
        return RedirectResponse(_safe_next(next), status_code=303)
    return _page("login.html", ERROR="", NEXT=_safe_next(next))


def _safe_next(target: str) -> str:
    """Page de retour après connexion : chemin local seulement, jamais un autre site."""
    return target if target.startswith("/") and not target.startswith(("//", "/\\")) and "\n" not in target else "/"


def _client_ip(request: Request) -> str:
    """Adresse du visiteur. Derrière Cloudflare ou le proxy du NAS, request.client est le proxy : ses en-têtes
    donnent l'adresse réelle, crus seulement si ce proxy est sur le réseau local ou privé."""
    host = request.client.host if request.client else "unknown"
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return host
    if address.is_private or address.is_loopback:
        forwarded = request.headers.get("cf-connecting-ip") or request.headers.get("x-forwarded-for", "").split(",")[0]
        if forwarded.strip():
            return forwarded.strip()
    return host


@app.post("/login")
async def login(request: Request, username: str = Form(""), password: str = Form(""), next: str = Form("/")):
    state = request.app.state
    client = _client_ip(request)
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
        "signals": [{"key": k, "label": signals.LABELS.get(k, k)} for k in found_signals.get("signals", [])],
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
            "ai": tokens.any_of(request.app.state.conn, "ai"),
        },
        # Démarrer en 3 étapes (Aujourd'hui, Paramètres) : même calcul que /api/today.
        "steps": _setup_steps(request.app.state),
    }


def _next_version_check(app_: FastAPI) -> str | None:
    job = app_.state.scheduler.get_job("latest") if hasattr(app_.state, "scheduler") else None
    if job is None or job.next_run_time is None:
        return None
    return iso(job.next_run_time.astimezone(timezone.utc))


@app.get("/api/domains")
@fast_json
async def list_domains(request: Request):
    checker = request.app.state.checker
    rows = request.app.state.conn.execute("SELECT * FROM domains ORDER BY name").fetchall()
    items = [_serialize(row, checker) for row in rows]
    # Score et avis IA, mêmes calculs que l'Explorer : filtres et tri communs à tous les écrans.
    values = await _off(request.app.state.explorer.value_of, [d["name"] for d in items])
    # Gandi : même règle que l'Explorer et la Veille (explore.gandi_effect), datée par la dernière lecture du registre.
    checks = gandi.load(request.app.state.conn, [d["name"] for d in items])
    listed = request.app.state.reserved.names()
    for d in items:
        v = values.get(d["name"]) or {}
        d.update(score=v.get("score", 0), reasons=v.get("reasons", []), parts=v.get("parts", {}), ai=v.get("ai"),
                 **_domain_gandi(d, checks.get(d["name"]), d["name"] in listed))
    return items


def _domain_gandi(d: dict, check: dict | None, listed: bool) -> dict:
    """Réponse de Gandi d'un domaine suivi (gandi, ou None si elle ne concerne pas son état) et ce qu'elle fait de
    l'état affiché (gandi_state). Registre lu en dernier : last_success_at ; entrée dans l'état : state_since."""
    code = explore.FREE if d["state"] == states.AVAILABLE else None
    if code is None or check is None:
        return {"gandi": None, "gandi_state": None}
    since, checked = d.get("state_since"), d.get("last_success_at") or ""
    _, kind = explore.gandi_effect(code, check, since, checked, listed)
    return {"gandi": gandi.public(check, since, checked), "gandi_state": kind}


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


# Plus grand entier SQLite : au-delà, la requête lèverait OverflowError (erreur 500).
MAX_ID = 2**63 - 1


def _get_row(request: Request, domain_id: int) -> sqlite3.Row:
    if not 0 < domain_id <= MAX_ID:
        raise HTTPException(404, "Domaine introuvable.")
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
    await checker.check(domain_id, manual=True)
    found = _serialize(_get_row(request, domain_id), checker)
    found.update(_domain_gandi(found, gandi.load(request.app.state.conn, [found["name"]]).get(found["name"]),
                               found["name"] in request.app.state.reserved.names()))
    return found


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
    """« Envoyer un essai » (carte Alertes) : types activés, rythmes et la meilleure pépite du moment. Rien n'est
    noté (ni pépite annoncée, ni quotidien fait) ; pas de rejeu, le bouton doit dire tout de suite si ça passe."""
    state = request.app.state
    text = await _off(notify.trial, state.explorer, state.store, utcnow(), _public_url(state), _order_url(state.store), VERSION)
    error = await state.telegram.send(text, retry=False)
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
    # Même adresse publique que les liens Telegram : APP_URL, sinon celle vue du navigateur, proxy compris.
    base = _public_url(request.app.state)
    if not base:
        scheme = request.headers.get("x-forwarded-proto") or request.url.scheme
        base = f"{scheme}://{request.headers.get('x-forwarded-host') or request.url.netloc}"
    return f"{base}/calendar/{token}.ics"


TELEGRAM_RETRY = 3600  # secondes : jeton refusé ou Telegram injoignable, pas de nouvel essai avant


def _telegram_retry_due(state) -> bool:
    failed_at = getattr(state, "telegram_failed_at", None)
    return failed_at is None or time.monotonic() - failed_at >= TELEGRAM_RETRY


@app.get("/api/settings")
async def get_settings(request: Request):
    state = request.app.state
    store, telegram = state.store, state.telegram
    token = telegram.token
    if token and not telegram.bot_name and _telegram_retry_due(state):
        try:
            await telegram.check_token()
        except (TelegramError, httpx.HTTPError):
            # Jeton refusé ou Telegram injoignable : l'écran ne rappelle pas Telegram à chaque ouverture.
            state.telegram_failed_at = time.monotonic()
    return {
        "telegram": {
            "token_set": bool(token),
            "bot_name": telegram.bot_name,
            "chat_set": bool(telegram.chat_id),
            "pairing": telegram.pairing_active,
        },
        "calendar_url": _calendar_url(request),
        "czds": _czds_settings(state.drops),
        "fr": {"enabled": state.fr.enabled, "url": state.store.get("fr_url"), "rate": state.fr.rate,
               "dns": state.store.get("fr_dns_enabled", "1") == "1"},
        "drops": {"keep_days": state.drops.keep_days, "min_score": _drops_min_score(state.store)},
        "seo": {"key_set": bool(state.seo.key)},
        "gandi": {**state.gandi.status(), "pass": state.gandi_pass.status(), "live": state.gandi_live.status()},
        "reserved": state.reserved.status(),
        "prenoms": state.prenoms.status(),
        "market": state.market.status(),
        "weights": {"values": value.weights_from(store.get("score_weights")), "labels": value.CRITERIA},
        "order_url": _order_url(store),
        "mcp": {"enabled": tokens.any_of(state.conn, "mcp")},
        "alerts": _alert_settings(store),
        "appearance": {
            "app_name": store.get("app_name") or DEFAULT_NAME,
            "theme": store.get("theme", "system"),
            "logo": _logo_path() is not None,
            "logo_height": _logo_height(),
        },
    }


@app.get("/api/settings/status")
async def settings_status(request: Request):
    """Ce qui bouge pendant une association Telegram ou un rafraîchissement des noms réservés : de quoi
    sonder sans relire tous les réglages (et sans rappeler Telegram)."""
    state = request.app.state
    telegram, held = state.telegram, state.reserved.status()
    names = state.prenoms
    return {
        "telegram": {"chat_set": bool(telegram.chat_id), "bot_name": telegram.bot_name, "pairing": telegram.pairing_active},
        "reserved": {"running": held["running"], "at": held.get("at"), "error": held.get("error"),
                     "count": sum(held["counts"].values())},
        "prenoms": {"running": names.running, "latest": names.latest},
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


class GapTld(BaseModel):
    tld: str


@app.post("/api/settings/czds/accept-gap")
async def accept_zone_gap(request: Request, payload: GapTld):
    """Écart de plus de 5 % refusé : la prochaine lecture de cette extension passe quand même, une fois."""
    watcher = request.app.state.drops
    tld = payload.tld.strip().lower().lstrip(".")
    if tld not in watcher.enabled():
        raise HTTPException(422, "Extension inconnue ou désactivée.")
    return {"accept_gap": watcher.accept_gap(tld), "gap_refused": watcher.gap_refused()}


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


class DropNames(BaseModel):
    names: list[str]


@app.post("/api/drops/hide")
async def hide_drops(request: Request, payload: DropNames):
    """Écarte des noms de l'écran Suppressions (ils restent dans l'Explorer)."""
    conn = request.app.state.conn
    now = iso(utcnow())
    names = _domain_names(payload.names)
    conn.executemany("INSERT OR IGNORE INTO drop_hidden (name, hidden_at) VALUES (?, ?)", [(n, now) for n in names])
    decisions.set_many(conn, names, "ignored", utcnow())  # « Ignorer » : la décision est gardée (regrets)
    return {"hidden": conn.execute("SELECT COUNT(*) FROM drop_hidden").fetchone()[0]}


def _domain_names(raw: list[str]) -> list[str]:
    """Noms de domaine seulement (lettres, chiffres, tirets, points ; 253 caractères au plus)."""
    return [n for n in (x.strip().lower() for x in raw[:20000]) if re.fullmatch(r"[a-z0-9.-]{3,253}", n) and "." in n]


@app.post("/api/drops/unhide")
async def unhide_drops(request: Request, payload: DropNames | None = None):
    """Sans corps : tout réafficher. Avec des noms : ces masquages seulement. La décision « ignoré » part avec."""
    conn = request.app.state.conn
    if payload is None:
        decisions.forget_ignored(conn)
        conn.execute("DELETE FROM drop_hidden")
    else:
        names = _domain_names(payload.names)
        decisions.forget_ignored(conn, names)
        conn.executemany("DELETE FROM drop_hidden WHERE name = ?", [(n,) for n in names])
    return {"hidden": conn.execute("SELECT COUNT(*) FROM drop_hidden").fetchone()[0]}


@app.post("/api/drops/fr/import", status_code=202)
async def import_fr_now(request: Request):
    fr = request.app.state.fr
    if not fr.enabled:
        raise HTTPException(409, "Suivi du .fr désactivé.")
    if fr.running:
        return {"started": False}
    async def run() -> None:
        await fr.import_file()
        await _off(request.app.state.explorer.warm)

    request.app.state.fr_task = asyncio.create_task(run())
    return {"started": True}


def _drop_rows(state) -> list:
    """Noms affichés dans la Veille : en cours de libération, ou libres depuis moins de keep_days jours ;
    sans les noms ignorés, ni les noms réservés quand ils sont retirés (Paramètres)."""
    watcher, conn = state.drops, state.conn
    # Un nom libre reste affiché keep_days jours ; il reste en base et dans l'Explorer.
    shown_since = iso(utcnow() - timedelta(days=watcher.keep_days))
    # Libres selon le seul WHOIS : affichés « à confirmer », depuis leur constat (pas de date de libération).
    rows = conn.execute(
        """SELECT * FROM drops WHERE (state IN (?, ?, ?) OR (state = ? AND available_since >= ?)
             OR (state = ? AND state_since >= ?)) AND origin IS NOT 'gandi'
           ORDER BY release_at IS NULL, release_at, name""",
        (*drops.TRACKED, states.AVAILABLE, shown_since, drops.UNCONFIRMED, shown_since),
    ).fetchall()
    held = state.reserved
    reserved_names = held.names()
    hide_reserved = held.mode == "hide"
    hidden = {r[0] for r in conn.execute("SELECT name FROM drop_hidden")}
    rows = [r for r in rows if r["name"] not in hidden
            and not (hide_reserved and (r["name"] in reserved_names or held.short_reserved(r["name"])))]
    if hide_reserved:
        # Indisponible chez Gandi : réservé comme les autres, retiré des noms à prendre.
        found = _drop_gandi(state, rows)
        rows = [r for r in rows if not found[r["name"]]["blocked"]]
    return rows


def _drop_gandi(state, rows) -> dict[str, dict]:
    """Réponse de Gandi pour chaque nom de la Veille, et ce qu'elle change, par la même règle que l'Explorer
    (explore.gandi_effect) : « available » fait d'un nom à confirmer un nom libre, « premium » un premium, un
    indisponible revu au registre n'est plus affiché libre ; rien sur un nom en rédemption ou repris. Premium :
    Gandi, avec son prix, sinon l'estimation des 3 caractères libres hors .fr (explore.PREMIUM), sans prix."""
    explorer = state.explorer
    checks = gandi.load(state.conn, [r["name"] for r in rows])
    premium_off = explorer.premium_tlds_off()
    listed = state.reserved.names()
    found = {}
    for r in rows:
        check, since, checked = checks.get(r["name"]), r["state_since"], gandi.registry_checked(r)
        code = explore.drop_code(r["state"])
        _, kind = explore.gandi_effect(code, check, since, checked, r["name"] in listed) if code is not None else (code, None)
        length = str(len(r["name"]) - len(r["tld"]) - 1)
        guess = (kind is None and code in (explore.FREE, explore.UNCONFIRMED) and r["tld"] != "fr"
                 and r["tld"] not in premium_off and length == str(explore.PREMIUM_LENGTH))
        price = round(check["price"]) if kind == "premium" and check["price"] is not None else None
        found[r["name"]] = {
            "check": check, "since": since, "checked": checked, "kind": kind, "premium": kind == "premium" or guess,
            "premium_price": price,
            "blocked": kind in gandi.BLOCKED, "free": kind == "available",
        }
    return found


def _drop_items(state, rows, values: dict) -> list[dict]:
    """Éléments de la Veille au format des cartes (aussi pour Aujourd'hui) : état, dates, score et avis IA."""
    conn = state.conn
    watched = {r[0] for r in conn.execute("SELECT name FROM domains")}
    owned = {r[0] for r in conn.execute("SELECT name FROM portfolio")}
    reserved_names = state.reserved.names()
    found = _drop_gandi(state, rows)
    return [
        {
            "name": r["name"],
            "tld": r["tld"],
            "kind": r["kind"],
            # Catégories du jour (listes et dérivées) ; celles gardées à la sortie de zone pour un nom sorti des listes.
            "categories": ([r["kind"]] if r["kind"] != "long" else [])
            + (wordlists.categories(r["label"]) or list(dict.fromkeys(wordlists.canonical(c) for c in r["lists"].split(",") if c))),
            "state": r["state"],
            "removed_on": r["removed_on"],
            "release_at": r["release_at"],
            "available_since": r["available_since"],
            "checked_at": r["checked_at"],
            "next_check_at": r["next_check_at"],
            "watched": r["name"] in watched,
            "in_portfolio": r["name"] in owned,
            "reserved": r["name"] in reserved_names or found[r["name"]]["blocked"],
            "state_since": r["state_since"],
            "premium": found[r["name"]]["premium"],
            "premium_price": found[r["name"]]["premium_price"],
            # Gandi : la réponse gardée (prix, date) et ce qu'elle dit de l'état affiché (gandi_state : available,
            # premium, unavailable, reserved, restricted ; vide si elle ne change rien ou date d'avant l'état actuel).
            "gandi": gandi.public(found[r["name"]]["check"], found[r["name"]]["since"], found[r["name"]]["checked"]),
            "gandi_state": found[r["name"]]["kind"],
            **values[r["name"]],
        }
        for r in rows
    ]


@app.get("/api/drops")
@fast_json
async def list_drops(request: Request):
    state = request.app.state
    watcher = state.drops
    conn = state.conn
    rows = _drop_rows(state)
    # Chiffre d'Aujourd'hui cliqué : seulement ses noms, calculés comme le compteur, pour la même période.
    focus = request.query_params.get("focus", "")
    focus = TODAY_FOCUS_ALIASES.get(focus, focus)
    if focus and focus not in TODAY_FOCUS:
        raise HTTPException(422, "Liste inconnue : freed, coming ou new_take.")
    period = _today_period(request)
    if focus:
        rows = (await _off(_today_sets, state, rows, utcnow(), TODAY_PERIODS[period],
                           request.query_params.get("nopremium") == "1"))[focus]
    pending = conn.execute("SELECT COUNT(*) FROM drops WHERE state IS NULL AND origin IS NOT 'gandi'").fetchone()[0]
    suspended = dict(conn.execute("SELECT tld, COUNT(*) FROM drops WHERE state = ? GROUP BY tld", (drops.SUSPENDED,)).fetchall())
    hidden = conn.execute("SELECT COUNT(*) FROM drop_hidden").fetchone()[0]
    # Rafraîchissement de l'état seul (écran ouvert) : pas de liste, quelques Ko au lieu de plusieurs Mo.
    # Le détail RDAP n'est pas dans la liste : la fiche le charge.
    with_items = request.query_params.get("items") != "0"
    values = await _off(state.explorer.value_of, [r["name"] for r in rows]) if with_items else {}
    return {
        "configured": watcher.configured or state.fr.enabled,
        "czds": watcher.configured,
        "running": watcher.running,
        "last_run": watcher.last_run(),
        "error": watcher.store.get("czds_error") or None,
        "zones": [{"tld": t, **watcher.status(t)} for t in watcher.enabled()],
        "gap_refused": watcher.gap_refused(),
        "accept_gap": watcher.gap_accepted(),
        "history": watcher.history(),
        "fr": {"enabled": state.fr.enabled, "running": state.fr.running, **state.fr.status(),
               "dns": state.fr_dns.status()},
        "cc_dns": _cc_status(state),
        "gandi": _gandi_line(state.gandi_pass.status()),
        "workers": watcher.workers,
        "pending_checks": pending,
        "suspended": suspended,
        "keep_days": watcher.keep_days,
        "min_score": _drops_min_score(state.store),
        "hidden": hidden,
        "kinds": drops.KINDS,
        "natures": explore.NATURES,
        "focus": focus or None,
        "period": period if focus else None,
        "items": _drop_items(state, rows, values) if with_items else None,
    }


def _gandi_line(status: dict) -> dict:
    """Ligne des sources de la Veille : dernière passe Gandi, en bref."""
    keys = ("enabled", "running", "started_at", "finished_at", "queued", "checked", "changed", "stopped", "error", "totals", "window")
    return {k: status.get(k) for k in keys}


# --- Aujourd'hui ------------------------------------------------------------------------

TODAY_ITEMS = 20
TODAY_SORTS = ("resale", "exploit")
# Sans avis IA, un nom sous ce score est « faible » : même seuil que la case de la Veille.
WEAK_SCORE = 30


def _setup_steps(state) -> list[dict]:
    """Les trois réglages qui font marcher l'appli : sources des noms, Telegram, avis IA."""
    return [
        {"key": "sources", "title": "Sources", "text": ".fr (Afnic) ou zones ICANN : les noms qui se libèrent",
         "done": bool(state.drops.configured or state.fr.enabled)},
        {"key": "telegram", "title": "Telegram", "text": "alertes immédiates et message du jour", "done": bool(state.telegram.configured)},
        {"key": "ai", "title": "Avis IA", "text": "notes et prix, facultatif", "done": tokens.any_of(state.conn, "ai")},
    ]


def _verdict(opinion: dict | None) -> str:
    return (opinion.get("comment") or "").split(".")[0] if opinion else ""


# Chiffres d'Aujourd'hui qui ouvrent la Veille sur exactement leurs noms (/api/drops?focus=…&period=…).
TODAY_FOCUS = ("freed", "coming", "new_take")
# Anciens noms des chiffres (avant le choix de la période) : encore acceptés.
TODAY_FOCUS_ALIASES = {"freed_24h": "freed", "coming_48h": "coming"}
# Période d'Aujourd'hui, en heures : libérés depuis, libération dans, nouveaux « À prendre » depuis.
TODAY_PERIODS = {"24h": 24, "7d": 7 * 24, "30d": 30 * 24}


def _today_period(request: Request) -> str:
    period = request.query_params.get("period") or "24h"
    if period not in TODAY_PERIODS:
        raise HTTPException(422, "Période inconnue : 24h, 7d ou 30d.")
    return period


def _freed_at(r, found: dict) -> str:
    """Libre depuis : libération constatée, ou, pour un nom à confirmer que Gandi dit libre, depuis son constat."""
    return r["available_since"] or (r["state_since"] if found[r["name"]]["free"] else "") or ""


def _today_sets(state, rows, now: datetime, hours: int = 24, nopremium: bool = False) -> dict:
    """Les noms derrière chaque chiffre d'Aujourd'hui pour la période (en heures, passée et à venir) :
    même calcul pour le compteur et pour la liste. Libre selon le WHOIS et libre chez Gandi : libéré ;
    indisponible chez Gandi : jamais libre ; nopremium : sans les premiums (Gandi ou probables)."""
    since, soon = iso(now - timedelta(hours=hours)), iso(now + timedelta(hours=hours))
    found = _drop_gandi(state, rows)
    if nopremium:
        rows = [r for r in rows if not found[r["name"]]["premium"]]
    free = [r for r in rows if (r["state"] == states.AVAILABLE or found[r["name"]]["free"]) and not found[r["name"]]["blocked"]]
    tracked = [r for r in rows if r["state"] in drops.TRACKED]
    # Avis « À prendre » reçus sur la période (premier avis ou second passage) sur un nom libre ou bientôt libre.
    opinions = ai.load(state.conn, [r["name"] for r in free + tracked])

    def new_take(r) -> bool:
        o = opinions.get(r["name"])
        return bool(o) and _verdict(o) == "À prendre" and max(o["rated_at"] or "", o["reviewed_at"] or "") >= since

    return {
        "freed": [r for r in free if _freed_at(r, found) >= since],
        "coming": [r for r in tracked if r["release_at"] and r["release_at"] <= soon],
        "new_take": [r for r in free + tracked if new_take(r)],
    }


def _today(state, sort: str, now: datetime, period: str = "24h", nopremium: bool = False) -> dict:
    """Écran d'accueil : compteurs de la période et les meilleurs noms libérés depuis ou libérés dans la période."""
    sets = _today_sets(state, _drop_rows(state), now, TODAY_PERIODS[period], nopremium)
    freed, near = sets["freed"], sets["coming"]
    candidates = freed + near
    values = state.explorer.value_of([r["name"] for r in candidates])

    def strong(v: dict) -> bool:
        # « Masquer les noms faibles » et les suites imprononçables, comme la Veille par défaut.
        verdict = _verdict(v.get("ai"))
        return v.get("good") is not False and (verdict != "À laisser" if verdict else v.get("score", 0) >= WEAK_SCORE)

    def rank(r) -> tuple:
        # Même ordre que la Veille : conseillés par l'IA d'abord, par la note de l'usage ; puis sans avis, par score.
        v = values[r["name"]]
        tier = 1 if not v.get("ai") else 0 if _verdict(v["ai"]) == "À laisser" else 2
        value = v["ai"][sort] if v.get("ai") else v.get("score", 0)
        return (-tier, -value, -v.get("score", 0), r["name"])

    best = sorted((r for r in candidates if strong(values[r["name"]])), key=rank)[:TODAY_ITEMS]
    # Pépites : Veille et Explorer, toutes extensions ; le compteur et la rubrique sont la même liste.
    minimum = notify.pepite_min(state.store)
    gems = pepites.today(state.conn, pepites.scan(state.explorer, now, minimum), now, TODAY_PERIODS[period], nopremium)
    return {
        "counts": {
            "freed": len(freed),
            "coming": len(near),
            "new_take": len(sets["new_take"]),
            "regrets": decisions.listing(state.conn, state.explorer)["regrets"],
            "pepites": len(gems["free"]) + len(gems["coming"]),
        },
        "pepites": {"min": minimum, "free": pepites.items(state.explorer, gems["free"], now),
                    "coming": pepites.items(state.explorer, gems["coming"], now)},
        "sort": sort,
        "period": period,
        "nopremium": nopremium,
        "items": _drop_items(state, best, values),
        "setup": _setup_steps(state),
    }


@app.get("/api/today")
@fast_json
async def today_screen(request: Request):
    sort = request.query_params.get("sort", "resale")
    if sort not in TODAY_SORTS:
        raise HTTPException(422, "Tri inconnu : resale ou exploit.")
    return await _off(_today, request.app.state, sort, utcnow(), _today_period(request),
                      request.query_params.get("nopremium") == "1")


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
    return _explore_params_of(request.query_params, request.app.state.explorer)


def _explore_params_of(p, explorer) -> dict:
    """Filtres de l'Explorer depuis des paramètres d'adresse (ou les filtres d'une vue enregistrée)."""
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
        gprice = int(float(p["gprice"])) if p.get("gprice") else None
    except ValueError:
        raise HTTPException(422, "Paramètre numérique invalide.") from None
    if gprice is not None and not 0 <= gprice <= 100_000_000:
        raise HTTPException(422, "Paramètre numérique hors limites.")
    # Une ou plusieurs catégories (cat=firstname,surname) : un nom de l'une ou de l'autre.
    cats = explore.split_cats(p.get("cat"))
    if any(c not in explore.KINDS for c in cats):
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
    if p.get("level") and p["level"] not in ("good", "gem"):
        raise HTTPException(422, "Niveau inconnu.")
    return {"tld": tld, "tlds": chosen, "q": p.get("q", ""), "mode": mode, "status": p.get("status", ""),
            "category": ",".join(cats), "length": length, "cursor": cursor, "nature": p.get("nat", ""),
            "max_rank": max_rank, "min_score": min_score, "taken": taken, "junk": p.get("junk") == "1",
            "seo": p.get("seo", "") if p.get("seo", "") in explore.SEO_FILTERS else "",
            "ai": _ai_param(p),
            # Prénoms et noms de famille n'ont pas de langue : avec l'une de ces listes cochée, la langue ne filtre pas.
            "lang": p.get("lang", "") if p.get("lang", "") in wordlists.LANGS and not set(cats) & set(wordlists.NO_LANG) else "",
            # Premiums (vus chez Gandi ou probables) affichés par défaut ; nopremium=1 les retire (« Masquer les premiums »).
            "premium": p.get("nopremium") != "1",
            # Prix d'achat Gandi au plus (euros TTC, création un an) : seulement les noms vérifiés chez Gandi.
            "gprice": gprice,
            "level": p.get("level", "") if p.get("level", "") in ("good", "gem") else ""}


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
                value = int(raw) if raw else None
            except ValueError:
                raise HTTPException(422, "Paramètre numérique invalide.") from None
            if value is not None and not 0 <= value <= 1_000_000_000:
                raise HTTPException(422, "Paramètre numérique hors limites.")
            bounds.append(value)
        ranges[field] = tuple(bounds)
    return explore.ai_filter(verdicts, ranges)


RANK_SORTS = ("score", "freq", "length", "seo", "ai", "release", "resale", "exploit", "recent")


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
        "absent_label": explore.absent_label(chosen[0]) if len({explore.absent_label(t) for t in chosen}) == 1 else explore.ABSENT_ZONE,
        # Prénoms récents (Insee) : liste du Top et tri « recent », absents tant qu'aucun fichier n'est lu.
        "recent": explorer.recent_meta(),
    }


# Calculs de l'Explorer hors de la boucle d'événements : un parcours de plusieurs secondes
# (nouvelle extension, filtres larges) ne bloque plus le reste de l'appli ni la connexion.
# Un seul fil : les calculs de l'écran passent l'un après l'autre, comme avant.
_EXPLORER_POOL = ThreadPoolExecutor(max_workers=1, thread_name_prefix="explorer")


async def _off(fn, *args):
    return await asyncio.get_running_loop().run_in_executor(_EXPLORER_POOL, fn, *args)


@app.get("/api/explore")
@fast_json
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
@fast_json
async def explore_rank(request: Request):
    """Les meilleures affaires d'abord, avec les compteurs de la sélection."""
    explorer = request.app.state.explorer
    params = _explore_params(request)
    params.pop("cursor")
    params["status"] = params["status"] or "available"
    limit = _limit(request, explore.MAX_LIMIT * 5)
    sort = request.query_params.get("sort", "score")
    if sort not in RANK_SORTS:
        raise HTTPException(422, "Tri inconnu.")
    chosen = params["tlds"]
    rest = {k: v for k, v in params.items() if k not in ("tld", "tlds")}
    return await _off(_rank, explorer, params, chosen, rest, limit, sort)


def _rank(explorer, params, chosen, rest, limit, sort):
    return {"items": _ranked_items(explorer, chosen, rest, limit, sort, params["category"]),
            **_explore_meta(explorer, params["tld"], chosen), "counts": _facets(explorer, params)}


def _ranked_items(explorer, chosen, rest, limit, sort, category=""):
    if len(chosen) == 1:
        return explorer.rank(chosen[0], **rest, limit=limit, sort=sort)["items"]
    # Plusieurs extensions : les meilleurs de chacune, fusionnés dans le même ordre.
    items = [item for tld in chosen for item in explorer.rank(tld, **rest, limit=limit, sort=sort)["items"]]
    if sort == "seo":
        rows = explorer._seo_rows([i["name"] for i in items])
        for item in items:
            row = rows.get(item["name"])
            item["_pr"], item["_wb"] = (row["pr"], row["wb_years"]) if row else (0, 0)
    items.sort(key=explorer.merge_key(sort, category))
    for item in items:
        item.pop("_pr", None)
        item.pop("_wb", None)
    return items[:limit]


TOP_SIZES = (10, 50, 100, 200, 500, 1000)
# État du tableau Top, en toutes lettres : les mêmes mots que les cartes.
TOP_STATES = {explore.REGISTERED: "pris", explore.SUSPENDED: "pris", explore.GONE: "probablement libre", explore.ABSENT: "probablement libre",
              explore.COMING: "à venir", explore.FREE: "libre", explore.RESERVED: "réservé", explore.INVALID: "pas lu",
              explore.PREMIUM: "premium probable", explore.UNCONFIRMED: "libre à confirmer",
              explore.UNCHECKED: "pas encore vérifié", explore.TAKEN_GANDI: "pris"}


def _top_state(tld: str, code: int) -> str:
    # .co, .ai, .io : seuls les meilleurs noms sont interrogés dans le DNS, les autres ne sont pas lus.
    return "pas interrogé" if code == explore.INVALID and tld in ccdns.TLDS else TOP_STATES.get(code, "")


@app.get("/api/explore/top")
@fast_json
async def explore_top(request: Request):
    """Les N premiers d'une liste, pris ou non, avec leur état sur chaque extension."""
    explorer = request.app.state.explorer
    # cat=sector (adresses anciennes) : Termes stars, qui contient tous les secteurs porteurs.
    category = wordlists.canonical(request.query_params.get("cat", "premium"))
    if category not in wordlists.SHOWN and category != prenoms.TOP_KEY:
        raise HTTPException(422, "Liste inconnue.")
    if category == prenoms.TOP_KEY and await _off(explorer.recent_order) is None:
        raise HTTPException(409, "Prénoms récents pas encore téléchargés (Paramètres, Prénoms récents).")
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
    lists = dict(wordlists.SHOWN)
    recent = explorer.recent_order()
    if recent is not None:
        lists[prenoms.TOP_KEY] = f"Prénoms {recent['year']}"
    if category == prenoms.TOP_KEY:
        # Rang et nombre de naissances de l'Insee ; les prénoms hors de l'univers (non suivis) sont sautés.
        ranked = [(*recent["ranks"][i], i) for i in recent["order"][:size]]
    else:
        order = u.by_frequency[category]
        if lang in wordlists.LANGS and category not in wordlists.NO_LANG:
            names = wordlists.languages()[lang]
            order = [i for i in order if u.label(i) in names]
        ranked = [(rank, None, i) for rank, i in enumerate(order[:size], 1)]
    opinions = ai.load(explorer.conn, [f"{u.label(i)}.{t}" for _, _, i in ranked for t in tlds])
    items = []
    for rank, count, i in ranked:
        label = u.label(i)
        cells = {t: {"code": data[t][i], "state": _top_state(t, data[t][i]), "ai": opinions.get(f"{label}.{t}")}
                 for t in tlds}
        items.append({"rank": rank, "label": label, "cells": cells, **({"count": count} if count else {})})
    return {"category": category, "size": size, "tlds": tlds, "items": items,
            "lists": lists, "sizes": TOP_SIZES, "year": recent["year"] if recent else None}


@app.get("/api/explore/map")
@fast_json
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
        try:
            offset = max(0, min(int(request.query_params.get("offset") or 0), 20 * ai.MAX_PENDING))
        except ValueError:
            raise HTTPException(422, "Paramètre numérique invalide.") from None
        names = await _off(ai.pending, explorer, _limit(request, ai.MAX_PENDING), offset)
    # Ventes et offres réelles du portefeuille : repères de prix ajoutés à la consigne.
    guide = ai.GUIDE + portfolio.guide_addendum(portfolio.market_examples(request.app.state.conn))
    return {"version": ai.VERSION, "guide": guide, "format": ai.FORMAT, "names": names}


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
    status_of = await _off(ai.status_lookup, explorer)  # état au moment de l'avis : mesure a posteriori
    context_of = await _off(ai.context_lookup, explorer, request.app.state.store)  # demande mesurée, renouvellement
    saved = ai.save(conn, payload.results, lambda name: name in asked or known(name), status_of, context_of)
    explorer.ai_changed()  # compteurs filtrés sur l'avis
    for name in ai.finish(conn, saved.pop("names")):
        await _notify_ai_done(request, name)
    return saved


async def _notify_ai_done(request: Request, name: str) -> None:
    """Analyse demandée prête : message Telegram avec le lien direct vers la fiche."""
    if not notify.enabled(request.app.state.store, "ai_ready"):
        return
    opinion = ai.load(request.app.state.conn, [name]).get(name) or {}
    base = _public_url(request.app.state)
    link = f"{base}/?{urlencode({'fiche': name, 'ai': '1'})}" if base else ""
    verdict, _, reason = (opinion.get("comment") or "").partition(". ")
    summary = (f"Avis IA : {verdict.rstrip('.')}, revente {opinion['resale']}, exploitation {opinion['exploit']}, "
               f"prix final {pepites.price_range(opinion['final_low'], opinion['final_high'])}") if opinion else ""
    text = "\n".join(x for x in (f"Analyse IA prête : {name}", summary, reason, "Fiche : " + link if link else "") if x)
    try:
        await request.app.state.telegram.send(text)
    except Exception as exc:  # noqa: BLE001  la notification dans l'appli reste
        log.warning("Analyse IA : message Telegram non envoyé (%s)", exc)


@app.get("/api/portfolio")
async def get_portfolio(request: Request):
    now = utcnow()
    found = portfolio.items(request.app.state.conn, now)
    return {"items": found, "totals": portfolio.totals(found), "years": portfolio.years(found, now),
            "statuses": portfolio.STATUSES}


@app.get("/api/portfolio/export")
async def export_portfolio(request: Request):
    """Le portefeuille en CSV (BOM, point-virgule), pour un tableur."""
    found = portfolio.items(request.app.state.conn, utcnow())
    return Response(portfolio.export_csv(found), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": 'attachment; filename="portefeuille.csv"'})


@app.post("/api/portfolio")
async def add_portfolio(request: Request, payload: dict):
    try:
        name = clean_domain(str(payload.get("name", "")))
        values = portfolio.clean(payload)
    except (InvalidDomain, ValueError) as exc:
        raise HTTPException(422, str(exc)) from None
    conn = request.app.state.conn
    if conn.execute("SELECT 1 FROM portfolio WHERE name = ?", (name,)).fetchone():
        raise HTTPException(409, f"{name} est déjà dans le portefeuille.")
    values.setdefault("status", "owned")
    cols = ["name", "created_at", *values]
    conn.execute(f"INSERT INTO portfolio ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                 (name, iso(utcnow()), *values.values()))
    decisions.set_decision(conn, name, "bought", None, utcnow())  # « J'ai acheté » : décision gardée
    # Surveillé aussi : l'échéance réelle vient du RDAP, et les changements arrivent sur Telegram.
    # La vérification part en tâche de fond : la réponse ne l'attend pas (checking), l'échéance se met à jour après.
    checking = False
    if not conn.execute("SELECT 1 FROM domains WHERE name = ?", (name,)).fetchone():
        _background(request.app.state.checker.add(name), f"Portefeuille : vérification de {name}")
        await asyncio.sleep(0)  # un tour de boucle : le domaine est inséré avant la réponse, la requête RDAP suit
        checking = True
    return {**await get_portfolio(request), "checking": checking}


_BACKGROUND: set[asyncio.Task] = set()


def _background(coro, label: str) -> asyncio.Task:
    """Tâche détachée, référence gardée, exception au journal (sinon elle se perdrait sans bruit)."""
    task = asyncio.create_task(coro)
    _BACKGROUND.add(task)

    def done(t: asyncio.Task) -> None:
        _BACKGROUND.discard(t)
        if not t.cancelled() and t.exception() is not None:
            log.warning("%s : %s", label, _exception_line(t.exception()))

    task.add_done_callback(done)
    return task


@app.put("/api/portfolio/{item_id}")
async def update_portfolio(request: Request, item_id: int, payload: dict):
    try:
        values = portfolio.clean(payload)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    conn = request.app.state.conn
    if not 0 < item_id <= MAX_ID or not conn.execute("SELECT 1 FROM portfolio WHERE id = ?", (item_id,)).fetchone():
        raise HTTPException(404, "Nom absent du portefeuille.")
    if values:
        conn.execute(f"UPDATE portfolio SET {', '.join(f'{k} = ?' for k in values)} WHERE id = ?", (*values.values(), item_id))
    return await get_portfolio(request)


@app.delete("/api/portfolio/{item_id}")
async def delete_portfolio(request: Request, item_id: int):
    if not 0 < item_id <= MAX_ID or not request.app.state.conn.execute("DELETE FROM portfolio WHERE id = ?", (item_id,)).rowcount:
        raise HTTPException(404, "Nom absent du portefeuille.")
    return await get_portfolio(request)


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
        "cc_dns": _cc_status(state),
        "ai": ai.stats(explorer),
        "reserved": state.reserved.status(),
        # Sans l'adresse saisie : le jeton en lecture seule ne voit aucun réglage.
        "prenoms": {k: v for k, v in state.prenoms.status().items() if k != "url"},
        "market": state.market.status(),
        "lists": {k: len(v) for k, v in wordlists.lists().items()},
        "domains": dict(conn.execute("SELECT COALESCE(state, 'inconnu'), COUNT(*) FROM domains GROUP BY 1").fetchall()),
        "drops": drops_by_state,
        "map": await _off(explorer.map),
        "jobs": [{"id": j.id, "name": j.name, "next": iso(j.next_run_time) if j.next_run_time else None}
                 for j in state.scheduler.get_jobs()],
        "scheduler": {k: list(v) for k, v in state.scheduler_events.items()},
        "telegram_outbox": state.telegram.pending(),
        # Réponses brutes de Gandi aux noms témoins (bouton Tester) : aucun secret, ni le jeton ni sa présence.
        "gandi_samples": state.gandi.samples(),
        # Passe de nuit Gandi : dernier passage, réglages de rythme, totaux par sorte ; ni le jeton ni sa présence.
        "gandi_pass": state.gandi_pass.status(),
        "gandi_live": state.gandi_live.status(),
        "recent_errors": list(RECENT.records),
    }


@app.post("/api/settings/ai-token")
async def ai_token(request: Request):
    """Nouveau jeton pour la tâche de notation ; l'ancien cesse de marcher. Montré une seule fois."""
    return {"token": ai.new_token(request.app.state.conn)}


class NewToken(BaseModel):
    kind: str
    label: str | None = None


class TokenLabel(BaseModel):
    label: str | None = None


def _calendar_row(state) -> dict | None:
    """Le lien du calendrier est un jeton aussi : listé avec les autres, révocable de la même façon."""
    if not state.store.get("calendar_token"):
        return None
    return {"id": "calendar", "kind": "calendar", "kind_label": "Calendrier", "label": "Lien du calendrier",
            "created_at": state.store.get("calendar_created_at") or None,
            "last_used_at": state.store.get("calendar_used_at") or None}


@app.get("/api/tokens")
async def list_tokens(request: Request):
    """Jetons d'accès : type, libellé, création, dernière utilisation. Jamais le jeton lui-même."""
    state = request.app.state
    items = tokens.listing(state.conn)
    if (cal := _calendar_row(state)) is not None:
        items.append(cal)
    return {"kinds": {k: {"label": v[0], "opens": v[2]} for k, v in tokens.KINDS.items()}, "items": items}


@app.post("/api/tokens")
async def create_token(request: Request, payload: NewToken):
    """Nouveau jeton, montré une seule fois ; pour le connecteur MCP, son adresse complète."""
    try:
        token_id, token = tokens.create(request.app.state.conn, payload.kind, payload.label)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    result = {"id": token_id, "token": token}
    if payload.kind == "mcp":
        base = _public_url(request.app.state) or str(request.base_url)
        result["url"] = f"{base.rstrip('/')}/mcp/{token}"
    return result


@app.put("/api/tokens/{token_id}")
async def rename_token(request: Request, token_id: int, payload: TokenLabel):
    if not tokens.rename(request.app.state.conn, token_id, payload.label):
        raise HTTPException(404, "Jeton introuvable.")
    return Response(status_code=204)


@app.delete("/api/tokens/{token_id}")
async def revoke_token(request: Request, token_id: str):
    state = request.app.state
    if token_id == "calendar":
        state.store.set("calendar_token", "")
        return Response(status_code=204)
    if not token_id.isdigit() or not tokens.revoke(state.conn, int(token_id)):
        raise HTTPException(404, "Jeton introuvable.")
    return Response(status_code=204)


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
        if not name or not isinstance(filters, dict):
            continue
        if len(filters) > 40:
            raise HTTPException(422, f"Vue « {name} » : {len(filters)} filtres, 40 au plus.")
        keep = [v for v in keep if v["name"] != name]  # même nom : la dernière l'emporte
        # Les listes d'extensions et de catégories peuvent être longues : pas de coupure pour elles.
        clean = {str(k)[:20]: ("" if v is None else str(v))[:400 if k in ("tld", "cat") else 40]
                 for k, v in filters.items() if v is None or isinstance(v, (str, int, float, bool))}
        # alert : les nouveaux noms libres qui répondent aux filtres partent sur Telegram (notify).
        keep.append({"name": name, "filters": clean, "alert": bool(view.get("alert"))})
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
            await _off(request.app.state.explorer.warm)

        request.app.state.reserved_task = asyncio.create_task(refresh())
    return {"started": not lists.running}


class PrenomsSettings(BaseModel):
    url: str = ""


@app.put("/api/settings/prenoms")
async def set_prenoms(request: Request, payload: PrenomsSettings):
    """Adresse du fichier des prénoms de l'Insee, si la découverte par data.gouv.fr échoue (vide : automatique)."""
    url = payload.url.strip()
    if url and not url.startswith("https://"):
        raise HTTPException(422, "Adresse du fichier : https:// attendu.")
    request.app.state.store.set("prenoms_url", url)
    return request.app.state.prenoms.status()


@app.post("/api/prenoms/refresh", status_code=202)
async def refresh_prenoms_now(request: Request):
    names = request.app.state.prenoms
    if names.running:
        return {"started": False}
    request.app.state.prenoms_task = asyncio.create_task(names.refresh())
    return {"started": True}


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


class GandiKey(BaseModel):
    key: str


@app.put("/api/settings/gandi")
async def set_gandi_key(request: Request, payload: GandiKey):
    """Jeton Gandi en écriture seule : jamais renvoyé ; vide, il est retiré."""
    request.app.state.gandi.set_key(payload.key)
    return request.app.state.gandi.status()


class GandiPassSettings(BaseModel):
    enabled: bool | None = None
    rate: float | None = None
    nightly: int | None = None
    days: int | None = None


@app.put("/api/settings/gandi/pass")
async def set_gandi_pass(request: Request, payload: GandiPassSettings):
    """Passe de nuit Gandi : activation, rythme (requêtes par seconde), plafond par nuit, délai avant de revoir un nom."""
    night, store = request.app.state.gandi_pass, request.app.state.store
    if payload.rate is not None and not gandi.MIN_RATE <= payload.rate <= gandi.MAX_RATE:
        raise HTTPException(422, f"Rythme entre {gandi.MIN_RATE} et {gandi.MAX_RATE:g} requêtes par seconde.")
    if payload.nightly is not None and not 1 <= payload.nightly <= 50_000:
        raise HTTPException(422, "Plafond entre 1 et 50 000 noms par nuit.")
    if payload.days is not None and not 1 <= payload.days <= 90:
        raise HTTPException(422, "Délai entre 1 et 90 jours.")
    if payload.enabled is not None:
        store.set(f"{night.key}_enabled", "1" if payload.enabled else "0")
    for field in ("rate", "nightly", "days"):
        value = getattr(payload, field)
        if value is not None:
            store.set(f"{night.key}_{field}", str(value))
    return night.status()


class GandiLiveSettings(BaseModel):
    enabled: bool | None = None
    rate: float | None = None
    days: int | None = None
    tlds: list[str] | None = None


@app.put("/api/settings/gandi/live")
async def set_gandi_live(request: Request, payload: GandiLiveSettings):
    """Passe continue des extensions par Gandi seul : activation (décochée, elle s'arrête), rythme, délai avant de revoir
    un nom, extensions concernées tant qu'aucune zone ne les lit."""
    state = request.app.state
    live, store = state.gandi_live, state.store
    if payload.rate is not None and not gandi.MIN_RATE <= payload.rate <= gandi.LIVE_MAX_RATE:
        raise HTTPException(422, f"Rythme entre {gandi.MIN_RATE} et {gandi.LIVE_MAX_RATE:g} requêtes par seconde.")
    if payload.days is not None and not 1 <= payload.days <= 90:
        raise HTTPException(422, "Délai entre 1 et 90 jours.")
    if payload.tlds is not None:
        tlds = [t.strip().lower().lstrip(".") for t in payload.tlds]
        if any(not re.fullmatch(r"[a-z]{2,24}", t) or t == "fr" or t in ccdns.TLDS for t in tlds):
            raise HTTPException(422, "Extensions génériques seulement (com, net…), ni .fr ni .co, .ai, .io.")
        store.set("gandi_only_tlds", ",".join(sorted(set(tlds))))
        state.explorer.invalidate()  # extensions de l'Explorer changées
    if payload.enabled is not None:
        store.set(f"{live.key}_enabled", "1" if payload.enabled else "0")
    for field in ("rate", "days"):
        value = getattr(payload, field)
        if value is not None:
            store.set(f"{live.key}_{field}", str(value))
    if live.due():
        state.start_gandi_live()
    return live.status()


@app.post("/api/settings/gandi/run", status_code=202)
async def run_gandi_pass(request: Request):
    """« Lancer maintenant » : la passe de nuit tout de suite, hors de sa fenêtre, jusqu'au plafond."""
    state = request.app.state
    if not state.gandi.key:
        raise HTTPException(409, "Jeton Gandi non configuré.")
    return {"started": state.start_gandi(manual=True), **state.gandi_pass.status()}


@app.post("/api/settings/gandi/test")
async def test_gandi(request: Request):
    """Quelques noms témoins interrogés chez Gandi : résumé pour l'écran, réponses brutes gardées pour le diagnostic."""
    found = request.app.state.gandi
    if not found.key:
        raise HTTPException(409, "Jeton Gandi non configuré.")
    return {**await found.test(), **found.status()}


@app.get("/api/explore/export")
async def explore_export(request: Request):
    """La liste affichée, en CSV : les premiers dans l'ordre de l'écran, puis le reste dans l'ordre des noms."""
    params = _explore_params(request)
    params["cursor"] = -1
    chosen = params.pop("tlds")
    params.pop("tld")
    sort = request.query_params.get("sort", "")
    if sort and sort not in RANK_SORTS:
        raise HTTPException(422, "Tri inconnu.")
    explorer = request.app.state.explorer
    items = await _off(_export_items, explorer, params, chosen, sort)
    lines = ["nom;etat;score;verdict;note_revente;note_exploitation;prix_final_bas;prix_final_haut;categories;"
             "cree_le;supprime_le;echeance;verifie_le;gandi_kind;gandi_price"]
    for item in items[:explore.EXPORT_LIMIT]:
        opinion = item.get("ai") or {}
        check = item.get("gandi") or {}
        lines.append(";".join([
            item["name"], item["status_label"], _cell(item.get("score")),
            (opinion.get("comment") or "").split(".")[0], _cell(opinion.get("resale")), _cell(opinion.get("exploit")),
            _cell(opinion.get("final_low")), _cell(opinion.get("final_high")), ",".join(item["categories"]),
            item["created_on"] or "", item["removed_on"] or "", item["expires_at"] or "", item["checked_at"] or "",
            check.get("kind") or "", _cell(check.get("price")),
        ]))
    filename = f"explorer-{'-'.join(chosen)}.csv"
    # BOM : Excel lit alors les accents correctement.
    return Response("\ufeff" + "\n".join(lines) + "\n", media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


def _cell(value) -> str:
    return "" if value is None else str(value)


def _export_items(explorer, params, chosen, sort: str = ""):
    items, seen = [], set()
    if sort:
        # Même ordre que l'écran pour la tête de liste (le classement s'arrête à sa limite, comme « 50 de plus »).
        rest = {**params, "status": params["status"] or "available"}
        rest.pop("cursor")
        items = _ranked_items(explorer, chosen, rest, explore.MAX_LIMIT * 5, sort, params["category"])
        seen = {i["name"] for i in items}
    for tld in chosen:
        found = [i for i in explorer.search(tld, **params, limit=explore.EXPORT_LIMIT)["items"] if i["name"] not in seen]
        # Score de chaque nom, comme à l'écran ; score minimum et niveau s'appliquent aussi à l'export.
        scores = explorer.value_of([i["name"] for i in found])
        opinions = explorer._opinions(tld) if params.get("level") else {}
        for item in found:
            item["score"] = scores.get(item["name"], {}).get("score", 0)
        items += [i for i in found if i["score"] >= (params["min_score"] or 0)
                  and explorer._level_ok(i["score"], opinions.get(i["name"]), params.get("level", ""))]
    return items


@app.get("/api/explore/name")
async def explore_name(request: Request):
    return await _off(request.app.state.explorer.card, request.query_params.get("label", ""))


@app.get("/api/explore/fiche")
@fast_json
async def explore_fiche(request: Request):
    try:
        name = clean_domain(request.query_params.get("name", ""))
    except InvalidDomain as exc:
        raise HTTPException(422, str(exc)) from None
    explorer = request.app.state.explorer
    found = await _off(explorer.fiche, name)
    # Nom surveillé : la libération estimée du suivi (Domaines) entre dans la ligne de temps.
    row = request.app.state.conn.execute("SELECT * FROM domains WHERE name = ?", (name,)).fetchone()
    estimate = request.app.state.checker.release_estimate(row) if row is not None else None
    if estimate is not None and not any(t["kind"] == "release" for t in found.get("timeline", [])):
        found["timeline"] = await _off(explorer.timeline, name, row, found.get("rdap"), iso(estimate))
    found["ai_eta"] = await _ai_eta(explorer, found)
    found["ai_request"] = ai.request_state(request.app.state.conn, name)
    found["in_portfolio"] = request.app.state.conn.execute("SELECT 1 FROM portfolio WHERE name = ?", (name,)).fetchone() is not None
    if found.get("tld") in request.app.state.cc_dns:
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


@app.post("/api/explore/mine")
async def add_mine(request: Request, payload: NameCheck):
    """« Ajouter aux noms suivis » : le libellé (sans l'extension) entre dans Mes ajouts, puis dans l'univers
    sans redémarrage. Le nom demandé (avec extension) est vérifié au registre tout de suite : les zones ne le
    chercheront qu'à leur prochaine lecture. Session seulement (jamais le jeton en lecture seule)."""
    try:
        label = wordlists.normalize(payload.name)
    except wordlists.InvalidLabel as exc:
        raise HTTPException(422, str(exc)) from None
    explorer = request.app.state.explorer
    added = await asyncio.to_thread(wordlists.add_mine, label)
    seconds = await _off(explorer.reload_lists)
    log.info("Mes ajouts : %s ajouté, univers rechargé en %.1f s", label, seconds)
    tld = payload.name.strip().lower().partition(".")[2]
    check, error = None, None
    if tld:
        try:
            check = await _verify(request, f"{label}.{tld}")
        except HTTPException as exc:
            error = exc.detail  # l'ajout est fait : la vérification se refait avec « Vérifier »
    return {"label": label, "added": added, "count": len(wordlists.mine()), "check": check, "check_error": error,
            "seconds": round(seconds, 2)}


@app.delete("/api/explore/mine/{label}")
async def remove_mine(request: Request, label: str):
    """« Retirer de mes ajouts » : le nom sort de l'univers s'il n'est dans aucune autre liste."""
    try:
        label = wordlists.normalize(label)
    except wordlists.InvalidLabel as exc:
        raise HTTPException(422, str(exc)) from None
    removed = await asyncio.to_thread(wordlists.remove_mine, label)
    if not removed:
        raise HTTPException(404, "Ce nom n'est pas dans Mes ajouts.")
    seconds = await _off(request.app.state.explorer.reload_lists)
    log.info("Mes ajouts : %s retiré, univers rechargé en %.1f s", label, seconds)
    return {"label": label, "removed": True, "count": len(wordlists.mine()), "seconds": round(seconds, 2)}


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
        await _off(request.app.state.explorer.warm)

    request.app.state.scan_task = asyncio.create_task(run())
    return {"started": True}


class TelegramToken(BaseModel):
    token: str


# Forme donnée par BotFather : identifiant numérique, deux-points, puis lettres, chiffres, - et _.
TELEGRAM_TOKEN = re.compile(r"\d+:[A-Za-z0-9_-]{20,}")


@app.put("/api/settings/telegram")
async def set_telegram_token(request: Request, payload: TelegramToken):
    state = request.app.state
    token = "".join(payload.token.split())
    if not TELEGRAM_TOKEN.fullmatch(token):
        raise HTTPException(422, "Jeton invalide : BotFather donne la forme 123456:ABC-DEF… (lettres, chiffres, - et _).")
    previous = state.telegram.token
    # Vérifié avant d'être écrit : un jeton refusé ne touche ni la base ni le bot en place.
    try:
        name = await state.telegram.check_token(token)
    except (TelegramError, httpx.HTTPError) as exc:
        raise HTTPException(422, f"Jeton refusé par Telegram : {exc}") from None
    state.store.set("telegram_token", token)
    state.telegram_failed_at = None
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
    request.app.state.telegram_failed_at = None
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


@app.post("/api/settings/calendar")
async def regenerate_calendar(request: Request):
    request.app.state.store.set("calendar_token", secrets.token_urlsafe(24))
    request.app.state.store.set("calendar_created_at", utcnow().isoformat(timespec="seconds"))
    return {"calendar_url": _calendar_url(request)}


@app.get("/calendar/{token}.ics")
async def calendar(request: Request, token: str):
    expected = request.app.state.store.get("calendar_token")
    # Lien secret, sans session : les applis de calendrier ne savent pas se connecter.
    if not expected or not hmac.compare_digest(token.encode(), expected.encode()):
        raise HTTPException(404)
    store, now = request.app.state.store, utcnow()
    used = parse_iso(store.get("calendar_used_at") or None)
    if used is None or now - used > tokens.TOUCH_EVERY:  # Jetons d'accès : dernière utilisation du lien
        store.set("calendar_used_at", now.isoformat(timespec="seconds"))
    body = calendar_ics.build(request.app.state.checker, now, request.app.state.settings.app_url)
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


# --- Alertes immédiates, mesure a posteriori des avis, décisions gardées ------------------

def _alert_settings(store) -> dict:
    return notify.settings(store)


def _view_params(explorer, filters: dict) -> dict | None:
    """Filtres d'une vue enregistrée traduits pour l'Explorer ; None s'ils ne passent plus (zone retirée…)."""
    try:
        return _explore_params_of({k: str(v) for k, v in filters.items()}, explorer)
    except HTTPException:
        return None


class AlertSettings(BaseModel):
    enabled: dict[str, bool] | None = None
    pepite_min: int | None = None
    daily_hour: int | None = None
    weekly_day: int | None = None
    morning_min: int | None = None


@app.get("/api/settings/alerts")
async def get_alert_settings(request: Request):
    return _alert_settings(request.app.state.store)


@app.put("/api/settings/alerts")
async def set_alert_settings(request: Request, payload: AlertSettings):
    """Carte Alertes : cases par type, seuil des pépites, heure du quotidien, jour du résumé de la semaine,
    score minimum des bons noms du jour. Champs absents : inchangés."""
    limits = {"pepite_min": (0, 100, "Seuil de revente"), "daily_hour": (0, 23, "Heure du quotidien"),
              "weekly_day": (0, 6, "Jour du résumé de la semaine"), "morning_min": (0, 100, "Score minimum")}
    for key, (lo, hi, label) in limits.items():
        value = getattr(payload, key)
        if value is not None and not lo <= value <= hi:
            raise HTTPException(422, f"{label} : entre {lo} et {hi}.")
    unknown = sorted(set(payload.enabled or {}) - set(notify.KEYS))
    if unknown:
        raise HTTPException(422, f"Type d'alerte inconnu : {', '.join(unknown)}.")
    store = request.app.state.store
    notify.save(store, payload.enabled, **{k: getattr(payload, k) for k in limits})
    return _alert_settings(store)


@app.get("/api/ai/outcomes")
async def ai_outcomes(request: Request):
    """Mesure a posteriori : par verdict et par tranche de note de revente, part des noms repris."""
    return ai.outcomes(request.app.state.conn)


class Decision(BaseModel):
    decision: str
    note: str | None = None


def _decision_name(name: str) -> str:
    names = _domain_names([name])
    if not names:
        raise HTTPException(422, "Nom de domaine attendu.")
    return names[0]


@app.get("/api/decisions")
async def list_decisions(request: Request):
    state = request.app.state
    return await _off(decisions.listing, state.conn, state.explorer)


@app.put("/api/decisions/{name}")
async def put_decision(request: Request, name: str, payload: Decision):
    if payload.decision not in decisions.KINDS:
        raise HTTPException(422, "Décision : ignored ou bought.")
    note = " ".join((payload.note or "").split())[:decisions.NOTE_MAX] or None
    decisions.set_decision(request.app.state.conn, _decision_name(name), payload.decision, note, utcnow())
    return await list_decisions(request)


@app.delete("/api/decisions/{name}", status_code=204)
async def delete_decision(request: Request, name: str):
    if not decisions.delete(request.app.state.conn, _decision_name(name)):
        raise HTTPException(404, "Aucune décision pour ce nom.")
    return Response(status_code=204)


# Outils MCP : chaque chemin est un écran en lecture seule (READ_PATHS), appelé directement, sans passer par le réseau.
MCP_READS = {"/api/explore/rank": explore_rank, "/api/explore/fiche": explore_fiche, "/api/today": today_screen,
             "/api/drops": list_drops, "/api/explore/top": explore_top, "/api/ai/rated": ai_rated,
             "/api/domains": list_domains}


async def _mcp_read(app_, path: str, params: dict):
    if path not in MCP_READS or not READ_PATHS.match(path):
        raise ValueError("Lecture non autorisée.")
    scope = {"type": "http", "method": "GET", "path": path, "query_string": urlencode(params).encode(),
             "headers": [], "app": app_}
    try:
        result = await MCP_READS[path](Request(scope))
    except HTTPException as exc:
        raise ValueError(str(exc.detail)) from None
    if isinstance(result, Response):
        return json.loads(result.body)
    return result


async def _mcp(request: Request, token: str | None):
    state = request.app.state
    if tokens.check(state.conn, ("mcp",), token) is None:
        return JSONResponse({"detail": "Jeton du connecteur absent, faux ou révoqué."}, status_code=401)
    try:
        message = json.loads(await request.body())
    except ValueError:
        return JSONResponse({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "JSON illisible."}},
                            status_code=400)
    reply = await mcp.handle(message, lambda path, params: _mcp_read(request.app, path, params), VERSION,
                             _public_url(state))
    if reply is None:
        return Response(status_code=202)
    return JSONResponse(reply)


@app.post("/mcp/{token}")
async def mcp_secret_url(request: Request, token: str):
    return await _mcp(request, token)


@app.post("/mcp")
async def mcp_bearer(request: Request):
    return await _mcp(request, tokens.bearer(request.headers.get("authorization")))


# Pas de flux serveur (SSE) ni de session à fermer : le client se contente des réponses JSON.
@app.api_route("/mcp/{token}", methods=["GET", "DELETE"])
@app.api_route("/mcp", methods=["GET", "DELETE"])
async def mcp_no_stream():
    return Response(status_code=405, headers={"Allow": "POST"})

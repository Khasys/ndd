import asyncio
import json
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable

import httpx

from . import alerts, states
from .config import Settings
from .rdap import RdapClient, clean_domain, tld_of

log = logging.getLogger(__name__)


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def parse_iso(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


class DuplicateDomain(ValueError):
    pass


class Checker:
    def __init__(
        self,
        conn: sqlite3.Connection,
        settings: Settings,
        rdap: RdapClient,
        http: httpx.AsyncClient,
        notify: Callable[[str], Awaitable[str | None]] | None = None,
    ):
        self.conn = conn
        self.settings = settings
        self.rdap = rdap
        self.http = http
        self.notify = notify or self._notify_from_env
        self.delays = states.load_delays()
        self._running: set[int] = set()
        self._bulk: asyncio.Task | None = None
        # FrWatcher, posé par main : les .fr passent par sa file (plafond par minute, pause Afnic),
        # un seul chemin vers l'Afnic. Absent ou désactivé : requête RDAP directe.
        self.fr = None

    async def _notify_from_env(self, text: str) -> str | None:
        return await alerts.send_telegram(self.http, self.settings.telegram_token, self.settings.telegram_chat_id, text)

    async def add(self, raw: str) -> sqlite3.Row:
        """Ajoute et vérifie tout de suite. Lève InvalidDomain ou DuplicateDomain."""
        name = clean_domain(raw)
        try:
            cur = self.conn.execute("INSERT INTO domains (name, added_at) VALUES (?, ?)", (name, iso(utcnow())))
        except sqlite3.IntegrityError:
            raise DuplicateDomain(f"{name} est déjà surveillé.") from None
        await self.check(cur.lastrowid)
        return self.conn.execute("SELECT * FROM domains WHERE id = ?", (cur.lastrowid,)).fetchone()

    def release_estimate(self, row: sqlite3.Row, now: datetime | None = None) -> datetime | None:
        release = states.estimate_release(
            row["state"],
            tld_of(row["name"]),
            parse_iso(row["expires_at"]),
            parse_iso(row["state_since"]),
            now or utcnow(),
            self.delays,
        )
        drop = states.drop_time(tld_of(row["name"]), self.delays)
        if release and drop:
            release = release.replace(hour=drop[0], minute=drop[1], second=0, microsecond=0)
        return release

    link_base = None  # adresse publique de l'appli (fonction), pour le lien vers la fiche

    def _link(self, name: str) -> str:
        base = (self.link_base() if self.link_base else "") or self.settings.app_url
        return f"{base}/?fiche={name}" if base else ""

    def _next_check(self, state: str | None, now: datetime) -> datetime:
        return now + timedelta(seconds=states.check_interval(state, self.settings.intervals))

    def _record(self, domain_id: int, now: datetime, field: str, old, new) -> None:
        self.conn.execute(
            "INSERT INTO history (domain_id, at, field, old_value, new_value) VALUES (?, ?, ?, ?, ?)",
            (domain_id, iso(now), field, old, new),
        )

    async def check(self, domain_id: int) -> None:
        if domain_id in self._running:
            return
        self._running.add(domain_id)
        try:
            await self._check(domain_id)
        finally:
            self._running.discard(domain_id)

    async def _check(self, domain_id: int) -> None:
        row = self.conn.execute("SELECT * FROM domains WHERE id = ?", (domain_id,)).fetchone()
        if row is None:
            return
        fr = self.fr if self.fr is not None and tld_of(row["name"]) == "fr" and self.fr.enabled else None
        if fr is not None:
            hold = fr.hold_until(utcnow())
            result = None if hold else await fr.lookup(row["name"], "domaines")
            if result is None:
                # Afnic en pause ou plafond de la minute atteint : le domaine garde son dernier état,
                # vérification suivante à la reprise.
                hold = hold or fr.paused_until(utcnow()) or utcnow() + timedelta(hours=1)
                self.conn.execute("UPDATE domains SET next_check_at = ? WHERE id = ?", (iso(hold), domain_id))
                return
        else:
            result = await self.rdap.lookup(row["name"])
        now = utcnow()
        # Le domaine a pu être supprimé pendant la requête.
        if self.conn.execute("SELECT 1 FROM domains WHERE id = ?", (domain_id,)).fetchone() is None:
            return

        if result.error:
            log.info("%s : %s", row["name"], result.error)
            self.conn.execute(
                "UPDATE domains SET check_error = ?, last_checked_at = ?, next_check_at = ? WHERE id = ?",
                (result.error, iso(now), iso(self._next_check(row["state"], now)), domain_id),
            )
            return

        info = result.info
        if result.available:
            new_state = states.AVAILABLE
            values = {
                "epp_statuses": "[]",
                "registered_at": None,
                "updated_at": None,
                "expires_at": None,
                "registrar": None,
                "nameservers": "[]",
            }
        else:
            new_state = states.compute_state(info.statuses, info.expires_at, now, self.settings.expiring_days,
                                             tld_of(row["name"]))
            values = {
                "epp_statuses": json.dumps(info.statuses),
                "registered_at": iso(info.registered_at),
                "updated_at": iso(info.updated_at),
                "expires_at": iso(info.expires_at),
                "registrar": info.registrar,
                "nameservers": json.dumps(info.nameservers),
            }

        first_check = row["state"] is None
        state_changed = row["state"] != new_state
        state_since = row["state_since"]
        if state_changed:
            since = now
            # Au premier check d'un domaine déjà en fin de vie, le dernier changement
            # côté registre date au mieux l'entrée dans cette phase. Sauf s'il précède l'échéance
            # (modification ancienne, sans rapport) : on garde le moment où on voit l'état, comme
            # drops.apply, sinon la libération estimée serait déjà passée.
            if first_check and info and info.last_changed_at and new_state in (states.REDEMPTION, states.PENDING_DELETE) and (
                    info.expires_at is None or info.last_changed_at >= info.expires_at):
                since = min(info.last_changed_at, now)
            state_since = iso(since)

        self.conn.execute("BEGIN")
        try:
            if not first_check:
                if state_changed:
                    self._record(domain_id, now, "state", row["state"], new_state)
                if row["registrar"] != values["registrar"]:
                    self._record(domain_id, now, "registrar", row["registrar"], values["registrar"])
                if row["nameservers"] != values["nameservers"]:
                    self._record(domain_id, now, "nameservers", row["nameservers"], values["nameservers"])
                if row["expires_at"] != values["expires_at"]:
                    self._record(domain_id, now, "expires_at", row["expires_at"], values["expires_at"])
            self.conn.execute(
                """UPDATE domains SET state = ?, state_since = ?, check_error = NULL,
                   epp_statuses = ?, registered_at = ?, updated_at = ?, expires_at = ?,
                   registrar = ?, nameservers = ?, last_checked_at = ?, last_success_at = ?,
                   next_check_at = ?, source = ? WHERE id = ?""",
                (
                    new_state,
                    state_since,
                    values["epp_statuses"],
                    values["registered_at"],
                    values["updated_at"],
                    values["expires_at"],
                    values["registrar"],
                    values["nameservers"],
                    iso(now),
                    iso(now),
                    iso(self._next_check(new_state, now)),
                    result.source,
                    domain_id,
                ),
            )
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

        # Un passage à « disponible » vide le registrar : l'alerte d'état suffit.
        registrar_changed = bool(row["registrar"] and values["registrar"] and row["registrar"] != values["registrar"])
        if not first_check and (state_changed or registrar_changed):
            updated = self.conn.execute("SELECT * FROM domains WHERE id = ?", (domain_id,)).fetchone()
            text = alerts.format_change(
                row["name"],
                row["state"],
                new_state,
                row["registrar"] if registrar_changed else None,
                values["registrar"] if registrar_changed else None,
                self.release_estimate(updated, now),
                self._link(row["name"]),
                unconfirmed=result.unconfirmed,
            )
            await self.notify(text)

    async def run_due(self) -> None:
        now = iso(utcnow())
        due = self.conn.execute(
            "SELECT id FROM domains WHERE next_check_at IS NULL OR next_check_at <= ? ORDER BY next_check_at",
            (now,),
        ).fetchall()
        for row in due:
            await self.check(row["id"])

    def check_all(self) -> bool:
        """Lance une vérification de tous les domaines en tâche de fond. False si déjà en cours."""
        if self._bulk and not self._bulk.done():
            return False

        async def run() -> None:
            ids = [r["id"] for r in self.conn.execute("SELECT id FROM domains ORDER BY name").fetchall()]
            for domain_id in ids:
                await self.check(domain_id)

        self._bulk = asyncio.create_task(run())
        return True

    @property
    def bulk_running(self) -> bool:
        return bool(self._bulk and not self._bulk.done())

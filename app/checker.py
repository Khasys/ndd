import asyncio
import json
import logging
import sqlite3
from datetime import datetime, timedelta, timezone

import httpx

from . import alerts, states
from .config import Settings
from .rdap import RdapClient, tld_of

log = logging.getLogger(__name__)


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def parse_iso(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


class Checker:
    def __init__(self, conn: sqlite3.Connection, settings: Settings, rdap: RdapClient, http: httpx.AsyncClient):
        self.conn = conn
        self.settings = settings
        self.rdap = rdap
        self.http = http
        self.delays = states.load_delays()
        self._running: set[int] = set()
        self._bulk: asyncio.Task | None = None

    def release_estimate(self, row: sqlite3.Row, now: datetime | None = None) -> datetime | None:
        return states.estimate_release(
            row["state"],
            tld_of(row["name"]),
            parse_iso(row["expires_at"]),
            parse_iso(row["state_since"]),
            now or utcnow(),
            self.delays,
        )

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
            new_state = states.compute_state(info.statuses, info.expires_at, now, self.settings.expiring_days)
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
            # côté registre date au mieux l'entrée dans cette phase.
            if first_check and info and info.last_changed_at and new_state in (states.REDEMPTION, states.PENDING_DELETE):
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
                self.settings.app_url,
            )
            await alerts.send_telegram(self.http, self.settings.telegram_token, self.settings.telegram_chat_id, text)

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

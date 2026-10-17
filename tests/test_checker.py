import asyncio
import json
from datetime import datetime, timedelta
from pathlib import Path

import httpx
import pytest

from app import db, states
from app.checker import Checker, parse_iso
from app.config import Settings
from app.rdap import RdapClient

FIXTURES = Path(__file__).parent / "fixtures"
INTERVALS = {
    "active": 86400,
    "expiring": 21600,
    "expired": 21600,
    "redemption": 3600,
    "pending_delete": 300,
    "available": 86400,
    "unknown": 3600,
}


def make_settings(tmp_path: Path) -> Settings:
    return Settings(
        data_dir=tmp_path,
        app_url="http://nas:8080",
        username="moi",
        password_hash="x",
        secret_key="s",
        session_max_age=3600,
        cookie_secure=False,
        login_max_failures=5,
        login_lock_seconds=900,
        intervals=INTERVALS,
        expiring_days=30,
        rdap_min_delay=0,
        rdap_timeout=5,
        rdap_retries=2,
        rdap_backoff=0,
        rdap_max_retry_after=60,
        whois_min_delay=0,
        telegram_token="t",
        telegram_chat_id="c",
    )


class Registry:
    """Faux serveur RDAP : chaque domaine renvoie la prochaine réponse de sa file."""

    def __init__(self):
        self.responses: dict[str, list] = {}
        self.calls: list[str] = []
        self.whois_answers: dict[str, object] = {}

    async def whois(self, server: str, name: str, timeout: float) -> str:
        self.calls.append(f"whois:{server}:{name}")
        if server == "whois.iana.org":
            return (FIXTURES / f"whois_iana_{name}.txt").read_text() if (FIXTURES / f"whois_iana_{name}.txt").exists() else "domain: X\n"
        answer = self.whois_answers.get(name)
        if isinstance(answer, Exception):
            raise answer
        return answer or ""

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == "data.iana.org":
            return httpx.Response(200, content=(FIXTURES / "bootstrap.json").read_bytes())
        domain = request.url.path.rsplit("/", 1)[-1]
        self.calls.append(domain)
        queue = self.responses.get(domain) or [httpx.Response(404)]
        response = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(response, Exception):
            raise response
        return response


def rdap_json(name: str) -> httpx.Response:
    return httpx.Response(200, content=(FIXTURES / name).read_bytes())


@pytest.fixture
def env(tmp_path):
    registry = Registry()
    transport = httpx.MockTransport(registry.handler)
    settings = make_settings(tmp_path)
    conn = db.connect(settings.db_path)
    db.migrate(conn, settings.backup_dir)
    rdap = RdapClient(
        tmp_path / "bootstrap.json", 0, 5, 2, 0, 60,
        transport=transport, whois_min_delay=0, whois_query=registry.whois,
    )
    sent: list[str] = []

    async def tg_handler(request):
        sent.append(json.loads(request.content)["text"])
        return httpx.Response(200, json={"ok": True})

    http = httpx.AsyncClient(transport=httpx.MockTransport(tg_handler))
    checker = Checker(conn, settings, rdap, http)
    yield registry, conn, checker, sent
    conn.close()


def add(conn, name):
    return conn.execute("INSERT INTO domains (name, added_at) VALUES (?, '2026-10-12T00:00:00+00:00')", (name,)).lastrowid


def row(conn, domain_id):
    return conn.execute("SELECT * FROM domains WHERE id = ?", (domain_id,)).fetchone()


def run(coro):
    return asyncio.run(coro)


def test_first_check_fills_fields_and_schedules(env):
    registry, conn, checker, sent = env
    registry.responses["exemple-test.com"] = [rdap_json("com_active.json")]
    domain_id = add(conn, "exemple-test.com")
    run(checker.check(domain_id))
    r = row(conn, domain_id)
    assert r["state"] == states.ACTIVE
    assert r["registrar"] == "Exemple Registrar SAS"
    assert json.loads(r["nameservers"]) == ["ns1.exemple-dns.net", "ns2.exemple-dns.net"]
    gap = parse_iso(r["next_check_at"]) - parse_iso(r["last_checked_at"])
    assert gap == timedelta(days=1)
    assert conn.execute("SELECT COUNT(*) FROM history").fetchone()[0] == 0
    assert sent == []


def test_404_means_available(env):
    registry, conn, checker, _ = env
    domain_id = add(conn, "libre-test.com")
    run(checker.check(domain_id))
    assert row(conn, domain_id)["state"] == states.AVAILABLE


def test_429_never_concludes_available(env):
    registry, conn, checker, _ = env
    registry.responses["bloque-test.com"] = [httpx.Response(429)]
    domain_id = add(conn, "bloque-test.com")
    run(checker.check(domain_id))
    r = row(conn, domain_id)
    assert r["state"] is None
    assert "429" in r["check_error"]
    assert registry.calls.count("bloque-test.com") == 3


def test_network_error_keeps_last_known_state(env):
    registry, conn, checker, sent = env
    registry.responses["exemple-test.com"] = [rdap_json("com_redemption.json")]
    domain_id = add(conn, "exemple-test.com")
    run(checker.check(domain_id))
    registry.responses["exemple-test.com"] = [httpx.ConnectError("boom")]
    run(checker.check(domain_id))
    r = row(conn, domain_id)
    assert r["state"] == states.REDEMPTION
    assert r["check_error"].startswith("Erreur réseau")
    gap = parse_iso(r["next_check_at"]) - parse_iso(r["last_checked_at"])
    assert gap == timedelta(hours=1)
    assert sent == []


def test_retry_after_too_long_gives_up_immediately(env):
    registry, conn, checker, _ = env
    registry.responses["lent-test.com"] = [httpx.Response(429, headers={"Retry-After": "3600"})]
    domain_id = add(conn, "lent-test.com")
    run(checker.check(domain_id))
    assert "reprise dans 3600 s" in row(conn, domain_id)["check_error"]
    assert registry.calls.count("lent-test.com") == 1


def test_transition_records_history_and_alerts(env):
    registry, conn, checker, sent = env
    registry.responses["exemple-test.com"] = [rdap_json("com_redemption.json")]
    domain_id = add(conn, "exemple-test.com")
    run(checker.check(domain_id))
    first = row(conn, domain_id)
    # Premier check en rédemption : la phase est datée par le « last changed » du registre.
    assert first["state_since"] == "2026-10-01T12:00:00+00:00"

    registry.responses["exemple-test.com"] = [rdap_json("com_pending_delete.json")]
    run(checker.check(domain_id))
    r = row(conn, domain_id)
    assert r["state"] == states.PENDING_DELETE
    gap = parse_iso(r["next_check_at"]) - parse_iso(r["last_checked_at"])
    assert gap == timedelta(minutes=5)

    history = conn.execute("SELECT field, old_value, new_value FROM history").fetchall()
    assert [tuple(h) for h in history] == [("state", "redemption", "pending_delete")]
    assert len(sent) == 1
    lines = sent[0].splitlines()
    assert lines[0] == "exemple-test.com"
    assert lines[1] == "En rédemption → Suppression imminente"
    assert lines[2].startswith("Libération estimée : ~")
    assert lines[3] == "http://nas:8080/?fiche=exemple-test.com"


def test_registrar_change_alerts(env):
    registry, conn, checker, sent = env
    registry.responses["exemple-test.com"] = [rdap_json("com_active.json")]
    domain_id = add(conn, "exemple-test.com")
    run(checker.check(domain_id))
    data = json.loads((FIXTURES / "com_active.json").read_text())
    data["entities"][0]["vcardArray"][1][1][3] = "Autre Registrar Inc."
    registry.responses["exemple-test.com"] = [httpx.Response(200, json=data)]
    run(checker.check(domain_id))
    assert len(sent) == 1
    assert "Registrar : Exemple Registrar SAS → Autre Registrar Inc." in sent[0]
    fields = [h["field"] for h in conn.execute("SELECT field FROM history")]
    assert fields == ["registrar"]


def test_run_due_only_checks_due_domains(env):
    registry, conn, checker, _ = env
    due = add(conn, "du-test.com")
    later = add(conn, "plus-tard-test.com")
    future = (datetime.now().astimezone() + timedelta(hours=2)).isoformat()
    conn.execute("UPDATE domains SET next_check_at = ? WHERE id = ?", (future, later))
    run(checker.run_due())
    assert registry.calls == ["du-test.com"]
    assert row(conn, due)["state"] == states.AVAILABLE
    assert row(conn, later)["state"] is None


def test_unknown_tld_is_an_error_not_available(env):
    registry, conn, checker, _ = env
    domain_id = add(conn, "exemple-test.zz")
    run(checker.check(domain_id))
    r = row(conn, domain_id)
    assert r["state"] is None
    assert r["check_error"] == "Ni RDAP ni WHOIS publié pour .zz"


def test_tld_missing_from_bootstrap_uses_extra_server(env):
    registry, conn, checker, _ = env
    registry.responses["exemple-test.so"] = [rdap_json("com_active.json")]
    domain_id = add(conn, "exemple-test.so")
    run(checker.check(domain_id))
    assert row(conn, domain_id)["state"] == states.ACTIVE


def test_whois_fallback_for_tld_without_rdap(env):
    registry, conn, checker, _ = env
    registry.whois_answers["google.co"] = (FIXTURES / "whois_co_found.txt").read_text()
    registry.whois_answers["libre-test.co"] = (FIXTURES / "whois_co_free.txt").read_text()
    found = add(conn, "google.co")
    free = add(conn, "libre-test.co")
    run(checker.check(found))
    run(checker.check(free))
    r = row(conn, found)
    assert r["state"] == states.ACTIVE
    assert r["source"] == "whois"
    assert r["registrar"] == "MarkMonitor, Inc."
    assert row(conn, free)["state"] == states.AVAILABLE
    assert "whois:whois.registry.co:google.co" in registry.calls


def test_unreadable_whois_is_an_error(env):
    registry, conn, checker, _ = env
    registry.whois_answers["bizarre-test.co"] = "Bienvenue sur notre service.\n"
    domain_id = add(conn, "bizarre-test.co")
    run(checker.check(domain_id))
    r = row(conn, domain_id)
    assert r["state"] is None
    assert r["check_error"] == "Réponse WHOIS non reconnue"

import asyncio
import json
import time
from datetime import datetime, timedelta, timezone
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


def test_long_retry_after_does_not_block_the_next_lookup(env):
    registry, conn, checker, _ = env
    registry.responses["lent-test.com"] = [httpx.Response(429, headers={"Retry-After": "3600"})]
    run(checker.check(add(conn, "lent-test.com")))
    # Même registre, autre domaine : erreur tout de suite, sans dormir une heure dans run_due.
    other = add(conn, "autre-test.com")
    started = time.monotonic()
    run(checker.check(other))
    assert time.monotonic() - started < 2
    assert "reprise dans" in row(conn, other)["check_error"]
    assert "autre-test.com" not in registry.calls


def test_truncated_bootstrap_cache_is_replaced(env):
    registry, conn, checker, _ = env
    cache = checker.rdap.cache_path
    cache.write_text('{"services": [[["com"], ["https://rdap.verisign.com/com/v1/"')  # écriture coupée
    domain_id = add(conn, "libre-test.com")
    run(checker.check(domain_id))
    assert row(conn, domain_id)["state"] == states.AVAILABLE
    assert json.loads(cache.read_text())["services"]


def test_stale_change_date_does_not_put_release_in_the_past(env):
    registry, conn, checker, _ = env
    data = json.loads((FIXTURES / "com_redemption.json").read_text())
    for event in data["events"]:
        if event["eventAction"] == "last changed":
            event["eventDate"] = "2024-01-05T12:00:00Z"  # modification ancienne, sans rapport
    registry.responses["vieux-test.com"] = [httpx.Response(200, json=data)]
    domain_id = add(conn, "vieux-test.com")
    run(checker.check(domain_id))
    r = row(conn, domain_id)
    assert r["state"] == states.REDEMPTION
    assert r["state_since"] == r["last_checked_at"]
    assert checker.release_estimate(r) > datetime.now(timezone.utc)


def test_iana_rate_limit_is_not_remembered_as_no_whois(tmp_path):
    answers = iter([
        "% Too many queries, try again later\n",  # l'IANA limite une fois
        (FIXTURES / "whois_iana_co.txt").read_text(),
        (FIXTURES / "whois_co_free.txt").read_text(),
    ])

    async def whois(server, name, timeout):
        return next(answers)

    rdap = RdapClient(tmp_path / "b.json", 0, 5, 0, 0, 60, transport=httpx.MockTransport(Registry().handler),
                      whois_min_delay=0, whois_query=whois)
    first = run(rdap.lookup("libre-test.co"))
    assert "Limite" in first.error
    second = run(rdap.lookup("libre-test.co"))
    assert second.available and second.source == "whois"


def test_fr_domains_go_through_the_afnic_queue(env, tmp_path):
    """Écran Domaines : un .fr passe par la file de frdata (journal, plafond par minute, pause Afnic)."""
    from app import drops, frdata
    from app.store import Store

    registry, conn, checker, _ = env
    store = Store(conn)
    watcher = drops.DropWatcher(conn, store, tmp_path, checker.rdap, checker.delays)
    fr = frdata.FrWatcher(conn, store, tmp_path, checker.rdap, watcher)
    checker.fr = fr
    registry.responses["exemple-test.fr"] = [rdap_json("fr_active.json")]
    # FrWatcher pas activé : chemin actuel, requête directe, rien dans le journal Afnic.
    domain_id = add(conn, "exemple-test.fr")
    run(checker.check(domain_id))
    state = row(conn, domain_id)["state"]
    assert state in ("active", "expiring")  # la fixture vieillit : l'échéance se rapproche
    assert conn.execute("SELECT COUNT(*) FROM fr_requests").fetchone()[0] == 0
    # Activé : la requête est journalisée comme les autres (source « domaines »).
    store.set("fr_enabled", "1")
    store.set("fr_rate", "1")
    run(checker.check(domain_id))
    assert [tuple(r) for r in conn.execute("SELECT name, source FROM fr_requests")] == [("exemple-test.fr", "domaines")]
    # Plafond de la minute atteint : pas de requête, vérification repoussée d'une minute.
    calls = len(registry.calls)
    run(checker.check(domain_id))
    assert len(registry.calls) == calls
    assert parse_iso(row(conn, domain_id)["next_check_at"]) - datetime.now(timezone.utc) <= timedelta(minutes=1)
    # Afnic en pause : le domaine garde son état, prochaine vérification à la fin de la pause.
    conn.execute("DELETE FROM fr_requests")
    paused = (datetime.now(timezone.utc) + timedelta(hours=3)).replace(microsecond=0)
    store.set("fr_paused_until", paused.isoformat())
    registry.responses["exemple-test.fr"] = [httpx.Response(404)]
    run(checker.check(domain_id))
    r = row(conn, domain_id)
    assert r["state"] == state and parse_iso(r["next_check_at"]) == paused and len(registry.calls) == calls
    # L'Afnic limite pendant la requête : pause posée par frdata, même traitement.
    store.set("fr_paused_until", "")
    registry.responses["exemple-test.fr"] = [httpx.Response(429)]
    run(checker.check(domain_id))
    r = row(conn, domain_id)
    assert r["state"] == state and fr.paused_until() and parse_iso(r["next_check_at"]) == fr.paused_until()


def test_whois_server_is_kept_in_the_database_for_a_week(tmp_path):
    from app.store import Store

    conn = db.connect(tmp_path / "t.db")
    db.migrate(conn, tmp_path / "backups")
    store = Store(conn)

    def client(registry):
        rdap = RdapClient(tmp_path / "b.json", 0, 5, 0, 0, 60, transport=httpx.MockTransport(registry.handler),
                          whois_min_delay=0, whois_query=registry.whois)
        rdap.whois_store = store
        return rdap

    registry = Registry()
    registry.whois_answers["libre-test.co"] = (FIXTURES / "whois_co_free.txt").read_text()
    assert run(client(registry).lookup("libre-test.co")).available
    saved = json.loads(store.get("whois_servers"))
    assert saved["co"]["server"] == "whois.registry.co" and saved["co"]["at"]
    assert registry.calls.count("whois:whois.iana.org:co") == 1
    # Nouveau client (redémarrage) : le serveur vient de la base, l'IANA n'est pas interrogée.
    registry.calls.clear()
    assert run(client(registry).lookup("libre-test.co")).available
    assert registry.calls == ["whois:whois.registry.co:libre-test.co"]
    # Plus de 7 jours : l'IANA est consultée de nouveau.
    saved["co"]["at"] = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
    store.set("whois_servers", json.dumps(saved))
    registry.calls.clear()
    assert run(client(registry).lookup("libre-test.co")).available
    assert registry.calls[0] == "whois:whois.iana.org:co"
    conn.close()

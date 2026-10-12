import asyncio
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app import bot, calendar_ics, db, digest, signals, states
from app.store import Store
from app.telegram import Telegram
from tests.test_checker import FIXTURES, Registry, add, make_settings, rdap_json, row, run

from app.checker import Checker
from app.rdap import RdapClient


@pytest.fixture
def env(tmp_path):
    registry = Registry()
    settings = make_settings(tmp_path)
    conn = db.connect(settings.db_path)
    db.migrate(conn, settings.backup_dir)
    rdap = RdapClient(tmp_path / "b.json", 0, 5, 2, 0, 60, transport=httpx.MockTransport(registry.handler),
                      whois_min_delay=0, whois_query=registry.whois)
    sent: list[str] = []

    async def notify(text):
        sent.append(text)

    checker = Checker(conn, settings, rdap, httpx.AsyncClient(), notify=notify)
    yield registry, conn, checker, sent
    conn.close()


# Telegram ----------------------------------------------------------------

class FakeTelegramApi:
    def __init__(self):
        self.messages: list[tuple[str, str]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content or b"{}")
        if method == "getMe":
            return httpx.Response(200, json={"ok": True, "result": {"username": "monbot"}})
        if method == "sendMessage":
            self.messages.append((str(body["chat_id"]), body["text"]))
            return httpx.Response(200, json={"ok": True, "result": {}})
        return httpx.Response(404, json={"ok": False, "description": "Not Found"})


def telegram_for(conn, api):
    store = Store(conn)
    store.set("telegram_token", "123:abc")
    return Telegram(store, httpx.AsyncClient(transport=httpx.MockTransport(api.handler))), store


def message(chat, text):
    return {"update_id": 1, "message": {"chat": {"id": chat}, "text": text}}


def test_pairing_binds_the_chat_that_sends_the_code(env):
    _, conn, checker, _ = env
    api = FakeTelegramApi()
    tg, store = telegram_for(conn, api)
    code = tg.start_pairing()
    run(tg.handle_update(message(42, "/start 000000" if code != "000000" else "/start 111111")))
    assert store.get("telegram_chat_id") == ""
    run(tg.handle_update(message(42, f"/start {code}")))
    assert store.get("telegram_chat_id") == "42"
    assert not tg.pairing_active
    assert api.messages[-1][0] == "42"


def test_commands_only_from_paired_chat(env):
    registry, conn, checker, _ = env
    api = FakeTelegramApi()
    tg, store = telegram_for(conn, api)
    store.set("telegram_chat_id", "42")
    tg.handler = bot.build_handler(checker, "v1.0.0")
    run(tg.handle_update(message(99, "/liste")))
    assert api.messages == []
    registry.responses["exemple-test.com"] = [rdap_json("com_active.json")]
    run(tg.handle_update(message(42, "/ajouter https://www.exemple-test.com")))
    assert api.messages[-1][1].startswith("Ajouté : exemple-test.com · Actif")
    run(tg.handle_update(message(42, "/statut")))
    assert "1 domaines surveillés, version v1.0.0" in api.messages[-1][1]
    run(tg.handle_update(message(42, "/supprimer exemple-test.com")))
    assert api.messages[-1][1] == "exemple-test.com supprimé."
    run(tg.handle_update(message(42, "/inconnue")))
    assert api.messages[-1][1].startswith("Commande inconnue")


def test_urgent_lists_only_non_active(env):
    registry, conn, checker, _ = env
    registry.responses["exemple-test.com"] = [rdap_json("com_redemption.json")]
    run(checker.add("exemple-test.com"))
    run(checker.add("libre-test.com"))
    handle = bot.build_handler(checker, "v1")
    text = run(handle("/urgent", ""))
    assert text.splitlines()[0].startswith("libre-test.com · Disponible")
    assert "exemple-test.com · En rédemption" in text


# Résumé ------------------------------------------------------------------

def test_digest_lists_changes_and_upcoming(env):
    registry, conn, checker, _ = env
    registry.responses["exemple-test.com"] = [rdap_json("com_redemption.json")]
    run(checker.add("exemple-test.com"))
    registry.responses["exemple-test.com"] = [rdap_json("com_pending_delete.json")]
    run(checker.check(row(conn, 1)["id"]))
    text = digest.build(checker, datetime.now(timezone.utc), "http://nas:8111")
    assert "exemple-test.com : État En rédemption → Suppression imminente" in text
    assert "Libérations estimées dans les 30 jours" in text
    assert text.endswith("http://nas:8111")


def test_digest_due_once_per_day(env):
    _, conn, _, _ = env
    store = Store(conn)
    monday_10 = datetime(2026, 10, 12, 10, 0)
    assert digest.due(store, monday_10)
    store.set("digest_last_sent", "2026-10-12")
    assert not digest.due(store, monday_10)
    assert not digest.due(store, datetime(2026, 10, 13, 10, 0))
    assert not digest.due(Store(conn, {}), datetime(2026, 10, 19, 8, 0))
    store.set("digest_enabled", "0")
    assert not digest.due(store, datetime(2026, 10, 19, 10, 0))


# Signaux -----------------------------------------------------------------

def test_suspicious_rules():
    assert not signals.suspicious(None)
    assert not signals.suspicious({"signals": ["no_mx"]})
    assert signals.suspicious({"signals": ["parking"]})
    assert signals.suspicious({"signals": ["no_dns", "no_mx"]})
    assert signals.suspicious({"signals": ["web_down", "tls_invalid"]})
    assert not signals.suspicious({"signals": ["web_error"]})


def test_collect_detects_parking_and_dead_site(monkeypatch):
    async def resolve(name, rdtype, timeout):
        return ["1.2.3.4"] if rdtype == "A" else []

    monkeypatch.setattr(signals, "_resolve", resolve)

    def site(request):
        return httpx.Response(200, text="<h1>This domain is for sale</h1>")

    http = httpx.AsyncClient(transport=httpx.MockTransport(site))
    result = asyncio.run(signals.collect("exemple.com", ["ns1.sedoparking.com"], http))
    assert result["signals"] == ["parking", "no_mx"]

    def down(request):
        raise httpx.ConnectError("refused")

    http = httpx.AsyncClient(transport=httpx.MockTransport(down))
    result = asyncio.run(signals.collect("exemple.com", [], http))
    assert "web_down" in result["signals"]


# Calendrier --------------------------------------------------------------

def test_calendar_has_expiry_and_timed_release(env):
    registry, conn, checker, _ = env
    registry.responses["exemple-test.com"] = [rdap_json("com_pending_delete.json")]
    run(checker.add("exemple-test.com"))
    ics = calendar_ics.build(checker, datetime.now(timezone.utc), "http://nas:8111")
    assert ics.startswith("BEGIN:VCALENDAR\r\n")
    assert "SUMMARY:Expiration exemple-test.com" in ics
    release = [l for l in ics.split("\r\n") if l.startswith("DTSTART:")]
    assert release and release[0].endswith("T120000Z")


def test_release_estimate_uses_registry_drop_time(env):
    registry, conn, checker, _ = env
    registry.responses["exemple-test.com"] = [rdap_json("com_redemption.json")]
    r = run(checker.add("exemple-test.com"))
    release = checker.release_estimate(r)
    assert (release.hour, release.minute) == (12, 0)
    assert states.drop_time("fr", checker.delays) is None


def test_resolver_outage_is_not_an_abandon_signal(env, monkeypatch):
    _, conn, _, _ = env

    async def broken(name, rdtype, timeout):
        raise signals.ResolverUnavailable("Timeout")

    monkeypatch.setattr(signals, "_resolve", broken)
    domain_id = add(conn, "exemple-test.com")
    conn.execute("UPDATE domains SET state = 'active', signals = ? WHERE id = ?", (json.dumps({"signals": []}), domain_id))
    r = row(conn, domain_id)
    assert run(signals.update_one(conn, r, httpx.AsyncClient())) is None
    assert json.loads(row(conn, domain_id)["signals"]) == {"signals": []}

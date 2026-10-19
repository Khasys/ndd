import asyncio
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app import alerts, bot, calendar_ics, db, digest, signals, states
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


def test_telegram_dates_follow_the_container_time_zone(monkeypatch):
    monkeypatch.setenv("TZ", "Europe/Paris")
    release = datetime(2026, 10, 30, 23, 30, tzinfo=timezone.utc)  # 00:30 le 31 à Paris
    text = alerts.format_change("velo.fr", states.REDEMPTION, states.PENDING_DELETE, None, None, release, "")
    assert "Libération estimée : ~31/10/2026" in text


# File de rejeu Telegram ---------------------------------------------------

def test_failed_telegram_messages_are_queued_then_replayed_together(env, caplog):
    from app import telegram as tg_module

    _, conn, _, _ = env
    api = FakeTelegramApi()
    api.status = 200

    def handler(request):
        if request.url.path.endswith("/sendMessage") and api.status != 200:
            return httpx.Response(api.status, json={"ok": False, "description": "Too Many Requests"})
        return api.handler(request)

    store = Store(conn)
    store.set("telegram_token", "123:abc")
    store.set("telegram_chat_id", "42")
    tg = Telegram(store, httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    api.status = 429
    # Échec passager : le message attend en file, l'appelant n'a pas à le renvoyer.
    assert run(tg.send("Première alerte")) is None
    assert run(tg.send("Deuxième alerte")) is None
    # Message de test : erreur tout de suite, jamais rejoué.
    assert run(tg.send("test", retry=False)).startswith("Telegram a refusé")
    # Refus définitif (400) : pas de rejeu non plus.
    api.status = 400
    assert run(tg.send("mauvais")).startswith("Telegram a refusé")
    assert tg.pending() == 2
    # Toujours en panne : essais comptés, prochain essai repoussé (5 min, puis doublé jusqu'à 6 h).
    api.status = 500
    assert run(tg.flush()) == 0
    rows = conn.execute("SELECT attempts, next_try_at FROM telegram_outbox").fetchall()
    assert [r["attempts"] for r in rows] == [1, 1]
    delay = datetime.fromisoformat(rows[0]["next_try_at"]) - datetime.now(timezone.utc)
    assert timedelta(minutes=4) < delay <= timedelta(minutes=5)
    conn.execute("UPDATE telegram_outbox SET attempts = 10, next_try_at = created_at")
    assert run(tg.flush()) == 0
    delay = datetime.fromisoformat(conn.execute("SELECT next_try_at FROM telegram_outbox").fetchone()[0]) - datetime.now(timezone.utc)
    assert timedelta(hours=5, minutes=59) < delay <= timedelta(hours=6)
    # Telegram répond : les deux messages partent en un seul, la file se vide.
    api.status = 200
    conn.execute("UPDATE telegram_outbox SET next_try_at = created_at")
    assert run(tg.flush()) == 2
    assert api.messages[-1] == ("42", "Première alerte\n\nDeuxième alerte") and tg.pending() == 0
    # Plus de 48 h : abandonné avec une ligne de journal.
    old = (datetime.now(timezone.utc) - timedelta(hours=49)).isoformat()
    conn.execute("INSERT INTO telegram_outbox (text, created_at, attempts, next_try_at, last_error) VALUES ('vieux', ?, 9, ?, 'x')", (old, old))
    with caplog.at_level("ERROR", logger="app.telegram"):
        assert run(tg.flush()) == 0
    assert "abandonné après 9 essais" in caplog.text and tg.pending() == 0
    assert tg_module.MAX_TEXT == 4000


def test_calendar_lines_are_folded_at_75_octets(env):
    registry, conn, checker, _ = env
    name = "tres-long-nom-de-domaine-pour-le-test-du-pliage-des-lignes.com"
    registry.responses[name] = [rdap_json("com_redemption.json")]
    run(checker.add(name))
    ics = calendar_ics.build(checker, datetime.now(timezone.utc), "https://domaines.exemple.fr/une/adresse/assez/longue")
    lines = ics.split("\r\n")
    assert all(len(line.encode()) <= 75 for line in lines)
    assert any(line.startswith(" ") for line in lines)
    unfolded = ics.replace("\r\n ", "")
    assert f"SUMMARY:Libération estimée {name}" in unfolded
    assert "DESCRIPTION:Estimation calculée depuis le statut et les délais du registre\\, pas une date garantie.\\nhttps://domaines.exemple.fr/une/adresse/assez/longue" in unfolded
    # Un caractère accentué n'est jamais coupé en deux.
    parts = calendar_ics.fold("DESCRIPTION:" + "é" * 80)
    assert all(len(p.encode()) <= 75 for p in parts) and "".join(p.lstrip(" ") for p in parts) == "DESCRIPTION:" + "é" * 80

"""Gandi : lecture des réponses, effet sur l'état des noms, passe de nuit, vérification, routes."""

import asyncio
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app import explore, gandi, states
from app.checker import iso, utcnow
from app.rdap import DomainInfo, LookupResult
from tests.conftest import FIXTURES
from tests.test_explore import FakeRdap, env, zone  # noqa: F401  (fixture env)
from tests.test_app import _opinion, client_factory, logged  # noqa: F401  (fixtures)

STANDARD = {"duration_unit": "y", "min_duration": 1, "max_duration": 10, "price_before_taxes": 11,
            "price_after_taxes": 13.2, "discount": False, "options": {"period": "golive"}}
# D'après la documentation de Gandi (pas encore vu en vrai) : premium disponible, lignes de prix de type « premium ».
PREMIUM = {"duration_unit": "y", "min_duration": 1, "max_duration": 1, "price_before_taxes": 732,
           "price_after_taxes": 878.4, "discount": False, "type": "premium"}


def answer(status: str, prices=None, name: str = "x.com") -> dict:
    product = {"name": name, "status": status, "process": "create", "taxes": [{"name": "vat", "type": "service", "rate": 20}]}
    if prices is not None:
        product["prices"] = prices
    return {"grid": "A", "currency": "EUR", "products": [product]}


def client_for(ex, handler=None):
    handler = handler or (lambda r: httpx.Response(200, json=answer("available", [STANDARD], r.url.params["name"])))
    client = gandi.Gandi(ex.fr.store, http_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)), conn=ex.conn)
    client.store.set("gandi_pat", "pat_test")
    client.on_change = ex.touch
    return client


# --- Lecture -------------------------------------------------------------------------------------

def test_read_real_answers_from_the_nas():
    samples = json.loads((FIXTURES / "gandi_samples.json").read_text())["samples"]
    for name in ("menu.co", "voyage.fr", "maison.com", "ai.io", "chat.ai"):
        assert gandi.read(samples[name]) == {"kind": "unavailable", "status": "unavailable", "price": None,
                                             "price_ht": None, "currency": "EUR"}, name
    assert gandi.read(samples["qzkwyxv.com"]) == {"kind": "available", "status": "available", "price": 13.2,
                                                  "price_ht": 11.0, "currency": "EUR"}


def test_read_premium_and_other_statuses():
    assert gandi.read(answer("available", [PREMIUM]))["kind"] == "premium"
    assert gandi.read(answer("available", [PREMIUM]))["price"] == 878.4
    # Plusieurs lignes : la première d'une durée minimale d'un an, la ligne premium pour un premium.
    lines = [{**STANDARD, "min_duration": 2}, {**PREMIUM, "min_duration": 2, "price_after_taxes": 2000}, PREMIUM]
    assert gandi.read(answer("available", lines))["price"] == 878.4
    assert gandi.read(answer("unavailable_premium")) == {"kind": "premium", "status": "unavailable_premium", "price": None,
                                                         "price_ht": None, "currency": "EUR"}
    kinds = {s: gandi.read(answer(s))["kind"] for s in (
        "available_reserved", "reserved_corporate", "unavailable_restricted", "available_preorder", "pending",
        "error_invalid", "error_refused", "error_timeout", "error_unknown", "error_eoi", "inconnu")}
    assert kinds == {"available_reserved": "reserved", "reserved_corporate": "reserved", "unavailable_restricted": "restricted",
                     "available_preorder": "available", "pending": "error", "error_invalid": "error", "error_refused": "error",
                     "error_timeout": "error", "error_unknown": "error", "error_eoi": "error", "inconnu": "error"}
    assert gandi.read({"currency": "EUR", "products": []})["kind"] == "error"


def test_gandi_code_never_touches_registered_or_listed_names():
    assert explore.gandi_code(explore.REGISTERED, "available") == explore.REGISTERED
    assert explore.gandi_code(explore.COMING, "unavailable") == explore.COMING
    assert explore.gandi_code(explore.RESERVED, "available", listed=True) == explore.RESERVED
    assert explore.gandi_code(explore.RESERVED, "available") == explore.FREE  # « court réservé » estimé : Gandi tranche
    assert explore.gandi_code(explore.UNCONFIRMED, "restricted") == explore.RESERVED
    assert explore.gandi_code(explore.ABSENT, "error") == explore.ABSENT


# --- Effet sur l'état des noms --------------------------------------------------------------------

def test_gandi_answers_set_the_state_of_names_thought_free(env):
    zone(env, [], ["maison"])
    env.fr.store.set("premium_off", "[]")  # .com : 3 caractères en premium probable, pour l'exemple
    env.invalidate("com")
    u = env.universe
    data = env.status_bytes("com")
    assert data[u.index("abd")] == explore.PREMIUM and data[u.index("jardin")] == explore.ABSENT
    env.facets("com", premium=True)  # compteurs en mémoire : corrigés de ±1 par touch
    client = client_for(env)
    now = utcnow()
    client.record("abd.com", gandi.read(answer("available", [STANDARD])), now)
    client.record("abe.com", gandi.read(answer("available", [PREMIUM])), now)
    client.record("jardin.com", gandi.read(answer("unavailable")), now)
    client.record("maison.com", gandi.read(answer("available", [STANDARD])), now)
    client.record("velo.com", gandi.read(answer("error_timeout")), now)
    data = env.status_bytes("com")
    assert data[u.index("abd")] == explore.FREE and data[u.index("abe")] == explore.PREMIUM
    assert data[u.index("jardin")] == explore.RESERVED and data[u.index("maison")] == explore.REGISTERED
    assert data[u.index("velo")] == explore.ABSENT
    touched, counts = bytes(data), env.facets("com", premium=True)
    env.invalidate("com")
    assert bytes(env.status_bytes("com")) == touched and env.facets("com", premium=True) == counts

    def one(label):
        return env.search("com", q=label, mode="exact", status="", premium=True)["items"][0]

    abd, abe, jardin, velo = one("abd"), one("abe"), one("jardin"), one("velo")
    assert abd["status_label"] == "Libre (Gandi)" and abd["premium_price"] is None and abd["gandi"]["price"] == 13.2
    assert abe["status_label"] == "Premium (Gandi)" and abe["premium_price"] == 878 and abe["gandi"]["kind"] == "premium"
    assert jardin["reserved"] == ["Indisponible chez Gandi"]
    assert velo["status_label"] == explore.ABSENT_ZONE and velo["gandi"]["error"] == "le registre n'a pas répondu à temps"
    # Une erreur ne remplace pas un résultat connu : le prix reste, l'erreur est notée.
    client.record("abd.com", gandi.read(answer("error_timeout")), now)
    assert one("abd")["status"] == explore.FREE and one("abd")["gandi"]["error"] and one("abd")["gandi"]["price"] == 13.2
    # Masquer les premiums (premium=False) ; prix d'achat au plus : seulement les noms vérifiés.
    assert env.search("com", q="abe", mode="exact", status="bookable")["items"] == []
    cheap = env.search("com", status="bookable", premium=True, gprice=20)["items"]
    assert [i["label"] for i in cheap] == ["abd"] and env.facets("com", premium=True, gprice=20)["bookable"] == 1
    ranked = env.rank("com", status="bookable", premium=True, gprice=1000, limit=10, sort="score")["items"]
    assert {i["label"] for i in ranked} == {"abd", "abe"}
    # Le nom change d'état après la vérification (ici libéré, vérifié en RDAP) : la réponse ne compte plus,
    # elle attend la passe suivante.
    env.drops.record("com", ["jardin"], [], now)
    later = iso(now + timedelta(minutes=5))
    env.conn.execute("UPDATE drops SET state = 'available', state_since = ?, available_since = ? WHERE name = 'jardin.com'",
                     (later, later))
    env.touch("jardin.com")
    assert one("jardin")["status"] == explore.FREE and one("jardin")["gandi"]["stale"] is True
    timeline = {t["kind"]: t["label"] for t in env.fiche("abe.com")["timeline"]}
    assert timeline["gandi"] == "Vérifié chez Gandi : premium, 878 €"
    assert env.fiche("abe.com")["gandi"]["kind"] == "premium"


def test_verify_asks_gandi_for_names_not_taken(env):
    zone(env, [], ["maison"])
    calls = []

    def handler(request):
        name = request.url.params["name"]
        calls.append(name)
        if name == "abg.com":
            raise httpx.ConnectError("coupé")
        return httpx.Response(200, json=answer("available", [PREMIUM], name))

    env.gandi = client_for(env, handler)
    expires = datetime.now(timezone.utc) + timedelta(days=200)
    env.drops.rdap = FakeRdap({"abd.com": LookupResult(available=True), "abg.com": LookupResult(available=True),
                               "maison.com": LookupResult(found=True, info=DomainInfo(statuses=["active"], expires_at=expires))})
    item = asyncio.run(env.verify("abd.com"))
    assert calls == ["abd.com"] and item["status"] == explore.PREMIUM and item["premium_price"] == 878
    assert item["gandi"]["kind"] == "premium" and item["gandi_note"] is None
    asyncio.run(env.verify("maison.com"))
    assert calls == ["abd.com"]  # enregistré : Gandi n'est pas interrogé
    item = asyncio.run(env.verify("abg.com"))
    assert item["status"] == explore.FREE and "injoignable" in item["gandi_note"]


# --- Passe de nuit ----------------------------------------------------------------------------------

def make_pass(ex, handler=None):
    client = client_for(ex, handler)
    night = gandi.NightPass(client, ex.conn, ex.fr.store, ex, timezone.utc)
    waits = []

    async def sleep(seconds):
        waits.append(seconds)

    night.sleep = sleep
    return night, waits


def test_night_queue_free_first_then_by_score_skipping_fresh_checks(env):
    zone(env, [], ["maison"])
    now = utcnow()
    env.drops.record("com", ["velo", "chat"], [], now)
    env.conn.execute("UPDATE drops SET state = 'available', state_since = ?, available_since = ? WHERE name = 'velo.com'",
                     (iso(now - timedelta(days=3)),) * 2)
    env.conn.execute("UPDATE drops SET state = 'unconfirmed', state_since = ? WHERE name = 'chat.com'", (iso(now - timedelta(days=1)),))
    for name, days, kind in (("jardin.com", 2, "available"), ("chat.com", 2, "available"), ("lune.com", 2, "error"),
                             ("ciel.com", 8, "unavailable")):
        env.conn.execute("INSERT INTO gandi_checks (name, status, kind, checked_at) VALUES (?, ?, ?, ?)",
                         (name, kind, kind, iso(now - timedelta(days=days))))
    env.invalidate("com")
    night, _ = make_pass(env)
    names = night.pending(10**6, now)
    assert names[0] == "velo.com"  # libre : en tête, avant les probablement libres
    assert "jardin.com" not in names  # vu il y a 2 jours, revu au bout de 7
    assert {"chat.com", "lune.com", "ciel.com"} <= set(names)  # changé d'état, en erreur, vu il y a 8 jours (réservé)
    assert "maison.com" not in names and len(names) == len(set(names))
    assert night.pending(3, now)[0] == "velo.com" and len(night.pending(3, now)) == 3
    night.store.set("gandi_pass_days", "1")
    assert "jardin.com" in night.pending(10**6, now)


def test_night_pass_paces_records_and_stops_on_refused_token(env):
    zone(env, [], [])
    calls = []

    def handler(request):
        name = request.url.params["name"]
        calls.append(name)
        if name == "stop.com":
            return httpx.Response(401)
        return httpx.Response(200, json=answer("available", [PREMIUM if name == "abe.com" else STANDARD], name))

    night, waits = make_pass(env, handler)
    night.pending = lambda limit, now=None: ["abd.com", "abe.com", "stop.com", "abf.com"]
    sent = []

    async def notify(text):
        sent.append(text)

    night.notify = notify
    stats = asyncio.run(night.run(manual=True))
    assert calls == ["abd.com", "abe.com", "stop.com"]
    assert stats["checked"] == 2 and stats["kinds"] == {"available": 1, "premium": 1} and "refusé" in stats["stopped"]
    assert waits and all(0 < w <= 1 / 3 + 0.01 for w in waits)  # 3 requêtes par seconde au plus
    assert "refusé" in night.store.get("gandi_error") and len(sent) == 1 and "Gandi" in sent[0]
    asyncio.run(night.run(manual=True))
    assert len(sent) == 1  # une alerte par jour au plus
    status = night.status()
    assert status["totals"]["known"] == 2 and status["totals"]["by_kind"] == {"available": 1, "premium": 1}
    assert status["error"] and status["running"] is False and "pat_" not in json.dumps(status)
    assert env.status_bytes("com")[env.universe.index("abe")] == explore.PREMIUM


def test_night_pass_waits_on_rate_limit_and_skips_unsold_extensions(env):
    zone(env, [], [])
    calls = []
    limited = {"abd.com": 1, "abe.com": 9}

    def handler(request):
        name = request.url.params["name"]
        calls.append(name)
        if name.endswith(".xyz"):
            return httpx.Response(400)
        if limited.get(name):
            limited[name] -= 1
            return httpx.Response(429, headers={"Retry-After": "7"})
        return httpx.Response(200, json=answer("available", [STANDARD], name))

    night, waits = make_pass(env, handler)
    night.pending = lambda limit, now=None: [f"a{n}.xyz" for n in range(7)] + ["abd.com"]
    stats = asyncio.run(night.run(manual=True))
    # 429 : attente demandée par Gandi, puis reprise au même nom ; .xyz écartée après 5 réponses invalides.
    assert calls == [f"a{n}.xyz" for n in range(5)] + ["abd.com", "abd.com"] and 7 in waits
    assert stats["checked"] == 1 and stats["unsold"] == ["xyz"] and night.unsold() == {"xyz"}
    night.pending = lambda limit, now=None: ["abe.com", "abf.com"]
    stats = asyncio.run(night.run(manual=True))
    assert "trop de requêtes" in stats["stopped"] and stats["checked"] == 0


def test_night_pass_window_switch_and_token(env, monkeypatch):
    zone(env, [], [])
    night, _ = make_pass(env)
    at = datetime(2026, 10, 18, 3, 40, tzinfo=timezone.utc)
    assert night.due(at) and not night.due(at.replace(hour=3, minute=29)) and not night.due(at.replace(hour=4, minute=45))
    night.store.set("gandi_pass_enabled", "0")
    assert not night.due(at)
    night.store.set("gandi_pass_enabled", "1")
    night.gandi.store.set("gandi_pat", "")
    assert not night.due(at) and asyncio.run(night.run())["started"] is False
    night.gandi.store.set("gandi_pat", "pat_test")
    # Passe de nuit : arrêt à l'heure de fin ; le jour est marqué, rien n'est relancé avant le lendemain.
    monkeypatch.setattr(gandi.NightPass, "_in_window", staticmethod(lambda now: False))
    night.pending = lambda limit, now=None: ["abd.com"]
    stats = asyncio.run(night.run())
    assert stats["stopped"] == "heure de fin atteinte" and stats["checked"] == 0
    monkeypatch.setattr(gandi.NightPass, "_in_window", staticmethod(lambda now: True))
    assert not night.due(datetime.now(timezone.utc))


# --- Routes ---------------------------------------------------------------------------------------

def _com_zone(st, labels=(), words=()):
    from app import drops

    bits = bytearray(drops.BITMAP_BYTES)
    for label in labels:
        i = drops.encode(label)
        bits[i // 8] |= 1 << (i % 8)
    st.store.set("czds_approved", '["com"]')
    st.drops._save("com", bits, set(words))
    st.explorer.invalidate("com")


def test_gandi_settings_run_and_diag_without_secret(logged):
    client = logged()
    st = client.app.state
    assert client.post("/api/settings/gandi/run").status_code == 409
    assert client.put("/api/settings/gandi/pass", json={"rate": 50}).status_code == 422
    assert client.put("/api/settings/gandi/pass", json={"nightly": 0}).status_code == 422
    saved = client.put("/api/settings/gandi/pass", json={"enabled": False, "rate": 2, "nightly": 500, "days": 10}).json()
    assert (saved["enabled"], saved["rate"], saved["nightly"], saved["days"]) == (False, 2.0, 500, 10)
    assert saved["window"] == "03:30-04:45"
    client.put("/api/settings/gandi", json={"key": "pat_secret-123"})
    st.gandi._http_factory = lambda: httpx.AsyncClient(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json=answer("available", [STANDARD], r.url.params["name"]))))
    st.gandi_pass.sleep = lambda s: asyncio.sleep(0)
    st.gandi_pass.pending = lambda limit, now=None: ["abd.com", "abe.com"]
    started = client.post("/api/settings/gandi/run")
    assert started.status_code == 202 and started.json()["started"] is True
    for _ in range(100):
        if not st.gandi_pass.running:
            break
        client.get("/api/settings/status")  # laisse tourner la boucle du client de test
    settings = client.get("/api/settings").json()["gandi"]
    assert settings["key_set"] and settings["pass"]["checked"] == 2 and settings["pass"]["manual"] is True
    diag = client.get("/api/diag").json()
    assert diag["gandi_pass"]["totals"]["known"] == 2
    assert "pat_secret" not in json.dumps(diag) and "key_set" not in json.dumps(diag["gandi_pass"])
    assert client.get("/api/drops?items=0").json()["gandi"]["checked"] == 2
    assert any(j["id"] == "check_gandi" for j in diag["jobs"])
    client.__exit__(None, None, None)


def test_gandi_in_veille_today_explorer_export_ai_and_domains(logged):
    client = logged()
    st = client.app.state
    conn = st.conn
    now = datetime.now(timezone.utc)
    _com_zone(st, words=["maison"])
    st.drops.record("com", ["velo", "chat", "lune", "abd", "ciel"], [], now)
    conn.execute("UPDATE drops SET state = 'available', available_since = ?, state_since = ? "
                 "WHERE name IN ('velo.com', 'lune.com', 'abd.com')", (iso(now - timedelta(hours=2)),) * 2)
    conn.execute("UPDATE drops SET state = 'unconfirmed', state_since = ? WHERE name = 'chat.com'", (iso(now - timedelta(hours=3)),))
    conn.execute("UPDATE drops SET state = 'redemption', state_since = ? WHERE name = 'ciel.com'", (iso(now - timedelta(hours=3)),))
    at = iso(now - timedelta(hours=1))
    for name, status, kind, price in (("velo.com", "available", "premium", 878.4), ("chat.com", "available", "available", 13.2),
                                      ("lune.com", "unavailable", "unavailable", None), ("ciel.com", "unavailable", "unavailable", None)):
        conn.execute("INSERT INTO gandi_checks (name, status, kind, price, checked_at) VALUES (?, ?, ?, ?, ?)",
                     (name, status, kind, price, at))
    _opinion(conn, "velo.com", "À prendre", 80)
    for name in ("velo.com", "chat.com", "lune.com", "abd.com", "ciel.com"):
        st.explorer.touch(name)
    items = {i["name"]: i for i in client.get("/api/drops").json()["items"]}
    # Indisponible chez Gandi : retiré comme un réservé (réglage par défaut) ; en rédemption, rien ne change.
    assert "lune.com" not in items and items["ciel.com"]["gandi_state"] is None and not items["ciel.com"]["reserved"]
    assert items["velo.com"]["premium"] and items["velo.com"]["premium_price"] == 878 and items["velo.com"]["gandi_state"] == "premium"
    assert items["chat.com"]["gandi_state"] == "available" and items["chat.com"]["gandi"]["price"] == 13.2
    assert items["abd.com"]["gandi"] is None
    # Aujourd'hui : un nom à confirmer que Gandi dit libre compte comme libéré ; « Masquer les premiums ».
    today = client.get("/api/today").json()
    assert today["counts"]["freed"] == 3 and "velo.com" in {i["name"] for i in today["items"]}
    hidden = client.get("/api/today", params={"nopremium": "1"}).json()
    assert hidden["counts"]["freed"] == 2 and "velo.com" not in {i["name"] for i in hidden["items"]}
    focus = client.get("/api/drops", params={"focus": "freed", "nopremium": "1"}).json()
    assert {i["name"] for i in focus["items"]} == {"chat.com", "abd.com"}
    # Explorer : premiums visibles par défaut, nopremium les retire ; prix d'achat au plus.
    shown = client.get("/api/explore/rank", params={"tld": "com", "status": "bookable", "level": "", "q": "velo", "mode": "exact"}).json()
    assert shown["items"][0]["status_label"] == "Premium (Gandi)"
    gone = client.get("/api/explore/rank", params={"tld": "com", "status": "bookable", "q": "velo", "mode": "exact", "level": "",
                                                   "nopremium": "1"}).json()
    assert gone["items"] == [] and gone["counts"]["bookable"] == shown["counts"]["bookable"] - 1
    cheap = client.get("/api/explore/rank", params={"tld": "com", "level": "", "gprice": "20"}).json()
    assert [i["name"] for i in cheap["items"]] == ["chat.com"] and cheap["counts"]["bookable"] == 1
    assert client.get("/api/explore/rank", params={"tld": "com", "gprice": "abc"}).status_code == 422
    csv = client.get("/api/explore/export", params={"tld": "com", "q": "velo", "mode": "exact", "level": ""}).text
    head, line = csv.strip().lstrip("﻿").splitlines()[:2]
    assert head.endswith(";gandi_kind;gandi_price") and line.endswith(";premium;878.4")
    # File IA : la réponse de Gandi accompagne le nom ; la consigne le dit.
    conn.execute("DELETE FROM domain_ai")
    st.explorer.ai_changed()
    st.explorer.invalidate()
    data = client.get("/api/ai/pending", params={"limit": 50}).json()
    names = {n["name"]: n for n in data["names"]}
    assert names["velo.com"]["gandi"] == {"kind": "premium", "price": 878}
    assert names["chat.com"]["gandi"] == {"kind": "available", "price": 13}
    assert "la revente doit couvrir ce prix" in data["guide"] and data["version"] == 5
    # Domaines : prix Gandi d'un domaine libre suivi.
    conn.execute("INSERT INTO domains (name, added_at, state) VALUES ('chat.com', 'x', ?)", (states.AVAILABLE,))
    domain = next(d for d in client.get("/api/domains").json() if d["name"] == "chat.com")
    assert domain["gandi"]["kind"] == "available" and domain["gandi"]["price"] == 13.2
    client.__exit__(None, None, None)


def test_watched_domain_check_asks_gandi_once_a_day(logged):
    client = logged()
    st = client.app.state
    calls = []

    def handler(request):
        calls.append(request.url.params["name"])
        return httpx.Response(200, json=answer("available", [STANDARD], request.url.params["name"]))

    st.gandi._http_factory = lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))
    st.store.set("gandi_pat", "pat_test")

    async def lookup(name):
        return LookupResult(available=True)

    st.checker.rdap.lookup = lookup
    domain_id = st.conn.execute("INSERT INTO domains (name, added_at) VALUES ('libre-test.com', 'x')").lastrowid
    asyncio.run(st.checker.check(domain_id))
    assert calls == ["libre-test.com"]  # premier état : changement, Gandi interrogé
    asyncio.run(st.checker.check(domain_id))
    assert calls == ["libre-test.com"]  # vu il y a moins d'un jour
    found = client.post(f"/api/domains/{domain_id}/check").json()
    assert calls == ["libre-test.com"] * 2 and found["gandi"]["price"] == 13.2  # à la main : toujours
    client.__exit__(None, None, None)

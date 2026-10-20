"""Gandi : lecture des réponses, effet sur l'état des noms, passe de nuit, vérification, routes."""

import asyncio
import json
import time
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app import drops, explore, gandi, states
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
    client.on_blocked = ex.recheck
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
    assert gandi.read(answer("unavailable_premium")) == {"kind": "unavailable", "status": "unavailable_premium", "price": None,
                                                         "price_ht": None, "currency": "EUR"}
    kinds = {s: gandi.read(answer(s))["kind"] for s in (
        "available_reserved", "reserved_corporate", "unavailable_restricted", "available_preorder", "pending",
        "error_invalid", "error_refused", "error_timeout", "error_unknown", "error_eoi", "inconnu")}
    assert kinds == {"available_reserved": "reserved", "reserved_corporate": "reserved", "unavailable_restricted": "restricted",
                     "available_preorder": "available", "pending": "error", "error_invalid": "error", "error_refused": "error",
                     "error_timeout": "error", "error_unknown": "error", "error_eoi": "error", "inconnu": "error"}
    assert gandi.read({"currency": "EUR", "products": []})["kind"] == "error"


def test_gandi_effect_never_touches_registered_or_listed_names():
    """Le détail de la règle est dans tests/test_state_rules.py (toutes les combinaisons)."""
    check = lambda kind: {"kind": kind, "checked_at": "2026-10-19T03:00:00+00:00"}  # noqa: E731
    assert explore.gandi_effect(explore.REGISTERED, check("available"))[0] == explore.REGISTERED
    assert explore.gandi_effect(explore.COMING, check("unavailable"))[0] == explore.COMING
    assert explore.gandi_effect(explore.RESERVED, check("available"), listed=True)[0] == explore.RESERVED
    assert explore.gandi_effect(explore.RESERVED, check("available"))[0] == explore.FREE  # « court réservé » estimé
    assert explore.gandi_effect(explore.UNCONFIRMED, check("restricted"))[0] == explore.RESERVED
    assert explore.gandi_effect(explore.ABSENT, check("error"))[0] == explore.ABSENT


# --- Effet sur l'état des noms --------------------------------------------------------------------

def test_gandi_answers_set_the_state_of_names_thought_free(env):
    zone(env, [], ["maison"])
    env.premium_tlds_off = lambda: frozenset()  # .com : 3 caractères en premium probable, pour l'exemple
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
    # jardin.com, absent de la zone, indisponible : sans doute enregistré depuis la zone d'hier. Le registre est
    # revu tout de suite (ligne de Veille sans sortie constatée) ; d'ici là, pas « Réservé ».
    row = env.drops.row("jardin.com")
    assert row["next_check_at"] == iso(now) and row["removed_on"] == "" and row["state"] is None
    assert env.status_bytes("com")[u.index("jardin")] == explore.ABSENT
    # Le registre le dit libre : l'indisponibilité de Gandi compte, réservé.
    assert env.drops.apply(row, LookupResult(available=True), now + timedelta(minutes=1)) == states.AVAILABLE
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
    assert abd["status_label"] == "Libre (vérifié)" and abd["premium_price"] is None and abd["gandi"]["price"] == 13.2
    assert abe["status_label"] == "Premium" and abe["premium_price"] == 878 and abe["gandi"]["kind"] == "premium"
    assert jardin["reserved"] == ["Indisponible à l'achat"] and jardin["status_label"] == "Indisponible à l'achat"
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
    assert timeline["gandi"] == "Prix vérifié : premium, 878 €"
    assert env.fiche("abe.com")["gandi"]["kind"] == "premium"


def test_unavailable_on_a_free_drop_rechecks_the_registry_first(env):
    """lesimpots.fr : libre dans la Veille, enregistré depuis, Gandi dit indisponible. Pas « Réservé » : le registre
    est revu tout de suite ; pris, la réponse de Gandi ne compte plus ; toujours libre, elle compte (réservé)."""
    zone(env, [], [])
    now = utcnow()
    before = iso(now - timedelta(hours=10))
    env.drops.record("com", ["jardin"], [], now)
    env.conn.execute("""UPDATE drops SET state = 'available', state_since = ?, available_since = ?, checked_at = ?,
                        next_check_at = ? WHERE name = 'jardin.com'""", (before, before, before, iso(now + timedelta(hours=14))))
    env.touch("jardin.com")
    client = client_for(env)
    client.record("jardin.com", gandi.read(answer("unavailable")), now)
    row = env.drops.row("jardin.com")
    assert row["next_check_at"] == iso(now)  # revérification au registre tout de suite

    def one():
        return env.search("com", q="jardin", mode="exact", status="", premium=True)["items"][0]

    found = one()
    assert found["status"] == explore.FREE and found["reserved"] == [] and found["gandi"]["pending"] is True
    fiche = env.fiche("jardin.com")
    assert fiche["gandi"]["pending"] is True and "Réservé" not in str(fiche.get("status_label"))
    # Le registre le dit toujours libre après Gandi : indisponible compte, nom réservé.
    env.conn.execute("UPDATE drops SET checked_at = ? WHERE name = 'jardin.com'", (iso(now + timedelta(minutes=2)),))
    env.touch("jardin.com")
    found = one()
    assert found["status"] == explore.RESERVED and found["reserved"] == ["Indisponible à l'achat"] and found["status_label"] == "Indisponible à l'achat"
    assert found["gandi"]["pending"] is False
    # prunes.com : absent de la zone d'hier, acheté depuis. Gandi indisponible, le registre revu le dit pris.
    client.record("prunes.com", gandi.read(answer("unavailable")), now)
    row = env.drops.row("prunes.com")
    assert row["next_check_at"] == iso(now)
    taken = LookupResult(found=True, info=DomainInfo(statuses=["active"], expires_at=now + timedelta(days=365)))
    assert env.drops.apply(row, taken, now + timedelta(minutes=1)) == "taken"
    i = env.universe.index("prunes")
    item = env.describe("com", [i])[0] if i is not None else env._describe_unknown("prunes.com", now)
    assert item["status"] == explore.REGISTERED and not item["reserved"]
    # Un nom pris ne bouge pas : la requête ne concerne que les libres de la Veille.
    env.conn.execute("UPDATE drops SET state = 'taken', next_check_at = NULL WHERE name = 'jardin.com'")
    client.record("jardin.com", gandi.read(answer("unavailable")), now + timedelta(minutes=3))
    assert env.drops.row("jardin.com")["next_check_at"] is None


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
    # Libre au registre jamais demandé (velo.com, Veille) en tête. Déjà dit libre au tarif normal par Gandi (jardin.com) :
    # pas revu, la zone dira s'il est repris et son prix est celui de la grille. Revus : état changé depuis la réponse
    # (chat.com), réponse en erreur (lune.com) ; pris il y a 8 jours (ciel.com) : pas avant 30 jours (réglable).
    assert names[0] == "velo.com" and {"chat.com", "lune.com"} <= set(names)
    assert not {"jardin.com", "ciel.com", "maison.com"} & set(names) and len(names) == len(set(names))
    assert night.pending(3, now)[0] == "velo.com" and len(night.pending(3, now)) == 3
    night.store.set("gandi_pass_days", "7")
    assert "ciel.com" in night.pending(10**6, now)
    # Premium : revu après 30 jours, 7 avec un avis « À prendre ».
    env.conn.execute("UPDATE gandi_checks SET kind = 'premium', status = 'available', checked_at = ? WHERE name = 'jardin.com'",
                     (iso(now - timedelta(days=10)),))
    env.invalidate("com")
    assert "jardin.com" not in night.pending(10**6, now)
    _take(env.conn, "jardin.com")
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
    # Plusieurs requêtes en vol : abf.com part avant le refus de stop.com, sa réponse n'est pas gardée.
    assert calls[:3] == ["abd.com", "abe.com", "stop.com"] and set(calls) <= {"abd.com", "abe.com", "stop.com", "abf.com"}
    assert "abf.com" not in gandi.load(env.conn, ["abf.com"])
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
    # 429 : attente demandée par Gandi, puis reprise au même nom ; .xyz écartée après 5 réponses invalides (les deux
    # suivantes, déjà parties, ne comptent pas : leur réponse arrive après l'écart et n'est pas lue).
    assert calls[:5] == [f"a{n}.xyz" for n in range(5)] and calls.count("abd.com") == 2 and 7 in waits
    assert set(calls) == {f"a{n}.xyz" for n in range(7)} | {"abd.com"}
    assert stats["checked"] == 1 and stats["unsold"] == ["xyz"] and night.unsold() == {"xyz"}
    night.pending = lambda limit, now=None: ["abe.com", "abf.com"]
    stats = asyncio.run(night.run(manual=True))
    assert "trop de requêtes" in stats["stopped"] and stats["checked"] == 0


def test_night_pass_overlaps_slow_answers(env):
    """Passe de nuit, même mécanique que la passe continue : 0,3 s par réponse, 16 noms. Une à une, 4,8 s ; ici
    plusieurs en vol (attente de rythme simulée), chaque nom une fois, tous gardés dans l'ordre."""
    zone(env, [], [])

    async def handler(request):
        await asyncio.sleep(0.3)
        return httpx.Response(200, json=answer("available", [STANDARD], request.url.params["name"]))

    night, _ = make_pass(env, handler)
    night.pending = lambda limit, now=None: ["chauffe.com"]
    asyncio.run(night.run(manual=True))  # premier passage hors chrono : seule la passe est mesurée
    names = [f"nuit{n}.com" for n in range(16)]
    night.pending = lambda limit, now=None: names
    started = time.monotonic()
    stats = asyncio.run(night.run(manual=True))
    elapsed = time.monotonic() - started
    assert stats["checked"] == 16 and elapsed < 3.0, elapsed  # une à une : 4,8 s au moins
    assert set(gandi.load(env.conn, names)) == set(names)


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
    conn.execute("UPDATE drops SET state = 'available', available_since = ?, state_since = ?, checked_at = ? "
                 "WHERE name IN ('velo.com', 'lune.com', 'abd.com')", (iso(now - timedelta(hours=2)),) * 2 + (iso(now),))
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
    assert shown["items"][0]["status_label"] == "Premium"
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
    assert "la revente doit couvrir ce prix" in data["guide"] and data["version"] == 6
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


def test_a_name_just_freed_gets_its_price_without_waiting_for_the_night(logged, monkeypatch):
    from app import main

    monkeypatch.setattr(main, "FREED_GANDI_DELAY", 0)
    client = logged()
    st = client.app.state
    asked = []

    def handler(request):
        asked.append(request.url.params["name"])
        return httpx.Response(200, json=answer("available", [STANDARD], request.url.params["name"]))

    st.gandi._http_factory = lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))
    st.store.set("gandi_pat", "pat_test")
    _com_zone(st)
    now = utcnow()
    st.drops.record("com", ["lune", "ciel"], [], now)
    st.conn.execute("UPDATE drops SET state = 'redemption', state_since = ? WHERE name IN ('lune.com', 'ciel.com')", (iso(now),))
    st.conn.execute("UPDATE drops SET origin = 'gandi' WHERE name = 'ciel.com'")

    async def free():
        for name in ("lune.com", "ciel.com"):
            st.drops.apply(st.drops.row(name), LookupResult(available=True), utcnow())
        await asyncio.sleep(0.2)
        st.drops.apply(st.drops.row("lune.com"), LookupResult(available=True), utcnow())  # toujours libre : rien de plus
        await asyncio.sleep(0.2)

    asyncio.run(free())
    # Libéré : prix demandé une fois ; revérification après un refus d'achat (ciel.com) : pas de nouvelle demande.
    assert asked == ["lune.com"]
    assert gandi.load(st.conn, ["lune.com"])["lune.com"]["kind"] == "available"
    client.__exit__(None, None, None)


# --- Extension par Gandi seul (.com sans zone) ---------------------------------------------------------

def gandi_only(ex, handler=None):
    """Explorer avec un jeton Gandi et la zone .com pas lue (accès CZDS sans lecture) : .com par Gandi seul."""
    ex.gandi = client_for(ex, handler)
    ex.invalidate()
    return ex.gandi


def _take(conn, name):
    conn.execute("""INSERT INTO domain_ai (name, exploit, resale, quick_low, quick_high, final_low, final_high, comment,
                    rated_at, version) VALUES (?, 70, 70, 0, 0, 0, 100, 'À prendre. Test. Acheteur type : test.', 'x', 5)""", (name,))


def test_com_without_zone_is_known_through_gandi_alone(env, monkeypatch):
    """Sans zone .com lisible : .com dans l'Explorer, chaque nom « pas encore vérifié » ; libre, premium ou pris d'après
    Gandi selon sa réponse, sans revérification au registre sauf pour un nom « À prendre », sous un plafond par jour."""
    assert env.tlds() == [] and env.gandi_tlds() == []  # sans jeton : rien
    client = gandi_only(env)
    assert env.tlds() == ["com"] and env.gandi_tlds() == ["com"]
    u = env.universe
    data = env.status_bytes("com")
    assert data[u.index("jardin")] == explore.UNCHECKED and data[drops.encode("-ab")] == explore.INVALID
    before = env.facets("com", premium=True)
    assert before["bookable"] == 0 and before["registered"] == 0 and before["all"] > 100_000
    now = utcnow()
    client.record("jardin.com", gandi.read(answer("available", [STANDARD])), now)
    client.record("abe.com", gandi.read(answer("available", [PREMIUM])), now)
    client.record("maison.com", gandi.read(answer("unavailable")), now)
    client.record("velo.com", gandi.read(answer("reserved_corporate")), now)
    client.record("lune.com", gandi.read(answer("error_timeout")), now)
    # Indisponible : pris d'après Gandi, aucune requête au registre (pas de ligne de Veille).
    assert env.drops.row("maison.com") is None and env.drops.row("velo.com") is None

    def one(label):
        return env.search("com", q=label, mode="exact", status="all", premium=True)["items"][0]

    jardin, abe, maison, velo, lune = (one(x) for x in ("jardin", "abe", "maison", "velo", "lune"))
    assert jardin["status"] == explore.FREE and jardin["status_label"] == "Libre (vérifié)" and jardin["gandi_state"] == "available"
    assert abe["status"] == explore.PREMIUM and abe["premium_price"] == 878
    assert maison["status"] == explore.TAKEN_GANDI and maison["status_label"] == "Enregistré"  # un seul statut « pris » à l'écran
    assert maison["gandi_state"] == "unavailable" and maison["reserved"] == []
    assert velo["status"] == explore.TAKEN_GANDI
    assert lune["status"] == explore.UNCHECKED and lune["status_label"] == "Pas encore vérifié" and lune["gandi"]["error"]
    counts = env.facets("com", premium=True)
    assert counts["registered"] == 2 and counts["free"] == 2 and env.facets("com")["free"] == 1 and counts["all"] == before["all"]
    # Corrigé en place, nom par nom : le même résultat qu'un calcul d'ensemble.
    touched, counted = bytes(env.status_bytes("com")), env.facets("com", premium=True)
    env.invalidate("com")
    assert bytes(env.status_bytes("com")) == touched and env.facets("com", premium=True) == counted
    # Pris d'après Gandi compte comme pris ailleurs (demande, filtre « pris sur une autre extension »).
    assert explore.TAKEN_GANDI in explore.TAKEN_CODES
    # Un nom « À prendre » dit indisponible : revu au registre, RECHECK_DAILY par jour au plus.
    monkeypatch.setattr(explore, "RECHECK_DAILY", 1)
    for name in ("soleil.com", "plage.com", "credit.com"):
        _take(env.conn, name)
    client.record("soleil.com", gandi.read(answer("unavailable")), now)
    client.record("plage.com", gandi.read(answer("unavailable")), now)
    client.record("credit.com", gandi.read(answer("available", [STANDARD])), now)
    assert env.drops.row("soleil.com")["next_check_at"] == iso(now) and env.drops.row("plage.com") is None
    assert one("soleil")["status"] == explore.TAKEN_GANDI  # en attendant le registre : pris d'après Gandi
    # Le registre le dit pris : « Enregistré » ; toujours libre : indisponible à l'achat (règle commune).
    taken = LookupResult(found=True, info=DomainInfo(statuses=["active"], expires_at=now + timedelta(days=365)))
    env.drops.apply(env.drops.row("soleil.com"), taken, now + timedelta(minutes=1))
    assert one("soleil")["status"] == explore.REGISTERED and one("soleil")["status_label"] == "Enregistré"
    # Libre d'après Gandi plus tard (le nom a été supprimé depuis) : la dernière lecture l'emporte.
    client.record("soleil.com", gandi.read(answer("available", [STANDARD])), now + timedelta(days=8))
    assert one("soleil")["status"] == explore.FREE
    # La carte, le Top et la fiche disent la même chose.
    assert env.map()["cells"]["com"]
    fiche = env.fiche("maison.com")
    assert fiche["item"]["status"] == explore.TAKEN_GANDI and fiche["gandi"]["kind"] == "unavailable"


def test_com_without_zone_uses_the_registry_dns_before_gandi(env, monkeypatch):
    """En attendant l'accès CZDS : le DNS du .com fait le gros du tri. Délégué : pris, jamais demandé à Gandi ;
    absent : demandé à Gandi en premier, qui le dit libre, premium ou pris (enregistré sans serveurs). Sorti du DNS :
    dans la Veille, comme un .co. La zone lue, la passe DNS .com s'arrête."""
    from app import ccdns, frdns

    monkeypatch.setattr(frdns, "RATE", 1_000_000.0)
    client = gandi_only(env)
    dns = ccdns.ComDns(env.conn, env.fr.store, env, timezone.utc)
    assert dns.ready() and dns.tld == "com" and dns.key == "com_dns"
    assert len(dns.pending(10**6)) > 150_000  # tous les noms qui ont un sens, pas seulement les 50 000 meilleurs
    dns.servers = ["192.0.2.1"]
    state = {"maison": True, "jardin": False, "velo": False, "credit": True}
    dns.pending = lambda limit, now=None: list(state)

    async def query(label):
        return state[label]

    dns.query = query
    asyncio.run(dns.run(limit=4))
    u = env.universe
    data = env.status_bytes("com")
    assert data[u.index("maison")] == explore.REGISTERED and data[u.index("credit")] == explore.REGISTERED
    assert data[u.index("jardin")] == explore.UNCHECKED and data[u.index("soleil")] == explore.UNCHECKED
    # Gandi : les absents du DNS d'abord, les délégués jamais.
    live = gandi.LivePass(client, env.conn, env.fr.store, env, timezone.utc)
    names, info = live.plan(10**7)
    # Seulement les absents : un délégué est pris, un nom pas encore interrogé attend la passe DNS.
    assert sorted(names) == ["jardin.com", "velo.com"] and info["total"] == info["due"] == 2
    # Premium vu il y a 31 jours, pas encore interrogé dans le DNS : revu quand même, en tête.
    env.conn.execute("INSERT INTO gandi_checks (name, status, kind, price, checked_at) VALUES ('soleil.com', "
                     "'available', 'premium', 878, ?)", (iso(utcnow() - timedelta(days=31)),))
    env.touch("soleil.com")
    assert live.plan(10**7)[0] == ["soleil.com", *names]
    # Absent du DNS et pris d'après Gandi (enregistré sans serveurs) : pris ; libre : libre.
    client.record("velo.com", gandi.read(answer("unavailable")))
    client.record("jardin.com", gandi.read(answer("available", [STANDARD])))
    data = env.status_bytes("com")
    assert data[u.index("velo")] == explore.TAKEN_GANDI and data[u.index("jardin")] == explore.FREE
    # Corrigé nom par nom = calcul d'ensemble.
    touched = bytes(data)
    env.invalidate("com")
    assert bytes(env.status_bytes("com")) == touched
    # Sorti du DNS : dans la Veille (vérifié au registre ensuite), « Sorti du DNS » dans la ligne de temps.
    state["maison"] = False
    asyncio.run(dns.run(limit=4))
    row = env.drops.row("maison.com")
    assert row is not None and row["next_check_at"] and row["removed_on"]
    assert env.status_bytes("com")[u.index("maison")] == explore.UNCHECKED
    assert any(k == "dns_out" for _, k, _ in [(t["at"], t["kind"], t["label"]) for t in env.fiche("maison.com")["timeline"]])
    # Zone .com lue : fonctionnement normal, la passe DNS .com n'a plus lieu d'être.
    zone(env, [], ["maison"])
    assert env.gandi_tlds() == [] and not dns.ready()


def test_verify_a_com_without_zone_reads_the_registry(env):
    """Vérifier interroge toujours le registre (RDAP), sans plafond : sa lecture fait l'état, Gandi précise le prix."""
    gandi_only(env)
    expires = datetime.now(timezone.utc) + timedelta(days=200)
    env.drops.rdap = FakeRdap({"maison.com": LookupResult(found=True, info=DomainInfo(statuses=["active"], expires_at=expires)),
                               "jardin.com": LookupResult(available=True)})
    env.gandi.record("maison.com", gandi.read(answer("unavailable")), utcnow() - timedelta(hours=1))
    assert asyncio.run(env.verify("maison.com"))["status"] == explore.REGISTERED
    item = asyncio.run(env.verify("jardin.com"))
    assert item["status"] == explore.FREE and item["gandi"]["kind"] == "available"


def test_live_pass_order_lists_first_then_score_with_revisits(env):
    client = gandi_only(env)
    live = gandi.LivePass(client, env.conn, env.fr.store, env, timezone.utc)
    u = env.universe
    now = utcnow()
    names, info = live.plan(10**7, now)
    assert info["due"] == info["total"] == len(names) > 150_000
    pkb = [f"{u.label(i)}.com" for i in u.frequency("premium,keyword,brand") if u.good[i]]
    first = set(pkb)
    local = [f"{u.label(i)}.com" for i in u.frequency("local") if u.good[i] and f"{u.label(i)}.com" not in first]
    assert names[:len(pkb)] == pkb and names[len(pkb):len(pkb) + len(local)] == local
    rest = names[len(pkb) + len(local):]
    scores = [u.base_table[u.index(n[:-4])] & 127 for n in rest[:2000]]
    assert scores == sorted(scores, reverse=True)
    # Revus selon revisit_due : libres au tarif normal jamais d'office, premiums après 30 jours, pris après 30 jours
    # (réglable), erreurs le lendemain.
    at = lambda days: iso(now - timedelta(days=days))  # noqa: E731
    for name, kind, days in (("jardin.com", "available", 2), ("abe.com", "premium", 2), ("maison.com", "unavailable", 2),
                             ("velo.com", "unavailable", 8), ("lune.com", "error", 2), ("soleil.com", "available", 0.5)):
        env.conn.execute("INSERT INTO gandi_checks (name, status, kind, checked_at) VALUES (?, ?, ?, ?)", (name, kind, kind, at(days)))
        env.touch(name)
    names, info = live.plan(10**7, now)
    assert "lune.com" in names and not {"jardin.com", "abe.com", "maison.com", "velo.com", "soleil.com"} & set(names)
    assert info["due"] == len(names) == info["total"] - 5
    assert live.plan(5, now)[0] == names[:5]
    live.store.set("gandi_live_days", "1")
    assert {"maison.com", "velo.com"} <= set(live.plan(10**7, now)[0])
    # Premium vu il y a plus de 30 jours : revu, en tête.
    env.conn.execute("UPDATE gandi_checks SET checked_at = ? WHERE name = 'abe.com'", (at(31),))
    env.touch("abe.com")
    assert live.plan(10**7, now)[0][0] == "abe.com"


def test_live_pass_keeps_its_rate_when_gandi_is_slow(env):
    """Gandi met environ 1 s à répondre depuis le NAS : une à une, la passe tombait à 1 nom par seconde. Ici 0,3 s
    par réponse et 10 noms par seconde réglés : 20 noms en 2 s environ, pas 6 s ; aucun nom sauté ni doublé."""
    async def handler(request):
        await asyncio.sleep(0.3)
        return httpx.Response(200, json=answer("available", [STANDARD], request.url.params["name"]))

    client = gandi_only(env, handler)
    live = gandi.LivePass(client, env.conn, env.fr.store, env, timezone.utc)
    live.store.set("gandi_live_rate", "10")
    names = [f"lent{n}.com" for n in range(20)]
    live.plan = lambda limit, now=None: (names, {"total": 20, "due": 20, "planned_at": iso(utcnow())})
    # Premier passage (Explorer et client prêts) hors chrono : seule la passe est mesurée.
    live.plan = lambda limit, now=None: (["chauffe.com"], {"total": 1, "due": 1, "planned_at": iso(utcnow())})
    asyncio.run(live.run(tours=1))
    live.plan = lambda limit, now=None: (names, {"total": 20, "due": 20, "planned_at": iso(utcnow())})
    started = time.monotonic()
    stats = asyncio.run(live.run(tours=1))
    elapsed = time.monotonic() - started
    assert stats["checked"] == 20 and stats["kinds"] == {"available": 20} and 1.8 <= elapsed < 3.5, elapsed
    assert {r[0] for r in env.conn.execute("SELECT name FROM gandi_checks WHERE name LIKE 'lent%'")} == set(names)


def test_empty_answers_wait_and_never_drop_com(env):
    """Vu sur le NAS le 19/10/2026 : environ 150 vérifications .com, puis des réponses sans ligne de création (Gandi
    limite), de nouveau 150 quinze minutes plus tard. Elles avaient écarté le .com 30 jours (« zone lue » affiché), puis
    été notées en erreur. Ici : 5 réponses, puis vides jusqu'à une attente d'une minute ; aucun nom noté en erreur, tous
    vérifiés, le .com reste."""
    quota = {"left": 5}

    def handler(request):
        name = request.url.params["name"]
        if quota["left"] <= 0:
            return httpx.Response(200, json={"currency": "EUR", "products": []})
        quota["left"] -= 1
        return httpx.Response(200, json=answer("available", [STANDARD], name))

    client = gandi_only(env, handler)
    live = gandi.LivePass(client, env.conn, env.fr.store, env, timezone.utc)
    waits = []

    async def sleep(seconds):
        waits.append(seconds)
        if seconds >= 1:
            quota["left"] = 5  # la limite de Gandi se recharge pendant l'attente (par tranches de 5 s)

    live.sleep = sleep
    names = [f"quota{n}.com" for n in range(12)]
    live.plan = lambda limit, now=None: (names, {"total": 12, "due": 12, "planned_at": iso(utcnow())})
    stats = asyncio.run(live.run(tours=1))
    found = gandi.load(env.conn, names)
    assert set(found) == set(names) and {r["kind"] for r in found.values()} == {"available"}
    assert stats["checked"] == 12 and stats["throttled"] >= 2 and stats["stopped"] == "fin des tours demandés"
    assert sum(w for w in waits if w >= 1) >= 2 * gandi.EMPTY_WAIT
    assert gandi.unsold(env.fr.store) == set() and live.tlds() == ["com"]
    # Marque laissée par l'ancienne version : ignorée pour une extension par Gandi seul.
    env.fr.store.set("gandi_unsold", json.dumps({"com": "2099-01-01", "xyz": "2099-01-01"}))
    assert gandi.unsold(env.fr.store) == {"xyz"} and live.tlds() == ["com"] and live.due()


def test_live_pass_paces_waits_and_stops_on_refused_token(env):
    calls = []
    limited = {"b.com": 2}

    def handler(request):
        name = request.url.params["name"]
        calls.append(name)
        if name == "stop.com":
            return httpx.Response(401)
        if limited.get(name):
            limited[name] -= 1
            return httpx.Response(429, headers={"Retry-After": "7"})
        return httpx.Response(200, json=answer("unavailable" if name == "b.com" else "available", [STANDARD], name))

    client = gandi_only(env, handler)
    live = gandi.LivePass(client, env.conn, env.fr.store, env, timezone.utc)
    waits = []

    async def sleep(seconds):
        waits.append(seconds)

    live.sleep = sleep
    sent = []

    async def notify(text):
        sent.append(text)

    live.notify = notify
    assert live.due() and live.status()["tlds"] == ["com"] and live.status()["rate"] == 5.0
    live.plan = lambda limit, now=None: (["a.com", "b.com", "c.com"], {"total": 10, "due": 3, "planned_at": iso(utcnow())})
    stats = asyncio.run(live.run(tours=1))
    # 429 : attente demandée (7 s), doublée au refus suivant, puis reprise au même nom ; 5 requêtes par seconde au plus,
    # plusieurs en vol (c.com part avant que b.com soit réglé).
    # (une réponse déjà partie au moment d'un refus est jetée et redemandée après l'attente : c.com peut repartir).
    assert calls[:3] == ["a.com", "b.com", "c.com"] and calls.count("b.com") == 3 and set(calls) == {"a.com", "b.com", "c.com"}
    assert stats["checked"] == 3 and stats["limited"] == 2
    assert sum(w for w in waits if w > 1) == 7 + 14
    assert all(w <= 0.2 + 0.01 for w in waits if w < 1)
    assert stats["stopped"] == "fin des tours demandés" and env.status_bytes("com")[env.universe.index("b")] == explore.TAKEN_GANDI
    status = live.status()
    assert status["verified"] == 3 and status["remaining"] == 0 and "pat_" not in json.dumps(status)
    # Jeton refusé : arrêt, erreur, alerte ; plus relancée avant le lendemain, sauf nouveau jeton.
    live.plan = lambda limit, now=None: (["stop.com", "d.com"], {"total": 10, "due": 2, "planned_at": iso(utcnow())})
    stats = asyncio.run(live.run())
    assert "refusé" in stats["stopped"] and len(sent) == 1 and "continue" in sent[0]
    assert not live.due() and live.status()["halted_at"]
    client.set_key("pat_nouveau")
    assert live.due()
    # File vide : prochain tour dans 15 minutes ; case décochée : arrêt.
    live.plan = lambda limit, now=None: ([], {"total": 10, "due": 0, "planned_at": iso(utcnow())})
    waits.clear()
    stats = asyncio.run(live.run(tours=1))
    assert stats["next_tour"] and sum(waits) == gandi.LIVE_IDLE
    live.store.set("gandi_live_enabled", "0")
    assert not live.due() and asyncio.run(live.run())["stopped"] == "arrêtée dans Paramètres"


def test_one_ceiling_for_every_gandi_request(env):
    """Passe de nuit, passe continue, Vérifier : un seul client, jamais plus de GLOBAL_PER_MINUTE requêtes par minute."""
    client = client_for(env)
    clock = [1000.0]
    waits = []

    async def sleep(seconds):
        waits.append(seconds)
        clock[0] += seconds

    client.clock = lambda: clock[0]
    client.sleep = sleep
    sent = []

    def handler(request):
        sent.append(clock[0])
        return httpx.Response(200, json=answer("available", [STANDARD], request.url.params["name"]))

    client._http_factory = lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))

    async def burst():
        async with client._http_factory() as http:
            for n in range(gandi.GLOBAL_PER_MINUTE + 50):
                await client.check(f"a{n}.com", http)
                clock[0] += 0.01

    asyncio.run(burst())
    cap = gandi.GLOBAL_PER_MINUTE
    assert cap < 1000 and waits and round(waits[0], 2) == round(60 - cap * 0.01, 2)
    assert all(sent[n + cap] - sent[n] >= 60 - 1e-6 for n in range(len(sent) - cap))


def test_zone_read_switches_com_back_to_normal(env):
    """CZDS accorde la zone et la lecture de nuit la lit : .com quitte la passe continue et revient à la zone pour
    l'état, avec la seule passe de nuit chez Gandi."""
    client = gandi_only(env)
    live = gandi.LivePass(client, env.conn, env.fr.store, env, timezone.utc)
    night = gandi.NightPass(client, env.conn, env.fr.store, env, timezone.utc)
    now = utcnow()
    client.record("maison.com", gandi.read(answer("unavailable")), now)
    client.record("jardin.com", gandi.read(answer("available", [STANDARD])), now)
    u = env.universe
    assert env.status_bytes("com")[u.index("maison")] == explore.TAKEN_GANDI
    assert live.tlds() == ["com"] and live.due() and night.tlds() == [] and night.pending(100, now) == []
    # Lecture de la zone .com (maison y est, velo non).
    zone(env, [], ["maison"])
    assert env.tlds() == ["com"] and env.gandi_tlds() == []
    data = env.status_bytes("com")
    assert data[u.index("maison")] == explore.REGISTERED and data[u.index("velo")] == explore.ABSENT
    assert data[u.index("jardin")] == explore.FREE  # absent de la zone, libre chez Gandi
    assert live.tlds() == [] and not live.due() and live.plan(100, now)[0] == []
    assert night.tlds() == ["com"] and "velo.com" in night.pending(10**6, now)
    stats = asyncio.run(live.run())
    assert stats["stopped"] == "zone lue : fonctionnement normal" and stats["checked"] == 0
    # Une passe continue en route s'arrête aussi dès que la zone est lue.
    env.drops._path("com", "short").unlink()
    env.invalidate("com")
    assert env.gandi_tlds() == ["com"]
    asked = []

    def handler(request):
        asked.append(request.url.params["name"])
        if len(asked) == 1:
            zone(env, [], ["maison"])  # la zone arrive pendant le tour
        return httpx.Response(200, json=answer("available", [STANDARD], request.url.params["name"]))

    client._http_factory = lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))
    live.sleep = lambda s: asyncio.sleep(0)
    live.plan = lambda limit, now=None: ([f"x{n}abc.com" for n in range(150)], {"total": 150, "due": 150, "planned_at": iso(utcnow())})
    asyncio.run(live.run(tours=3))
    assert len(asked) == 100  # relu tous les 100 noms : la suite n'est pas demandée


def test_gandi_live_settings_and_diag_without_secret(logged):
    client = logged()
    st = client.app.state
    assert client.put("/api/settings/gandi/live", json={"rate": 16}).status_code == 422
    assert client.put("/api/settings/gandi/live", json={"days": 0}).status_code == 422
    assert client.put("/api/settings/gandi/live", json={"tlds": ["fr"]}).status_code == 422
    assert client.put("/api/settings/gandi/live", json={"tlds": ["co"]}).status_code == 422
    saved = client.put("/api/settings/gandi/live", json={"enabled": False, "rate": 8, "days": 10}).json()
    assert (saved["enabled"], saved["rate"], saved["days"], saved["tlds"], saved["configured"]) == (False, 8.0, 10, [], ["com"])
    client.put("/api/settings/gandi", json={"key": "pat_secret-123"})
    live = client.get("/api/settings").json()["gandi"]["live"]
    assert live["tlds"] == ["com"] and live["per_minute_cap"] == gandi.GLOBAL_PER_MINUTE and live["max_rate"] == 15
    diag = client.get("/api/diag").json()
    assert diag["gandi_live"]["tlds"] == ["com"] and "pat_secret" not in json.dumps(diag)
    assert any(j["id"] == "check_gandi_live" for j in diag["jobs"])
    # Une ligne de .com par Gandi seul : « pas encore vérifié », état de base de chaque nom.
    items = client.get("/api/explore", params={"tld": "com", "q": "jardin", "mode": "exact", "status": "all"}).json()["items"]
    assert items and items[0]["status"] == explore.UNCHECKED and items[0]["status_label"] == "Pas encore vérifié"
    st.store.set("gandi_live_enabled", "0")
    client.__exit__(None, None, None)


def test_price_grid_one_call_per_extension_a_day(env):
    """Prix normal de chaque extension : un appel par jour sur un nom tiré au hasard, au lieu de redemander chaque
    libre. Un prix qui change est reporté sur tous les libres au tarif normal de l'extension, pas sur les premiums."""
    calls = []
    price = {"value": 13.2}

    def handler(request):
        calls.append(request.url.params["name"])
        line = {**STANDARD, "price_after_taxes": price["value"], "price_before_taxes": round(price["value"] / 1.2, 2)}
        return httpx.Response(200, json=answer("available", [line], request.url.params["name"]))

    client = client_for(env, handler)
    now = utcnow()
    for name, kind, value in (("jardin.com", "available", 13.2), ("abe.com", "premium", 878.4), ("velo.xyz", "available", 13.2)):
        env.conn.execute("INSERT INTO gandi_checks (name, status, kind, price, checked_at) VALUES (?, 'available', ?, ?, ?)",
                         (name, kind, value, iso(now)))
    assert asyncio.run(client.refresh_grid(["com", "xyz"], now)) == {"com": 13.2, "xyz": 13.2}
    assert len(calls) == 2 and all(len(c.split(".")[0]) == 16 for c in calls)
    assert set(gandi.grid(env.fr.store)) == {"com", "xyz"}
    assert asyncio.run(client.refresh_grid(["com", "xyz"], now)) == {} and len(calls) == 2  # une fois par jour
    price["value"] = 15.6
    assert asyncio.run(client.refresh_grid(["com"], now + timedelta(days=1))) == {"com": 15.6}
    found = gandi.load(env.conn, ["jardin.com", "abe.com", "velo.xyz"])
    assert found["jardin.com"]["price"] == 15.6 and found["abe.com"]["price"] == 878.4 and found["velo.xyz"]["price"] == 13.2


def test_a_free_name_taken_since_loses_its_price(env):
    """Un nom dit libre (prix d'achat) puis enregistré : « Enregistré » partout, plus de prix, plus dans le filtre
    « prix d'achat au plus » ; la ligne de Gandi est effacée (au fil de la passe DNS, et chaque heure pour la zone et
    le registre). Libéré de nouveau, il est redemandé à Gandi."""
    client = gandi_only(env)
    client.record("jardin.com", gandi.read(answer("available", [STANDARD])))
    client.record("velo.com", gandi.read(answer("available", [STANDARD])))

    def one(label, **k):
        return env.search("com", q=label, mode="exact", status="all", premium=True, **k)["items"]

    assert one("jardin", gprice=20)[0]["status"] == explore.FREE
    # Repris, vu par la seule règle d'état (ligne de Gandi encore là) : plus achetable, même au filtre de prix.
    env.conn.execute("INSERT INTO cc_dns (name, present, checked_at, seen_at) VALUES ('velo.com', 1, ?, ?)",
                     (iso(utcnow()),) * 2)
    env.touch("velo.com")
    velo = one("velo")[0]
    assert velo["status"] == explore.REGISTERED and not velo.get("gandi_state") and one("velo", gprice=20) == []
    assert env.forget_taken_prices() == 1 and "velo.com" not in gandi.load(env.conn, ["velo.com"])
    # Vu délégué par la passe DNS : effacé tout de suite.
    from app import ccdns
    ccdns.ComDns(env.conn, env.fr.store, env, timezone.utc).save("jardin", True, iso(utcnow()))
    assert gandi.load(env.conn, ["jardin.com"]) == {}

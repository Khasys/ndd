"""Passe des absents de zone : état au registre des noms absents d'une zone ICANN, sans bruit dans la Veille."""

import asyncio
from datetime import datetime, timedelta, timezone

import httpx

from app import absent, drops, explore, gandi, morning, notify, states
from app.checker import iso, utcnow
from app.rdap import LookupResult
from tests.test_explore import FakeRdap, env, zone  # noqa: F401  (fixture env)


async def _off(fn, *args):
    return fn(*args)


BASE = "https://rdap.test/"


async def _server(name):
    return BASE


def registered(*statuses):
    expires = (datetime.now(timezone.utc) + timedelta(days=200)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {"objectClassName": "domain", "status": list(statuses or ["active"]),
            "events": [{"eventAction": "expiration", "eventDate": expires}]}


def make(env, answers, names):
    """Passe sur ces seuls noms : answers[nom] est un code HTTP (404, 429…) ou une réponse RDAP (200)."""
    zone(env, [], ["maison"])
    calls = []

    def handler(request):
        name = request.url.path.rsplit("/", 1)[1]
        calls.append(name)
        answer = answers[name]
        if callable(answer):
            answer = answer()
        if isinstance(answer, int):
            return httpx.Response(answer, headers={"Retry-After": "30"} if answer == 429 else {})
        return httpx.Response(200, json=answer)

    slept = []
    clock = {"now": 1000.0}

    async def sleep(seconds):
        slept.append(seconds)
        clock["now"] += seconds  # temps simulé : une pause de 30 min ne dure rien
        await asyncio.sleep(0)

    run = absent.AbsentPass(env, env.fr.store, servers=_server, sleep=sleep,
                            http_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    queued = {"left": list(names)}

    def pending(limit, now=None):
        found, queued["left"] = queued["left"], []
        return found

    run.pending = pending
    run.clock = lambda: clock["now"]
    freed = []
    env.drops.on_freed = freed.append
    return run, calls, freed, slept


def test_pending_takes_absent_names_best_first_and_skips_known_ones(env):
    zone(env, [], ["maison"])
    run = absent.AbsentPass(env, env.fr.store)
    assert run.tlds() == ["com"]
    names = run.pending(300)
    data = env.status_bytes("com")
    u = env.universe
    assert names and all(data[u.index(n[:-4])] == explore.ABSENT for n in names)
    assert "maison.com" not in names and all(len(n) > 6 for n in names)  # pas de nom de 1 ou 2 caractères
    # Du meilleur score de base au moins bon.
    scores = [u.base_table[u.index(n[:-4])] & 127 for n in names]
    assert scores == sorted(scores, reverse=True)
    # Un nom déjà dans la Veille (vérifié à la main, sorti de la zone) n'est pas redemandé.
    env.drops.ensure(names[0], utcnow())
    assert names[0] not in run.pending(300)


def test_pass_records_the_registry_state_quietly(env):
    answers = {"jardin.com": 404, "forhome.com": registered("redemption period"), "smart.com": registered(),
               "held.com": registered("client hold")}
    run, calls, freed, _ = make(env, answers, list(answers))
    stats = asyncio.run(run.run(_off))
    assert stats["asked"] == 4 and sorted(calls) == sorted(answers)
    assert run.today["kinds"] == {states.AVAILABLE: 1, states.REDEMPTION: 1, drops.TAKEN: 1, drops.SUSPENDED: 1}
    data, u = env.status_bytes("com"), env.universe
    assert data[u.index("jardin")] == explore.FREE
    assert data[u.index("forhome")] == explore.COMING
    assert data[u.index("smart")] == explore.REGISTERED
    assert data[u.index("held")] == explore.SUSPENDED
    # Ni Gandi tout de suite (des milliers de noms), ni Veille, ni alertes, ni quotidien : pas une libération.
    assert freed == []
    assert all(r["origin"] == "rdap" and r["removed_on"] == drops.MANUAL for r in env.conn.execute("SELECT * FROM drops"))
    since = utcnow() - timedelta(hours=1)
    assert notify.changed(env.conn, since) == []
    assert not morning.sections(env.conn, env, {}, "", utcnow(), 0)
    # La fiche dit ce que le registre a répondu, sans « Libération constatée ».
    kinds = [e["kind"] for e in env.timeline("jardin.com")]
    assert "drop_seen" in kinds and "available" not in kinds
    assert run.status()["totals"] == {states.AVAILABLE: 1, states.REDEMPTION: 1, drops.TAKEN: 1, drops.SUSPENDED: 1}


def test_a_later_release_is_a_real_event(env):
    """Vu en rédemption par la passe, puis libre à la vérification suivante : là, c'est une libération (prix Gandi)."""
    run, _, freed, _ = make(env, {"forhome.com": registered("redemption period")}, ["forhome.com"])
    asyncio.run(run.run(_off))
    env.drops.rdap = FakeRdap({"forhome.com": LookupResult(available=True)})
    asyncio.run(env.drops.check(env.drops.row("forhome.com")))
    assert freed == ["forhome.com"]


def test_manual_check_takes_the_name_into_the_veille(env):
    run, _, _, _ = make(env, {"jardin.com": 404}, ["jardin.com"])
    asyncio.run(run.run(_off))
    env.drops.rdap = FakeRdap({"jardin.com": LookupResult(available=True)})
    asyncio.run(env.verify("jardin.com"))
    assert env.drops.row("jardin.com")["origin"] == "manual"


def test_refusal_halves_the_rate_pauses_and_asks_the_name_again(env):
    """429 : rien n'est écrit, le rythme est divisé par deux, pause du Retry-After, puis le même nom est redemandé."""
    tries = {"n": 0}

    def limited():
        tries["n"] += 1
        return 429 if tries["n"] == 1 else 404

    run, calls, _, slept = make(env, {"jardin.com": limited}, ["jardin.com"])
    asyncio.run(run.run(_off))
    assert calls == ["jardin.com", "jardin.com"]
    assert run.rate_of(BASE) == max(absent.MIN_RATE, absent.START_RATE / 2) and 30 in [round(s) for s in slept]
    assert run.today["refused"] == 1 and run.today["kinds"] == {states.AVAILABLE: 1}
    registry = run.status()["registries"]["rdap.test"]
    assert "429" in registry["last_error"] and registry["refused"] == 1 and registry["checked"] == 1


def test_a_blocked_registry_pauses_long_without_stopping_the_others(env):
    """403 sur un registre : pause longue (BLOCKED_PAUSE) pour lui seul ; l'autre continue pendant ce temps."""
    blocked = {"n": 0}

    def com():
        blocked["n"] += 1
        return 403 if blocked["n"] == 1 else 404

    run, calls, _, slept = make(env, {"jardin.com": com, "a.org": 404, "b.org": 404, "c.org": 404},
                                ["jardin.com", "a.org", "b.org", "c.org"])

    async def servers(name):
        return "https://rdap.verisign.test/" if name.endswith(".com") else "https://rdap.pir.test/"

    run._servers = servers
    asyncio.run(run.run(_off))
    assert calls.index("c.org") < len(calls) - 1 and calls[-1] == "jardin.com"  # l'.org n'a pas attendu le .com
    assert round(max(slept)) >= absent.BLOCKED_PAUSE - 5
    registries = run.status()["registries"]
    assert registries["rdap.verisign.test"]["refused"] == 1 and "403" in registries["rdap.verisign.test"]["last_error"]
    assert registries["rdap.pir.test"]["refused"] == 0 and registries["rdap.pir.test"]["checked"] == 3


def test_rate_grows_while_the_registry_keeps_up(env, monkeypatch):
    monkeypatch.setattr(absent, "GROW_EVERY", 5)
    names = [f"nom{n}.com" for n in range(12)]
    run, _, _, _ = make(env, {n: 404 for n in names}, names)
    asyncio.run(run.run(_off))
    assert run.rate_of(BASE) == round(round(absent.START_RATE * absent.GROW, 2) * absent.GROW, 2)
    run.store.set(absent.RATES_KEY, '{"https://rdap.test/": 1000}')
    assert run.rate_of(BASE) == absent.MAX_RATE


def test_names_seen_registered_are_asked_again_after_a_month(env):
    run, _, _, _ = make(env, {"smart.com": registered()}, ["smart.com"])
    asyncio.run(run.run(_off))
    real = absent.AbsentPass(env, env.fr.store)
    assert "smart.com" not in real.pending(10_000_000)
    old = iso(utcnow() - timedelta(days=absent.RECHECK_DAYS + 1))
    env.conn.execute("UPDATE drops SET checked_at = ? WHERE name = 'smart.com'", (old,))
    assert real.pending(10_000_000)[-1] == "smart.com"


def test_pass_waits_during_the_zone_reading_and_can_be_turned_off(env):
    run, calls, _, slept = make(env, {"jardin.com": 404}, ["jardin.com"])
    reading = {"left": 2}

    def busy():
        reading["left"] -= 1
        return reading["left"] >= 0

    asyncio.run(run.run(_off, busy=busy))
    assert slept[:2] == [60, 60] and calls == ["jardin.com"]
    env.fr.store.set("absent_pass_enabled", "0")
    assert asyncio.run(run.run(_off)) == {"started": False}


def test_free_com_names_skip_gandi_and_use_the_grid_price(env):
    """.com : prix unique du registre. La passe Gandi ne les prend plus ; le filtre de prix les garde au prix de la grille."""
    run, _, _, _ = make(env, {"jardin.com": 404}, ["jardin.com"])
    asyncio.run(run.run(_off))
    night = gandi.NightPass(gandi.Gandi(env.fr.store, conn=env.conn), env.conn, env.fr.store, env, timezone.utc)
    assert night.pending(100) == []
    env.fr.store.set(gandi.GRID_KEY, '{"com": {"price": 13.2, "price_ht": 11, "currency": "EUR", "at": "x"}}')
    i = env.universe.index("jardin")
    assert i in env._gandi_indices("com", 20) and i not in env._gandi_indices("com", 10)

"""Passe des absents de zone : état au registre des noms absents d'une zone ICANN, sans bruit dans la Veille."""

import asyncio
from datetime import datetime, timedelta, timezone

from app import absent, drops, explore, morning, notify, states
from app.checker import iso, utcnow
from app.rdap import DomainInfo, LookupResult
from tests.test_explore import FakeRdap, env, zone  # noqa: F401  (fixture env)


async def _off(fn, *args):
    return fn(*args)


def _registered(statuses=("active",), days=200):
    return LookupResult(found=True, info=DomainInfo(statuses=list(statuses),
                                                    expires_at=datetime.now(timezone.utc) + timedelta(days=days)))


def make(env, results):
    zone(env, [], ["maison"])
    env.drops.rdap = FakeRdap(results)
    freed = []
    env.drops.on_freed = freed.append
    return absent.AbsentPass(env, env.fr.store), freed


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
    run, freed = make(env, {"jardin.com": LookupResult(available=True), "forhome.com": _registered(["redemption period"]),
                            "smart.com": _registered(), "held.com": _registered(["client hold"])})
    run._queue, run._queue_at = ["jardin.com", "forhome.com", "smart.com", "held.com"], utcnow().timestamp()
    stats = asyncio.run(run.run(_off))
    assert stats["checked"] == 4 and stats["error"] is None
    assert stats["kinds"] == {states.AVAILABLE: 1, states.REDEMPTION: 1, drops.TAKEN: 1, drops.SUSPENDED: 1}
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
    keys = [e["kind"] for e in env.timeline("jardin.com")]
    assert "drop_seen" in keys and "available" not in keys
    assert run.status()["totals"] == {states.AVAILABLE: 1, states.REDEMPTION: 1, drops.TAKEN: 1, drops.SUSPENDED: 1}


def test_a_later_release_is_a_real_event(env):
    """Vu en rédemption par la passe, puis libre à la vérification suivante : là, c'est une libération (prix Gandi)."""
    run, freed = make(env, {"forhome.com": _registered(["redemption period"])})
    run._queue, run._queue_at = ["forhome.com"], utcnow().timestamp()
    asyncio.run(run.run(_off))
    env.drops.rdap.results["forhome.com"] = LookupResult(available=True)
    asyncio.run(env.drops.check(env.drops.row("forhome.com")))
    assert freed == ["forhome.com"]


def test_manual_check_takes_the_name_into_the_veille(env):
    run, _ = make(env, {"jardin.com": LookupResult(available=True)})
    run._queue, run._queue_at = ["jardin.com"], utcnow().timestamp()
    asyncio.run(run.run(_off))
    asyncio.run(env.verify("jardin.com"))
    assert env.drops.row("jardin.com")["origin"] == "manual"


def test_registry_errors_stop_the_batch_and_write_nothing(env):
    limit = LookupResult(error="Limite de requêtes du registre atteinte (429)")
    run, _ = make(env, {f"nom{n}.com": limit for n in range(10)})
    run._queue, run._queue_at = [f"nom{n}.com" for n in range(10)], utcnow().timestamp()
    stats = asyncio.run(run.run(_off))
    assert stats["checked"] == 0 and "429" in stats["error"]
    assert len(env.drops.rdap.calls) == absent.ERROR_STOP and len(run._queue) == 10 - absent.ERROR_STOP
    assert env.conn.execute("SELECT COUNT(*) FROM drops").fetchone()[0] == 0


def test_names_seen_registered_are_asked_again_after_a_month(env):
    run, _ = make(env, {"smart.com": _registered()})
    run._queue, run._queue_at = ["smart.com"], utcnow().timestamp()
    asyncio.run(run.run(_off))
    assert "smart.com" not in run.pending(10_000_000)
    old = iso(utcnow() - timedelta(days=absent.RECHECK_DAYS + 1))
    env.conn.execute("UPDATE drops SET checked_at = ? WHERE name = 'smart.com'", (old,))
    assert run.pending(10_000_000)[-1] == "smart.com"


def test_pass_can_be_turned_off(env):
    run, _ = make(env, {})
    env.fr.store.set("absent_pass_enabled", "0")
    assert asyncio.run(run.run(_off)) == {"started": False}

import asyncio
import time
from datetime import datetime, timedelta, timezone

import pytest

from app import db, drops, explore, frdata, states
from app.rdap import DomainInfo, LookupResult
from app.store import Store


class FakeRdap:
    def __init__(self, results):
        self.results = results
        self.calls = []

    async def lookup(self, name):
        self.calls.append(name)
        return self.results[name]


@pytest.fixture
def env(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    db.migrate(conn, tmp_path / "backups")
    store = Store(conn, {})
    watcher = drops.DropWatcher(conn, store, tmp_path, rdap=FakeRdap({}), delays=states.load_delays())
    watcher.workers = 1
    fr = frdata.FrWatcher(conn, store, tmp_path, rdap=watcher.rdap, drops_watcher=watcher)
    store.set("fr_enabled", "1")
    store.set("czds_approved", '["com"]')
    yield explore.Explorer(conn, watcher, fr)
    conn.close()


def add_fr(ex, label, removed=None):
    ex.conn.execute("INSERT INTO fr_names (label, created_on, removed_on) VALUES (?, '2010-02-01', ?)", (label, removed))


def zone(ex, labels, words):
    bits = bytearray(drops.BITMAP_BYTES)
    for label in labels:
        i = drops.encode(label)
        bits[i // 8] |= 1 << (i % 8)
    ex.drops._save("com", bits, set(words))


def test_universe_skips_names_with_edge_hyphens(env):
    u = env.universe
    status = bytearray([explore.ABSENT]) * u.size
    u.apply_valid(status)
    assert status[drops.encode("a-b")] == explore.ABSENT
    assert status[drops.encode("-ab")] == explore.INVALID and status[drops.encode("ab-")] == explore.INVALID
    assert status[drops.encode("-")] == explore.INVALID
    assert u.label(u.index("navarro")) == "navarro"


def test_fr_states_and_filters(env):
    add_fr(env, "navarro")
    add_fr(env, "velo", removed="2026-01-01")
    assert env.tlds() == ["fr"]
    counts = env.counts("fr")
    assert counts["registered"] == 1 and counts["maybe_free"] > 1_000_000

    found = env.search("fr", q="nav", mode="prefix", category="surname", status="registered")
    assert [i["label"] for i in found["items"]] == ["navarro"]
    assert found["items"][0]["status_label"] == "Enregistré" and found["items"][0]["created_on"] == "2010-02-01"

    gone = env.search("fr", status="maybe_free", category="word", q="velo", mode="exact")
    assert [i["status_label"] for i in gone["items"]] == ["Supprimé, non repris"]


def test_paging_with_a_cursor_covers_everything_once(env):
    seen = []
    cursor = -1
    while True:
        page = env.search("fr", q="ab", mode="prefix", cursor=cursor, limit=500)
        seen += [i["label"] for i in page["items"]]
        if page["cursor"] is None:
            break
        cursor = page["cursor"]
    assert len(seen) == len(set(seen))
    # « ab » + 0, 1 ou 2 caractères, sans tiret final.
    assert "ab" in seen and "abc" in seen and "ab9z" in seen and "ab-" not in seen
    assert len([s for s in seen if len(s) <= 4]) == 1 + 36 + 36 * 37


def test_contains_and_length(env):
    items = env.search("fr", q="zz", mode="contains", length=3, limit=500)["items"]
    assert items and all("zz" in i["label"] and len(i["label"]) == 3 for i in items)


def test_zone_status_and_drops_overlay(env):
    zone(env, ["abcd", "x"], ["navarro"])
    now = datetime.now(timezone.utc)
    env.drops.record("com", ["wxyz"], [], now)
    env.conn.execute("UPDATE drops SET state = 'available', available_since = ? WHERE label = 'wxyz'", (now.isoformat(),))
    assert "com" in env.tlds()
    card = {i["tld"]: i for i in env.card("navarro")["items"]}
    assert card["com"]["status_label"] == "Enregistré"
    by_label = {i["label"]: i for i in env.search("com", q="wxyz", mode="exact")["items"]}
    assert by_label["wxyz"]["status"] == explore.FREE
    absent = env.search("com", q="qqqq", mode="exact")["items"][0]
    assert absent["status_label"] == explore.ABSENT_ZONE


def test_suggestions(env):
    assert "navarro" in env.suggest("navar") and "navar" not in env.suggest("navar")
    # Un nom court est toujours proposé tel quel : il fait partie des noms suivis.
    assert env.suggest("qz")[0] == "qz"


def test_status_scan_jumps_fast(env):
    add_fr(env, "zzzz")
    start = time.monotonic()
    page = env.search("fr", status="registered")
    assert [i["label"] for i in page["items"]] == ["zzzz"] and page["cursor"] is None
    assert time.monotonic() - start < 5


def test_manual_check_goes_through_the_afnic_log(env):
    add_fr(env, "velo")
    env.fr.rdap = env.drops.rdap = FakeRdap({"velo.fr": LookupResult(available=True)})
    item = asyncio.run(env.verify("velo.fr"))
    assert item["status"] == explore.FREE and item["drop_state"] == states.AVAILABLE
    assert [r[0] for r in env.conn.execute("SELECT source FROM fr_requests")] == ["manuel"]


def test_manual_check_of_a_registered_name_creates_no_drop(env):
    add_fr(env, "navarro")
    expires = datetime.now(timezone.utc) + timedelta(days=100)
    env.fr.rdap = FakeRdap({"navarro.fr": LookupResult(found=True, info=DomainInfo(statuses=["active"], expires_at=expires))})
    item = asyncio.run(env.verify("navarro.fr"))
    assert item["status"] == explore.REGISTERED
    assert env.conn.execute("SELECT COUNT(*) FROM drops").fetchone()[0] == 0
    row = env.conn.execute("SELECT * FROM fr_names WHERE label = 'navarro'").fetchone()
    assert datetime.fromisoformat(row["next_check_at"]).date() == (expires + timedelta(days=1)).date()


def test_paused_afnic_refuses_manual_checks(env):
    env.fr._pause("Limite (429)", datetime.now(timezone.utc))
    with pytest.raises(explore.VerifyError, match="pause"):
        asyncio.run(env.verify("velo.fr"))

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
    counts = env.facets("fr", junk=True)
    assert counts["registered"] == 1 and counts["maybe_free"] > 1_000_000
    # Par défaut, seuls les noms qui ont un sens sont comptés.
    assert env.facets("fr")["maybe_free"] < 200_000

    found = env.search("fr", q="nav", mode="prefix", category="surname", status="registered")
    assert [i["label"] for i in found["items"]] == ["navarro"]
    assert found["items"][0]["status_label"] == "Enregistré" and found["items"][0]["created_on"] == "2010-02-01"

    gone = env.search("fr", status="maybe_free", category="word", q="velo", mode="exact")
    assert [i["status_label"] for i in gone["items"]] == ["Supprimé, non repris"]


def test_paging_with_a_cursor_covers_everything_once(env):
    seen = []
    cursor = -1
    while True:
        page = env.search("fr", q="ab", mode="prefix", cursor=cursor, limit=500, junk=True)
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
    by_label = {i["label"]: i for i in env.search("com", q="wxyz", mode="exact", junk=True)["items"]}
    assert by_label["wxyz"]["status"] == explore.FREE
    absent = env.search("com", q="qqqq", mode="exact", junk=True)["items"][0]
    assert absent["status_label"] == explore.ABSENT_ZONE


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


def test_manual_check_outside_fr_keeps_a_held_name(env):
    # Enregistré mais sans DNS (client hold) : absent de la zone, la vérification doit le dire.
    zone(env, ["abcd"], ["navarro"])
    expires = datetime.now(timezone.utc) + timedelta(days=200)
    held = LookupResult(found=True, info=DomainInfo(statuses=["client hold", "client delete prohibited"], expires_at=expires))
    active = LookupResult(found=True, info=DomainInfo(statuses=["active"], expires_at=expires))
    env.drops.rdap = FakeRdap({"kylian.com": held, "wxyz.com": active})
    assert env.search("com", q="kylian", mode="exact")["items"][0]["status_label"] == explore.ABSENT_ZONE
    item = asyncio.run(env.verify("kylian.com"))
    assert item["status"] == explore.SUSPENDED and item["status_label"] == "Enregistré, suspendu (hold)"
    assert asyncio.run(env.verify("wxyz.com"))["status"] == explore.REGISTERED


def test_paused_afnic_refuses_manual_checks(env):
    env.fr._pause("Limite (429)", datetime.now(timezone.utc))
    with pytest.raises(explore.VerifyError, match="pause"):
        asyncio.run(env.verify("velo.fr"))


def test_value_scores_are_explained():
    from app import value
    assert value.base("ab") > value.base("abc") > value.base("mako") > value.base("1234") > value.base("ab12") > value.base("xkcd")
    assert value.base("oye") > value.base("qsd")
    found = value.score("maison", ["com"])
    assert found["reasons"][:2] == ["6 lettres", "mot n° 153"] and "pris en .com" in found["reasons"]
    old = {"created_on": "2008-01-01", "removed_on": "2026-03-01"}
    assert value.score("maison", [], old, today=datetime(2026, 10, 1).date())["score"] > value.score("maison", [])["score"]


def test_rank_puts_the_best_first_and_respects_filters(env):
    zone(env, ["ab"], ["maison"])
    add_fr(env, "zz")
    found = env.rank("fr", limit=20)["items"]
    scores = [i["score"] for i in found]
    assert scores == sorted(scores, reverse=True)
    assert all(i["status"] in (explore.GONE, explore.ABSENT, explore.FREE, explore.COMING) for i in found)
    assert "zz.fr" not in [i["name"] for i in found]  # enregistré : pas à saisir
    # Pris en .com : la demande remonte le nom.
    words = env.rank("fr", category="word", limit=5)["items"]
    assert words and all("word" in i["categories"] for i in words) and words[0]["reasons"]
    assert all(len(i["label"]) == 3 for i in env.rank("fr", length=3, limit=10)["items"])
    typed = env.rank("fr", q="mais", limit=5)["items"]
    assert typed and all(i["label"].startswith("mais") for i in typed)


def test_facets_follow_the_filters(env):
    add_fr(env, "ab")
    add_fr(env, "a1", removed="2025-01-01")
    all_fr = env.facets("fr")
    two = env.facets("fr", length=2)
    letters2 = env.facets("fr", category="letters", length=2)
    mixed2 = env.facets("fr", category="mixed", length=2)
    assert two["registered"] == 1 and letters2["registered"] == 1 and mixed2["registered"] == 0
    assert letters2["available"] == 26 * 26 - 1
    assert letters2["available"] + mixed2["available"] + env.facets("fr", category="digits", length=2)["available"] == two["available"]
    assert all_fr["available"] > two["available"]


def test_map_counts_per_extension_and_category(env):
    zone(env, ["ab"], ["maison"])
    add_fr(env, "ab")
    found = env.map()
    assert found["tlds"] == ["fr", "com"] and "Mots" in found["rows"].values()
    assert found["filters"]["short"] == {"cat": "", "len": -3} and found["filters"]["l4"] == {"cat": "letters", "len": 4}
    assert found["cells"]["fr"]["short"] == env.facets("fr", length=-3)["available"]
    assert found["cells"]["com"]["word"] == len(env.universe.by_list["word"]) - 1


def test_seo_analysis_reads_pagerank_and_wayback(env):
    import httpx
    from app import seo

    def handler(request):
        if request.url.host == "openpagerank.com":
            assert request.headers["API-OPR"] == "cle"
            return httpx.Response(200, json={"status_code": 200, "response": [
                {"domain": "velo.fr", "page_rank_decimal": 3.21, "status_code": 200}]})
        return httpx.Response(200, json=[["timestamp"], ["20040312000000"], ["20110101000000"], ["20190101000000"]])

    env.fr.store.set("opr_key", "cle")
    analyzer = seo.Seo(env.conn, env.fr.store, lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    rows = asyncio.run(analyzer.analyze(["velo.fr"]))
    row = rows["velo.fr"]
    assert row["pr"] == 3.21 and row["wb_first"] == "2004-03-12" and row["wb_years"] == 3 and row["error"] is None
    add_fr(env, "velo", removed="2025-01-01")
    item = env.rank("fr", q="velo", mode="exact")["items"][0]
    assert "PageRank 3,2" in item["reasons"] and any("2004" in r for r in item["reasons"])


def test_nature_rank_and_taken_filters(env):
    zone(env, ["ab"], ["manger", "maison"])
    verbs = env.rank("fr", category="word", nature="verb", sort="freq", limit=10)["items"]
    assert verbs and all("verb" in __import__("app").wordlists.natures()[i["label"]] for i in verbs)
    assert verbs[0]["label"] == "etre"
    top = env.rank("fr", category="word", max_rank=100, limit=500)["items"]
    assert top and all(i["rank"] <= 100 for i in top)
    taken = env.rank("fr", taken="com", limit=50)["items"]
    assert {i["label"] for i in taken} <= {"ab", "manger", "maison"} and "ab" in {i["label"] for i in taken}
    strong = env.rank("fr", min_score=45, limit=50)["items"]
    assert strong and all(i["score"] >= 45 for i in strong)


def test_facets_follow_every_filter(env):
    zone(env, ["ab"], ["manger"])
    verbs = env.facets("fr", category="word", nature="verb")
    assert verbs["available"] == len(env.universe.by_nature["verb"]) - len(
        [i for i in env.universe.by_nature["verb"] if "word" not in env.universe.categories(env.universe.label(i))])
    assert env.facets("fr", taken="com")["available"] == 2
    assert env.facets("fr", category="firstname", max_rank=10)["available"] == 10


def test_weights_change_the_order(env):
    env.fr.store.set("score_weights", '{"shape": 0, "popularity": 2}')
    first = env.rank("fr", limit=1)["items"][0]
    assert first["parts"]["shape"] == 0 and first["parts"]["popularity"] >= 50


def test_rdap_details_are_kept(env):
    from datetime import datetime, timezone
    add_fr(env, "velo")
    env.fr.rdap = FakeRdap({"velo.fr": LookupResult(found=True, info=DomainInfo(
        statuses=["active"], registrar="OVH", expires_at=datetime(2027, 1, 1, tzinfo=timezone.utc), nameservers=["ns1.ovh.net"]))})
    asyncio.run(env.verify("velo.fr"))
    item = env.search("fr", q="velo", mode="exact")["items"][0]
    assert item["rdap"]["registrar"] == "OVH" and item["rdap"]["nameservers"] == ["ns1.ovh.net"]


def test_one_name_is_patched_in_place_and_matches_a_full_rebuild(env):
    zone(env, ["ab"], ["maison"])
    add_fr(env, "velo")
    add_fr(env, "mot", removed="2025-01-01")
    filters = [{}, {"category": "word"}, {"category": "letters", "length": 4}, {"taken": "com"}, {"length": -3}]
    env.warm()
    before = [env.facets("fr", **f) for f in filters]
    env.fr.rdap = env.drops.rdap = FakeRdap({"mot.fr": LookupResult(available=True), "velo.fr": LookupResult(available=True)})
    asyncio.run(env.verify("mot.fr"))
    asyncio.run(env.verify("velo.fr"))
    patched = [env.facets("fr", **f) for f in filters]
    assert patched != before
    status = env.status_bytes("fr")
    assert status[env.universe.index("velo")] == explore.FREE
    # Recalcul complet à neuf : mêmes chiffres que la correction en place.
    env.invalidate()
    assert [env.facets("fr", **f) for f in filters] == patched
    assert env.map() == env._build_map()


def test_background_checks_patch_the_explorer(env):
    add_fr(env, "velo")
    env.warm()
    now = datetime.now(timezone.utc)
    env.drops.record("fr", ["velo"], [], now)
    env.drops.apply(env.drops.row("velo.fr"), LookupResult(available=True), now)
    assert env.status_bytes("fr")[env.universe.index("velo")] == explore.FREE


def test_seo_filter_and_sort(env):
    for label, pr, years in (("velo", 4.0, 12), ("ciel", 1.5, 2), ("mer", None, 8)):
        add_fr(env, label, removed="2024-01-01")
        env.conn.execute("INSERT INTO domain_seo (name, pr, wb_years, checked_at) VALUES (?, ?, ?, '2026-10-01')", (f"{label}.fr", pr, years))
    by_seo = [i["label"] for i in env.rank("fr", sort="seo", limit=10)["items"]]
    # Les noms analysés d'abord, puis les autres par score : le tri ne vide pas la liste.
    assert by_seo[:3] == ["velo", "ciel", "mer"] and len(by_seo) == 10
    assert [i["label"] for i in env.rank("fr", seo="pr3", limit=10)["items"]] == ["velo"]
    assert {i["label"] for i in env.rank("fr", seo="wb5", limit=10)["items"]} == {"velo", "mer"}
    assert env.facets("fr", seo="pr1")["available"] == 2


def test_length_sort_orders_long_names_too(env):
    items = env.rank("fr", category="word", length=5, sort="length", limit=200)["items"]
    lengths = [len(i["label"]) for i in items]
    assert lengths == sorted(lengths) and lengths[0] == 5


def test_coming_names_use_the_legend_labels(env):
    # « Libération à venir » regroupe trois états : la ligne montre le bon, comme l'écran Domaines.
    add_fr(env, "velo")
    now = datetime.now(timezone.utc)
    env.drops.record("fr", ["velo"], [], now)
    env.conn.execute("UPDATE drops SET state = ? WHERE name = 'velo.fr'", (states.REDEMPTION,))
    env.touch("velo.fr")
    item = env.search("fr", q="velo", mode="exact", status="coming")["items"][0]
    assert item["status"] == explore.COMING and item["status_label"] == "En rédemption"


def test_fr_dns_marks_delegated_names_as_taken(env, monkeypatch):
    from app import frdns

    monkeypatch.setattr(frdns, "RATE", 1_000_000.0)
    add_fr(env, "velo", removed="2025-01-01")
    add_fr(env, "navarro")
    checker = frdns.FrDns(env.conn, env.fr.store, env, timezone.utc)
    asked = []

    async def query(label):
        asked.append(label)
        return {"velo": True, "maison": False}.get(label, None if label == "chat" else False)

    checker.query = query
    labels = checker.pending(1_000_000)
    assert "velo" in labels and "maison" in labels and "navarro" not in labels
    assert not any(len(x) <= 2 for x in labels)  # formats réservés
    env.warm()
    chosen = ["velo", "maison", "chat", "lampe"]
    checker.pending = lambda limit, now=None: chosen
    stats = asyncio.run(checker.run(limit=4))
    assert stats["newly_taken"] == 1 and stats["skipped"] == (1 if "chat" in asked else 0)
    status = env.status_bytes("fr")
    assert status[env.universe.index("velo")] == explore.REGISTERED
    assert status[env.universe.index("maison")] == explore.ABSENT
    assert env.fiche("velo.fr")["dns"]["present"] is True
    # Correction en place identique au recalcul complet.
    patched = env.facets("fr")
    env.invalidate()
    assert env.facets("fr") == patched
    # Revu le lendemain seulement ; un nom repris par le DNS reste dans la file s'il en sort.
    del checker.pending
    assert "maison" not in checker.pending(1_000_000)
    env.conn.execute("UPDATE name_dns SET checked_at = '2000-01-01T00:00:00Z'")
    assert "velo" in checker.pending(1_000_000)
    # Une passe par jour.
    assert not checker.due(datetime.now(timezone.utc))


def test_new_lists_wait_for_the_next_zone_read(env):
    from app import wordlists

    zone(env, [], ["maison"])
    assert {"insurance", "maison"} <= wordlists.lists()["english"] | wordlists.lists()["word"]
    u = env.universe
    assert env.status_bytes("com")[u.index("insurance")] == explore.ABSENT
    # Lecture faite avant l'ajout des nouvelles listes : leurs noms sont masqués, pas « libres ».
    env.drops.store.set("zone_lists_com", "")
    env.invalidate("com")
    status = env.status_bytes("com")
    assert status[u.index("insurance")] == explore.INVALID
    assert status[u.index("maison")] == explore.REGISTERED and status[u.index("courir")] == explore.ABSENT
    assert env.drops.covered("com") == set(wordlists.LEGACY)


def test_language_filter(env):
    add_fr(env, "zz")
    fr = [i["label"] for i in env.rank("fr", lang="fr", limit=200)["items"]]
    en = [i["label"] for i in env.rank("fr", lang="en", category="sector", limit=200, sort="freq")["items"]]
    assert fr and all(l in __import__("app.wordlists", fromlist=["x"]).languages()["fr"] for l in fr)
    assert "insurance" in en and "mutuelle" not in en
    counts = env.facets("fr", lang="en")
    assert 0 < counts["available"] < env.facets("fr")["available"]

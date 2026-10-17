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


def test_auto_renewed_fr_is_registered_with_a_next_read(env):
    # Statut « auto renew period » et nouvelle échéance : renouvelé, pas expiré ; une lecture reste prévue.
    add_fr(env, "seven")
    now = datetime.now(timezone.utc)
    expires = now + timedelta(days=362)
    renewed = LookupResult(found=True, info=DomainInfo(statuses=["active", "auto renew period"], expires_at=expires))
    env.drops.record("fr", ["seven"], [], now)  # suivi dans Suppressions après une lecture « expiré »
    env.fr.rdap = env.drops.rdap = FakeRdap({"seven.fr": renewed})
    item = asyncio.run(env.verify("seven.fr"))
    assert item["status"] == explore.REGISTERED
    row = env.conn.execute("SELECT * FROM fr_names WHERE label = 'seven'").fetchone()
    at = datetime.fromisoformat(row["next_check_at"])
    assert now + timedelta(days=44) < at < now + timedelta(days=46)
    assert env.fiche("seven.fr")["rdap_next"]["why"] == "suivi des échéances .fr"
    # Lecture de date anniversaire : même conclusion, rien n'est confié à l'écran Suppressions.
    env.conn.execute("DELETE FROM drops")
    env.conn.execute("UPDATE fr_names SET next_check_at = '2000-01-01T00:00:00Z' WHERE label = 'seven'")
    asyncio.run(env.fr.tick())
    assert env.drops.row("seven.fr") is None
    row = env.conn.execute("SELECT * FROM fr_names WHERE label = 'seven'").fetchone()
    assert datetime.fromisoformat(row["next_check_at"]) > now + timedelta(days=44)


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
    assert found["reasons"][:2] == ["6 lettres", "153e mot le plus courant"] and "pris en .com" in found["reasons"]
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
    # .com : pas de premium chez le registre (prix unique) ; tous les mots libres comptent.
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
    env.drops._path("com", "searched").unlink()
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


def test_fiche_tells_when_rdap_is_next_read(env):
    add_fr(env, "velo")
    env.conn.execute("UPDATE fr_names SET next_check_at = '2027-01-02T12:00:00Z' WHERE label = 'velo'")
    assert env.fiche("velo.fr")["rdap_next"] == {"at": "2027-01-02T12:00:00Z", "why": "suivi des échéances .fr"}
    assert env.fiche("zzqx.fr")["rdap_next"]["at"] is None



def test_names_added_after_a_zone_read_wait_for_the_next_one(env):
    zone(env, [], ["maison"])
    # Lecture faite avec une liste sans « insurance » : il attend la lecture suivante.
    env.drops._write(env.drops._path("com", "searched"), b"maison\ncourir")
    env.invalidate("com")
    status = env.status_bytes("com")
    u = env.universe
    assert status[u.index("insurance")] == explore.INVALID
    assert status[u.index("courir")] == explore.ABSENT and status[u.index("maison")] == explore.REGISTERED


def test_fr_dns_slows_down_then_recovers(env, monkeypatch):
    from app import frdns

    monkeypatch.setattr(frdns, "RATE", 1_000_000.0)
    monkeypatch.setattr(frdns, "MIN_RATE", 1000.0)
    checker = frdns.FrDns(env.conn, env.fr.store, env, timezone.utc)
    labels = [f"nom{i}" for i in range(200)]
    checker.pending = lambda limit, now=None: labels
    calls = []

    async def query(label):
        calls.append(label)
        return None if label in ("nom0", "nom1", "nom2") else False

    checker.query = query
    stats = asyncio.run(checker.run(limit=200))
    # Trois silences au début, puis tout répond : la passe va au bout sans rester au ralenti.
    assert stats["skipped"] == 3 and stats["checked"] == 197 and len(calls) == 200


def test_fr_dns_stops_on_refusal_and_lowers_the_cap(env, monkeypatch):
    from app import frdns

    monkeypatch.setattr(frdns, "RATE", 1_000_000.0)
    alerts = []

    async def notify(text):
        alerts.append(text)

    checker = frdns.FrDns(env.conn, env.fr.store, env, timezone.utc, notify=notify)
    checker.pending = lambda limit, now=None: [f"nom{i}" for i in range(50)]

    async def query(label):
        if label == "nom3":
            return frdns.Failure(frdns.TIMED_OUT, "192.0.2.1")
        return frdns.Failure(frdns.REFUSED, "192.0.2.2") if label == "nom5" else False

    checker.query = query
    stats = asyncio.run(checker.run(limit=50))
    # Le refus arrête la passe : on n'insiste pas, la cause et le serveur sont gardés.
    assert stats["checked"] == 4 and stats["stopped"] == "refus du serveur 192.0.2.2"
    assert stats["failures"] == {frdns.TIMED_OUT: 1, frdns.REFUSED: 1}
    assert stats["failure_log"][-1]["server"] == "192.0.2.2" and stats["failure_log"][-1]["name"] == "nom5.fr"
    assert checker.cap() < frdns.MAX_RATE and alerts and "refus" in alerts[0]

    # Un serveur muet : arrêt après FAIL_STOP échecs d'affilée.
    async def silent(label):
        return frdns.Failure(frdns.TIMED_OUT, "192.0.2.1")

    checker.query = silent
    env.fr.store.set("fr_dns_cap", "")
    monkeypatch.setattr(frdns, "MIN_RATE", 1000.0)
    stats = asyncio.run(checker.run(limit=50))
    assert stats["skipped"] == frdns.FAIL_STOP and "d'affilée" in stats["stopped"]


def test_cc_dns_flags_names_that_leave_the_dns(env, monkeypatch):
    from app import ccdns, frdns

    monkeypatch.setattr(frdns, "RATE", 1_000_000.0)
    alerts = []

    async def notify(text):
        alerts.append(text)

    checker = ccdns.CcDns("io", env.conn, env.fr.store, env, timezone.utc, notify=notify)
    labels = checker.pending(200)
    assert labels and all(len(x) > 2 for x in labels)
    checker.pending = lambda limit, now=None: ["insurance", "maison", "cloud"]
    state = {"insurance": True, "maison": False, "cloud": True}

    async def query(label):
        return state[label]

    checker.query = query
    first = asyncio.run(checker.run(limit=3))
    # Première nuit : rien n'est un changement.
    assert first["checked"] == 3 and first["newly_taken"] == 0 and not alerts
    state["cloud"] = False
    asyncio.run(checker.run(limit=3))
    found = ccdns.load_names(env.conn, ["cloud.io", "insurance.io"])
    assert found["cloud.io"]["present"] is False and found["cloud.io"]["lost_at"]
    assert found["insurance.io"]["present"] is True and found["insurance.io"]["lost_at"] is None
    # Pas de message à part : le résumé du matin les liste.
    assert not alerts and checker.status()["freed_names"] == ["cloud.io"]
    assert env.fr.store.get("io_dns_status") and not env.fr.store.get("fr_dns_status")


def test_morning_summary_lists_good_names_with_links(env):
    from app import morning

    now = datetime.now(timezone.utc)
    env.drops.record("fr", ["velo", "zzqx"], [], now)
    env.conn.execute("UPDATE drops SET state = 'available', available_since = ? WHERE name IN ('velo.fr', 'zzqx.fr')",
                     (now.isoformat(),))
    env.touch("velo.fr")
    text = morning.build(env.conn, env, {}, "https://nas.exemple", now, min_score=30)
    assert text.startswith("Ce matin") and "velo.fr" in text and "https://nas.exemple/?fiche=velo.fr" in text
    assert "zzqx.fr" not in text  # score trop bas, pas d'avis : pas dans le résumé
    # Noms .co / .ai / .io sortis du DNS dans les dernières 24 h.
    cc = {"io": {"finished_at": now.isoformat(), "freed_names": ["cloud.io"]}}
    assert "cloud.io" in morning.build(env.conn, env, cc, "", now, min_score=30)
    # Rien de bon : pas de message.
    assert morning.build(env.conn, env, {}, "", now, min_score=100) is None


def test_health_alerts_open_and_close_once(env):
    from app import morning

    store = env.fr.store
    assert morning.health_message(store, {"zone:org": "Zone .org en erreur"}).startswith("Problème :")
    assert morning.health_message(store, {"zone:org": "Zone .org en erreur"}) is None  # déjà signalé
    closed = morning.health_message(store, {})
    assert closed.startswith("Réglé :") and "Zone .org" in closed
    assert morning.health_message(store, {}) is None


def test_premium_probable_bookable_and_release_sort(env):
    zone(env, [], ["maison"])
    u = env.universe
    # .com et .net sont sans premium par défaut : la règle s'y applique une fois cochée.
    assert env.status_bytes("com")[u.index("abd")] == explore.ABSENT
    env.fr.store.set("premium_off", "[]")
    env.invalidate("com")
    status = env.status_bytes("com")
    assert status[u.index("abd")] == explore.PREMIUM and status[u.index("maison")] == explore.REGISTERED
    # Masqués par défaut, visibles avec premium=1.
    hidden = env.search("com", q="abd", mode="exact", status="bookable")["items"]
    shown = env.search("com", q="abd", mode="exact", status="bookable", premium=True)["items"]
    assert hidden == [] and [i["label"] for i in shown] == ["abd"]
    counts = env.facets("com", premium=True)
    assert counts["premium"] > 0 and counts["bookable"] > env.facets("com")["bookable"]
    # Prix saisi dans Paramètres : affiché sur le nom ; dévaluation : score divisé par deux.
    env.fr.store.set("premium_prices", '{"com": {"3": 1200}}')
    assert shown and env.search("com", q="abd", mode="exact", premium=True)["items"][0]["premium_price"] == 1200
    before = env.value_of(["abd.com"])["abd.com"]["score"]
    env.fr.store.set("premium_devalue", "1")
    env.refresh_settings()
    after = env.value_of(["abd.com"])["abd.com"]
    assert after["score"] == before // 2 and "premium probable" in after["reasons"]
    # Tri par libération : la plus proche d'abord.
    now = datetime.now(timezone.utc)
    env.drops.record("com", ["maison"], [], now)
    env.conn.execute("UPDATE drops SET state = 'redemption', release_at = ? WHERE name = 'maison.com'",
                     ((now + timedelta(days=20)).isoformat(),))
    env.touch("maison.com")
    items = env.rank("com", status="coming", sort="release")["items"]
    assert [i["label"] for i in items] == ["maison"]


def test_release_sort_keeps_names_without_estimate(env):
    """Bientôt libres, tri par libération : un nom sans date estimée reste, à la fin."""
    zone(env, [], ["maison", "jardin"])
    now = datetime.now(timezone.utc)
    env.drops.record("com", ["maison", "jardin"], [], now)
    env.conn.execute("UPDATE drops SET state = 'redemption', release_at = ? WHERE name = 'maison.com'",
                     ((now + timedelta(days=20)).isoformat(),))
    env.conn.execute("UPDATE drops SET state = 'redemption', release_at = NULL WHERE name = 'jardin.com'")
    env.touch("maison.com")
    env.touch("jardin.com")
    items = env.rank("com", status="coming", sort="release")["items"]
    assert [i["label"] for i in items] == ["maison", "jardin"]


def test_ai_health_counts_opinions_of_every_guide_version(env):
    """Après un changement de version de consigne, un avis récent d'ancienne version n'est pas une panne."""
    from types import SimpleNamespace
    from app import morning

    env.fr.store.set("ai_token_hash", "x")
    now = datetime.now(timezone.utc)
    state = SimpleNamespace(drops=env.drops, fr=env.fr, explorer=env)
    env.conn.execute("INSERT INTO domain_ai (name, exploit, resale, quick_low, quick_high, final_low, final_high, comment, "
                     "rated_at, version) VALUES ('a.fr', 1, 1, 0, 0, 0, 0, 'À laisser.', ?, 1)", ((now - timedelta(days=5)).isoformat(),))
    assert "ai" in morning.problems(state, now)
    env.conn.execute("UPDATE domain_ai SET rated_at = ?", ((now - timedelta(hours=3)).isoformat(),))
    assert "ai" not in morning.problems(state, now)


def test_english_only_and_conjugated_words_count_less_in_fr(env):
    found = env.value_of(["should.fr", "vais.fr", "maison.fr"])
    assert "mot anglais en .fr" in found["should.fr"]["reasons"]
    assert "forme conjuguée" in found["vais.fr"]["reasons"]
    assert not {"mot anglais en .fr", "forme conjuguée"} & set(found["maison.fr"]["reasons"])


def test_levels_and_resale_exploit_rankings(env):
    zone(env, [], ["maison"])
    # Un nom faible mais noté « À prendre » par l'IA : il passe le niveau « pépites » et prend la tête
    # du classement « pour revendre ».
    env.conn.execute("""INSERT INTO domain_ai (name, exploit, resale, quick_low, quick_high, final_low, final_high,
                        comment, rated_at, version) VALUES ('saucisse.com', 40, 95, 0, 0, 100, 300,
                        'À prendre. Test. Acheteur type : test.', '2026-10-17', 4)""")
    env.ai_changed()
    assert [i["label"] for i in env.rank("com", q="saucisse", mode="exact", level="gem")["items"]] == ["saucisse"]
    assert env.rank("com", q="poutre", mode="exact", level="gem")["items"] == []
    assert all(i["score"] >= 50 for i in env.rank("com", status="available", level="gem", limit=50)["items"])
    resale = env.rank("com", status="available", sort="resale", limit=5)["items"]
    assert resale[0]["label"] == "saucisse"
    exploit = env.rank("com", status="available", sort="exploit", limit=5)["items"]
    assert exploit[0]["label"] != "saucisse" or exploit[0]["score"] <= 40


def test_shortest_first_fills_the_page_with_a_level(env):
    """« Les plus courts » avec un niveau : la page est pleine, pas tronquée par le filtre."""
    add_fr(env, "zzzzz")
    items = env.rank("fr", status="bookable", level="gem", sort="length", limit=50)["items"]
    assert len(items) == 50
    assert all(i["score"] >= explore.LEVEL_GEM for i in items)
    lengths = [len(i["label"]) for i in items]
    assert lengths == sorted(lengths)


def test_search_skips_absent_states_in_one_pass(env):
    """Un état absent de la suite ne fait pas relire tout le tableau à chaque nom trouvé."""
    import time

    add_fr(env, "zzzzz")
    started = time.monotonic()
    # 50 000 noms (taille d'un export) : 55 s avec un find par état, 1,3 s en une passe.
    found = env.search("fr", status="bookable", limit=50000)["items"]
    assert len(found) == 50000 and time.monotonic() - started < 15

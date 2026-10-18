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
    assert value.base("ab") > value.base("abc") > value.base("mako") > value.base("1234")
    # Mélanges et suites imprononçables : tout en bas, quel que soit l'ordre entre eux.
    assert max(value.base("ab12"), value.base("qzxw")) < 15 < value.base("1234") + 5
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
    checker.servers = ["192.0.2.1"]  # pas de résolution DNS dans les tests
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
    checker.servers = ["192.0.2.1"]  # pas de résolution DNS dans les tests
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
    checker.servers = ["192.0.2.1"]  # pas de résolution DNS dans les tests
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
    checker.servers = ["192.0.2.1"]  # pas de résolution DNS dans les tests
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


def test_demand_counts_only_what_is_rare_for_the_shape(env):
    """calp.fr : pris en .com, mais presque tous les 4 lettres prononçables le sont : peu de demande."""
    from app import value

    assert value.demand(["com", "org"])[0] == 14
    low, reason = value.demand(["com", "org"], {"com": 0.95, "org": 0.91})
    assert low <= 1 and "courant pour cette forme" in reason
    assert value.history("2008-01-01", "2026-01-01", datetime(2026, 10, 1).date(), common=0.9)[0] <= 3
    add_fr(env, "zzzzz")
    u = env.universe
    good4 = [drops.decode(i) for i in u.good_letters(4)]
    zone(env, good4, [])  # tous les 4 lettres prononçables pris en .com
    ctx = env._bonus_context("fr")
    assert ctx["rates"]["com"]["L4g"] == 1.0 and ctx["rates"]["com"]["L4b"] == 0.0
    found = env.value_of([f"{good4[0]}.fr"])[f"{good4[0]}.fr"]
    assert found["parts"]["demand"] == 0 and any("courant pour cette forme" in r for r in found["reasons"])


def test_review_pass_brings_back_ratings_far_from_the_estimate(env):
    """Second passage : un avis v5 très loin de l'estimation du score repasse, une seule fois, en tête."""
    from app import ai, quality

    add_fr(env, "zzzzz")
    estimate = quality.predict("maison", "fr")[0]
    env.conn.execute("""INSERT INTO domain_ai (name, exploit, resale, quick_low, quick_high, final_low, final_high, comment,
                        rated_at, version) VALUES ('maison.fr', 5, 5, 0, 0, 0, 100, 'À laisser. Test. Acheteur type : test.', 'x', ?)""",
                     (ai.VERSION,))
    assert estimate > 30
    first = ai.pending(env, 10)[0]
    assert first["name"] == "maison.fr" and first["review"] == {"previous_note": 5, "score_estimate": round(estimate)}
    env.conn.execute("UPDATE domain_ai SET reviewed_at = 'x' WHERE name = 'maison.fr'")
    assert all(n["name"] != "maison.fr" for n in ai.pending(env, 10))


def test_unranked_short_names_keep_a_true_bound(env):
    """Un 4 caractères mélangé n'est pas calculé à la construction (table à 0) : la borne qui sert à
    l'écarter vaut quand même au moins son score."""
    add_fr(env, "ab12", removed="2026-01-01")
    zone(env, ["ab12"], [])  # pris en .com : demande
    u = env.universe
    i = u.index("ab12")
    assert u.base_table[i] & 127 == 0
    score = env.value_of(["ab12.fr"])["ab12.fr"]["score"]
    assert env._bound("fr", i, "ab12", env._bonus_context("fr")) >= score
    items = env.rank("fr", q="ab12", mode="exact", junk=True, min_score=score)["items"]
    assert [x["label"] for x in items] == ["ab12"]


def test_review_pass_skips_names_outside_the_universe(env):
    """Un avis demandé depuis la fiche pour un nom hors listes ne prend pas la place d'un second passage."""
    from app import ai, quality

    add_fr(env, "zzzzz")
    rows = [(f"xqzwvb{n}.fr",) for n in range(ai.REVIEW_PER_NIGHT + 5)] + [("maison.fr",)]
    env.conn.executemany("""INSERT INTO domain_ai (name, exploit, resale, quick_low, quick_high, final_low, final_high,
                            comment, rated_at, version) VALUES (?, 5, 5, 0, 0, 0, 100, 'À laisser. Test. Acheteur type : test.',
                            'x', %d)""" % ai.VERSION, rows)
    assert ai.to_review(env) == [("maison.fr", 5, round(quality.predict("maison", "fr")[0]))]
    names = [n["name"] for n in ai.pending(env, 10)]
    assert names[0] == "maison.fr" and len(names) == 10


def test_resale_ranking_rates_only_the_names_that_can_enter(env, monkeypatch):
    """Classement « pour revendre » : les avis conseillés passent devant tout nom sans avis, dans l'ordre de
    leur note, sans noter chaque nom ; un avis « À laisser » ne passe devant personne."""
    from app import ai

    add_fr(env, "zzzzz")
    u = env.universe
    advised = [drops.decode(i) for i in u.good_letters(4)[:300]]
    env.conn.executemany("""INSERT INTO domain_ai (name, exploit, resale, quick_low, quick_high, final_low, final_high,
                            comment, rated_at, version) VALUES (?, 60, ?, 0, 0, 0, 100, ?, 'x', %d)""" % ai.VERSION,
                         [(f"{label}.fr", 50 + n % 50, "À prendre. Test. Acheteur type : test.") for n, label in enumerate(advised)]
                         + [("maison.fr", 95, "À laisser. Test. Acheteur type : test.")])
    env.ai_changed()
    calls = []
    original = env._value
    monkeypatch.setattr(env, "_value", lambda *a, **k: calls.append(a[2]) or original(*a, **k))
    items = env.rank("fr", sort="resale", limit=10)["items"]
    notes = [i["ai"]["resale"] for i in items]
    assert len(items) == 10 and notes == sorted(notes, reverse=True) and notes[0] == 99
    assert "maison" not in [i["label"] for i in items]
    assert len(calls) == 10


def test_hyphen_facet_counts_only_hyphenated_names(env):
    hyphen = env.facets("fr", category="hyphen", junk=True)["available"]
    mixed = env.facets("fr", category="mixed", junk=True)["available"]
    assert 0 < hyphen < mixed < env.facets("fr", junk=True)["available"]


def test_all_status_keeps_premiums_when_asked(env):
    zone(env, [], ["maison"])
    env.fr.store.set("premium_off", "[]")
    env.invalidate("com")
    assert env.search("com", q="abd", mode="exact", status="all")["items"] == []
    assert [i["label"] for i in env.search("com", q="abd", mode="exact", status="all", premium=True)["items"]] == ["abd"]
    assert env.facets("com", premium=True)["all"] > env.facets("com")["all"]


def test_save_keeps_the_grid_against_old_format_and_batch_duplicates(env):
    from app import ai

    add_fr(env, "zzzzz")
    known = ai.known_name(env)
    v5 = {"name": "velo.fr", "oral": 10, "radio": 10, "memo": 10, "sens": 10, "marque": 9, "marche": 8, "ext": 10,
          "acheteurs": 9, "risque": 0, "quick_low": 3000, "quick_high": 8000, "final_low": 15000, "final_high": 50000,
          "comment": "Mot que tout le monde tape, rare en 4 lettres. Acheteur type : enseigne vélo."}
    assert ai.save(env.conn, [v5, v5], known)["saved"] == 2
    version, reviewed = env.conn.execute("SELECT version, reviewed_at FROM domain_ai WHERE name = 'velo.fr'").fetchone()
    assert version == ai.VERSION and reviewed is None  # en double dans le lot : pas un second passage
    v4 = {"name": "velo.fr", "exploit": 20, "resale": 20, "quick_low": 0, "quick_high": 0, "final_low": 0,
          "final_high": 100, "comment": "À laisser. Mot faible. Acheteur type : personne."}
    assert ai.save(env.conn, [v4], known)["rejected"][0]["reason"] == "déjà noté avec la grille"
    assert env.conn.execute("SELECT version FROM domain_ai WHERE name = 'velo.fr'").fetchone()[0] == ai.VERSION
    bad = {**v5, "quick_low": 15000, "quick_high": 15000, "final_low": 10000, "final_high": 50000}
    assert ai.save(env.conn, [bad], known)["rejected"][0]["reason"] == "revente rapide plus chère que l'utilisateur final"


# --- Fiche : comparables, repères du portefeuille, ligne de temps ; avis IA : pourquoi, paliers ------

GRIDS = {
    "velo": {"oral": 10, "radio": 10, "memo": 10, "sens": 10, "marque": 9, "marche": 8, "ext": 10, "acheteurs": 9, "risque": 0},
    "ciel": {"oral": 9, "radio": 8, "memo": 8, "sens": 8, "marque": 4, "marche": 2, "ext": 3, "acheteurs": 3, "risque": 0},
    "kalo": {"oral": 9, "radio": 8, "memo": 9, "sens": 0, "marque": 7, "marche": 2, "ext": 6, "acheteurs": 4, "risque": 0},
    "sommes": {"oral": 8, "radio": 5, "memo": 5, "sens": 3, "marque": 2, "marche": 2, "ext": 7, "acheteurs": 2, "risque": 1},
    "puer": {"oral": 8, "radio": 7, "memo": 7, "sens": 6, "marque": 1, "marche": 1, "ext": 6, "acheteurs": 1, "risque": 8},
    "femmes": {"oral": 9, "radio": 8, "memo": 8, "sens": 9, "marque": 7, "marche": 8, "ext": 9, "acheteurs": 8, "risque": 0},
}


def rate(env, name, grid, final=(1000, 3000), rated_at="2026-09-01T00:00:00Z", reviewed_at=None):
    """Un avis v5 enregistré comme le ferait ai.save : notes et verdict dérivés de la grille."""
    import json
    from app import ai_format

    d = ai_format.derive(grid)
    env.conn.execute("""INSERT INTO domain_ai (name, exploit, resale, quick_low, quick_high, final_low, final_high,
                        comment, rated_at, version, grid, reviewed_at) VALUES (?, ?, ?, 0, 0, ?, ?, ?, ?, 5, ?, ?)""",
                     (name, d["exploit"], d["resale"], final[0], final[1], f"{d['verdict']}. Raison. Acheteur type : x.",
                      rated_at, json.dumps(grid), reviewed_at))
    env.ai_changed()
    return d


def test_fiche_lists_comparables_of_the_same_family_and_portfolio_references(env):
    add_fr(env, "velo", removed="2026-01-01")
    zone(env, [], set())  # .com suivi, sans avis
    rate(env, "velo.fr", GRIDS["velo"], (15000, 50000))             # mot de 4 lettres, revente 90
    rate(env, "ciel.fr", GRIDS["ciel"], (800, 3000))                # mot de 4 lettres, revente 40
    rate(env, "sel.fr", GRIDS["femmes"], (5000, 15000))             # mot de 3 lettres, revente 75
    rate(env, "table.fr", GRIDS["kalo"], (800, 2000))               # mot de 5 lettres, revente 40
    rate(env, "maison.fr", GRIDS["velo"], (15000, 50000))           # 6 lettres : trop loin
    rate(env, "durand.fr", GRIDS["velo"], (15000, 50000))           # nom de famille : autre liste
    rate(env, "lea.fr", GRIDS["femmes"], (5000, 15000))             # liste principale : prénom, pas mot
    env.conn.execute("""INSERT INTO domain_ai (name, exploit, resale, quick_low, quick_high, final_low, final_high, comment,
                        rated_at, version) VALUES ('moto.fr', 90, 90, 0, 0, 10000, 30000, 'À prendre. Ok. Acheteur type : x.', 'x', 4)""")
    env.ai_changed()
    fiche = env.fiche("velo.fr")
    names = [c["name"] for c in fiche["comparables"]]
    assert names == ["sel.fr", "ciel.fr", "table.fr"]  # les plus proches de 90 d'abord ; moto.fr (v4) écarté
    first = fiche["comparables"][0]
    assert first["verdict"] == "À prendre" and first["resale"] == 75 and first["final_high"] == 15000
    assert first["status_label"] == "Jamais enregistré" and first["exploit"] == 75
    assert fiche["item"]["ai"]["why"] == "" and fiche["item"]["ai"]["version"] == 5
    # Nom court hors des listes : même longueur et même forme (chiffres, lettres prononçables ou non).
    rate(env, "qsd.fr", GRIDS["sommes"], (100, 300))
    rate(env, "wxz.fr", GRIDS["sommes"], (100, 300))
    rate(env, "382.fr", GRIDS["sommes"], (100, 300))
    rate(env, "591.fr", GRIDS["kalo"], (100, 300))
    assert [c["name"] for c in env.fiche("382.fr")["comparables"]] == ["591.fr"]
    assert [c["name"] for c in env.fiche("qsd.fr")["comparables"]] == ["wxz.fr"]
    rate(env, "mai.fr", GRIDS["femmes"], (3000, 10000))  # mot avant prénom : lea.fr n'en est pas
    assert [c["name"] for c in env.fiche("mai.fr")["comparables"]] == ["sel.fr", "velo.fr", "ciel.fr"]
    # Sans avis sur le nom : les comparables suivent l'estimation du score.
    assert len(env.fiche("auto.fr")["comparables"]) == 3
    assert env.fiche("zzqx.fr")["comparables"] == []
    # Repères : ventes et offres reçues du portefeuille, les plus proches en longueur, 3 au plus.
    env.conn.executemany("INSERT INTO portfolio (name, status, sold_on, sold_price, best_offer, created_at) VALUES (?, ?, ?, ?, ?, 'x')", [
        ("moto.fr", "sold", "2026-03-01", 900, None), ("recette.fr", "for_sale", None, None, 250),
        ("lea.fr", "sold", "2026-04-01", 400, None), ("forumdomotique.fr", "sold", "2026-05-01", 300, None),
        ("velo.fr", "sold", "2026-06-01", 5000, None), ("ciel.fr", "owned", None, None, None)])
    refs = env.fiche("velo.fr")["references"]
    assert [r["name"] for r in refs] == ["moto.fr", "lea.fr", "recette.fr"]
    assert refs[0] == {"name": "moto.fr", "kind": "vendu", "sold_price": 900, "best_offer": None, "sold_on": "2026-03-01"}
    assert refs[2]["kind"] == "offre reçue" and refs[2]["best_offer"] == 250 and refs[2]["sold_price"] is None
    # Autres extensions : l'avis résumé quand il existe.
    others = {o["tld"]: o for o in env.fiche("velo.com")["others"]}
    assert others["fr"]["ai_summary"] == {"verdict": "À prendre", "resale": 90, "final_low": 15000, "final_high": 50000}
    assert env.fiche("velo.fr")["others"][0]["ai_summary"] is None


def test_public_opinion_says_why_in_plain_words(env):
    from app import ai

    rate(env, "puer.fr", GRIDS["puer"], (50, 200))
    rate(env, "sommes.fr", GRIDS["sommes"], (200, 800))
    rate(env, "kalo.fr", GRIDS["kalo"], (800, 2000))
    rate(env, "velo.fr", GRIDS["velo"], (15000, 50000), reviewed_at="2026-09-02T00:00:00Z")
    env.conn.execute("""INSERT INTO domain_ai (name, exploit, resale, quick_low, quick_high, final_low, final_high, comment,
                        rated_at, version) VALUES ('moto.fr', 20, 20, 0, 0, 0, 100, 'À laisser. Faible. Acheteur type : x.', 'x', 4)""")
    found = ai.load(env.conn, ["puer.fr", "sommes.fr", "kalo.fr", "velo.fr", "moto.fr"])
    assert found["puer.fr"]["why"] == "risque fort (marque connue, sens négatif ou gênant)"
    assert found["sommes.fr"]["why"] == "peu de potentiel de marque"      # la sous-note la plus faible (marque 2)
    assert found["kalo.fr"]["why"] == "sens faible"                         # « À surveiller » : son point faible
    assert found["velo.fr"]["why"] == "" and found["velo.fr"]["reviewed_at"] == "2026-09-02T00:00:00Z"
    assert found["moto.fr"]["why"] == "" and found["moto.fr"]["version"] == 4 and found["moto.fr"]["reviewed_at"] is None
    assert ai.why("À laisser", {"sens": 5, "marque": 5, "marche": 1, "acheteurs": 4, "risque": 0}) == "marché étroit"
    assert ai.why("À laisser", {"sens": 5, "marque": 5, "marche": 4, "acheteurs": 1, "risque": 0}) == "peu d'acheteurs"


def test_fiche_timeline_gathers_dated_facts(env):
    add_fr(env, "velo", removed="2026-01-01")
    env.conn.execute("""INSERT INTO drops (name, tld, label, kind, removed_on, state, state_since, available_since)
                        VALUES ('velo.fr', 'fr', 'velo', 'word', '2026-01-01', 'available', '2026-02-01T08:00:00Z', '2026-02-01T08:00:00Z')""")
    env.conn.execute("""INSERT INTO drops (name, tld, label, kind, removed_on, state, state_since)
                        VALUES ('velo.com', 'com', 'velo', 'word', '2026-03-01', 'redemption', '2026-03-02T06:00:00Z')""")
    rate(env, "velo.fr", GRIDS["velo"], (15000, 50000), rated_at="2026-02-10T00:00:00Z")
    rate(env, "velo.com", GRIDS["velo"], (15000, 50000), rated_at="2026-03-10T00:00:00Z", reviewed_at="2026-03-20T00:00:00Z")
    domain_id = env.conn.execute("INSERT INTO domains (name, added_at) VALUES ('velo.fr', '2026-02-02T00:00:00Z')").lastrowid
    env.conn.executemany("INSERT INTO history (domain_id, at, field, old_value, new_value) VALUES (?, ?, ?, ?, ?)", [
        (domain_id, "2026-02-03T00:00:00Z", "state", None, "available"),
        (domain_id, "2026-02-20T00:00:00Z", "state", "available", "active"),
        (domain_id, "2026-02-20T00:00:00Z", "registrar", None, "Gandi"),
        (domain_id, "2026-02-21T00:00:00Z", "nameservers", "[]", '["ns1"]')])
    env.conn.execute("INSERT INTO portfolio (name, status, bought_on, sold_on, sold_price, created_at) "
                     "VALUES ('velo.fr', 'sold', '2026-02-19', '2026-06-01', 5000, 'x')")
    line = env.fiche("velo.fr")["timeline"]
    assert [e["at"] for e in line] == sorted(e["at"] for e in line)
    assert [(e["kind"], e["label"]) for e in line] == [
        ("created", "Créé (Afnic)"), ("removed", "Supprimé (Afnic)"), ("available", "Libération constatée"),
        ("watched", "Surveillé"), ("state", "Disponible"), ("rated", "Noté par l'IA"), ("bought", "Acheté"),
        ("state", "Actif"), ("registrar", "Registrar : Gandi"), ("sold", "Vendu (5000 €)")]
    assert line[0]["at"] == "2010-02-01"
    com = env.fiche("velo.com")["timeline"]
    assert [(e["kind"], e["label"]) for e in com] == [("zone_out", "Sorti de la zone"), ("drop_redemption", "Entré en rédemption"),
                                                       ("rated", "Renoté par l'IA (second passage)")]
    assert env.fiche("zzqx.fr")["timeline"] == []


def test_price_tiers_are_smooth_above_80():
    from app import ai, ai_format

    assert ai_format.price_band(85, "fr") == (8000, 80000) and ai_format.price_band(90, "fr") == (10000, 150000)
    assert ai_format.price_band(95, "fr") == (20000, 300000) and ai_format.price_band(80, "fr") == (5000, 30000)
    # Un palier de tolérance de chaque côté ; plus de « sans plafond » à 90, seulement pour les noms très courts.
    assert ai_format.price_limits(90, "fr", "velo") == (8000, 300000)
    assert ai_format.price_limits(80, "fr", "velo") == (3000, 80000)
    assert ai_format.price_limits(95, "fr", "ev") == (10000, 1000000)
    assert ai_format.price_limits(95, "com", "abc") == (15000, 1000000) and ai_format.price_limits(95, "fr", "abc")[1] == 300000
    assert ai_format.price_limits(0, "fr", "zzzzz") == (0, 300)
    velo = {"name": "velo.fr", **GRIDS["velo"], "quick_low": 3000, "quick_high": 8000, "final_low": 15000, "final_high": 50000,
            "comment": "Mot que tout le monde tape. Acheteur type : enseigne vélo."}
    assert ai_format.check_v5(velo, lambda n: True) is None
    assert ai_format.check_v5({**velo, "final_high": 300000, "quick_high": 100000}, lambda n: True) is None
    assert ai_format.check_v5({**velo, "final_high": 500000, "quick_high": 100000}, lambda n: True).startswith("prix utilisateur final hors palier")
    assert ai_format.check_v5({**velo, "name": "ev.fr", "final_high": 500000, "quick_high": 100000}, lambda n: True) is None
    # La consigne dit la même grille que le code.
    for floor, low, high in ai_format.PRICE_TIERS[:-1]:
        euros = lambda n: f"{n:,}".replace(",", " ")  # noqa: E731
        assert f"{floor}{' et plus' if floor == ai_format.PRICE_TIERS[0][0] else ''}, {euros(low)} à {euros(high)}" in ai.GUIDE

def _opinion(conn, name, verdict, resale, rated_at="2026-10-01T00:00:00+00:00", status_at=None):
    conn.execute(
        "INSERT INTO domain_ai (name, exploit, resale, quick_low, quick_high, final_low, final_high, comment, rated_at, version, status_at) "
        "VALUES (?, 80, ?, 500, 1000, 3000, 8000, ?, ?, 5, ?)",
        (name, resale, f"{verdict}. Mot courant, rare en 4 lettres. Acheteur type : enseigne.", rated_at, status_at))


def test_take_alert_once_per_state_grouped_and_thresholded(env):
    from app import notify

    store = env.fr.store
    now = datetime.now(timezone.utc)
    labels = ["velo", "mai"] + [f"v{n:03d}" for n in range(12)]
    env.drops.record("fr", labels, [], now)
    env.conn.execute("UPDATE drops SET state = 'available', available_since = ? WHERE name != 'mai.fr'", (now.isoformat(),))
    env.conn.execute("UPDATE drops SET state = 'redemption', state_since = ?, release_at = ? WHERE name = 'mai.fr'",
                     (now.isoformat(), (now + timedelta(days=30)).isoformat()))
    _opinion(env.conn, "velo.fr", "À prendre", 90)
    _opinion(env.conn, "mai.fr", "À prendre", 75)
    for n in range(12):
        _opinion(env.conn, f"v{n:03d}.fr", "À prendre" if n < 10 else "À surveiller", 70)
    args = (env.conn, env, store, "https://nas.exemple", "https://shop/?s={domain}")
    # Seuil : 80 ne garde que velo.fr.
    store.set("alert_take_min", "80")
    text, pairs = notify.take_message(*args, now)
    assert pairs == [("velo.fr", "free")] and text.startswith("À prendre : 1 nom")
    assert "velo.fr : libre" in text and "Exploitation 80, revente 90" in text and "3000 à 8000 €" in text
    assert "https://nas.exemple/?fiche=velo.fr" in text and "Réserver : https://shop/?s=velo.fr" in text
    assert "Acheteur type : enseigne." in text and "mai.fr" not in text
    # Seuil par défaut (70) : 12 noms, les 10 meilleurs détaillés, puis « et 2 autres » ; la rédemption est datée.
    store.delete("alert_take_min")
    text, pairs = notify.take_message(*args, now)
    assert len(pairs) == 12 and ("mai.fr", "coming") in pairs and "et 2 autres" in text and "v010.fr" not in text
    assert text.count("Réserver : ") == 10 and "mai.fr : libre ~" in text
    # Une fois envoyée, plus jamais pour le même état ; un nom masqué ne part pas.
    notify.mark_sent(env.conn, pairs, now)
    assert notify.take_message(*args, now) == (None, [])
    env.conn.execute("UPDATE drops SET state = 'available', available_since = ? WHERE name = 'mai.fr'", (now.isoformat(),))
    env.conn.execute("INSERT INTO drop_hidden (name, hidden_at) VALUES ('mai.fr', 'x')")
    assert notify.take_message(*args, now) == (None, [])
    env.conn.execute("DELETE FROM drop_hidden")
    text, pairs = notify.take_message(*args, now)
    assert pairs == [("mai.fr", "free")] and "mai.fr : libre\n" in text
    # Désactivée : rien.
    store.set("alert_take_enabled", "0")
    assert notify.take_message(*args, now) == (None, [])


def test_ai_outcomes_measure_names_taken_after_rating(env):
    from app import ai

    add_fr(env, "mot")                        # enregistré aujourd'hui
    add_fr(env, "velo", removed="2025-01-01")  # toujours supprimé
    add_fr(env, "chat")                        # enregistré, mais au portefeuille : pas repris par un autre
    add_fr(env, "ciel")
    env.conn.execute("INSERT INTO portfolio (name, created_at) VALUES ('chat.fr', 'x')")
    old = "2026-09-01T00:00:00+00:00"
    _opinion(env.conn, "mot.fr", "À prendre", 85, rated_at=old, status_at=explore.GONE)
    _opinion(env.conn, "velo.fr", "À surveiller", 50, rated_at=old, status_at=explore.GONE)
    _opinion(env.conn, "chat.fr", "À prendre", 85, rated_at=old, status_at=explore.FREE)
    _opinion(env.conn, "ciel.fr", "À prendre", 85, rated_at=old, status_at=explore.REGISTERED)  # noté déjà pris : hors mesure
    _opinion(env.conn, "mai.fr", "À prendre", 85, rated_at="2026-10-10T00:00:00+00:00", status_at=explore.ABSENT)  # trop récent
    now = datetime(2026, 10, 18, tzinfo=timezone.utc)
    assert ai.update_outcomes(env.conn, env, now) == 3
    rows = {r["name"]: r for r in env.conn.execute("SELECT * FROM ai_outcomes")}
    assert set(rows) == {"mot.fr", "velo.fr", "chat.fr"}
    assert rows["mot.fr"]["taken_at"] == now.isoformat() and rows["velo.fr"]["taken_at"] is None and rows["chat.fr"]["taken_at"] is None
    summary = ai.outcomes(env.conn)
    assert summary["total"] == 3 and summary["by_verdict"]["À prendre"] == {"count": 2, "taken": 1, "share": 0.5}
    assert summary["by_resale"]["80-100"] == {"count": 2, "taken": 1, "share": 0.5}
    assert summary["by_resale"]["40-59"] == {"count": 1, "taken": 0, "share": 0.0} and summary["by_resale"]["0-39"]["share"] is None
    assert {"verdict": "À prendre", "band": "80-100", "count": 2, "taken": 1, "share": 0.5} in summary["cross"]
    # Nuit suivante : la date de reprise ne bouge pas, même si le nom retombe.
    env.conn.execute("UPDATE fr_names SET removed_on = '2026-10-19' WHERE label = 'mot'")
    env.invalidate("fr")
    ai.update_outcomes(env.conn, env, now + timedelta(days=1))
    assert env.conn.execute("SELECT taken_at FROM ai_outcomes WHERE name = 'mot.fr'").fetchone()[0] == now.isoformat()

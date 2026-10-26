import asyncio
import json
import time
from datetime import datetime, timedelta, timezone

import pytest

from app import db, drops, explore, frdata, states
from app.checker import iso, utcnow
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


def test_manual_check_of_a_name_outside_the_lists_gives_its_state(env):
    # Nom tapé dans l'Explorer, absent de toutes les listes : la vérification doit quand même dire
    # libre ou pris, avec le registrar et l'échéance, pour réserver ou surveiller.
    expires = datetime.now(timezone.utc) + timedelta(days=300)
    taken = LookupResult(found=True, info=DomainInfo(statuses=["active"], registrar="Gandi", expires_at=expires))
    env.drops.rdap = env.fr.rdap = FakeRdap({"monsuperprojet.com": LookupResult(available=True), "monsuperprojet.fr": taken})
    assert env.universe.index("monsuperprojet") is None
    free = asyncio.run(env.verify("monsuperprojet.com"))
    assert free["status"] == explore.FREE and free["status_label"] == "Libre (vérifié)" and free["known"] is False
    assert free["rdap"]["available"] and free["checked_at"] and free["watched"] is False and free["score"] == 0
    pris = asyncio.run(env.verify("monsuperprojet.fr"))
    assert pris["status"] == explore.REGISTERED and pris["status_label"] == "Enregistré"
    assert pris["rdap"]["registrar"] == "Gandi" and pris["rdap"]["expires_at"].startswith(expires.strftime("%Y-%m-%d"))


def test_paused_afnic_refuses_manual_checks(env):
    env.fr._pause("Limite (429)", datetime.now(timezone.utc))
    with pytest.raises(explore.VerifyError, match="pause"):
        asyncio.run(env.verify("velo.fr"))


def test_value_scores_are_explained():
    from app import value
    # abd plutôt que abc : un sigle courant (sigles.txt) vaut plus qu'une suite de 3 lettres.
    assert value.base("ab") > value.base("abd") > value.base("mako") > value.base("1234")
    # Mélanges et suites imprononçables : tout en bas, quel que soit l'ordre entre eux.
    assert max(value.base("ab12"), value.base("qzxw")) < 15 < value.base("1234") + 5
    assert value.base("oye") > value.base("qsd")
    found = value.score("maison", ["com"])
    assert found["reasons"][:2] == ["6 lettres", "153e mot le plus courant"] and "pris en .com" in found["reasons"]
    old = {"created_on": "2008-01-01", "removed_on": "2026-03-01"}
    assert value.score("maison", [], old, today=datetime(2026, 10, 1).date())["score"] > value.score("maison", [])["score"]


def test_quality_families_and_short_measures():
    from app import quality
    fam = lambda label, tld="fr": quality.family(quality.features(label, tld), label)
    # Deux lettres : toujours la famille des suites courtes, même si la paire est dans la liste anglaise.
    assert fam("tv") == fam("jx") == fam("ok") == "short" and fam("abc") == "words" and fam("maison") == "words"
    assert fam("777") == fam("06") == fam("2015") == "digits" and fam("amelie") == "names"
    f = quality.features("007", "fr")
    assert f["zero_lead"] == 1 and f["round"] == 0 and f["repeat"] == 0 and f["digits_com"] == 0
    assert quality.features("100", "com")["round"] == 1 and quality.features("100", "com")["digits_com"] == 1
    assert quality.features("2015", "com")["year"] == 1 and quality.features("2015", "com")["digits_com"] == 0
    assert quality.features("101", "fr")["palindrome"] == 1 and quality.features("360", "fr")["tens"] == 1
    assert quality.features("06", "fr")["dept"] == 1 and quality.features("06", "com")["dept"] == 0
    # Deux chiffres sont déjà un nombre : pas de bonus de motif.
    assert quality.features("88", "fr")["repeat"] == 0 and quality.features("8899", "fr")["pairs"] == 1
    assert quality.features("tv", "fr")["acronym"] == 1 and quality.features("jx", "fr")["acronym"] == 0
    assert quality.features("hp", "com")["brand"] == 1 and quality.features("tv", "com")["brand"] == 0
    assert set(quality.model()["families"]) == set(quality.FAMILIES)
    # Ce que les avis IA disent des formes courtes : sigle connu > paire quelconque > lettres rares ;
    # marque célèbre pénalisée ; chiffres ronds ou répétés > chiffres quelconques ; .com > .fr pour 3 chiffres.
    note = lambda label, tld: quality.predict(label, tld)[0]
    assert note("tv", "fr") > note("nb", "fr") > note("qx", "fr")
    assert note("hp", "com") < note("nb", "com")
    assert note("100", "fr") > note("382", "fr") and note("777", "com") > note("382", "com") > note("382", "fr")
    assert note("123", "fr") > note("382", "fr") > note("007", "fr")


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
    # Les cases comptent ce que la puce « Libres » liste : réservables, libération à venir exclue.
    assert found["cells"]["fr"]["short"] == env.facets("fr", length=-3)["bookable"]
    # .com : pas de premium chez le registre (prix unique) ; tous les mots libres comptent.
    assert found["cells"]["com"]["word"] == len(env.universe.by_list["word"]) - 1
    env.drops.record("com", ["maison"], [], datetime.now(timezone.utc))
    env.conn.execute("UPDATE drops SET state = 'redemption' WHERE name = 'maison.com'")
    env.invalidate()
    assert env.facets("com", category="word")["coming"] == 1
    assert env.map()["cells"]["com"]["word"] == env.facets("com", category="word")["bookable"]


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
    # Prénoms : les 10 premiers de chaque source (France et États-Unis), sans les noms pas encore cherchés en .fr.
    ranks, u = __import__("app").wordlists.ranks(), env.universe
    top = {u.index(w) for k in ("firstname", "firstname_us") for w, n in ranks[k].items() if n <= 10} - env.unread("fr")
    assert env.facets("fr", category="firstname", max_rank=10)["available"] == len(top) > 10


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


def test_us_names_are_first_names_and_surnames_in_english(env):
    """Prénoms et noms de famille des États-Unis : dans Prénoms et Noms de famille (pas de catégorie à part), en
    anglais pour le filtre de langue, rang et raison de leur source ; un nom des deux pays passe les deux filtres."""
    from app import wordlists

    zone(env, [], [])  # .com lu avec toutes les listes : aucun nom masqué faute d'avoir été cherché

    def found(q, **kw):
        return {i["label"]: i for i in env.rank("com", q=q, mode="prefix", limit=50, **kw)["items"]}

    # bernice : prénom des seuls États-Unis (dans aucune autre liste).
    assert wordlists.own("bernice") == ["firstname_us"] and wordlists.categories("bernice") == ["firstname"]
    assert "bernice" in found("bernice", category="firstname", lang="en")
    assert "bernice" not in found("bernice", category="firstname", lang="fr")
    assert "bernice" not in found("bernice", category="surname")
    item = found("bernice", category="firstname")["bernice"]
    assert item["categories"] == ["firstname"] and item["ranks"] == {"firstname_us": 396}
    assert "396e prénom le plus donné aux États-Unis" in item["reasons"]
    # ford : nom de famille des seuls États-Unis ; martin et thomas : des deux pays, les deux langues.
    item = found("ford", category="surname", lang="en")["ford"]
    assert "124e nom de famille le plus porté aux États-Unis" in item["reasons"] and "ford" not in found("ford", lang="fr")
    for name, cat in (("martin", "surname"), ("thomas", "firstname")):
        assert name in found(name, category=cat, lang="fr") and name in found(name, category=cat, lang="en")
    # Un prénom seulement français : en français seulement, son rang et sa raison de toujours.
    item = found("jean-pierre", category="firstname", lang="fr")["jean-pierre"]
    assert item["ranks"]["firstname"] == 106 and "106e prénom le plus donné" in item["reasons"]
    assert "jean-pierre" not in found("jean-pierre", category="firstname", lang="en")
    # Tri par fréquence : la carte lit le rang dans la source où le nom est le mieux placé.
    top = env.rank("com", category="firstname", sort="freq", limit=6)["items"]
    assert [(i["label"], i["freq_list"]) for i in top[:4]] == [("jean", "firstname"), ("james", "firstname_us"),
                                                               ("marie", "firstname"), ("john", "firstname_us")]


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



def test_premium_probable_bookable_and_release_sort(env):
    zone(env, [], ["maison"])
    u = env.universe
    # .com et .net sont sans premium : la règle ne s'y applique pas (ici, on fait comme si le .com en avait).
    assert env.status_bytes("com")[u.index("abd")] == explore.ABSENT
    env.premium_tlds_off = lambda: frozenset()
    env.invalidate("com")
    status = env.status_bytes("com")
    assert status[u.index("abd")] == explore.PREMIUM and status[u.index("maison")] == explore.REGISTERED
    # Masqués par défaut, visibles avec premium=1.
    hidden = env.search("com", q="abd", mode="exact", status="bookable")["items"]
    shown = env.search("com", q="abd", mode="exact", status="bookable", premium=True)["items"]
    assert hidden == [] and [i["label"] for i in shown] == ["abd"]
    counts = env.facets("com", premium=True)
    assert counts["premium"] > 0 and counts["bookable"] > env.facets("com")["bookable"]
    # Pas de prix sans réponse de Gandi : seul son prix s'affiche.
    assert shown and env.search("com", q="abd", mode="exact", premium=True)["items"][0]["premium_price"] is None
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

    from app import tokens
    tokens.create(env.conn, "ai")
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


def test_grade_splits_each_verdict_on_the_expected_yield(env):
    """Note de A à F : « À prendre » coupé en A et B, « À surveiller » en C, D et E, selon le rendement rapporté au
    renouvellement ; la note de revente départage un avis sans rendement. Le filtre et le classement « pour
    revendre » la suivent, rendement d'abord dans chaque note."""
    from app import ai

    assert ai.grade("À prendre", 50, 4 * 7.2, 7.2) == "A" and ai.grade("À prendre", 50, 3 * 7.2, 7.2) == "B"
    assert ai.grade("À prendre", 70, None, None) == "A" and ai.grade("À prendre", 65, None, None) == "B"
    assert ai.grade("À surveiller", 40, 0.5 * 15, None) == "C" and ai.grade("À surveiller", 40, 4.0, 15) == "D"
    assert ai.grade("À surveiller", 60, 1.0, 15) == "E" and ai.grade("À surveiller", 60, -2.0, 15) == "E"
    assert [ai.grade("À surveiller", r, None, None) for r in (55, 50, 45)] == ["C", "D", "E"]
    assert ai.grade("À laisser", 90, 999, 7) == "F" and ai.grade("", 90, 999, 7) is None
    zone(env, [], ["maison"])
    rows = [("pain.com", "À prendre", 60, 100.0, 14.0),     # A : 7 fois le renouvellement
            ("lait.com", "À prendre", 60, 30.0, 14.0),      # B
            ("miel.com", "À surveiller", 40, 20.0, 14.0),   # C, mieux que sucre
            ("sucre.com", "À surveiller", 40, 8.0, 14.0),   # C
            ("riz.com", "À surveiller", 40, 4.0, 14.0),     # D : plus d'un quart
            ("sel.com", "À surveiller", 60, 2.0, 14.0),     # E malgré sa note
            ("thym.com", "À laisser", 80, -5.0, 14.0)]      # F
    env.conn.executemany("""INSERT INTO domain_ai (name, exploit, resale, quick_low, quick_high, final_low, final_high,
                            comment, rated_at, version, yield, renewal) VALUES (?, 40, ?, 0, 0, 500, 1000, ?, 'x', 7, ?, ?)""",
                         [(n, r, f"{v}. Test. Acheteur type : test.", y, c) for n, v, r, y, c in rows])
    env.ai_changed()
    grades = {n: g for n, g in env.conn.execute(f"SELECT name, {ai.grade_sql()} FROM domain_ai")}
    assert grades == {"pain.com": "A", "lait.com": "B", "miel.com": "C", "sucre.com": "C", "riz.com": "D", "sel.com": "E",
                      "thym.com": "F"}
    picked = env.rank("com", status="all", ai=explore.ai_filter([], {}, ["C", "D"]), limit=10)["items"]
    assert sorted(i["label"] for i in picked) == ["miel", "riz", "sucre"]
    order = [i["label"] for i in env.rank("com", status="all", sort="resale", limit=60)["items"]]
    assert order[:6] == ["pain", "lait", "miel", "sucre", "riz", "sel"] and "thym" not in order[:10]
    assert [i["ai"]["grade"] for i in env.rank("com", q="pain", mode="exact", status="all")["items"]] == ["A"]


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


def test_pending_can_keep_only_some_extensions(env):
    """Lot lancé à la main en .fr et .com : la file ne garde que ces extensions, dans le même ordre."""
    from app import ai

    add_fr(env, "zzzzz")
    zone(env, [], ["maison"])
    full = [n["name"] for n in ai.pending(env, 300)]
    only = [n["name"] for n in ai.pending(env, 100, only=("com",))]
    assert only and all(n.endswith(".com") for n in only)
    assert only[:20] == [n for n in full if n.endswith(".com")][:20]


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


def test_review_estimate_is_kept_and_redone_for_a_new_version(env, monkeypatch):
    """Le second passage ne recalcule plus l'estimation de chaque avis à chaque lot (30 s de file sur le NAS) :
    gardée avec l'avis, elle n'est refaite que pour un nouvel avis ou une nouvelle version de l'appli."""
    from app import ai, config, quality

    add_fr(env, "zzzzz")  # .fr suivi
    env.conn.execute("""INSERT INTO domain_ai (name, exploit, resale, quick_low, quick_high, final_low, final_high, comment,
                        rated_at, version) VALUES ('maison.fr', 5, 5, 0, 0, 0, 100, 'À laisser. Test.', 'x', ?)""", (ai.VERSION,))
    estimate = round(quality.predict("maison", "fr")[0])
    assert ai.to_review(env) == [("maison.fr", 5, estimate)]
    assert env.conn.execute("SELECT estimate FROM domain_ai WHERE name = 'maison.fr'").fetchone()[0] == estimate
    calls = []
    monkeypatch.setattr(quality, "predict", lambda *a, **k: calls.append(a) or (5.0, 0.0, 0.0))
    assert ai.to_review(env) == [("maison.fr", 5, estimate)] and calls == []  # gardée : rien de recalculé
    monkeypatch.setattr(config, "VERSION", "v99.0.0")
    assert ai.to_review(env) == [] and calls == [("maison", "fr")]  # nouvelle version : refaite (écart 0)


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
    env.premium_tlds_off = lambda: frozenset()
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
    assert others["fr"]["ai_summary"] == {"verdict": "À prendre", "resale": 90, "final_low": 15000, "final_high": 50000,
                                          "grade": "A"}
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

def test_guide_examples_match_the_computed_notes():
    """Les exemples de la consigne passent le contrôle et annoncent les notes que l'appli calcule."""
    import json
    import re

    from app import ai, ai_format

    examples = re.findall(r'^(\{"name".*\})\n  \(calculé : exploitation (\d+), revente (\d+), (.*?)\)$', ai.GUIDE, re.M)
    assert len(examples) >= 10
    for raw, exploit, resale, verdict in examples:
        item = json.loads(raw)
        assert ai_format.check_v5(item, lambda n: True) is None, item["name"]
        assert ai_format.derive(item) == {"exploit": int(exploit), "resale": int(resale), "verdict": verdict}, item["name"]
    # Un mot français en .com n'est pas un défaut : la consigne le dit et l'illustre.
    assert any(json.loads(raw)["name"].endswith(".com") for raw, *_ in examples)
    assert "en français comme en anglais" in ai.GUIDE


def _opinion(conn, name, verdict, resale, rated_at="2026-10-01T00:00:00+00:00", status_at=None):
    conn.execute(
        "INSERT INTO domain_ai (name, exploit, resale, quick_low, quick_high, final_low, final_high, comment, rated_at, version, status_at) "
        "VALUES (?, 80, ?, 500, 1000, 3000, 8000, ?, ?, 5, ?)",
        (name, resale, f"{verdict}. Mot courant, rare en 4 lettres. Acheteur type : enseigne.", rated_at, status_at))



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

def test_health_flags_silent_dns_passes_czds_silence_and_missed_jobs(env):
    from types import SimpleNamespace
    from app import morning

    now = datetime.now(timezone.utc)

    class Pass:
        def __init__(self, **status):
            self._status = status

        def status(self):
            return self._status

    fr_dns = Pass(enabled=True, finished_at=now.isoformat(), queued=1200, checked=0, stopped="refus du serveur")
    cc = {"co": Pass(enabled=True, finished_at=now.isoformat(), error="résolveur muet", queued=10, checked=0),
          "ai": Pass(enabled=True, finished_at=now.isoformat(), queued=10, checked=10),
          "io": Pass(enabled=True, queued=10, checked=0)}  # jamais terminée : rien à dire
    events = {"missed": [{"at": (now - timedelta(hours=h)).isoformat(), "job": "scan_zones"} for h in (1, 2, 3, 30)], "errors": []}
    state = SimpleNamespace(drops=env.drops, fr=env.fr, explorer=env, fr_dns=fr_dns, cc_dns=cc, scheduler_events=events)
    found = morning.problems(state, now)
    assert found["dns:fr"] == "Passe DNS .fr : aucun nom vérifié sur 1200 en file (refus du serveur)"
    assert found["dns:co"].startswith("Passe DNS .co en erreur") and "dns:ai" not in found and "dns:io" not in found
    assert "scheduler" not in found  # 3 manquées en 24 h : tolérées
    events["missed"].append({"at": now.isoformat(), "job": "check_drops"})
    assert morning.problems(state, now)["scheduler"] == "Planificateur : 4 exécutions manquées en 24 h (dernière : check_drops)"
    # Compte CZDS configuré et lectures lancées, mais aucune zone lue avec succès depuis 36 h.
    env.drops.store.set("czds_username", "moi")
    env.drops.store.set("czds_password", "x")
    env.drops.store.set("czds_last_run", now.date().isoformat())
    assert morning.problems(state, now)["czds_late"] == "Zones ICANN : aucune lecture réussie à ce jour"
    env.drops.store.set("zone_com", json.dumps({"ok_at": (now - timedelta(hours=40)).isoformat()}))
    found = morning.problems(state, now)
    assert found["czds_late"].startswith("Zones ICANN : aucune lecture réussie depuis le") and "zone:com" in found
    env.drops.store.set("zone_com", json.dumps({"ok_at": (now - timedelta(hours=2)).isoformat()}))
    assert "czds_late" not in morning.problems(state, now)


def test_fiche_of_a_watched_name_shows_the_last_check_state(env):
    """Un nom surveillé et vérifié : la fiche dit l'état du dernier check, pas celui de l'open data."""
    from app import states

    add_fr(env, "zzzzz")
    env.conn.execute("""INSERT INTO domains (name, added_at, state, last_success_at, last_checked_at, registrar, tags)
                        VALUES ('velo.fr', '2026-10-01', ?, '2026-10-17T00:00:00Z', '2026-10-17T00:00:00Z', 'Gandi', '[]')""",
                     (states.ACTIVE,))
    item = env.fiche("velo.fr")["item"]
    assert item["status"] == explore.REGISTERED and item["status_label"] == "Enregistré"  # pris : un seul libellé


def set_recent(ex, tmp_path, ranked, year=2024):
    """Prénoms récents de l'Insee, fabriqués pour le test (aucune donnée réelle)."""
    from app import prenoms
    (tmp_path / prenoms.FILE_NAME).write_text(json.dumps({"latest": year, "years": {str(year): ranked}, "url": "x"}))
    ex.recent = prenoms.RecentNames(ex.fr.store, tmp_path)


RECENT = [["gabriel", 5000], ["xylophonix", 4900], ["louise", 4800], ["jade", 4700], ["arthur", 4600], ["raphael", 4500]]


def test_recent_first_names_sort_with_filters(env, tmp_path):
    assert env.recent_meta() is None
    set_recent(env, tmp_path, RECENT)
    add_fr(env, "louise")
    assert env.recent_meta() == {"key": "firstname_recent", "year": 2024, "label": "Prénoms 2024"}

    def labels(**kw):
        return [i["label"] for i in env.rank("fr", category="firstname", sort="recent", **kw)["items"]]

    # Les plus donnés en 2024 d'abord (louise, prise, et le prénom hors des listes sautés), puis l'historique.
    found = labels(limit=6)
    assert found[:4] == ["gabriel", "jade", "arthur", "raphael"]
    assert found[4] == "jean" and "xylophonix" not in found
    assert labels(length=-4, limit=1) == ["jade"]
    assert labels(q="ra", limit=1) == ["raphael"]
    assert labels(status="registered", limit=1) == ["louise"]
    # Plusieurs extensions : même ordre à la fusion.
    key = env.merge_key("recent", "firstname")
    merged = sorted([{"label": "jean", "tld": "fr"}, {"label": "raphael", "tld": "fr"}, {"label": "gabriel", "tld": "com"}], key=key)
    assert [i["label"] for i in merged] == ["gabriel", "raphael", "jean"]


def test_recent_rank_is_a_reason_not_points(env, tmp_path):
    add_fr(env, "louise")
    before = {i["tld"]: i for i in env.card("jade")["items"]}["fr"]
    set_recent(env, tmp_path, RECENT)
    after = {i["tld"]: i for i in env.card("jade")["items"]}["fr"]
    assert after["score"] == before["score"]
    assert "4e prénom donné en 2024" in after["reasons"]
    # Mieux classée que le rang historique : avant lui, pour que la carte la montre.
    assert after["reasons"].index("4e prénom donné en 2024") < after["reasons"].index("260e prénom le plus donné")
    assert env.fiche("gabriel.fr")["item"]["reasons"].count("1er prénom donné en 2024") == 1
    assert not any("donné en" in r for r in {i["tld"]: i for i in env.card("jean")["items"]}["fr"]["reasons"])


def test_timeline_shows_what_is_coming(env):
    """Dates à venir dans la ligne de temps : expiration, libération estimée, prochaine vérification."""
    env.conn.execute("""INSERT INTO drops (name, tld, label, kind, removed_on, state, state_since, release_at, next_check_at)
                        VALUES ('velo.com', 'com', 'velo', 'word', '2026-03-01', 'redemption', '2026-03-02T06:00:00Z',
                                '2099-01-05T12:00:00Z', '2099-01-05T12:05:00Z')""")
    line = env.fiche("velo.com")["timeline"]
    kinds = [(e["kind"], e["future"]) for e in line]
    assert ("drop_redemption", False) in kinds and ("release", True) in kinds and ("next_check", True) in kinds
    assert line[-2]["label"] == "Libération estimée" and line[-1]["label"] == "Prochaine vérification"
    # Une fois libre, plus de libération « estimée ».
    env.conn.execute("UPDATE drops SET state = 'available', available_since = '2099-01-05T13:00:00Z' WHERE name = 'velo.com'")
    assert all(e["kind"] != "release" for e in env.fiche("velo.com")["timeline"])


def cc(ex, tld, names, at="2026-10-18T03:00:00+00:00"):
    """Résultat d'une passe DNS .co/.ai/.io : {label: (présent, lost_at)}."""
    for label, (present, lost) in names.items():
        ex.conn.execute("INSERT INTO cc_dns (name, present, checked_at, seen_at, lost_at) VALUES (?, ?, ?, ?, ?)",
                        (f"{label}.{tld}", int(present), at, at if present or lost else None, lost))
    ex.fr.store.set(f"{tld}_dns_status", json.dumps({"finished_at": at, "checked": len(names)}))


def test_cc_dns_names_are_an_explorer_extension(env):
    add_fr(env, "cloud")
    assert env.tlds() == ["fr"]
    cc(env, "co", {"cloud": (True, None), "insurance": (True, None), "maison": (False, None), "abc": (False, None),
                   "velo": (False, None)})
    assert env.tlds() == ["fr", "co"]  # nouvelle passe : nouvelle version, l'extension apparaît
    by_label = {i["label"]: i for i in env.search("co", status="all", limit=50)["items"]}
    assert by_label["cloud"]["status_label"] == "Enregistré" and by_label["maison"]["status_label"] == explore.ABSENT_DNS
    assert "zebre" not in by_label  # jamais interrogé : hors des listes
    counts = env.facets("co")
    # 3 caractères absents : premium probable, comme sur une zone ; les noms pas interrogés ne comptent nulle part.
    assert counts["registered"] == 2 and counts["maybe_free"] == 2 and counts["premium"] == 1 and counts["all"] == 4
    ranked = [i["label"] for i in env.rank("co", status="available")["items"]]
    assert set(ranked) == {"maison", "velo"}
    card = {i["tld"]: i for i in env.card("zebre")["items"]}
    assert card["co"]["status"] == explore.INVALID and card["co"]["status_label"] == "Pas interrogé"
    fiche = env.fiche("maison.co")
    assert fiche["item"]["status_label"] == explore.ABSENT_DNS and "DNS du .co" in fiche["rdap_next"]["why"]
    # Pris en .co : demande pour le .fr ; taux de base mesurés parmi les noms interrogés seulement.
    ctx = env._bonus_context("fr")
    assert ctx["rates"]["co"]["W"] == pytest.approx(2 / 3)  # cloud, insurance pris ; maison absent
    assert "pris en .co" in " ".join(env.value_of(["insurance.fr"])["insurance.fr"]["reasons"])
    assert env.map()["cells"]["co"]["word"] >= 1
    # La Veille par-dessus : un nom suivi en rédemption passe « à venir ».
    env.drops.record("co", ["velo"], [], datetime.now(timezone.utc))
    env.conn.execute("UPDATE drops SET state = 'redemption' WHERE name = 'velo.co'")
    env.touch("velo.co")
    assert env.search("co", q="velo", mode="exact", status="all")["items"][0]["status"] == explore.COMING
    assert env.facets("co")["coming"] == 1


def test_cc_dns_lost_name_goes_to_the_veille_and_leaves_when_back(env, monkeypatch):
    from app import ccdns, frdns

    monkeypatch.setattr(frdns, "RATE", 1_000_000.0)
    checker = ccdns.CcDns("ai", env.conn, env.fr.store, env, timezone.utc)
    checker.servers = ["192.0.2.1"]
    checker.pending = lambda limit, now=None: ["cloud", "maison", "insurance"]
    state = {"cloud": True, "maison": False, "insurance": True}

    async def query(label):
        return state[label]

    checker.query = query
    asyncio.run(checker.run(limit=3))
    assert "ai" in env.tlds()
    # Absent dès la première passe : pas dans la Veille, « Absent du DNS, à vérifier » dans l'Explorer.
    assert env.conn.execute("SELECT COUNT(*) FROM drops").fetchone()[0] == 0
    assert env.status_bytes("ai")[env.universe.index("cloud")] == explore.REGISTERED
    state["cloud"] = False
    asyncio.run(checker.run(limit=3))
    row = env.drops.row("cloud.ai")
    assert row is not None and row["next_check_at"] and row["state"] is None and env.drops.row("maison.ai") is None
    # Vérifié auprès du registre au passage suivant : en rédemption, il est dans Bientôt libres.
    expires = datetime.now(timezone.utc) - timedelta(days=40)
    env.drops.rdap = FakeRdap({"cloud.ai": LookupResult(found=True, info=DomainInfo(statuses=["redemption period"], expires_at=expires))})
    assert asyncio.run(env.drops.check_due()) == 1
    assert env.drops.row("cloud.ai")["state"] == "redemption"
    assert env.status_bytes("ai")[env.universe.index("cloud")] == explore.COMING
    # Revenu dans le DNS : il sort de la Veille, comme un nom revenu dans sa zone.
    state["cloud"] = True
    asyncio.run(checker.run(limit=3))
    assert env.drops.row("cloud.ai")["state"] == drops.TAKEN
    assert env.status_bytes("ai")[env.universe.index("cloud")] == explore.REGISTERED
    assert checker.status()["totals"] == {"known": 3, "present": 2, "absent": 1, "lost": 0}


def test_cc_dns_good_names_daily_others_weekly(env):
    """DNS .co, .ai, .io, .com sans zone : un nom à fort score de base (ou de Mes ajouts) revu chaque jour, les autres
    chaque semaine ; jamais interrogés d'abord. Avant : tous chaque jour, la même liste de 50 000 noms."""
    from app import ccdns, frdns

    checker = ccdns.CcDns("co", env.conn, env.fr.store, env, timezone.utc)
    u = env.universe
    labels = checker.pending(10**6)
    good = next(x for x in labels if (u.base_table[u.index(x)] & 127) >= frdns.TOP_BASE)
    weak = next(x for x in reversed(labels) if (u.base_table[u.index(x)] & 127) < frdns.TOP_BASE)
    two_days = iso(utcnow() - timedelta(days=2))
    for label in (good, weak):
        checker.save(label, True, two_days)
    again = checker.pending(10**6)
    assert good in again and weak not in again and again[:2] != [good, weak]
    assert len(again) == len(labels) - 1


def test_dns_pass_shows_its_progress_live(env, monkeypatch):
    """Paramètres et /api/diag : une passe DNS en cours dit où elle en est (noms interrogés, présents, rythme), pas
    seulement « en cours » ; finie, le compteur disparaît et l'état de la passe prend le relais."""
    from app import ccdns, frdns

    monkeypatch.setattr(frdns, "RATE", 1_000_000.0)
    checker = ccdns.CcDns("io", env.conn, env.fr.store, env, timezone.utc)
    checker.servers = ["192.0.2.1"]
    checker.pending = lambda limit, now=None: ["cloud", "maison", "insurance", "velo"]
    seen = []

    async def query(label):
        seen.append(dict(checker.status()["current"]))
        return label != "maison"

    checker.query = query
    asyncio.run(checker.run(limit=4))
    assert [c["checked"] for c in seen] == [0, 1, 2, 3] and seen[-1]["present"] == 2 and seen[-1]["queued"] == 4
    assert seen[0]["started_at"] and seen[-1]["rate"]
    status = checker.status()
    assert "current" not in status and status["checked"] == 4 and status["present"] == 3


def test_cc_dns_names_lost_before_the_veille_are_resumed_once(env):
    from app import ccdns

    cc(env, "io", {"ammonium": (False, "2026-10-17T04:00:00+00:00"), "maison": (False, None), "cloud": (True, None)})
    checker = ccdns.CcDns("io", env.conn, env.fr.store, env, timezone.utc)
    assert checker.resume_lost() == 1
    row = env.drops.row("ammonium.io")
    assert row["removed_on"] == "2026-10-17" and row["next_check_at"]
    assert env.drops.row("maison.io") is None and env.drops.row("cloud.io") is None
    assert checker.resume_lost() == 0  # déjà suivi : pas de nouvelle entrée


def test_cc_dns_pass_changes_the_explorer_version(env, monkeypatch):
    from app import ccdns, frdns

    monkeypatch.setattr(frdns, "RATE", 1_000_000.0)
    cc(env, "co", {"cloud": (False, None)})
    i = env.universe.index("maison")
    assert env.status_bytes("co")[i] == explore.INVALID and env.facets("co")["all"] == 1
    checker = ccdns.CcDns("co", env.conn, env.fr.store, env, timezone.utc)
    checker.servers = ["192.0.2.1"]
    checker.pending = lambda limit, now=None: ["maison"]

    async def query(label):
        return True

    checker.query = query
    asyncio.run(checker.run(limit=1))
    # Premier passage sur ce nom (pas un changement) : seule la fin de passe met l'Explorer à jour.
    assert env.status_bytes("co")[i] == explore.REGISTERED and env.facets("co")["all"] == 2


def test_morning_summary_does_not_repeat_a_name_lost_from_the_dns(env):
    from app import morning

    now = datetime.now(timezone.utc)
    env.drops.record("io", ["cloud"], [], now)
    env.conn.execute("UPDATE drops SET state = 'redemption', state_since = ? WHERE name = 'cloud.io'", (now.isoformat(),))
    status = {"io": {"finished_at": now.isoformat(), "freed_names": ["cloud.io", "maison.io"]}}
    text = morning.build(env.conn, env, status, "", now, min_score=0)
    assert text.count("cloud.io") == 1 and "Entrés en rédemption" in text
    assert "Sortis du DNS, à vérifier : maison.io" in text


def test_scores_are_kept_until_the_name_changes(env):
    """value_of garde le score de chaque nom ; touch() l'oublie pour ce libellé, invalidate() pour tous."""
    zone(env, ["ab"], ["maison"])
    add_fr(env, "maison")
    first = env.value_of(["maison.fr", "maison.com"])
    assert env._values and set(env._values) == {"maison.fr", "maison.com"}
    first["maison.fr"]["score"] = -1  # une copie : modifier le résultat ne touche pas la mémoire
    assert env.value_of(["maison.fr"])["maison.fr"]["score"] != -1
    env.touch("maison.fr")
    assert "maison.fr" not in env._values and "maison.com" not in env._values
    env.value_of(["maison.fr"])
    env.invalidate()
    assert not env._values


def test_kept_ranking_is_redone_when_one_of_its_names_changes(env):
    add_fr(env, "velo")
    first = env.rank("fr", category="word", limit=5)["items"]
    assert first
    name = first[0]["name"]
    again = env.rank("fr", category="word", limit=5)["items"]
    assert [i["name"] for i in again] == [i["name"] for i in first]
    # Le premier nom est pris entre-temps : la liste gardée ne le montre plus comme libre.
    env.fr.rdap = env.drops.rdap = FakeRdap({name: LookupResult(found=True, info=DomainInfo())})
    asyncio.run(env.verify(name))
    after = env.rank("fr", category="word", limit=5)["items"]
    assert name not in [i["name"] for i in after]


def test_map_is_refreshed_in_the_background_after_a_change(env):
    add_fr(env, "velo")
    env.warm()
    before = env.map()
    env.fr.rdap = env.drops.rdap = FakeRdap({"velo.fr": LookupResult(available=True)})
    asyncio.run(env.verify("velo.fr"))
    assert env._map_dirty
    assert env.map() is before  # lue tout de suite : la précédente, refaite en arrière-plan
    for _ in range(100):
        if not env._map_running:
            break
        time.sleep(0.05)
    assert env.map() == env._build_map() and env.map() != before

# --- .co, .io, .ai : libre selon le seul WHOIS, à confirmer -----------------------------

def test_whois_only_free_cc_name_is_not_verified_free(env):
    """menu.co relevé sur le NAS : jamais vu dans le DNS du .co, WHOIS « not found » (nom réservé ou premium
    du registre, inconnu du WHOIS). Ni « Libre (vérifié) », ni « Sorti du DNS », ni « Libération constatée »."""
    cc(env, "co", {"menu": (False, None), "cloud": (True, None)})
    env.drops.rdap = FakeRdap({"menu.co": LookupResult(available=True, source="whois")})
    item = asyncio.run(env.verify("menu.co"))
    assert item["status"] == explore.UNCONFIRMED and item["status_label"] == "Libre selon le WHOIS, à confirmer"
    assert item["drop_state"] == drops.UNCONFIRMED and item["available_since"] is None and item["removed_on"] is None
    row = env.drops.row("menu.co")
    assert row["removed_on"] == drops.MANUAL and row["next_check_at"] is None
    kinds = [e["kind"] for e in env.fiche("menu.co")["timeline"]]
    assert "dns_out" not in kinds and "available" not in kinds and "drop_unconfirmed" in kinds
    # Filtres : parmi les noms à réserver et les probablement libres, jamais parmi les libres vérifiés.
    counts = env.facets("co")
    assert counts["free"] == 0 and counts["maybe_free"] == 1 and counts["bookable"] == 1
    assert [i["name"] for i in env.search("co", q="menu", mode="exact", status="bookable")["items"]] == ["menu.co"]
    assert env.search("co", q="menu", mode="exact", status="free")["items"] == []


def test_rdap_free_stays_verified_and_a_real_dns_exit_is_dated(env):
    cc(env, "io", {"ammonium": (False, "2026-10-17T04:00:00+00:00")})
    env.drops.rdap = FakeRdap({"ammonium.io": LookupResult(available=True)})
    item = asyncio.run(env.verify("ammonium.io"))
    assert item["status"] == explore.FREE and env.drops.row("ammonium.io")["available_since"]
    line = {e["kind"]: e for e in env.fiche("ammonium.io")["timeline"]}
    assert line["dns_out"]["at"].startswith("2026-10-17") and line["available"]["label"] == "Libération constatée"


def test_manual_check_of_a_zone_name_is_not_a_zone_exit(env):
    zone(env, [], [])
    env.drops.rdap = FakeRdap({"qqqq.com": LookupResult(available=True)})
    asyncio.run(env.verify("qqqq.com"))
    assert all(e["kind"] != "zone_out" for e in env.fiche("qqqq.com")["timeline"])


def test_repair_marks_whois_only_free_names_and_drops_false_dns_exits(env):
    from app import rdapinfo

    now = datetime.now(timezone.utc).replace(microsecond=0)
    cc(env, "co", {"menu": (False, None), "cloud": (False, "2026-10-17T04:00:00+00:00")})
    # État écrit par la v0.12.7 : menu.co vérifié à la main, libre sur la foi du WHOIS, « sorti » le jour même.
    env.drops.record("co", ["menu", "cloud"], [], now)
    env.conn.execute("UPDATE drops SET state = 'available', available_since = ? WHERE name IN ('menu.co', 'cloud.co')",
                     (now.isoformat(),))
    rdapinfo.save(env.conn, "menu.co", LookupResult(available=True, source="whois"))
    rdapinfo.save(env.conn, "cloud.co", LookupResult(available=True))
    assert env.drops.repair() == {"unconfirmed": 1, "removed_on": 1, "recheck": 0}
    menu, cloud = env.drops.row("menu.co"), env.drops.row("cloud.co")
    assert menu["state"] == drops.UNCONFIRMED and menu["available_since"] is None and menu["state_since"] == now.isoformat()
    assert menu["removed_on"] == drops.MANUAL
    assert cloud["state"] == states.AVAILABLE and cloud["removed_on"] == now.date().isoformat()  # vu puis sorti : gardé
    assert env.drops.repair() == {"unconfirmed": 0, "removed_on": 0, "recheck": 0}  # idempotent


def test_repair_rechecks_free_names_refused_by_gandi_before_the_immediate_recheck(env):
    now = datetime.now(timezone.utc).replace(microsecond=0)
    env.drops.record("com", ["cida", "velo", "chat"], [], now - timedelta(days=8))
    later = (now - timedelta(days=3)).isoformat()
    for name in ("cida.com", "velo.com", "chat.com"):
        env.conn.execute("UPDATE drops SET state = 'available', checked_at = ?, next_check_at = NULL WHERE name = ?",
                         ((now - timedelta(days=8)).isoformat(), name))
    # Indisponible chez Gandi après la dernière vérification au registre (avant que la revérification n'existe) ;
    # velo.com : libre chez Gandi ; chat.com : registre revu depuis.
    for name, kind in (("cida.com", "unavailable"), ("velo.com", "available"), ("chat.com", "reserved")):
        env.conn.execute("INSERT INTO gandi_checks (name, status, kind, checked_at) VALUES (?, ?, ?, ?)",
                         (name, kind, kind, later))
    env.conn.execute("UPDATE drops SET checked_at = ? WHERE name = 'chat.com'", (now.isoformat(),))
    assert env.drops.repair()["recheck"] == 1
    assert env.drops.row("cida.com")["next_check_at"] is not None
    assert env.drops.row("velo.com")["next_check_at"] is None and env.drops.row("chat.com")["next_check_at"] is None
    assert env.drops.repair()["recheck"] == 0  # déjà en file


def test_whois_only_free_names_are_not_freed_in_alerts_and_morning(env):
    from app import morning, notify

    now = datetime.now(timezone.utc).replace(microsecond=0)
    env.drops.record("co", ["menu"], [], now)
    env.conn.execute("UPDATE drops SET state = ?, state_since = ? WHERE name = 'menu.co'", (drops.UNCONFIRMED, now.isoformat()))
    assert notify.changed(env.conn, now - timedelta(hours=1)) == []
    text = morning.build(env.conn, env, {}, "", now, min_score=0)
    assert "Libérés" not in text and "Libres selon le WHOIS, à confirmer chez un registrar (1 sur 1)" in text and "menu.co" in text


def test_several_categories_are_a_union(env):
    """Plusieurs catégories à la fois (cat=firstname,surname) : un nom de l'une ou de l'autre liste, dans les
    compteurs, la liste, le tri par fréquence et le filtre de rang ; une seule catégorie ne change pas."""
    from app import wordlists

    u = env.universe
    assert explore.Filters(category="surname, firstname,surname").category == "firstname,surname"
    union = set(u.by_list["firstname"]) | set(u.by_list["surname"])
    # Prénoms et noms des États-Unis pas encore cherchés en .fr (aucun import dans le test) : masqués, pas comptés.
    shown = union - env.unread("fr")
    both = env.facets("fr", category="surname,firstname", junk=True)["available"]
    assert both == len(shown) and both > max(env.facets("fr", category=c, junk=True)["available"] for c in ("firstname", "surname"))
    # Ordre des puces indifférent : même clé, même compte.
    assert env.facets("fr", category="firstname,surname", junk=True)["available"] == both
    items = env.rank("fr", category="firstname,surname", limit=200, sort="score")["items"]
    cats = [set(i["categories"]) & {"firstname", "surname"} for i in items]
    assert all(cats) and any("firstname" in c for c in cats) and any("surname" in c for c in cats)
    # Forme et liste mêlées : 2 caractères en chiffres ou en mots, deux ensembles disjoints.
    mixed = env.facets("fr", category="digits,word", length=2, junk=True)["available"]
    assert mixed == env.facets("fr", category="digits", length=2, junk=True)["available"] + env.facets("fr", category="word", length=2, junk=True)["available"]
    # Les plus fréquents : les deux listes mêlées par rang relatif, rang lu dans la liste où le nom est le mieux placé.
    # Chaque catégorie avec ses deux sources (France, États-Unis).
    sources = ("firstname", "firstname_us", "surname", "surname_us")
    freq = env.rank("fr", category="firstname,surname", sort="freq", limit=60)["items"]
    assert {i["freq_list"] for i in freq} == set(sources)
    assert all(i["freq_list"] == min((k for k in sources if k in i["ranks"]), key=lambda k: wordlists.relative(k, i["ranks"][k]))
               for i in freq)
    order = u.frequency("firstname,surname")
    places = [u.position("firstname,surname", u.index(i["label"])) for i in freq]
    assert places == sorted(places) and len(order) == len(union)
    assert {i["freq_list"] for i in env.rank("fr", category="firstname", sort="freq", limit=10)["items"]} == {"firstname", "firstname_us"}
    # Rang : dans les 10 premiers de l'une ou de l'autre liste, de l'une ou de l'autre source.
    ranks = wordlists.ranks()
    top = {u.index(w) for k in sources for w, n in ranks[k].items() if n <= 10} - env.unread("fr")
    assert env.facets("fr", category="firstname,surname", max_rank=10, junk=True)["available"] == len(top)
    assert env.best_rank("martin", "firstname,surname") == min(r["martin"] for k, r in ranks.items() if k in sources and "martin" in r) == 1
    assert env.best_rank("thomas", "firstname") == ranks["firstname_us"]["thomas"] < ranks["firstname"]["thomas"]


def test_renewed_by_owner_is_not_shown_as_taken_again():
    """« Repris » seulement pour un nouvel enregistrement : une date de création antérieure à la sortie du nom
    (ou d'un an au moins avant l'état vu) dit que le titulaire l'a gardé."""
    event = explore.Explorer._drop_event
    kept = {"state": drops.TAKEN, "state_since": "2026-10-15T08:50:00+00:00", "removed_on": ""}
    assert event(kept, {"registered_at": "2006-10-13T07:05:19+00:00"}) == "Renouvelé par le titulaire"
    retaken = {"state": drops.TAKEN, "state_since": "2026-10-15T08:50:00+00:00", "removed_on": "2026-09-01"}
    assert event(retaken, {"registered_at": "2026-10-14T10:00:00+00:00"}) == "Repris"
    assert event(retaken, None) == "Repris"
    assert event({"state": states.RENEWED, "state_since": "2026-10-15", "removed_on": ""}, {}) == "Renouvelé d'office, à confirmer"


def test_ai_queue_new_lists_first_after_free_names(env):
    """File IA : libres et bientôt libres d'abord (inchangé) ; puis les noms à saisir des nouvelles listes, termes
    premium, mots-clés et marques mêlés par rang, puis métier + ville ; ensuite le reste comme avant."""
    from app import ai

    zone(env, [], [])
    u = env.universe
    now = datetime.now(timezone.utc)
    env.drops.record("com", ["maison"], [], now)
    env.conn.execute("UPDATE drops SET state = 'available', state_since = ?, available_since = ? WHERE name = 'maison.com'",
                     (iso(now),) * 2)
    env.invalidate()
    data = env.status_bytes("com")
    # 1 ou 2 caractères jamais vus : réservés par les registres, jamais dans la file (« ia », terme star par dérivation).
    first = [u.label(i) for i in u.frequency("premium,keyword,brand")
             if u.good[i] and data[i] == explore.ABSENT and len(u.label(i)) > 2]
    seen = set(first)
    local = [u.label(i) for i in u.frequency("local")
             if u.good[i] and data[i] == explore.ABSENT and u.label(i) not in seen and len(u.label(i)) > 2]
    assert len(first) > 1000 and len(local) > 1000
    picked, _ = ai._queue(env, 1 + len(first) + len(local) + 50)
    names = [label for _, label, tld in picked]
    assert names[0] == "maison"
    assert names[1:1 + len(first)] == first and names[1 + len(first):1 + len(first) + len(local)] == local
    rest = names[1 + len(first) + len(local):]
    assert rest and not set(rest) & (seen | set(local))
    # Un nom déjà noté ne revient pas ; la file publiée suit le même ordre.
    env.conn.execute("""INSERT INTO domain_ai (name, exploit, resale, quick_low, quick_high, final_low, final_high, comment,
                        rated_at, version) VALUES (?, 50, 50, 0, 0, 0, 100, 'À surveiller. Test. Acheteur type : test.', 'x', ?)""",
                     (f"{first[0]}.com", ai.VERSION))
    assert [n["name"] for n in ai.pending(env, 3)] == ["maison.com", f"{first[1]}.com", f"{first[2]}.com"]


def test_ai_queue_free_names_in_full_score_order(env):
    """Libres en tête de la file IA, dans l'ordre du score complet, même quand la borne du score sert à n'en noter
    qu'une partie (milliers de .com libres d'après Gandi)."""
    from app import ai, gandi
    from tests.test_gandi import STANDARD, answer, client_for

    zone(env, [], [])  # zone .com lue, vide : chaque nom absent, libre dès que Gandi le dit
    env.gandi = client_for(env)
    env.invalidate()
    u = env.universe
    labels = [u.label(i) for i in u.frequency("english")[:400:2]] + [u.label(i) for i in u.good_letters(4)[:200:4]]
    for label in labels:
        env.gandi.record(f"{label}.com", gandi.read(answer("available", [STANDARD])), utcnow())
    ctx = env._bonus_context("com")
    expected = sorted(((env._value("com", u.index(x), x, ctx)["score"], x) for x in labels if u.good[u.index(x)]),
                      key=lambda x: (-x[0], x[1]))
    picked = [label for _, label, _ in ai._queue(env, len(expected))[0]]
    assert picked == [x for _, x in expected]
    assert [label for _, label, _ in ai._queue(env, 7)[0]] == picked[:7]


def test_long_names_lose_points_and_saved_opinions_are_recomputed(env):
    """Au-delà de 12 lettres, la note baisse ; les avis gardés sont recalculés une fois depuis leurs sous-notes."""
    import json

    from app import ai, ai_format

    grid = {"oral": 8, "radio": 8, "memo": 3, "sens": 8, "marque": 3, "marche": 8, "ext": 9, "acheteurs": 5, "risque": 1}
    assert ai_format.length_factor("monassurance.fr") == 1 and ai_format.length_factor("x" * 30 + ".com") == 0.7
    short, long = ai_format.derive({"name": "assurances.com", **grid}), ai_format.derive({"name": "assurancesantesenior.com", **grid})
    assert long["resale"] < short["resale"] and long["resale"] <= 45
    env.conn.execute(
        "INSERT INTO domain_ai (name, exploit, resale, quick_low, quick_high, final_low, final_high, comment, rated_at, version, grid) "
        "VALUES ('assurancesantesenior.com', 50, 55, 600, 1500, 1500, 7500, 'À surveiller. Mutuelle senior. Acheteur type : courtier.', 'x', 5, ?)",
        (json.dumps(grid),))
    assert ai.rederive(env.conn, env.fr.store) == 1 and ai.rederive(env.conn, env.fr.store) == 0
    row = env.conn.execute("SELECT * FROM domain_ai").fetchone()
    assert row["resale"] == long["resale"] and row["comment"] == f"{long['verdict']}. Mutuelle senior. Acheteur type : courtier."
    assert row["final_high"] < 7500


def test_take_needs_resale_and_memo_is_computed(env):
    """« À prendre » se juge sur la revente ; la mémorisation vient de la longueur (la confirmation d'un
    « À prendre » passe par l'escalade : tests/test_escalation.py)."""
    from app import ai_format

    assert ai_format.memo_for("lesmotos.com") == ai_format.memo_for("lespianos.com") == 4
    assert ai_format.memo_for("velo.fr") == 10 and ai_format.memo_for("x" * 14 + ".com") == 2
    pianos = {"name": "lespianos.com", "oral": 8, "radio": 8, "memo": 7, "sens": 8, "marque": 5, "marche": 5, "ext": 9,
              "acheteurs": 5, "risque": 0}
    notes = ai_format.derive(pianos)
    assert notes["exploit"] >= 60 > notes["resale"] and notes["verdict"] == "À surveiller"


def test_v5_opinions_left_aside_are_not_rated_again(env):
    """Version 6 (délais) : les avis v5 « À prendre » et « À surveiller » repassent, les « À laisser » restent."""
    from app import ai

    for name, verdict in (("a.fr", "À prendre"), ("b.fr", "À surveiller"), ("c.fr", "À laisser")):
        env.conn.execute("INSERT INTO domain_ai (name, exploit, resale, quick_low, quick_high, final_low, final_high, comment, "
                         "rated_at, version) VALUES (?, 50, 50, 0, 0, 0, 100, ?, 'x', 5)", (name, f"{verdict}. X. Acheteur type : y."))
    assert ai._done(env.conn) == {"c.fr"}


def test_more_results_extend_the_same_ranking(env):
    """« 50 de plus » : les 50 premiers d'une page de 100 sont ceux de la page de 50, même avec des notes
    à égalité ; entre égaux, l'autre note et le prix final départagent avant l'ordre alphabétique."""
    from app import ai

    add_fr(env, "zzzzz")
    labels = [drops.decode(i) for i in env.universe.good_letters(4)[:240]]
    env.conn.executemany("""INSERT INTO domain_ai (name, exploit, resale, quick_low, quick_high, final_low, final_high,
                            comment, rated_at, version) VALUES (?, ?, 60, 0, 0, ?, 1000, ?, 'x', %d)""" % ai.VERSION,
                         [(f"{label}.fr", 40 + n % 7, 100 * (n % 5), "À surveiller. Test. Acheteur type : test.")
                          for n, label in enumerate(labels)])
    env.ai_changed()
    for sort in ("resale", "exploit", "score"):
        first = [i["name"] for i in env.rank("fr", sort=sort, limit=50)["items"]]
        more = [i["name"] for i in env.rank("fr", sort=sort, limit=100)["items"]]
        assert more[:50] == first, sort
    items = env.rank("fr", sort="resale", limit=100)["items"]
    keys = [(i["ai"]["resale"], i["ai"]["exploit"], i["ai"]["final_low"]) for i in items if i.get("ai")]
    assert keys == sorted(keys, reverse=True)
    # Plusieurs extensions : même ordre que sur une seule.
    merged = sorted(items, key=env.merge_key("resale"))
    assert [i["name"] for i in merged] == [i["name"] for i in items]


def test_timeline_uses_the_real_expiry_during_the_registry_grace_period(env):
    """autoRenewPeriod hors .fr : le registre affiche l'échéance prolongée d'un an que personne n'a payée (license.io,
    « expire le 25/09/2027 » alors qu'il est expiré). La frise montre la vraie échéance, passée."""
    rdap = {"available": False, "statuses": ["clientHold", "autoRenewPeriod"], "registered_at": "2014-09-25T15:03:27+00:00",
            "expires_at": "2099-09-25T15:03:27+00:00"}
    line = env.timeline("velo.io", rdap=rdap)
    expiry = [e for e in line if e["kind"] == "expires"]
    assert [e["at"][:10] for e in expiry] == ["2098-09-25"] and "renouvelée d'office" in expiry[0]["label"]
    # Sans période de grâce, l'échéance du registre telle quelle.
    plain = env.timeline("velo.io", rdap={**rdap, "statuses": ["clientTransferProhibited"]})
    assert [e["at"][:10] for e in plain if e["kind"] == "expires"] == ["2099-09-25"]


def test_rank_keeps_scores_between_filters_and_forgets_a_changed_name(env):
    """Le classement garde le score de chaque nom calculé (9 s sur le NAS à chaque nouveau filtre) : même résultat
    qu'un calcul à neuf ; un nom qui change (touch) est recalculé ; une nouvelle version des données repart de zéro."""
    zone(env, [], ["maison"])
    first = env.rank("com", limit=30, status="bookable")
    scores = env._score_cache("com")
    u = env.universe
    ctx = env._bonus_context("com")
    for item in first["items"]:
        i = u.index(item["label"])
        assert scores[i] == item["score"] == env._value("com", i, item["label"], ctx)["score"]
    env._forget("rank")
    again = env.rank("com", limit=30, status="bookable")
    assert [i["name"] for i in again["items"]] == [i["name"] for i in first["items"]]
    assert [i["score"] for i in again["items"]] == [i["score"] for i in first["items"]]
    name = first["items"][0]["name"]
    env.touch(name)
    assert scores[u.index(name[:-4])] == -1
    env.invalidate()
    assert "com" not in env._scores

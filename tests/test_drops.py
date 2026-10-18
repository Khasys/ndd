import asyncio
import gzip
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app import db, drops, states
from app.rdap import DomainInfo, LookupResult
from app.store import Store

DELAYS = states.load_delays()


def zone(lines: list[str]) -> bytes:
    return gzip.compress(("\n".join(lines) + "\n").encode())


def bits_of(labels: list[str]) -> bytearray:
    bits = bytearray(drops.BITMAP_BYTES)
    for label in labels:
        i = drops.encode(label)
        bits[i >> 3] |= 1 << (i & 7)
    return bits


def test_encode_decode_round_trip():
    for label in ("aaaa", "zz99", "a-b1", "0000", "9--9"):
        assert drops.decode(drops.encode(label)) == label
    assert drops.encode("----") == drops.CELLS - 1
    assert drops.encode("a") == 0 and drops.decode(drops.encode("zz")) == "zz"
    assert drops.BITMAP_BYTES == 240778


def test_kind_and_validity():
    assert drops.kind_of("abcd") == "letters"
    assert drops.kind_of("1234") == "digits"
    assert drops.kind_of("ab12") == "mixed"
    assert drops.kind_of("ab-c") == "hyphen"
    assert drops.valid_label("ab-c") and not drops.valid_label("-abc") and not drops.valid_label("abc-")
    assert drops.valid_label("x") and drops.valid_label("x1") and not drops.valid_label("-")
    assert not drops.valid_label("abcde") and not drops.valid_label("ab_c")
    assert drops.kind_of("martinez") == "long"


def test_scan_keeps_only_four_character_names_of_the_zone():
    data = zone([
        "com.\t900\tin\tsoa\ta.gtld-servers.net. nstld.verisign-grs.com. 1 1800 900 604800 86400",
        "abcd.com.\t172800\tin\tns\tns1.example.net.",
        "abcd.com.\t172800\tin\tns\tns2.example.net.",
        "AB12.COM.\t172800\tIN\tNS\tns1.example.net.",
        "x-y1.com.\t172800\tin\tns\tns1.example.net.",
        "-abc.com.\t172800\tin\tns\tns1.example.net.",
        "abcde.com.\t172800\tin\tns\tns1.example.net.",
        "abc.com.\t172800\tin\tns\tns1.example.net.",
        "wxyz.net.\t172800\tin\tns\tns1.example.net.",
        "ns1.abcd.com.\t172800\tin\ta\t192.0.2.1",
        "qrst.com.au.\t172800\tin\tns\tns1.example.net.",
    ])
    # Morceaux minuscules : les coupures tombent au milieu des lignes et des membres gzip.
    chunks = [data[i:i + 7] for i in range(0, len(data), 7)]
    bits = drops.scan_bytes(chunks, "com")
    found = sorted(drops.diff(bytes(drops.BITMAP_BYTES), bits)[1])
    assert found == ["ab12", "abc", "abcd", "x-y1"]


def test_scan_reads_concatenated_gzip_members():
    data = zone(["abcd.com.\t1\tin\tns\ta."]) + zone(["wxyz.com.\t1\tin\tns\ta."])
    bits = drops.scan_bytes([data], "com")
    assert drops.count(bits) == 2


def test_diff_reports_left_and_joined():
    removed, added = drops.diff(bits_of(["aaaa", "bbbb", "cc11"]), bits_of(["aaaa", "dd-d"]))
    assert sorted(removed) == ["bbbb", "cc11"] and added == ["dd-d"]


@pytest.fixture
def watcher(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    db.migrate(conn, tmp_path / "backups")
    store = Store(conn, {})
    w = drops.DropWatcher(conn, store, tmp_path, rdap=None, delays=DELAYS)
    w.workers = 1
    yield w
    conn.close()


class FakeIcann:
    def __init__(self, zones: dict[str, bytes]):
        self.zones = zones
        self.auth_calls = 0
        self.expire_token_once = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == "account-api.icann.org":
            self.auth_calls += 1
            body = json.loads(request.content)
            if body["password"] != "secret":
                return httpx.Response(401)
            return httpx.Response(200, json={"accessToken": f"tok{self.auth_calls}"})
        if request.headers.get("authorization", "").startswith("Bearer tok") is False:
            return httpx.Response(401)
        if self.expire_token_once:
            self.expire_token_once = False
            return httpx.Response(401)
        if request.url.path == "/czds/downloads/links":
            return httpx.Response(200, json=[f"https://czds-download-api.icann.org/czds/downloads/{t}.zone" for t in self.zones])
        tld = request.url.path.rsplit("/", 1)[-1].removesuffix(".zone")
        return httpx.Response(200, content=self.zones[tld])


def use(watcher, icann: FakeIcann, password="secret"):
    watcher.store.set("czds_username", "moi@example.com")
    watcher.store.set("czds_password", password)
    watcher._http_factory = lambda: httpx.AsyncClient(transport=httpx.MockTransport(icann.handler))


def test_first_scan_is_a_baseline_then_differences_are_recorded(watcher):
    icann = FakeIcann({"com": zone(["abcd.com.\t1\tin\tns\ta.", "wxyz.com.\t1\tin\tns\ta."]), "net": zone([])})
    use(watcher, icann)
    first = asyncio.run(watcher.run())
    assert [(r.tld, r.first, r.names) for r in first] == [("com", True, 2), ("net", True, 0)]
    assert watcher.conn.execute("SELECT COUNT(*) FROM drops").fetchone()[0] == 0
    assert watcher.approved() == ["com", "net"]

    icann.zones["com"] = zone(["wxyz.com.\t1\tin\tns\ta.", "a1b2.com.\t1\tin\tns\ta."])
    second = asyncio.run(watcher.run())
    assert (second[0].removed, second[0].added) == (1, 1)
    row = watcher.conn.execute("SELECT * FROM drops").fetchone()
    assert (row["name"], row["kind"], row["state"]) == ("abcd.com", "letters", None)

    # Revenu dans la zone le lendemain : renouvelé, il sort des listes mais reste en base.
    icann.zones["com"] = zone(["abcd.com.\t1\tin\tns\ta.", "wxyz.com.\t1\tin\tns\ta.", "a1b2.com.\t1\tin\tns\ta."])
    asyncio.run(watcher.run())
    row = watcher.conn.execute("SELECT * FROM drops").fetchone()
    assert (row["state"], row["next_check_at"]) == (drops.TAKEN, None)


def test_only_enabled_zones_are_scanned(watcher):
    icann = FakeIcann({"com": zone([]), "net": zone([])})
    use(watcher, icann)
    watcher.store.set("czds_enabled", json.dumps(["net"]))
    assert [r.tld for r in asyncio.run(watcher.run())] == ["net"]


def test_wrong_password_is_reported_without_scanning(watcher):
    icann = FakeIcann({"com": zone([])})
    use(watcher, icann, password="faux")
    assert asyncio.run(watcher.run()) == []
    assert "refusé" in watcher.store.get("czds_error")


def test_expired_token_is_renewed_once(watcher):
    icann = FakeIcann({"com": zone(["abcd.com.\t1\tin\tns\ta."])})
    icann.expire_token_once = True
    use(watcher, icann)
    results = asyncio.run(watcher.run())
    assert results[0].names == 1 and icann.auth_calls == 2


def test_corrupt_zone_keeps_the_previous_bitmap(watcher):
    icann = FakeIcann({"com": zone(["abcd.com.\t1\tin\tns\ta."])})
    use(watcher, icann)
    asyncio.run(watcher.run())
    icann.zones["com"] = b"pas du gzip"
    result = asyncio.run(watcher.run())[0]
    assert result.error == "Fichier de zone corrompu ou incomplet."
    assert drops.count(watcher._load("com")) == 1
    assert watcher.status("com")["error"] == result.error


def test_truncated_zone_is_rejected_not_compared(watcher):
    full = zone([f"{drops.decode(i)}.com.\t1\tin\tns\ta." for i in range(0, 300000, 97)])
    icann = FakeIcann({"com": full})
    use(watcher, icann)
    asyncio.run(watcher.run())
    icann.zones["com"] = full[: len(full) // 2]
    result = asyncio.run(watcher.run())[0]
    assert result.error == "Fichier de zone corrompu ou incomplet."
    assert watcher.conn.execute("SELECT COUNT(*) FROM drops").fetchone()[0] == 0


def test_abnormal_drop_count_is_ignored(watcher):
    names = [f"{drops.decode(i)}.com.\t1\tin\tns\ta." for i in range(0, 300000, 97)]
    icann = FakeIcann({"com": zone(names)})
    use(watcher, icann)
    baseline = asyncio.run(watcher.run())[0].names
    icann.zones["com"] = zone(names[: len(names) // 2])
    result = asyncio.run(watcher.run())[0]
    assert result.error.startswith("Écart anormal")
    assert watcher.conn.execute("SELECT COUNT(*) FROM drops").fetchone()[0] == 0
    assert drops.count(watcher._load("com")) == baseline > 1000


def test_network_cut_is_recorded_and_other_zones_still_read(watcher):
    icann = FakeIcann({"com": zone([]), "net": zone(["abcd.net.\t1\tin\tns\ta."])})
    handler = icann.handler

    def cut(request):
        if request.url.path.endswith("com.zone"):
            raise httpx.ReadError("coupure")
        return handler(request)

    use(watcher, icann)
    watcher._http_factory = lambda: httpx.AsyncClient(transport=httpx.MockTransport(cut))
    results = {r.tld: r for r in asyncio.run(watcher.run())}
    assert "interrompu" in results["com"].error or "injoignable" in results["com"].error
    assert results["net"].names == 1


def test_due_once_a_day_after_the_hour(watcher):
    watcher.store.set("czds_username", "moi")
    watcher.store.set("czds_password", "x")
    watcher.store.set("czds_hour", "6")
    assert not watcher.due(datetime(2026, 10, 13, 5, 59))
    assert watcher.due(datetime(2026, 10, 13, 6, 0))
    watcher.store.set("czds_last_run", "2026-10-13")
    assert not watcher.due(datetime(2026, 10, 13, 23, 0))
    assert watcher.due(datetime(2026, 10, 14, 6, 30))


class FakeRdap:
    def __init__(self, results: dict[str, LookupResult]):
        self.results = results

    async def lookup(self, name):
        return self.results[name]


def test_rdap_check_classifies_and_estimates_release(watcher):
    now = datetime.now(timezone.utc)
    deleted = now - timedelta(days=3)
    watcher.record("com", ["abcd", "wxyz", "a1b2", "zz99"], [], now)
    watcher.rdap = FakeRdap({
        "abcd.com": LookupResult(found=True, info=DomainInfo(statuses=["redemption period"], last_changed_at=deleted)),
        "wxyz.com": LookupResult(available=True),
        "a1b2.com": LookupResult(found=True, info=DomainInfo(statuses=["client hold"], expires_at=now + timedelta(days=200))),
        "zz99.com": LookupResult(error="Serveur RDAP en erreur (503)"),
    })
    assert asyncio.run(watcher.check_due()) == 4
    rows = {r["name"]: r for r in watcher.conn.execute("SELECT * FROM drops")}

    redemption = rows["abcd.com"]
    assert redemption["state"] == states.REDEMPTION
    release = datetime.fromisoformat(redemption["release_at"])
    # 30 jours de rédemption + 5 de suppression après la date de suppression, à l'heure de drop .com.
    assert release.date() == (deleted + timedelta(days=35)).date() and (release.hour, release.minute) == (12, 0)

    assert rows["wxyz.com"]["state"] == states.AVAILABLE and rows["wxyz.com"]["available_since"]
    assert rows["a1b2.com"]["state"] == drops.SUSPENDED
    assert rows["zz99.com"]["state"] is None and "503" in rows["zz99.com"]["error"]


def test_checks_only_when_something_should_change(watcher):
    now = datetime.now(timezone.utc)
    deleted = now - timedelta(days=3)
    watcher.record("com", ["abcd", "wxyz", "a1b2", "q9q9"], [], now)
    watcher.rdap = FakeRdap({
        "abcd.com": LookupResult(found=True, info=DomainInfo(statuses=["redemption period"], last_changed_at=deleted)),
        "wxyz.com": LookupResult(available=True),
        "a1b2.com": LookupResult(found=True, info=DomainInfo(statuses=["client hold"], expires_at=now + timedelta(days=200))),
        "q9q9.com": LookupResult(found=True, info=DomainInfo(statuses=["active"], expires_at=now + timedelta(days=200))),
    })
    asyncio.run(watcher.check_due())
    rows = {r["name"]: r for r in watcher.conn.execute("SELECT * FROM drops")}
    # Rédemption : une seule vérification, à la libération estimée.
    following = datetime.fromisoformat(rows["abcd.com"]["next_check_at"])
    assert following == datetime.fromisoformat(rows["abcd.com"]["release_at"]) + timedelta(minutes=30)
    # Libre : plus de vérification automatique. Repris : idem, gardé pour l'historique.
    assert rows["wxyz.com"]["next_check_at"] is None
    assert (rows["q9q9.com"]["state"], rows["q9q9.com"]["next_check_at"]) == (drops.TAKEN, None)
    # Suspendu : une fois par semaine.
    assert datetime.fromisoformat(rows["a1b2.com"]["next_check_at"]) > now + timedelta(days=6)


def test_overdue_release_is_watched_every_six_hours(watcher):
    now = datetime.now(timezone.utc)
    assert watcher._next(states.REDEMPTION, now, now - timedelta(hours=2)) == now + timedelta(hours=6)
    assert watcher._next(states.REDEMPTION, now, None) == now + timedelta(hours=24)


def test_fr_is_left_to_the_afnic_queue(watcher):
    now = datetime.now(timezone.utc)
    watcher.record("fr", ["abcd"], [], now - timedelta(minutes=1))
    watcher.rdap = FakeRdap({})
    assert asyncio.run(watcher.check_due()) == 0


def test_scanner_finds_short_names_and_listed_words_only():
    data = zone([
        "x.com.\t1\tin\tns\ta.",
        "q7.com.\t1\tin\tns\ta.",
        "martinez.com.\t1\tin\tns\ta.",
        "martinez.com.\t1\tin\tns\tb.",
        "MAISON.COM.\t1\tIN\tNS\ta.",
        "zzkqwpl.com.\t1\tin\tns\ta.",
        "martinez.net.\t1\tin\tns\ta.",
    ])
    scanner = drops.ZoneScanner("com")
    for i in range(0, len(data), 11):
        scanner.feed(data[i:i + 11])
    bits, words = scanner.finish()
    assert sorted(drops.diff(bytes(drops.BITMAP_BYTES), bits)[1]) == ["q7", "x"]
    assert words == {"martinez", "maison"}


def test_listed_words_are_tracked_with_their_categories(watcher):
    icann = FakeIcann({"com": zone(["navarro.com.\t1\tin\tns\ta.", "courir.com.\t1\tin\tns\ta.",
                                    "martin.com.\t1\tin\tns\ta.", "velo.com.\t1\tin\tns\ta."])})
    use(watcher, icann)
    asyncio.run(watcher.run())
    icann.zones["com"] = zone([])
    result = asyncio.run(watcher.run())[0]
    assert result.removed == 4
    rows = {r["name"]: (r["kind"], r["lists"]) for r in watcher.conn.execute("SELECT * FROM drops")}
    assert rows == {
        "navarro.com": ("long", "surname,english"),
        "courir.com": ("long", "word"),
        "martin.com": ("long", "word,surname,firstname,english"),
        "velo.com": ("letters", "word,trend"),
    }


def test_word_lists_are_loaded_and_normalised():
    from app import wordlists

    lists = wordlists.lists()
    assert {"maison", "courir", "coeur", "eleve"} <= lists["word"]
    assert {"martinez", "navarro", "dupont", "martin", "legall"} <= lists["surname"]
    assert all(w == w.lower() and w.isascii() and " " not in w for names in lists.values() for w in names)
    assert {"jacques", "jean-pierre", "marie"} <= lists["firstname"]
    assert len(lists["word"]) == 20000 and len(lists["surname"]) > 20000 and len(lists["firstname"]) > 4000


def test_parallel_scan_gives_the_same_result_as_a_single_process():
    from app import wordlists

    names = [drops.decode(i) for i in range(0, drops.CELLS, 211)] + sorted(wordlists.lists()["surname"])[:3000]
    data = zone([f"{n}.com.\t1\tin\tns\ta." for n in names] + ["QZXUPPER.COM.\t1\tIN\tNS\ta.", "navarro.com.\t1\tin\tns\tb."])
    chunks = [data[i:i + 4096] for i in range(0, len(data), 4096)]
    results = []
    for workers in (1, 3):
        scanner = drops.ZoneScanner("com", workers=workers)
        for chunk in chunks:
            scanner.feed(chunk)
        results.append(scanner.finish())
    assert results[0][0] == results[1][0] and results[0][1] == results[1][1]
    assert "navarro" in results[1][1] and "qzxupper" not in results[1][1]
    assert drops.count(results[1][0]) > 8000


def test_scan_history_keeps_fourteen_runs_with_timings(watcher):
    icann = FakeIcann({"com": zone(["abcd.com.\t1\tin\tns\ta."])})
    use(watcher, icann)
    for _ in range(16):
        asyncio.run(watcher.run())
    rows = watcher.history()
    assert len(rows) == drops.KEEP_SCANS
    row = rows[0]
    assert row["tld"] == "com" and row["workers"] == 1 and row["error"] is None
    assert row["seconds"] >= 0 and row["wait_seconds"] >= 0 and row["compute_seconds"] >= 0


class _CutStream(httpx.AsyncByteStream):
    def __init__(self, data: bytes):
        self.data = data

    async def __aiter__(self):
        yield self.data[: len(self.data) // 2]
        raise httpx.ReadError("coupure en cours de route")


def test_cut_in_the_middle_of_a_download_is_reported(watcher):
    full = zone([f"{drops.decode(i)}.com.\t1\tin\tns\ta." for i in range(0, 300000, 97)])
    icann = FakeIcann({"com": full})
    handler = icann.handler

    def cut(request):
        if request.url.path.endswith("com.zone"):
            return httpx.Response(200, stream=_CutStream(full))
        return handler(request)

    use(watcher, icann)
    watcher._http_factory = lambda: httpx.AsyncClient(transport=httpx.MockTransport(cut))
    result = asyncio.run(watcher.run())[0]
    assert result.error == "Téléchargement interrompu (ReadError)."
    assert watcher._load("com") is None and watcher.history()[0]["error"] == result.error


def test_stale_change_date_does_not_put_release_in_the_past(watcher):
    # Cas réel (jhgh.biz) : dernière modification RDAP bien avant l'échéance ; la rédemption vient
    # d'être vue, la libération estimée ne peut pas être déjà passée.
    now = datetime.now(timezone.utc)
    watcher.record("com", ["jhgh"], [], now)
    watcher.rdap = FakeRdap({"jhgh.com": LookupResult(found=True, info=DomainInfo(
        statuses=["redemption period"], expires_at=now - timedelta(days=44), last_changed_at=now - timedelta(days=350)))})
    asyncio.run(watcher.check_due())
    row = watcher.conn.execute("SELECT * FROM drops WHERE name = 'jhgh.com'").fetchone()
    assert datetime.fromisoformat(row["release_at"]) > now + timedelta(days=30)


def test_unreadable_zone_list_is_reported_not_raised(watcher):
    icann = FakeIcann({"com": zone([])})
    use(watcher, icann)

    def handler(request):
        if request.url.path == "/czds/downloads/links":
            return httpx.Response(200, text="<html>Maintenance</html>")  # portail en maintenance
        return icann.handler(request)

    watcher._http_factory = lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))
    assert asyncio.run(watcher.run()) == []
    assert "illisible" in watcher.store.get("czds_error")


def test_refused_credentials_wait_an_hour_before_a_new_try(watcher):
    icann = FakeIcann({"com": zone([])})
    use(watcher, icann, password="faux")
    now = datetime(2026, 10, 17, 7, 0)
    assert watcher.due(now)
    asyncio.run(watcher.run())
    assert "refusé" in watcher.store.get("czds_error") and icann.auth_calls == 1
    # Plus d'essai toutes les 15 minutes (96 tentatives par jour chez ICANN) : une heure d'attente.
    assert not watcher.due(now)
    watcher.store.set("czds_retry_at", "2020-01-01T00:00:00+00:00")
    assert watcher.due(now)
    asyncio.run(watcher.run())
    assert not watcher.due(now)
    # Nouveau mot de passe accepté : l'attente tombe.
    use(watcher, icann)
    asyncio.run(watcher.refresh_approved())
    assert watcher.store.get("czds_retry_at") == "" and watcher.due(now)

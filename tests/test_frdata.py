import asyncio
import io
import json
import zipfile
from datetime import date, datetime, timedelta, timezone

import httpx
import pytest

from app import db, drops, frdata, states
from app.rdap import DomainInfo, LookupResult
from app.store import Store

HEADER = "Nom de domaine;Pays BE;Departement BE;Ville BE;Nom BE;Sous domaine;Type du titulaire;Pays titulaire;Departement titulaire;Date de création;Date de retrait"


def csv_text(rows: list[str]) -> str:
    return "\n".join([HEADER, *rows]) + "\n"


def make_zip(text: str) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("201601_OPENDATA_A-NomsDeDomaineEnPointFr.csv", text.encode("latin-1"))
    return buffer.getvalue()


def test_columns_are_found_by_their_titles():
    columns = frdata.find_columns(HEADER.split(";"))
    assert columns == {"name": 0, "created": 9, "removed": 10}
    with pytest.raises(frdata.FrDataError, match="introuvable"):
        frdata.find_columns(["a", "b"])


def test_dates_in_several_formats():
    assert frdata.parse_date("15-03-2020") == date(2020, 3, 15)
    assert frdata.parse_date("15/03/2020") == date(2020, 3, 15)
    assert frdata.parse_date("2020-03-15") == date(2020, 3, 15)
    assert frdata.parse_date("") is None and frdata.parse_date("n/a") is None


def test_only_watched_second_level_names_are_kept():
    text = csv_text([
        "navarro.fr;FR;;;;;PP;FR;;01-02-2010;",
        "abc.fr;FR;;;;;PM;FR;;01-02-2001;",
        "zzkqwplx.fr;FR;;;;;PP;FR;;01-02-2010;",
        "dupont.asso.fr;FR;;;;asso;PP;FR;;01-02-2010;",
        "courir.com;FR;;;;;PP;FR;;01-02-2010;",
        "soleil.fr;FR;;;;;PP;FR;;01-02-2005;10-01-2020",
        "soleil.fr;FR;;;;;PP;FR;;03-04-2021;",
    ])
    kept, stats = frdata.read_rows(iter(io.StringIO(text)))
    assert set(kept) == {"navarro", "abc", "soleil"}
    # Supprimé en 2020 puis recréé en 2021 : la ligne la plus récente fait foi.
    assert kept["soleil"] == (date(2021, 4, 3), None)
    assert stats["rows"] == 7 and stats["columns"]["removed"] == "Date de retrait"


@pytest.fixture
def fr(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    db.migrate(conn, tmp_path / "backups")
    store = Store(conn, {})
    watcher = drops.DropWatcher(conn, store, tmp_path, rdap=None, delays=states.load_delays())
    watcher.workers = 1
    fr = frdata.FrWatcher(conn, store, tmp_path, rdap=None, drops_watcher=watcher)
    store.set("fr_enabled", "1")
    yield fr
    conn.close()


def serve(fr, archive: bytes, modified="2026-10-15T00:00:00"):
    calls = []

    def handler(request):
        calls.append(request.url.host)
        if request.url.host == "www.afnic.fr":
            return httpx.Response(404)
        if request.url.host == "www.data.gouv.fr":
            return httpx.Response(200, json={"resources": [
                {"title": "B-Indicateurs par pays", "url": "https://opendata.afnic.fr/B.zip", "last_modified": modified},
                {"title": "A-NomsDeDomaineEnPointFr", "url": "https://opendata.afnic.fr/A-NomsDeDomaineEnPointFr.zip", "last_modified": modified},
            ]})
        return httpx.Response(200, content=archive)

    fr._http_factory = lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return calls


def test_import_keeps_watched_names_and_queues_recent_removals(fr):
    recent = (datetime.now(timezone.utc) - timedelta(days=10)).strftime("%d-%m-%Y")
    archive = make_zip(csv_text([
        "navarro.fr;FR;;;;;PP;FR;;01-02-2010;",
        f"velo.fr;FR;;;;;PP;FR;;01-02-2010;{recent}",
        "courir.fr;FR;;;;;PP;FR;;01-02-2010;01-01-2015",
    ]))
    calls = serve(fr, archive)
    status = asyncio.run(fr.import_file())
    assert status["error"] is None and status["kept"] == 3 and status["registered"] == 1
    assert not list(fr.dir.glob("afnic.*"))
    rows = {r["label"]: r for r in fr.conn.execute("SELECT * FROM fr_names")}
    # Créé un 1er février : vérifié le 2 février suivant, à midi UTC.
    following = datetime.fromisoformat(rows["navarro"]["next_check_at"])
    assert (following.month, following.day, following.hour) == (2, 2, 12)
    assert following > datetime.now(timezone.utc)
    assert rows["velo"]["next_check_at"] is None
    tracked = [r["name"] for r in fr.conn.execute("SELECT name FROM drops")]
    assert tracked == ["velo.fr"]
    assert "opendata.afnic.fr" in calls

    # Même fichier la semaine suivante : rien n'est retéléchargé.
    calls.clear()
    asyncio.run(fr.import_file())
    assert "opendata.afnic.fr" not in calls and calls[-1] == "www.data.gouv.fr"


def test_unknown_columns_are_reported_not_hidden(fr):
    serve(fr, make_zip("colonne1;colonne2\nx;y\n"))
    status = asyncio.run(fr.import_file())
    assert "introuvable" in status["error"] and "colonne1" in status["error"]
    assert not list(fr.dir.glob("afnic.*"))


class FakeRdap:
    def __init__(self, results):
        self.results = results

    async def lookup(self, name):
        return self.results[name]


def test_anniversary_dates():
    assert frdata.next_anniversary(date(2015, 3, 12), date(2026, 3, 11)) == date(2026, 3, 12)
    assert frdata.next_anniversary(date(2015, 3, 12), date(2026, 3, 12)) == date(2027, 3, 12)
    assert frdata.next_anniversary(date(2016, 2, 29), date(2026, 1, 1)) == date(2026, 2, 28)


def add_due(fr, label, created="2015-03-12"):
    past = (datetime.now(timezone.utc) - timedelta(minutes=1)).replace(microsecond=0).isoformat()
    fr.conn.execute("INSERT INTO fr_names (label, created_on, next_check_at) VALUES (?, ?, ?)", (label, created, past))


def test_check_follows_the_real_expiry_and_hands_drops_over(fr):
    fr.store.set("fr_rate", "10")
    now = datetime.now(timezone.utc).replace(microsecond=0)
    for label in ("navarro", "soleil", "velo"):
        add_due(fr, label)
    expires = now + timedelta(days=200)
    fr.rdap = FakeRdap({
        "navarro.fr": LookupResult(found=True, info=DomainInfo(statuses=["active"], expires_at=expires)),
        "soleil.fr": LookupResult(found=True, info=DomainInfo(statuses=["redemption period"], last_changed_at=now)),
        "velo.fr": LookupResult(available=True),
    })
    assert asyncio.run(fr.tick()) == 3
    rows = {r["label"]: r for r in fr.conn.execute("SELECT * FROM fr_names")}
    following = datetime.fromisoformat(rows["navarro"]["next_check_at"])
    assert following.date() == (expires + timedelta(days=1)).date()
    assert rows["soleil"]["next_check_at"] is None and rows["velo"]["next_check_at"] is None
    # Passés à l'écran Suppressions avec le résultat déjà lu : pas de seconde requête.
    found = {r["name"]: r for r in fr.conn.execute("SELECT * FROM drops")}
    assert found["soleil.fr"]["state"] == states.REDEMPTION and found["velo.fr"]["state"] == states.AVAILABLE
    # .fr : rédemption de 30 jours, libre juste après. Vérifié une fois, à cette date.
    release = datetime.fromisoformat(found["soleil.fr"]["release_at"])
    assert release.date() == (now + timedelta(days=30)).date()
    assert found["velo.fr"]["next_check_at"] is None
    assert asyncio.run(fr.tick()) == 0
    assert fr.conn.execute("SELECT COUNT(*) FROM fr_requests").fetchone()[0] == 3


def test_a_passed_fr_expiry_without_redemption_is_a_late_renewal(fr):
    """oncle.fr : vérifié le lendemain de l'échéance, RDAP encore à l'ancienne date, statut actif.
    Pas de délai de grâce en .fr : ce n'est pas une libération, on regarde de nouveau le lendemain."""
    fr.store.set("fr_rate", "10")
    now = datetime.now(timezone.utc).replace(microsecond=0)
    add_due(fr, "oncle")
    late = LookupResult(found=True, info=DomainInfo(statuses=["active"], expires_at=now - timedelta(hours=20)))
    fr.rdap = fr.drops.rdap = FakeRdap({"oncle.fr": late})
    asyncio.run(fr.tick())
    assert fr.conn.execute("SELECT COUNT(*) FROM drops").fetchone()[0] == 0
    row = fr.conn.execute("SELECT * FROM fr_names WHERE label = 'oncle'").fetchone()
    assert datetime.fromisoformat(row["next_check_at"]).date() == (now + timedelta(days=1)).date()
    # Un nom déjà passé à l'écran Suppressions pour cette raison (ancienne version) : la
    # vérification suivante le rend à son propriétaire et aux dates anniversaire.
    fr.drops.record("fr", ["oncle"], [], now)
    fr.conn.execute("UPDATE drops SET state = ? WHERE name = 'oncle.fr'", (states.EXPIRED,))
    fr.conn.execute("UPDATE fr_names SET next_check_at = NULL WHERE label = 'oncle'")
    state = fr.drops.apply(fr.drops.row("oncle.fr"), late, now)
    assert state == drops.TAKEN
    row = fr.conn.execute("SELECT * FROM fr_names WHERE label = 'oncle'").fetchone()
    assert row["removed_on"] is None and row["next_check_at"] is not None


def test_requests_are_capped_per_minute(fr):
    for i in range(10):
        add_due(fr, f"nom{i}")
    fr.rdap = FakeRdap({f"nom{i}.fr": LookupResult(available=True) for i in range(10)})
    assert asyncio.run(fr.tick()) == frdata.DEFAULT_RATE == 2
    # Même minute : le plafond est atteint, y compris par les vérifications manuelles.
    assert asyncio.run(fr.tick()) == 0


def test_removals_and_anniversaries_share_one_queue(fr):
    fr.store.set("fr_rate", "10")
    now = datetime.now(timezone.utc)
    add_due(fr, "navarro")
    fr.drops.record("fr", ["velo"], [], now - timedelta(hours=1))
    fr.rdap = FakeRdap({
        "velo.fr": LookupResult(available=True),
        "navarro.fr": LookupResult(found=True, info=DomainInfo(statuses=["active"], expires_at=now + timedelta(days=90))),
    })
    asyncio.run(fr.tick())
    sources = [r[0] for r in fr.conn.execute("SELECT source FROM fr_requests ORDER BY id")]
    # Le plus en retard d'abord : la suppression, prévue il y a une heure.
    assert sources == ["suppression", "anniversaire"]


def test_rate_limit_pauses_and_backs_off(fr):
    add_due(fr, "navarro")
    add_due(fr, "dupont")
    fr.rdap = FakeRdap({
        "dupont.fr": LookupResult(error="Limite de requêtes du registre atteinte (429)"),
        "navarro.fr": LookupResult(error="Limite de requêtes du registre atteinte (429)"),
    })
    assert asyncio.run(fr.tick()) == 1
    first = fr.paused_until()
    assert first and timedelta(minutes=59) < first - datetime.now(timezone.utc) <= timedelta(hours=1)
    assert asyncio.run(fr.tick()) == 0
    # Rien n'a été reporté : les deux noms seront vérifiés à la reprise.
    assert fr.conn.execute("SELECT COUNT(*) FROM fr_names WHERE checked_at IS NULL").fetchone()[0] == 2
    # Nouveau signal à la reprise : pause doublée.
    fr.store.set("fr_paused_until", "")
    fr.conn.execute("DELETE FROM fr_requests")
    asyncio.run(fr.tick())
    assert fr.paused_until() - datetime.now(timezone.utc) > timedelta(minutes=119)
    # Une pause demandée par l'Afnic plus longue est respectée.
    fr._pause("Limite de requêtes du registre atteinte, reprise dans 36000 s", datetime.now(timezone.utc))
    assert fr.paused_until() - datetime.now(timezone.utc) > timedelta(hours=9)


def test_new_removals_are_checked_whatever_their_date(fr):
    old = (datetime.now(timezone.utc) - timedelta(days=200)).strftime("%d-%m-%Y")
    recent = (datetime.now(timezone.utc) - timedelta(days=60)).strftime("%d-%m-%Y")
    serve(fr, make_zip(csv_text([
        "navarro.fr;FR;;;;;PP;FR;;01-02-2010;",
        f"velo.fr;FR;;;;;PP;FR;;01-02-2010;{recent}",
        f"courir.fr;FR;;;;;PP;FR;;01-02-2010;{old}",
    ])))
    asyncio.run(fr.import_file())
    # Premier import : suppressions des 90 derniers jours seulement.
    assert [r[0] for r in fr.conn.execute("SELECT name FROM drops")] == ["velo.fr"]

    # Fichier suivant : navarro supprimé il y a 70 jours (le fichier arrive en retard), vérifié quand même.
    later = (datetime.now(timezone.utc) - timedelta(days=70)).strftime("%d-%m-%Y")
    serve(fr, make_zip(csv_text([
        f"navarro.fr;FR;;;;;PP;FR;;01-02-2010;{later}",
        f"velo.fr;FR;;;;;PP;FR;;01-02-2010;{recent}",
        f"courir.fr;FR;;;;;PP;FR;;01-02-2010;{old}",
    ])), modified="2026-11-15T00:00:00")
    asyncio.run(fr.import_file())
    assert sorted(r[0] for r in fr.conn.execute("SELECT name FROM drops")) == ["navarro.fr", "velo.fr"]


def test_work_of_the_day_is_reported(fr):
    add_due(fr, "navarro")
    fr.conn.execute(
        "INSERT INTO fr_names (label, created_on, next_check_at) VALUES ('dupont', '2015-03-12', ?)",
        ((datetime.now(timezone.utc) + timedelta(days=1, hours=12)).isoformat(),),
    )
    work = fr.status()["work"]
    assert work["left_today"]["anniversaries"] >= 1 and work["due_now"] == 1
    assert work["current"] is None and work["done_today"] == {}


def test_disabled_fr_does_nothing(fr):
    fr.store.set("fr_enabled", "0")
    assert asyncio.run(fr.tick()) == 0
    assert not fr.due(datetime.now())


def test_moved_dataset_is_found_through_search(fr):
    archive = make_zip(csv_text(["navarro.fr;FR;;;;;PP;FR;;01-02-2010;"]))

    def handler(request):
        if request.url.host == "www.afnic.fr":
            return httpx.Response(404)
        if request.url.path.endswith("/open-data-du-fr-1/"):
            return httpx.Response(404, json={"message": "Not Found"})
        if request.url.host == "www.data.gouv.fr":
            return httpx.Response(200, json={"data": [{"resources": [
                {"title": "Indicateurs", "url": "https://x/B.zip"},
                {"title": "Noms de domaine en .fr 2026-08", "url": "https://x/old.zip", "last_modified": "2026-08-15"},
                {"title": "Noms de domaine en .fr 2026-09", "url": "https://x/new.zip", "last_modified": "2026-09-15"},
            ]}]})
        assert request.url.path == "/new.zip"
        return httpx.Response(200, content=archive)

    fr._http_factory = lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))
    status = asyncio.run(fr.import_file())
    assert status["error"] is None and status["kept"] == 1


def test_missing_file_lists_what_was_seen(fr):
    def handler(request):
        if request.url.host == "www.afnic.fr":
            return httpx.Response(404)
        if "q=" in str(request.url):
            return httpx.Response(200, json={"data": []})
        return httpx.Response(200, json={"resources": [{"title": "Indicateurs par pays", "url": "https://x/B.zip"}]})

    fr._http_factory = lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))
    error = asyncio.run(fr.import_file())["error"]
    assert "Indicateurs par pays" in error and "Adresse du fichier" in error


def test_recent_months():
    assert frdata.recent_months(date(2026, 2, 10)) == ["202602", "202601", "202512", "202511"]


def afnic_files(fr, available: dict[str, bytes]):
    fetched = []

    def handler(request):
        if request.url.host != "www.afnic.fr":
            raise AssertionError("data.gouv.fr ne doit pas servir")
        month = request.url.path.rsplit("/", 1)[-1][:6]
        fetched.append(month)
        if month in available:
            return httpx.Response(200, content=available[month])
        return httpx.Response(404)

    fr._http_factory = lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return fetched


def test_latest_monthly_file_is_taken_from_afnic(fr):
    months = frdata.recent_months(datetime.now(timezone.utc).date())
    archive = make_zip(csv_text(["navarro.fr;FR;;;;;PP;FR;;01-02-2010;"]))
    # Le fichier du mois n'est pas encore sorti : on prend celui du mois d'avant.
    fetched = afnic_files(fr, {months[1]: archive, months[2]: archive})
    status = asyncio.run(fr.import_file())
    assert status["error"] is None and status["kept"] == 1
    assert json.loads(fr.store.get("fr_status"))["file_date"] == months[1]
    assert fetched[:2] == [months[0], months[1]]


def test_pasted_monthly_address_follows_new_months(fr):
    months = frdata.recent_months(datetime.now(timezone.utc).date())
    fr.store.set("fr_url", "https://www.afnic.fr/wp-media/ftp/documentsOpenData/202001_OPENDATA_A-NomsDeDomaineEnPointFr.zip")
    archive = make_zip(csv_text(["navarro.fr;FR;;;;;PP;FR;;01-02-2010;"]))
    fetched = afnic_files(fr, {months[0]: archive})
    assert asyncio.run(fr.import_file())["error"] is None
    assert fetched[0] == months[0] and "202001" not in fetched


def test_cut_download_leaves_nothing_on_disk(fr):
    def handler(request):
        raise httpx.ReadError("coupure")

    fr.store.set("fr_url", "https://example.org/fichier.zip")
    fr._http_factory = lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))
    assert "interrompu" in asyncio.run(fr.import_file())["error"]
    assert not list(fr.dir.glob("afnic.*"))


def test_utf8_file_keeps_creation_dates(fr, tmp_path):
    # Fichier en UTF-8 : lu en latin-1, « création » devenait « crÃ©ation » et la colonne disparaissait.
    path = tmp_path / "utf8.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("x.csv", csv_text(["navarro.fr;FR;;;;;PP;FR;;01-02-2010;"]).encode("utf-8"))
    kept, stats = frdata.read_zip(path)
    assert kept["navarro"] == (date(2010, 2, 1), None) and stats["dated"] == 1
    assert stats["columns"]["created"] == "Date de création"


def test_latin1_file_still_read(tmp_path):
    path = tmp_path / "latin1.zip"
    path.write_bytes(make_zip(csv_text(["navarro.fr;FR;;;;;PP;FR;;01-02-2010;"])))
    kept, stats = frdata.read_zip(path)
    assert stats["columns"]["created"] == "Date de création" and kept["navarro"][0] == date(2010, 2, 1)


def test_fixed_reader_reimports_the_same_month(fr):
    fr.store.set("fr_status", json.dumps({"at": datetime.now(timezone.utc).isoformat(), "file_date": "202608", "reader": 1}))
    assert fr.due(datetime.now())
    fr.store.set("fr_status", json.dumps({"at": datetime.now(timezone.utc).isoformat(), "file_date": "202608",
                                          "reader": frdata.READER_VERSION, "lists": frdata.lists_version()}))
    assert not fr.due(datetime.now())


def test_new_lists_force_a_reimport_without_flooding_checks(fr):
    old = (datetime.now(timezone.utc) - timedelta(days=400)).strftime("%d-%m-%Y")
    serve(fr, make_zip(csv_text(["navarro.fr;FR;;;;;PP;FR;;01-02-2010;"])))
    asyncio.run(fr.import_file())
    # Les listes changent : le même fichier est relu.
    status = json.loads(fr.store.get("fr_status"))
    status["lists"] = "ancienne"
    fr.store.set("fr_status", json.dumps(status))
    assert fr.due(datetime.now())
    serve(fr, make_zip(csv_text(["navarro.fr;FR;;;;;PP;FR;;01-02-2010;", f"maisons.fr;FR;;;;;PP;FR;;01-02-2010;{old}"])))
    asyncio.run(fr.import_file())
    assert fr.conn.execute("SELECT COUNT(*) FROM fr_names WHERE label = 'maisons'").fetchone()[0] == 1
    # Supprimé il y a plus d'un an, nouveau dans les listes : pas de vérification.
    assert fr.conn.execute("SELECT COUNT(*) FROM drops").fetchone()[0] == 0


def test_apply_runs_in_a_worker_thread_with_its_own_connection(fr, monkeypatch):
    """L'import écrit des centaines de milliers de lignes : hors de la boucle (to_thread), avec la
    transaction et drops.record depuis ce fil ; le résultat est visible depuis la boucle."""
    import threading

    threads = []
    original = fr._apply

    def spy(kept, now):
        threads.append(threading.current_thread() is threading.main_thread())
        original(kept, now)

    monkeypatch.setattr(fr, "_apply", spy)
    recent = (datetime.now(timezone.utc) - timedelta(days=10)).strftime("%d-%m-%Y")
    serve(fr, make_zip(csv_text([
        "navarro.fr;FR;;;;;PP;FR;;01-02-2010;",
        f"velo.fr;FR;;;;;PP;FR;;01-02-2010;{recent}",
    ])))
    asyncio.run(fr.import_file())
    assert threads == [False]
    assert fr.conn.execute("SELECT COUNT(*) FROM fr_names").fetchone()[0] == 2
    assert [r[0] for r in fr.conn.execute("SELECT name FROM drops")] == ["velo.fr"]


def test_yesterdays_anniversary_is_read_today(fr):
    # Import le 17 au matin : l'anniversaire du 16 se lit le 17 à midi, celui du 17 se lit le 18.
    now = datetime(2026, 10, 17, 6, tzinfo=timezone.utc)
    fr._apply({"velo": (date(2025, 10, 16), None), "auto": (date(2025, 10, 17), None)}, now)
    rows = dict(fr.conn.execute("SELECT label, next_check_at FROM fr_names").fetchall())
    assert rows == {"velo": "2026-10-17T12:00:00+00:00", "auto": "2026-10-18T12:00:00+00:00"}

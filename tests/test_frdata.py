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
    assert calls == ["www.data.gouv.fr"]


def test_unknown_columns_are_reported_not_hidden(fr):
    serve(fr, make_zip("colonne1;colonne2\nx;y\n"))
    status = asyncio.run(fr.import_file())
    assert "introuvable" in status["error"] and "colonne1" in status["error"]


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
    now = datetime.now(timezone.utc).replace(microsecond=0)
    for label in ("navarro", "soleil", "velo"):
        add_due(fr, label)
    expires = now + timedelta(days=200)
    fr.rdap = FakeRdap({
        "navarro.fr": LookupResult(found=True, info=DomainInfo(statuses=["active"], expires_at=expires)),
        "soleil.fr": LookupResult(found=True, info=DomainInfo(statuses=["redemption period"], last_changed_at=now)),
        "velo.fr": LookupResult(available=True),
    })
    assert asyncio.run(fr.sweep()) == 3
    rows = {r["label"]: r for r in fr.conn.execute("SELECT * FROM fr_names")}
    following = datetime.fromisoformat(rows["navarro"]["next_check_at"])
    assert following.date() == (expires + timedelta(days=1)).date()
    assert rows["soleil"]["next_check_at"] is None and rows["velo"]["next_check_at"] is None
    assert sorted(r["name"] for r in fr.conn.execute("SELECT name FROM drops")) == ["soleil.fr", "velo.fr"]
    assert asyncio.run(fr.sweep()) == 0


def test_requests_are_capped_per_run(fr):
    for i in range(10):
        add_due(fr, f"nom{i}")
    fr.rdap = FakeRdap({f"nom{i}.fr": LookupResult(available=True) for i in range(10)})
    assert asyncio.run(fr.sweep()) == frdata.SWEEP_BATCH == 3


def test_rate_limit_pauses_checks_for_a_day(fr):
    add_due(fr, "navarro")
    add_due(fr, "dupont")
    fr.rdap = FakeRdap({
        "dupont.fr": LookupResult(error="Limite de requêtes du registre atteinte (429)"),
        "navarro.fr": LookupResult(error="Limite de requêtes du registre atteinte (429)"),
    })
    assert asyncio.run(fr.sweep()) == 1
    assert fr.status()["paused_until"]
    assert asyncio.run(fr.sweep()) == 0
    # Rien n'a été reporté : les deux noms seront vérifiés à la reprise.
    assert fr.conn.execute("SELECT COUNT(*) FROM fr_names WHERE checked_at IS NULL").fetchone()[0] == 2


def test_disabled_fr_does_nothing(fr):
    fr.store.set("fr_enabled", "0")
    assert asyncio.run(fr.sweep()) == 0
    assert not fr.due(datetime.now())

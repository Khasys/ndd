import asyncio
import io
import json
import zipfile

import httpx
import pytest

from app import db, prenoms
from app.store import Store

# Petits fichiers fabriqués ici, aux deux formats connus de l'Insee : aucune donnée réelle.
OLD = ("sexe;preusuel;annais;nombre\n"
       "1;GABRIEL;2023;5000\n2;LÉA;2023;3000\n1;LEA;2023;10\n1;JEAN-PIERRE;2023;40\n"
       "1;_PRENOMS_RARES;2023;9000\n2;ZOÉ;XXXX;40\n2;ZOÉ;2023;2500\n"
       "1;GABRIEL;2022;4000\n2;LÉA;2022;4100\n1;GABRIEL;2021;3900\n1;GABRIEL;2020;3800\n1;GABRIEL;1990;100\n")
NEW = ("sexe,prenom,periode,valeur\n"
       "F,Louise,2024,4200\nM,Gabriel,2024,4800\nF,Jade,2024,4300\nM,Raphaël,2024,4000\n"
       "M,Xylophonix,2024,4100\nF,_PRENOMS_RARES,2024,8000\nF,Gabrielle,XXXX,12\nM,Arthur,2023,4500\n")
DATASETS = {"data": [
    {"title": "Fichier des prénoms", "slug": "fichier-des-prenoms", "organization": {"name": "Institut national de la statistique et des études économiques (Insee)"},
     "resources": [
         {"title": "Fichier France, édition 2025", "url": "https://insee.example/prenoms-2024-nat_csv.zip", "format": "zip", "last_modified": "2025-06-01T00:00:00"},
         {"title": "Fichier France, édition 2024", "url": "https://insee.example/prenoms-2023-nat_csv.zip", "format": "zip", "last_modified": "2024-06-01T00:00:00"},
         {"title": "Fichier départements", "url": "https://insee.example/prenoms-2024-dpt_csv.zip", "format": "zip", "last_modified": "2025-06-02T00:00:00"},
         {"title": "Documentation", "url": "https://insee.example/doc.pdf", "format": "pdf"},
     ]},
    {"title": "Prénoms des élus", "organization": {"name": "Une mairie"},
     "resources": [{"title": "France", "url": "https://autre.example/nat.csv", "format": "csv", "last_modified": "2026-01-01"}]},
]}


def zipped(text: str, name: str = "prenoms.csv", encoding: str = "utf-8") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        archive.writestr("lisezmoi.txt", "x")
        archive.writestr(name, text.encode(encoding))
    return buf.getvalue()


@pytest.fixture
def store(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    db.migrate(conn, tmp_path / "backups")
    yield Store(conn, {})
    conn.close()


def names_for(store, tmp_path, routes: dict, calls: list | None = None) -> prenoms.RecentNames:
    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(str(request.url))
        for prefix, response in routes.items():
            if str(request.url).startswith(prefix):
                return response() if callable(response) else response
        return httpx.Response(404)

    return prenoms.RecentNames(store, tmp_path,
                               http_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)))


def test_normalize_like_the_historic_list():
    assert prenoms.normalize("Léa") == "lea"
    assert prenoms.normalize("JEAN-PIERRE") == "jean-pierre"
    assert prenoms.normalize("Chloé") == "chloe" and prenoms.normalize("Maël") == "mael"
    assert prenoms.normalize("N'Golo") == "ngolo"
    assert prenoms.normalize("-Anne") is None and prenoms.normalize("A") is None


def test_old_format_merges_sexes_and_keeps_three_years():
    years, stats = prenoms.read_rows(iter(io.StringIO(OLD)))
    assert sorted(years) == [2021, 2022, 2023]
    assert years[2023] == {"gabriel": 5000, "lea": 3010, "jean-pierre": 40, "zoe": 2500}
    ranked = prenoms.ranking(years)
    assert ranked["2023"][:2] == [["gabriel", 5000], ["lea", 3010]]
    assert ranked["2022"][0] == ["lea", 4100]
    assert stats["columns"] == {"name": "preusuel", "year": "annais", "count": "nombre", "sex": "sexe"}


def test_new_format_and_rare_names():
    years, _ = prenoms.read_rows(iter(io.StringIO(NEW)))
    assert sorted(years[2024]) == ["gabriel", "jade", "louise", "raphael", "xylophonix"]
    assert "gabrielle" not in {n for y in years.values() for n in y}


def test_total_rows_are_not_counted_twice():
    text = "sexe;prenom;periode;valeur\n1;LINA;2024;10\n2;LINA;2024;20\n0;LINA;2024;30\n"
    years, _ = prenoms.read_rows(iter(io.StringIO(text)))
    assert years[2024] == {"lina": 30}


def test_unknown_columns_show_what_was_read():
    with pytest.raises(prenoms.PrenomsError, match="Colonnes lues : genre, nom, an, total"):
        prenoms.read_rows(iter(io.StringIO("genre;nom;an;total\n1;LEA;2024;3\n")))


def test_zip_and_csv_files(tmp_path):
    archive = tmp_path / "a.zip"
    archive.write_bytes(zipped(OLD, encoding="latin-1"))
    years, _ = prenoms.read_file(archive)
    assert years[2023]["lea"] == 3010 and years[2023]["zoe"] == 2500
    plain = tmp_path / "b.csv"
    plain.write_text(NEW, encoding="utf-8")
    assert prenoms.read_file(plain)[0][2024]["raphael"] == 4000


def test_pick_the_latest_national_file_of_insee():
    found = prenoms.pick_resource(DATASETS["data"])
    assert found["url"] == "https://insee.example/prenoms-2024-nat_csv.zip"
    assert prenoms.pick_resource([DATASETS["data"][1]]) is None


def test_refresh_finds_downloads_and_keeps_the_ranking(store, tmp_path):
    calls = []
    names = names_for(store, tmp_path, {
        "https://www.data.gouv.fr/": httpx.Response(200, json=DATASETS),
        "https://insee.example/prenoms-2024-nat_csv.zip": lambda: httpx.Response(200, content=zipped(NEW)),
    }, calls)
    status = asyncio.run(names.refresh())
    assert status["error"] is None and status["latest"] == 2024 and status["years"] == [2024, 2023]
    assert status["names"] == 5 and status["untracked"] == 1 and status["untracked_examples"] == ["xylophonix"]
    assert names.rank_of("gabriel") == (1, 4800) and names.rank_of("arthur") is None
    saved = json.loads((tmp_path / prenoms.FILE_NAME).read_text())
    assert saved["file_date"] == "2025-06-01T00:00:00" and saved["latest"] == 2024
    # Le fichier téléchargé ne reste pas sur le disque.
    assert not list(tmp_path.glob("prenoms_recents.download")) and not list(tmp_path.glob("*.tmp"))
    # Relu depuis le disque au redémarrage.
    again = prenoms.RecentNames(store, tmp_path)
    assert again.latest == 2024 and again.rank_of("jade") == (2, 4300)
    # Même édition le mois suivant : pas de nouveau téléchargement.
    calls.clear()
    asyncio.run(names.refresh())
    assert all("insee.example" not in c for c in calls)
    assert not names.due()


def test_refresh_errors_keep_the_previous_ranking(store, tmp_path):
    names = names_for(store, tmp_path, {
        "https://www.data.gouv.fr/": httpx.Response(200, json=DATASETS),
        "https://insee.example/": lambda: httpx.Response(200, content=zipped(NEW)),
    })
    asyncio.run(names.refresh())
    store.set("prenoms_url", "https://ailleurs.example/prenoms.csv")
    broken = names_for(store, tmp_path, {
        "https://ailleurs.example/": httpx.Response(200, content=b"genre;nom\nF;Lea\n"),
    })
    status = asyncio.run(broken.refresh())
    assert "Colonnes lues : genre, nom" in status["error"]
    assert status["latest"] == 2024 and broken.rank_of("louise") == (3, 4200)
    assert broken.due() is False  # nouvel essai le lendemain


def test_discovery_failure_says_what_to_do(store, tmp_path):
    names = names_for(store, tmp_path, {"https://www.data.gouv.fr/": httpx.Response(200, json={"data": []})})
    status = asyncio.run(names.refresh())
    assert "introuvable" in status["error"] and "Adresse du fichier" in status["error"]
    assert status["latest"] is None and names.due() is False

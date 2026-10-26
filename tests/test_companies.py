import asyncio
import io
import json
import zipfile
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from app import companies, db
from app.checker import utcnow
from app.store import Store


async def _inline(fn, *args):
    return fn(*args)


async def _no_wait(_seconds):
    return None


def result(siren, name, category, tranche, ca=None, sigle=None, commercial=None, enseignes=None, city="PARIS",
           nature="5599"):
    """Une entreprise comme l'API la rend (UniteLegaleResponse, minimal=true, include=siege,finances)."""
    return {"siren": siren, "nom_complet": f"{name} ({sigle})" if sigle else name, "nom_raison_sociale": name,
            "sigle": sigle, "categorie_entreprise": category, "tranche_effectif_salarie": tranche,
            "annee_tranche_effectif_salarie": "2023", "activite_principale": "64.19Z", "date_creation": "1864-05-04",
            "etat_administratif": "A", "nature_juridique": nature,
            "siege": {"siret": siren + "00011", "libelle_commune": city, "code_postal": "75009",
                      "nom_commercial": commercial, "liste_enseignes": enseignes},
            "finances": {"2022": {"ca": (ca or 0) // 2, "resultat_net": 1}, "2023": {"ca": ca, "resultat_net": 2}}
            if ca is not None else None}


@pytest.fixture
def conn(tmp_path):
    connection = db.connect(tmp_path / "t.db")
    db.migrate(connection, tmp_path / "b")
    yield connection
    connection.close()


def test_names_drop_legal_forms_and_accents():
    assert companies.labels_of("L'ORÉAL") == ["loreal", "l-oreal"]
    assert companies.labels_of("SOCIETE GENERALE") == ["societegenerale", "societe-generale"]
    assert companies.labels_of("ALL SUITES SAS") == ["allsuites", "all-suites"]
    assert companies.labels_of("SA BNP PARIBAS") == ["bnpparibas", "bnp-paribas"]
    assert companies.labels_of("Moët & Chandon") == ["moetetchandon", "moet-et-chandon"]
    assert companies.labels_of("SNCF") == ["sncf"] and companies.labels_of("SAS") == [] and companies.labels_of("") == []
    company = {"name": "SOCIETE GENERALE", "sigle": "SG", "commercial": ["Boursorama"], "extra": ["SG Bank"]}
    assert companies.names_of(company) == [("societegenerale", "raison sociale"), ("societe-generale", "raison sociale"),
                                           ("sg", "sigle"), ("boursorama", "nom commercial"), ("sgbank", "ajouté"),
                                           ("sg-bank", "ajouté")]


def test_first_word_of_a_subsidiary_when_it_is_a_brand():
    """STELLANTIS AUTO SAS : stellantis ; pas un mot courant (SOCIETE GENERALE, ELECTRICITE DE FRANCE), qui ferait
    croire à un trou, ni un nom d'un seul mot (déjà vérifié tel quel)."""
    assert companies.first_word("STELLANTIS AUTO SAS") == "stellantis"
    assert companies.first_word("SOCIETE GENERALE") is None and companies.first_word("ELECTRICITE DE FRANCE") is None
    assert companies.first_word("ORANGE") is None and companies.first_word("LA POSTE") is None
    names = companies.names_of({"name": "AIRBUS OPERATIONS", "sigle": None, "commercial": []})
    assert ("airbus", "raison sociale (1er mot)") in names


def test_parse_keeps_commercial_names_and_latest_revenue():
    found = companies.parse(result("552120222", "SOCIETE GENERALE", "GE", "53", ca=25_000_000_000, sigle="SG",
                                   commercial="SOCIETE GENERALE", enseignes=["BOURSORAMA", "Boursorama"]))
    assert found["commercial"] == ["BOURSORAMA"]  # la raison sociale et les doublons écartés
    assert found["revenue"] == 25_000_000_000 and found["revenue_year"] == "2023" and found["net_income"] == 2
    assert found["siret"] == "55212022200011" and found["city"] == "PARIS" and found["headcount"] == "53"
    assert companies.parse(result("1" * 9, "X", "PME", "11"))["revenue"] is None


def api(pages, calls=None, refuse_first=False):
    """L'API de recherche : pages[(catégorie, tranche)] = liste des résultats, 25 par page."""
    state = {"refused": not refuse_first}

    def handler(request):
        query = {k: v[0] for k, v in parse_qs(urlsplit(str(request.url)).query).items()}
        if calls is not None:
            calls.append(query)
        if not state["refused"]:
            state["refused"] = True
            return httpx.Response(429, headers={"Retry-After": "1"})
        if "q" in query:
            q = query["q"]
            hits = [r for rs in pages.values() for r in rs if q in (r["siren"], r["nom_raison_sociale"])]
            return httpx.Response(200, json={"results": hits, "total_results": len(hits), "page": 1, "per_page": 8,
                                             "total_pages": 1})
        found = pages.get((query["categorie_entreprise"], query.get("tranche_effectif_salarie")), [])
        page, per = int(query["page"]), int(query["per_page"])
        return httpx.Response(200, json={"results": found[(page - 1) * per:page * per], "total_results": len(found),
                                         "page": page, "per_page": per, "total_pages": -(-len(found) // per)})

    return lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_top_reads_units_by_their_own_headcount_until_enough():
    """GE et ETI, tranche d'effectif propre par tranche, la plus grande d'abord (la catégorie est celle du groupe :
    une petite filiale d'un grand groupe est « GE ») ; la dernière tranche lue en entier, rangée par chiffre
    d'affaires ; administrations et associations écartées ; 429 : on attend et on redemande."""
    t53 = [result(f"{n:09d}", f"GRANDE {n}", "GE", "53", ca=10_000 - n) for n in range(30)]  # deux pages
    t53.append(result("999999999", "VILLE DE PARIS", "GE", "53", nature="7210"))
    t52 = [result(f"{100 + n:09d}", f"MOYENNE {n}", "ETI" if n % 2 else "GE", "52", ca=n) for n in range(10)]
    t51 = [result(f"{200 + n:09d}", f"PETITE {n}", "ETI", "51") for n in range(5)]
    calls, waits = [], []

    async def sleep(seconds):
        waits.append(seconds)

    directory = companies.Directory(http_factory=api({("GE,ETI", "53"): t53, ("GE,ETI", "52"): t52, ("GE,ETI", "51"): t51},
                                                     calls, refuse_first=True), sleep=sleep)
    top = asyncio.run(directory.top(limit=35))
    assert [c["siren"] for c in top[:30]] == [f"{n:09d}" for n in range(30)]  # sans la Ville de Paris
    assert [c["name"] for c in top[30:]] == ["MOYENNE 9", "MOYENNE 8", "MOYENNE 7", "MOYENNE 6", "MOYENNE 5"]
    assert not any(q.get("tranche_effectif_salarie") == "51" for q in calls)  # assez après la tranche 52 : arrêt
    assert 1.0 in waits  # Retry-After respecté
    assert all(q["etat_administratif"] == "A" and q["minimal"] == "true" and q["include"] == "siege,finances"
               and q["categorie_entreprise"] == "GE,ETI" for q in calls if "categorie_entreprise" in q)
    assert companies.labels_of("NON DIFFUSIBLE") == [] and companies.labels_of("[NON-DIFFUSIBLE]") == []


def test_saved_top_lists_names_and_gaps(conn):
    now = utcnow()
    sg = companies.parse(result("552120222", "SOCIETE GENERALE", "GE", "53", ca=9, sigle="SG", enseignes=["BOURSORAMA"]))
    other = companies.parse(result("111111111", "ACME", "ETI", "52"))
    companies.save_top(conn, [sg, other], now)
    companies.save_checks(conn, [
        {"name": "societegenerale.fr", "label": "societegenerale", "tld": "fr", "state": "taken", "created_on": "1996-01-01",
         "expires_on": None, "registrar": None, "source": "afnic 202609", "checked_at": "x", "error": None},
        {"name": "societegenerale.com", "label": "societegenerale", "tld": "com", "state": "taken", "created_on": None,
         "expires_on": (now + timedelta(days=20)).date().isoformat(), "registrar": "CSC", "source": "rdap",
         "checked_at": "x", "error": None},
        {"name": "boursorama.fr", "label": "boursorama", "tld": "fr", "state": "free", "created_on": None,
         "expires_on": None, "registrar": None, "source": "afnic 202609", "checked_at": "x", "error": None}])
    found = companies.listing(conn)
    assert found["total"] == 2 and [i["siren"] for i in found["items"]] == ["552120222", "111111111"]
    sg_item = found["items"][0]
    assert sg_item["headcount_label"] == "10 000 salariés et plus" and sg_item["rank"] == 1
    assert sg_item["gaps"] == {"free": 1, "expiring": 1}
    assert {d["name"]: d["state"] for d in sg_item["domains"]}["boursorama.fr"] == "free"
    assert [i["siren"] for i in companies.listing(conn, gaps="free")["items"]] == ["552120222"]
    assert [i["siren"] for i in companies.listing(conn, q="bourso")["items"]] == ["552120222"]  # nom commercial
    assert [i["siren"] for i in companies.listing(conn, q="5521")["items"]] == ["552120222"]   # SIREN
    assert [i["siren"] for i in companies.listing(conn, q="55212022200011")["items"]] == ["552120222"]  # SIRET
    assert [s["siren"] for s in companies.suggest_local(conn, "soc")] == ["552120222"]
    # Hors du top à la relecture suivante : disparaît, avec ses noms ; une entreprise ajoutée à la main reste.
    companies.add(conn, companies.parse(result("222222222", "MA PME", "PME", "11")), now)
    companies.save_top(conn, [other], now)
    assert {r[0] for r in conn.execute("SELECT siren FROM companies")} == {"111111111", "222222222"}
    assert conn.execute("SELECT COUNT(*) FROM company_domains WHERE label LIKE 'societe%'").fetchone()[0] == 0
    assert companies.remove(conn, "222222222") and not companies.remove(conn, "111111111")
    assert companies.set_extra(conn, "111111111", ["Acme Rockets", "  ", "!!"])["extra"] == ["Acme Rockets"]
    assert ("acmerockets.com", ("acmerockets", "com")) in companies.wanted(conn).items()


def test_com_pass_writes_only_clear_answers(conn, tmp_path):
    """404 libre, 200 enregistré (création, échéance, registrar) ; refus et erreurs réseau : rien d'écrit, le
    nom repasse au passage suivant au lieu d'attendre 30 jours."""
    companies.save_top(conn, [companies.parse(result("333333333", "ALPHA BETA", "GE", "53"))], utcnow())
    expires = (datetime.now(timezone.utc) + timedelta(days=300)).strftime("%Y-%m-%dT%H:%M:%SZ")

    def handler(request):
        name = request.url.path.rsplit("/", 1)[1]
        if name == "alphabeta.com":
            return httpx.Response(200, json={"objectClassName": "domain", "status": ["active"],
                                             "events": [{"eventAction": "registration", "eventDate": "2001-02-03T00:00:00Z"},
                                                        {"eventAction": "expiration", "eventDate": expires}],
                                             "entities": [{"roles": ["registrar"], "vcardArray": ["vcard", [["fn", {}, "text", "CSC"]]]}]})
        if name == "alpha-beta.com":
            return httpx.Response(429, headers={"Retry-After": "5"})
        return httpx.Response(404)

    audit = companies.Audit(conn, Store(conn, {}), tmp_path, sleep=_no_wait,
                            http_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    stats = asyncio.run(audit.check_com(_inline))
    assert stats == {"asked": 2, "checked": 1, "failed": 1}
    row = conn.execute("SELECT * FROM company_domains WHERE name = 'alphabeta.com'").fetchone()
    assert row["state"] == "taken" and row["created_on"] == "2001-02-03" and row["expires_on"] == expires[:10]
    assert conn.execute("SELECT COUNT(*) FROM company_domains WHERE name = 'alpha-beta.com'").fetchone()[0] == 0
    assert companies.due_com(conn, utcnow()) == ["alpha-beta.com"]


class FakeFr:
    """FrWatcher réduit : l'adresse du fichier de l'Afnic et son client HTTP."""

    def __init__(self, rows):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("noms.csv", "Nom de domaine;Date de création;Date de retrait du WHOIS\n" + "".join(
                f"{name};{created};{removed}\n" for name, created, removed in rows))
        self.zip = buffer.getvalue()
        self._http_factory = lambda: httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, content=self.zip)))

    async def resource_url(self, http):
        return "https://example.test/afnic.zip", "202609"


def test_fr_names_come_from_the_afnic_file(conn, tmp_path):
    store = Store(conn, {})
    companies.save_top(conn, [companies.parse(result("444444444", "GAMMA", "GE", "53", enseignes=["DELTA OMEGA"]))], utcnow())
    audit = companies.Audit(conn, store, tmp_path)
    assert audit.fr_due("202609", utcnow())
    fr = FakeFr([("gamma.fr", "15-03-1999", ""), ("deltaomega.fr", "01-01-2005", "02-02-2020"), ("autre.fr", "01-01-2001", "")])
    assert asyncio.run(audit.check_fr(_inline, fr)) == {"names": 3, "taken": 1}
    states = {r["name"]: (r["state"], r["created_on"]) for r in conn.execute("SELECT * FROM company_domains")}
    assert states == {"gamma.fr": ("taken", "1999-03-15"), "deltaomega.fr": ("free", "2005-01-01"),
                      "delta-omega.fr": ("free", None)}
    assert not audit.fr_due("202609", utcnow())                        # lu à l'instant
    assert not audit.fr_due("202609", utcnow() + timedelta(hours=7))   # rien de nouveau
    assert audit.fr_due("202610", utcnow() + timedelta(hours=7))       # nouveau fichier de l'Afnic
    assert not (tmp_path / "companies-afnic.zip").exists()


from tests.test_app import client_factory, logged  # noqa: E402,F401  (fixtures)


def test_screen_routes(logged):
    """Liste, auto-complétion (le top puis l'Annuaire), ajout d'une entreprise hors du top, noms ajoutés, retrait."""
    from app import main

    client = logged()
    state = client.app.state
    companies.save_top(state.conn, [companies.parse(result("552120222", "SOCIETE GENERALE", "GE", "53", sigle="SG"))], utcnow())
    pme = result("987654321", "SOCIETE DES PETITS PAINS", "PME", "11")
    state.companies.directory = companies.Directory(http_factory=api({("PME", "11"): [pme]}), sleep=_no_wait)
    listed = client.get("/api/companies").json()
    assert listed["total"] == 1 and listed["items"][0]["domains"][0]["name"] == "societegenerale.fr"
    assert listed["status"]["companies"] == 1 and listed["status"]["names"] == 6  # 3 noms en .fr et .com
    found = client.get("/api/companies/suggest?q=SOCIETE DES PETITS PAINS").json()["items"]
    assert [(i["siren"], i["listed"]) for i in found] == [("987654321", False)]  # rien dans le top : l'Annuaire
    assert [i["siren"] for i in client.get("/api/companies/suggest?q=gener").json()["items"]] == ["552120222"]
    added = client.post("/api/companies/987654321").json()
    assert added["added_at"] and added["rank"] is None and added["headcount_label"] == "10 à 19 salariés"
    named = client.put("/api/companies/987654321/names", json={"names": ["Petits Pains"]}).json()
    assert "petitspains.com" in [d["name"] for d in named["domains"]]
    assert client.get("/api/companies/12345").status_code == 422
    assert client.get("/api/companies/111111111").status_code == 404
    assert client.delete("/api/companies/987654321").json() == {"removed": True}
    assert client.get("/api/diag").json()["companies"]["companies"] == 1
    assert main.READ_PATHS.match("/api/companies") and main.READ_PATHS.match("/api/companies/552120222")
    assert not main.READ_PATHS.match("/api/companies/suggest")
    client.__exit__(None, None, None)

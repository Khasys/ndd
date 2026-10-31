"""Ventes réelles (DNJournal) : lecture des rapports, comparables, panne de la source signalée tout de suite."""

import asyncio
import json
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import httpx

from app import ai_format, db, market, morning
from tests.test_explore import env  # noqa: F401  (fixture env)

REPORT = """<html><head><title>x</title><script>var a = "Highest Reported";</script></head><body>
<p>Highest Reported Domain Sales - Mon. Sep. 21 - Sun. Oct. 4, 2026</p>
<p>1. Velo.com $25,000 Sedo</p><p>2. jetsprives.fr €3,000 = $3,400 Sedo</p>
<p>Keep in mind</p>
<p>Additional Sedo .com Sales Between $2,000 - $4,999</p>
<p>privatejets.com $4,800</p><p>bonvelo.com $2,100</p>
</body></html>"""
ARCHIVE = '<a href="domainsales/2026/1007.htm">x</a><a href="domainsales/2026/0923.htm">y</a>'


class Store(dict):
    def get(self, key, default=""):
        return dict.get(self, key, default)

    def set(self, key, value):
        self[key] = value


def make(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    db.migrate(conn, tmp_path / "b")
    return market.Market(conn, Store())


def test_a_report_is_read_and_feeds_comparables(tmp_path):
    sales = market.parse(market.text_of(REPORT), "https://dnjournal.com/archive/domainsales/2026/1007.htm")
    assert {s["name"]: s["price"] for s in sales} == {"velo.com": 25000, "jetsprives.fr": 3400, "privatejets.com": 4800,
                                                      "bonvelo.com": 2100}
    assert {s["date"] for s in sales} == {"2026-10-04"}
    m = make(tmp_path)
    m.save("u", sales, datetime.now(timezone.utc))
    assert market.split_words("jetsprives") == ["jets", "prives"]
    found = [s["name"] for s in m.comparables("jetsprives.com", today=date(2026, 10, 20))]
    assert set(found[:2]) == {"privatejets.com", "jetsprives.fr"}  # mots en commun d'abord
    assert found.index("bonvelo.com") > 1  # même extension, aucun mot en commun
    assert market.median_price([{"price": 1000}, {"price": 4000}]) == 2000


def test_refresh_reads_new_reports_and_reports_a_broken_source(tmp_path, monkeypatch):
    monkeypatch.setattr(market, "PAUSE", 0)
    m = make(tmp_path)
    pages = {"domainsales-archive.htm": ARCHIVE, "domainsales/2026/1007.htm": REPORT, "domainsales/2026/0923.htm": REPORT}

    def handler(request):
        path = request.url.path.split("/archive/")[-1]
        return httpx.Response(200, text=pages[path]) if path in pages else httpx.Response(404)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await m.refresh(client)

    status = asyncio.run(run())
    assert status["sales"] == 4 and status["reports"] == 2 and status["error"] is None
    now = datetime.now(timezone.utc)
    assert m.problem(now) is None or "rapport" in m.problem(now)  # dernier rapport du 07/10/2026
    # Mise en page changée : rapport sans vente lisible, panne signalée (alerte Telegram immédiate).
    pages["domainsales-archive.htm"] += '<a href="domainsales/2026/1021.htm">z</a>'
    pages["domainsales/2026/1021.htm"] = "<p>rien</p>"
    status = asyncio.run(run())
    assert "aucune vente lue" in status["error"] and "aucune vente lue" in m.problem(now)
    assert "market" in morning.BLOCKING


def test_no_new_report_for_five_weeks_is_a_problem(tmp_path):
    m = make(tmp_path)
    now = datetime(2026, 12, 1, tzinfo=timezone.utc)
    m.store.set("market_status", '{"at": "2026-11-30T00:00:00+00:00", "ok_at": "2026-11-30T00:00:00+00:00", '
                                 '"error": null, "latest_report": "2026-10-07"}')
    assert "aucun nouveau rapport" in m.problem(now)
    m.store.set("market_status", '{"at": "2026-11-30T00:00:00+00:00", "ok_at": "2026-11-30T00:00:00+00:00", '
                                 '"error": null, "latest_report": "2026-11-25"}')
    assert m.problem(now) is None


def test_yield_decides_the_verdict():
    """Rendement : chance de vente réelle (1 à 2 % par an, plus si le nom est pris ailleurs et bon) × prix bas,
    commission et renouvellement ôtés."""
    assert ai_format.sell_rate(50, 0) == 0.014 and ai_format.sell_rate(50, 9) == 0.073
    assert ai_format.sell_rate(0, 0) == ai_format.SELL_RATE_MIN and ai_format.sell_rate(100, 4) == ai_format.SELL_RATE_MAX
    assert round(ai_format.median_years(0.01)) == 69
    grid = {"oral": 8, "radio": 8, "sens": 8, "marque": 5, "marche": 7, "ext": 9, "acheteurs": 6, "risque": 0}
    common = {"name": "motsimple.com", **grid, "memo": 4, "final_low": 500, "renewal": 13.0}
    assert ai_format.derive({**common, "taken": 0})["verdict"] == "À laisser"     # ne rapporte pas son renouvellement
    good = {**common, "final_low": 3000, "taken": 3}
    notes = ai_format.derive(good)
    assert notes["verdict"] == "À prendre" and notes["yield_"] >= 2 * 13
    assert "yield_" not in ai_format.derive({k: v for k, v in good.items() if k != "taken"})  # sans demande mesurée


def test_pending_offset_returns_the_next_slice(env):
    """Sessions en parallèle : chaque tranche de la file est demandée au NAS, seuls ses noms sont décrits."""
    from app import ai

    first = [n["name"] for n in ai.pending(env, 10)]
    second = [n["name"] for n in ai.pending(env, 5, offset=5)]
    assert second == first[5:10]


def test_comparables_stop_early_but_pick_what_a_full_sort_picks(tmp_path):
    """Des milliers de ventes de même forme en .com : la recherche s'arrête dès qu'aucune vente restante ne peut
    entrer dans les k meilleures, sans changer le choix d'un tri complet (notes égales départagées par le prix)."""
    import random

    random.seed(3)
    words = ["velo", "maison", "jardin", "credit", "voyage", "photo", "music", "sport", "garage", "piscine", "cuisine",
             "travel", "money", "house", "green", "smart", "auto", "bio", "zen", "pro"]
    sales, seen = [], set()
    while len(sales) < 1500:
        label = "".join(random.sample(words, random.randint(1, 3)))
        name = f"{label}.{random.choice(['com', 'com', 'com', 'fr', 'net'])}"
        if name not in seen:
            seen.add(name)
            day = date(2019, 1, 1) + timedelta(days=random.choice(range(0, 2800, 7)))  # dates communes : égalités
            sales.append({"name": name, "price": random.choice([2000, 2500, 3000, 5000]), "date": day.isoformat(),
                          "venue": "Sedo", "source": "test"})
    m = make(tmp_path)
    m.save("u", sales, datetime.now(timezone.utc))
    every, _, _ = m.index()

    def full_sort(name, k, today):
        label, _, tld = name.partition(".")
        mine = market.split_words(label)
        shared_words, lang, shape = market.keywords(mine), market.language(mine), min(len(mine), 3)
        scored = []
        for i, s in enumerate(every):
            shared = len(shared_words & s["stems"])
            if not shared and (s["tld"] != tld or min(len(s["words"]), 3) != shape):
                continue  # ni mot commun ni même forme : pas candidate
            score = 40 * shared + (25 if s["tld"] == tld else 10 if s["tld"] in market.GENERIC and tld in market.GENERIC else 0)
            score += 10 if min(len(s["words"]), 3) == shape else 0
            score += 15 if lang and s["lang"] == lang else 0
            score -= 3 * abs(len(s["label"]) - len(label))
            score -= 2 * max(0.0, (today - date.fromisoformat(s["date"])).days / 365)
            scored.append((score, -s["price"], i))
        scored.sort(reverse=True)
        return [every[i]["name"] for _, _, i in scored[:k]]

    queries = [s["name"] for s in sales[:150]] + ["velomaison.com", "zzqx.com", "photo.fr", "smartgreenhouse.net", "a.com"]
    for k in (1, 4, 12):
        for today in (date(2026, 10, 26), date(2018, 1, 1)):
            for name in queries:
                assert [s["name"] for s in m.comparables(name, k, today)] == full_sort(name, k, today), (name, k, today)


OLD_REPORT = """<p>The DN Journal Top 20 Highest Reported Domain Sales - Mon. May 25, 2020 - Sun. June 7 2020 ( Foreign currency
to U.S. Dollar Conversions Based on Rates in Effect June 10, 2020) Domain Sold For Where Sold</p>
<p>1. GreenStreet.com $100,890 Buckley Media</p><p>2. tie Sleep.com.au AUD 100,000 = $70,000 Drop.com.au</p>
<p>3. KPH.com $24,999 NameJet</p><p>Keep in mind</p>"""


def test_history_since_2015_reads_old_links_and_layouts():
    """Radar des vendus : rapports d'avant 2024 (« 20220817.htm », adresse complète en 2020), date sans virgule et
    montant en code de devise (« AUD 100,000 = $70,000 »)."""
    page = ('<a href="domainsales/2015/20150128.htm">a</a><a href="https://www.dnjournal.com/archive/domainsales/2020/'
            '20200617.htm">b</a><a href="domainsales/2024/0626.htm">c</a><a href="domainsales/2014/20141231.htm">d</a>')
    links = market.report_links(page)
    assert [market.Market._report_date(u) for u in links] == ["2024-06-26", "2020-06-17", "2015-01-28"]
    assert all(u.startswith(market.SITE + "domainsales/") for u in links)
    sales = market.parse(market.text_of(OLD_REPORT), links[1])
    assert {s["name"]: s["price"] for s in sales} == {"greenstreet.com": 100890, "sleep.com.au": 70000, "kph.com": 24999}
    assert {s["date"] for s in sales} == {"2020-06-07"}


def test_sold_names_become_a_list_with_their_best_sale(tmp_path):
    """Liste « Déjà vendus » : libellés du plus cher au moins cher, meilleure vente par libellé et par nom exact, et la
    raison montrée sur les cartes."""
    from app import wordlists

    wordlists.configure(tmp_path / "data")
    try:
        m = make(tmp_path)
        now = datetime.now(timezone.utc)
        m.save("u1", [{"name": "velo.com", "price": 25000, "date": "2019-07-07", "venue": "Sedo", "source": "u1"},
                      {"name": "velo.fr", "price": 3000, "date": "2021-03-07", "venue": "Sedo", "source": "u1"},
                      {"name": "bonsai.net", "price": 5000, "date": "2018-01-07", "venue": "Afternic", "source": "u1"},
                      {"name": "sleep.com.au", "price": 70000, "date": "2020-06-07", "venue": "Drop", "source": "u1"}], now)
        assert m.publish_sold() == 3 and wordlists.sold() == ("sleep", "velo", "bonsai")
        assert "sold" in wordlists.categories("bonsai") and wordlists.LABELS["sold"] == "Déjà vendus"
        assert m.label_sale("velo")["name"] == "velo.com" and m.name_sale("velo.fr")["price"] == 3000
        assert m.name_sale("velo.net") is None
        assert market.sale_reason(m.label_sale("velo"), "velo.fr") == "vendu 25 000 $ en .com (2019)"
        assert market.sale_reason(m.label_sale("velo"), "velo.com") == "vendu 25 000 $ en 2019"
        assert m.publish_sold() == 3 and not wordlists.set_sold(list(wordlists.sold()))  # inchangée : rien à refaire
        version = m.version
        m.save("u2", [{"name": "zen.com", "price": 9000, "date": "2022-01-02", "venue": "", "source": "u2"}], now)
        assert m.version == version  # pas à chaque rapport : une fois la liste publiée
        assert m.publish_sold() == 4 and m.version > version and m.label_sale("zen")["price"] == 9000
    finally:
        wordlists.configure(None)


def test_a_sold_name_shows_its_sale_first_in_the_explorer(env, tmp_path):
    from app import wordlists

    wordlists.configure(tmp_path / "data")
    try:
        m = market.Market(env.conn, Store())
        env.market = m
        m.save("u", [{"name": "jardin.com", "price": 12000, "date": "2019-07-07", "venue": "Sedo", "source": "u"}],
               datetime.now(timezone.utc))
        m.publish_sold()
        found = env.value_of(["jardin.fr", "maison.fr"])
        assert found["jardin.fr"]["reasons"][0] == "vendu 12 000 $ en .com (2019)"
        assert found["jardin.fr"]["sold"]["name"] == "jardin.com" and "sold" not in found["maison.fr"]
        assert "sold" in env.universe.categories("jardin")
    finally:
        wordlists.configure(None)


def test_a_longer_history_is_caught_up_at_the_next_run(tmp_path):
    """Historique allongé (FIRST_YEAR) : le passage suivant lit les années manquantes, sans attendre 20 h ; ensuite,
    le rythme normal revient."""
    m = make(tmp_path)
    now = datetime(2026, 10, 31, 15, 40, tzinfo=timezone.utc)
    m.store.set("market_status", '{"at": "2026-10-31T06:00:15+00:00", "ok_at": "2026-10-31T06:00:15+00:00", '
                                 '"error": null, "left": 0}')
    assert m.due(now)  # lu à 6 h avec l'historique depuis 2025 seulement
    m.store.set("market_status", '{"at": "2026-10-31T15:37:00+00:00", "ok_at": "2026-10-31T15:37:00+00:00", '
                                 f'"error": null, "left": 0, "first_year": {market.FIRST_YEAR}}}')
    assert not m.due(now)


def test_an_unreadable_old_report_is_skipped_not_blocking(tmp_path, monkeypatch):
    """Rattrapage : un ancien rapport sans vente lisible est passé (noté lu, compté dans skipped) et la suite est lue ;
    la liste des vendus est publiée. Le rapport le plus récent illisible reste une panne. Lecteur changé (PARSER) :
    les rapports passés sont relus."""
    monkeypatch.setattr(market, "PAUSE", 0)
    m = make(tmp_path)
    archive = ('<a href="domainsales/2026/1007.htm">x</a><a href="domainsales/2022/20221019.htm">y</a>'
               '<a href="domainsales/2018/20180711.htm">z</a>')
    pages = {"domainsales-archive.htm": archive, "domainsales/2026/1007.htm": REPORT,
             "domainsales/2022/20221019.htm": "<p>rien</p>", "domainsales/2018/20180711.htm": OLD_REPORT}
    published = []
    monkeypatch.setattr(m, "publish_sold", lambda: published.append(1) or 0)

    def handler(request):
        path = request.url.path.split("/archive/")[-1]
        return httpx.Response(200, text=pages[path]) if path in pages else httpx.Response(404)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await m.refresh(client)

    status = asyncio.run(run())
    assert status["error"] is None and status["reports"] == 3 and status["skipped"] == 1 and status["sales"] == 7
    assert published and m.due(datetime.now(timezone.utc)) is False
    # Lecteur changé : le rapport passé est relu (toujours illisible ici, toujours passé).
    m.store.set("market_status", json.dumps({**json.loads(m.store.get("market_status")), "parser": 1}))
    assert asyncio.run(run())["skipped"] == 1
    # Le rapport le plus récent illisible : panne, comme avant.
    pages["domainsales-archive.htm"] = '<a href="domainsales/2026/1021.htm">n</a>' + archive
    pages["domainsales/2026/1021.htm"] = "<p>rien</p>"
    assert "aucune vente lue" in asyncio.run(run())["error"]

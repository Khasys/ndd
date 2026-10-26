"""Ventes réelles (DNJournal) : lecture des rapports, comparables, panne de la source signalée tout de suite."""

import asyncio
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

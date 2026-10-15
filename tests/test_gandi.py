import asyncio

import httpx
import pytest

from app import db, drops, explore, frdata, gandi, states
from app.store import Store


def product(status, price):
    return {"currency": "EUR", "products": [{"process": "create", "status": status,
            "prices": [{"duration_unit": "y", "min_duration": 1, "price_after_taxes": price}]}]}


@pytest.fixture
def env(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    db.migrate(conn, tmp_path / "backups")
    store = Store(conn, {})
    store.set("fr_enabled", "1")
    watcher = drops.DropWatcher(conn, store, tmp_path, rdap=None, delays=states.load_delays())
    fr = frdata.FrWatcher(conn, store, tmp_path, rdap=None, drops_watcher=watcher)
    ex = explore.Explorer(conn, watcher, fr)
    conn.execute("INSERT INTO fr_names (label, created_on) VALUES ('zz', '2010-01-01')")
    answers = {"velo.fr": product("available", 2400.0), "chat.fr": product("available", 9.6),
               "pm.fr": product("unavailable", None)}
    seen = []

    def handler(request):
        seen.append(request.headers["Authorization"])
        name = request.url.params["name"]
        return httpx.Response(200, json=answers.get(name, product("available", 9.6)))

    prices = gandi.Gandi(conn, store, ex, lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    store.set("gandi_token", "secret")
    yield prices, ex, seen
    conn.close()


def test_premium_standard_and_refused(env):
    prices, ex, seen = env
    velo = asyncio.run(prices.check("velo.fr"))
    assert velo["available"] and velo["premium"] and velo["price"] == 2400.0 and velo["normal"] == 9.6
    assert not asyncio.run(prices.check("chat.fr"))["premium"]
    pm = asyncio.run(prices.check("pm.fr"))
    assert pm["available"] is False and not pm["premium"]
    assert seen[0] == "Bearer secret"
    # Le prix normal de l'extension est gardé : une seule requête de référence.
    assert len(seen) == 4

    names = lambda key: [i["name"] for i in ex.search("fr", status="", price=key, q="", limit=50)["items"]]
    assert names("premium") == ["velo.fr"] and names("std") == ["chat.fr"] and names("no") == ["pm.fr"]
    assert ex.fiche("velo.fr")["price"]["premium"]


def test_refused_token_is_reported(env):
    prices, ex, seen = env
    prices._http_factory = lambda: httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(401)))
    with pytest.raises(PermissionError):
        asyncio.run(prices.check("velo.fr"))
    ex.conn.execute("INSERT INTO drops (name, tld, label, kind, lists, state, removed_on) VALUES ('velo.fr', 'fr', 'velo', 'letters', '', ?, '2026-10-01')",
                    (states.AVAILABLE,))
    assert prices.pending(10) == ["velo.fr"]
    result = asyncio.run(prices.run())
    assert result["error"] == "jeton Gandi refusé" and prices.status()["error"] == "jeton Gandi refusé"

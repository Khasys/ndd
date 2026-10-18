import asyncio
import time
import json
from datetime import datetime, timedelta, timezone

import bcrypt
import pytest
from fastapi.testclient import TestClient

from app.main import app

PASSWORD = "motdepasse-de-test"


@pytest.fixture
def client_factory(tmp_path, monkeypatch):
    def make(**env):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        monkeypatch.setenv("UPDATE_REPOSITORY", "")
        for key in ("AUTH_USERNAME", "AUTH_PASSWORD_HASH", "SECRET_KEY"):
            monkeypatch.delenv(key, raising=False)
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        return TestClient(app, follow_redirects=False)

    return make


def test_setup_mode_locks_everything_but_setup(client_factory):
    with client_factory() as client:
        assert client.get("/").headers["location"] == "/setup"
        assert client.get("/login").headers["location"] == "/setup"
        assert client.get("/api/domains").status_code == 503
        assert client.get("/health").json()["status"] == "ok"
        page = client.get("/setup").text
        assert "AUTH_USERNAME et AUTH_PASSWORD_HASH" in page


def test_setup_generates_compose_ready_hash(client_factory):
    with client_factory() as client:
        assert "12 caractères" in client.post("/setup", data={"password": "court", "confirm": "court"}).text
        assert "diffèrent" in client.post("/setup", data={"password": PASSWORD, "confirm": PASSWORD + "x"}).text
        page = client.post("/setup", data={"password": PASSWORD, "confirm": PASSWORD}).text
    escaped = page.split('<pre class="hash">')[1].split("</pre>")[0]
    assert escaped.startswith("$$2b$$12$$")
    assert bcrypt.checkpw(PASSWORD.encode(), escaped.replace("$$", "$").encode())


def test_login_flow_with_generated_secret(client_factory, tmp_path):
    hashed = bcrypt.hashpw(PASSWORD.encode(), bcrypt.gensalt(4)).decode()
    with client_factory(AUTH_USERNAME="moi", AUTH_PASSWORD_HASH=hashed) as client:
        assert client.get("/setup").headers["location"] == "/login"
        assert client.get("/").headers["location"] == "/login"
        response = client.post("/login", data={"username": "moi", "password": PASSWORD})
        assert response.status_code == 303
        cookie = response.headers["set-cookie"].lower()
        assert "httponly" in cookie and "samesite=lax" in cookie
        assert client.get("/api/domains").json() == []
    secret = (tmp_path / "secret_key").read_text()
    assert len(secret) > 40


@pytest.mark.parametrize("wrap", [lambda h: h, lambda h: h.replace("$", "$$"), lambda h: f"'{h}'", lambda h: f' "{h}" '])
def test_login_tolerates_compose_escaping_and_quotes(client_factory, wrap):
    hashed = bcrypt.hashpw(PASSWORD.encode(), bcrypt.gensalt(4)).decode()
    with client_factory(AUTH_USERNAME=" Moi ", AUTH_PASSWORD_HASH=wrap(hashed)) as client:
        response = client.post("/login", data={"username": "moi ", "password": PASSWORD})
        assert response.status_code == 303


def test_login_failure_is_logged_with_reason(client_factory, caplog):
    hashed = bcrypt.hashpw(PASSWORD.encode(), bcrypt.gensalt(4)).decode()
    with client_factory(AUTH_USERNAME="moi", AUTH_PASSWORD_HASH=hashed) as client:
        caplog.set_level("INFO", logger="domain-watch")
        client.post("/login", data={"username": "admin", "password": PASSWORD})
        client.post("/login", data={"username": "moi", "password": "faux"})
    assert "identifiant 'admin' inconnu" in caplog.text
    assert "mot de passe incorrect" in caplog.text


def test_invalid_hash_is_reported_at_startup(client_factory, caplog):
    caplog.set_level("INFO", logger="domain-watch")
    with client_factory(AUTH_USERNAME="moi", AUTH_PASSWORD_HASH="pas-un-hash"):
        pass
    assert "n'est pas un hash bcrypt valide" in caplog.text


def test_meta_reports_newer_published_version(client_factory, monkeypatch):
    hashed = bcrypt.hashpw(PASSWORD.encode(), bcrypt.gensalt(4)).decode()
    monkeypatch.setattr("app.main.VERSION", "v0.2.2")
    with client_factory(AUTH_USERNAME="moi", AUTH_PASSWORD_HASH=hashed) as client:
        client.post("/login", data={"username": "moi", "password": PASSWORD})
        assert client.get("/api/meta").json()["update_available"] is None
        client.app.state.latest_version = "v0.3.0"
        assert client.get("/api/meta").json()["update_available"] == "v0.3.0"
        client.app.state.latest_version = "v0.2.2"
        assert client.get("/api/meta").json()["update_available"] is None


@pytest.fixture
def logged(client_factory):
    hashed = bcrypt.hashpw(PASSWORD.encode(), bcrypt.gensalt(4)).decode()

    def make():
        client = client_factory(AUTH_USERNAME="moi", AUTH_PASSWORD_HASH=hashed)
        client.__enter__()
        client.post("/login", data={"username": "moi", "password": PASSWORD})
        return client

    return make


def test_tags_are_normalized(logged):
    client = logged()
    conn = client.app.state.conn
    domain_id = conn.execute("INSERT INTO domains (name, added_at) VALUES ('a-test.fr', 'x')").lastrowid
    data = client.put(f"/api/domains/{domain_id}/tags", json={"tags": [" Projet  X ", "projet x", "", "achat"]}).json()
    assert data["tags"] == ["projet x", "achat"]
    client.__exit__(None, None, None)


def test_calendar_needs_secret_link_but_no_session(logged):
    client = logged()
    url = client.post("/api/settings/calendar").json()["calendar_url"]
    path = url.split("://", 1)[1].split("/", 1)[1]
    client.cookies.clear()
    response = client.get("/" + path)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/calendar")
    assert client.get("/calendar/mauvais.ics").status_code == 404
    client.__exit__(None, None, None)


def test_settings_without_telegram(logged):
    client = logged()
    cfg = client.get("/api/settings").json()
    assert cfg["telegram"] == {"token_set": False, "bot_name": None, "chat_set": False, "pairing": False}
    assert cfg["digest"]["enabled"] is True and cfg["digest"]["day"] == 0
    assert client.post("/api/settings/telegram/pair").status_code == 409
    assert client.put("/api/settings/digest", json={"enabled": True, "day": 9, "hour": 9}).status_code == 422
    assert client.put("/api/settings/digest", json={"enabled": False, "day": 4, "hour": 18}).json()["digest"]["day"] == 4
    client.__exit__(None, None, None)


def test_version_check_is_separate_from_check_all(logged, monkeypatch):
    client = logged()
    calls = []

    async def fake():
        calls.append(1)
        client.app.state.latest_version = "v9.9.9"

    client.app.state.refresh_latest = fake
    monkeypatch.setattr("app.main.VERSION", "v1.0.0")
    assert client.post("/api/check-all").status_code == 202
    assert calls == []
    data = client.post("/api/version/check").json()
    assert calls == [1]
    assert data["update_available"] == "v9.9.9" and data["latest_version"] == "v9.9.9"
    # La vérification manuelle repousse la prochaine automatique à 24 h.
    next_check = datetime.fromisoformat(data["version_next_check"])
    assert timedelta(hours=23, minutes=59) < next_check - datetime.now(timezone.utc) <= timedelta(hours=24)
    client.__exit__(None, None, None)


def test_pwa_files_are_public(client_factory):
    hashed = bcrypt.hashpw(PASSWORD.encode(), bcrypt.gensalt(4)).decode()
    with client_factory(AUTH_USERNAME="moi", AUTH_PASSWORD_HASH=hashed) as client:
        manifest = client.get("/manifest.webmanifest")
        assert manifest.status_code == 200
        assert manifest.json()["display"] == "standalone" and manifest.json()["name"] == "domain-watch"
        worker = client.get("/sw.js")
        assert worker.status_code == 200 and "{{VERSION}}" not in worker.text
        assert client.get("/offline").status_code == 200
        assert client.get("/static/icons/icon-192.png").status_code == 200
        assert client.get("/api/domains").status_code == 401


def test_session_is_renewed_on_each_visit(logged):
    client = logged()
    response = client.get("/")
    assert response.status_code == 200 and "dw_session" in response.headers.get("set-cookie", "")
    client.__exit__(None, None, None)


def test_logo_height_is_bounded_and_rendered(logged):
    client = logged()
    cfg = client.put("/api/settings/appearance", json={"app_name": "x", "theme": "system", "logo_height": 64}).json()
    assert cfg["appearance"]["logo_height"] == 64
    assert 'style="--logo-h: 64px"' in client.get("/").text
    cfg = client.put("/api/settings/appearance", json={"app_name": "x", "theme": "system", "logo_height": 999}).json()
    assert cfg["appearance"]["logo_height"] == 120
    # Sans le champ, la taille enregistrée est gardée.
    cfg = client.put("/api/settings/appearance", json={"app_name": "y", "theme": "system"}).json()
    assert cfg["appearance"]["logo_height"] == 120
    client.__exit__(None, None, None)


def test_appearance_is_rendered_server_side(logged):
    client = logged()
    page = client.get("/").text
    assert '<html lang="fr" style="--logo-h: 32px">' in page and '<span class="appname">domain-watch</span>' in page
    cfg = client.put("/api/settings/appearance", json={"app_name": "  Mes   domaines ", "theme": "dark"}).json()
    assert cfg["appearance"] == {"app_name": "Mes domaines", "theme": "dark", "logo": False, "logo_height": 32}
    page = client.get("/").text
    assert '<html lang="fr" data-theme="dark" style="--logo-h: 32px">' in page
    assert '<meta name="color-scheme" content="dark">' in page
    assert "<title>Mes domaines</title>" in page
    assert client.put("/api/settings/appearance", json={"app_name": "x", "theme": "rose"}).status_code == 422
    client.cookies.clear()
    assert 'data-theme="dark"' in client.get("/login").text
    client.__exit__(None, None, None)


def test_logo_upload_limits_and_csp(logged):
    client = logged()
    svg = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'
    assert client.post("/api/settings/logo", files={"file": ("l.gif", b"GIF89a", "image/gif")}).status_code == 422
    assert client.post("/api/settings/logo", files={"file": ("l.png", b"x" * (300 * 1024 + 1), "image/png")}).status_code == 422
    assert client.post("/api/settings/logo", files={"file": ("l.svg", svg, "image/svg+xml")}).json()["appearance"]["logo"]
    response = client.get("/logo")
    assert response.headers["content-type"].startswith("image/svg+xml")
    assert "default-src 'none'" in response.headers["content-security-policy"]
    assert '<img class="logo" src="/logo?v=' in client.get("/").text
    assert client.delete("/api/settings/logo").json()["appearance"]["logo"] is False
    assert client.get("/logo").status_code == 404
    client.__exit__(None, None, None)


def test_czds_settings_never_return_the_password(logged):
    import httpx as _httpx

    client = logged()
    watcher = client.app.state.drops

    def handler(request):
        if request.url.host == "account-api.icann.org":
            ok = json.loads(request.content)["password"] == "bon-secret"
            return _httpx.Response(200 if ok else 401, json={"accessToken": "t"})
        return _httpx.Response(200, json=["https://czds-download-api.icann.org/czds/downloads/com.zone",
                                          "https://czds-download-api.icann.org/czds/downloads/net.zone"])

    watcher._http_factory = lambda: _httpx.AsyncClient(transport=_httpx.MockTransport(handler))
    refused = client.put("/api/settings/czds", json={"username": "moi@example.com", "password": "faux"})
    assert refused.status_code == 422 and "refusé" in refused.json()["detail"]
    assert client.get("/api/settings").json()["czds"]["password_set"] is False

    cfg = client.put("/api/settings/czds", json={"username": "moi@example.com", "password": "bon-secret"}).json()
    assert cfg["approved"] == ["com", "net"] and cfg["enabled"] == ["com", "net"] and cfg["password_set"]
    assert "bon-secret" not in client.get("/api/settings").text

    cfg = client.put("/api/settings/czds/zones", json={"enabled": ["net", "org"], "hour": 7}).json()
    assert cfg["enabled"] == ["net"] and cfg["hour"] == 7

    data = client.get("/api/drops").json()
    assert data["configured"] and data["items"] == [] and [z["tld"] for z in data["zones"]] == ["net"]
    assert client.delete("/api/settings/czds").status_code == 204
    assert client.get("/api/drops").json()["configured"] is False
    assert client.post("/api/drops/scan").status_code == 409
    client.__exit__(None, None, None)


def test_drops_list_marks_watched_names(logged):
    client = logged()
    conn = client.app.state.conn
    conn.execute("INSERT INTO domains (name, added_at) VALUES ('abcd.com', '2026-10-01T00:00:00+00:00')")
    conn.execute(
        "INSERT INTO drops (name, tld, label, kind, removed_on, state, release_at) VALUES "
        "('abcd.com', 'com', 'abcd', 'letters', '2026-10-01', 'redemption', '2026-11-05T12:00:00+00:00'),"
        "('wx12.net', 'net', 'wx12', 'mixed', '2026-10-01', 'available', NULL),"
        "('zz99.com', 'com', 'zz99', 'mixed', '2026-10-13', NULL, NULL),"
        "('old1.com', 'com', 'old1', 'mixed', '2026-01-01', 'available', NULL)"
    )
    recent = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
    conn.execute("UPDATE drops SET available_since = ? WHERE name = 'wx12.net'", (recent,))
    conn.execute("UPDATE drops SET available_since = '2026-01-02T00:00:00+00:00' WHERE name = 'old1.com'")
    data = client.get("/api/drops").json()
    # Libre depuis plus que la durée d'affichage : masqué, mais toujours en base.
    assert {i["name"]: i["watched"] for i in data["items"]} == {"abcd.com": True, "wx12.net": False}
    assert data["pending_checks"] == 1
    client.__exit__(None, None, None)


def test_explorer_and_drops_settings(logged):
    client = logged()
    conn = client.app.state.conn
    assert client.get("/api/explore").status_code == 404
    conn.execute("INSERT INTO fr_names (label, created_on) VALUES ('navarro', '2010-02-01')")
    data = client.get("/api/explore", params={"q": "navarro", "mode": "exact"}).json()
    assert data["tld"] == "fr" and data["items"][0]["status_label"] == "Enregistré"
    assert data["counts"]["registered"] == 1
    assert client.get("/api/explore", params={"mode": "nimporte"}).status_code == 422
    card = client.get("/api/explore/name", params={"label": "Navarro.fr"}).json()
    assert card["known"] and card["items"][0]["name"] == "navarro.fr"
    ranked = client.get("/api/explore/rank", params={"cat": "surname", "limit": 5}).json()
    assert ranked["items"] and ranked["counts"]["available"] > 0 and "Mots" in ranked["kinds"].values()
    assert client.get("/api/explore/map").json()["tlds"] == ["fr"]
    assert client.get("/api/explore/rank", params={"status": "nimporte"}).status_code == 422
    assert client.put("/api/settings/seo", json={"key": "abc"}).json() == {"key_set": True}
    assert client.get("/api/settings").json()["seo"] == {"key_set": True}
    assert client.post("/api/explore/seo", json={"names": []}).status_code == 422
    csv = client.get("/api/explore/export", params={"status": "registered"})
    assert csv.headers["content-type"].startswith("text/csv") and "navarro.fr;Enregistré" in csv.text

    # Plusieurs extensions : listes fusionnées, compteurs additionnés.
    from app import drops as drops_mod
    state = client.app.state
    state.store.set("czds_approved", '["com"]')
    bits = bytearray(drops_mod.BITMAP_BYTES)
    state.drops._save("com", bits, {"navarro"})
    state.explorer.invalidate()
    one = client.get("/api/explore/rank", params={"tld": "fr", "cat": "surname", "status": "registered"}).json()
    both = client.get("/api/explore/rank", params={"tld": "com,fr", "cat": "surname", "status": "registered"}).json()
    assert both["tld"] == "fr,com" and both["counts"]["registered"] == one["counts"]["registered"] + 1
    assert {i["name"] for i in both["items"]} == {"navarro.fr", "navarro.com"}
    scores = [i["score"] for i in client.get("/api/explore/rank", params={"tld": "fr,com", "limit": 30}).json()["items"]]
    assert scores == sorted(scores, reverse=True)
    # Extension inconnue (zone retirée, vue ancienne) : ignorée, l'écran reste utilisable.
    assert client.get("/api/explore/rank", params={"tld": "fr,xyz"}).json()["tld"] == "fr"
    assert client.get("/api/explore/rank", params={"tld": "xyz"}).status_code == 404
    assert client.get("/api/explore/rank", params={"tld": "FR"}).json()["tld"] == "fr"
    # Cas qui figeaient le serveur ou renvoyaient 500.
    assert client.get("/api/explore/rank", params={"limit": "abc"}).status_code == 422
    assert client.get("/api/explore", params={"limit": "0"}).status_code == 422
    assert client.get("/api/explore", params={"cat": "zzz"}).status_code == 422
    assert client.get("/api/explore", params={"tld": "fr", "taken": "fr"}).json()["items"]
    assert client.get("/api/explore", params={"tld": "fr", "taken": "any", "cat": "surname"}).json()["counts"]["registered"] >= 0
    for sort in ("freq", "length"):
        merged = client.get("/api/explore/rank", params={"tld": "fr,com", "cat": "surname", "sort": sort, "limit": 40}).json()["items"]
        alone = client.get("/api/explore/rank", params={"tld": "fr", "cat": "surname", "sort": sort, "limit": 40}).json()["items"]
        assert [i["label"] for i in merged if i["tld"] == "fr"] == [i["label"] for i in alone][:len([i for i in merged if i["tld"] == "fr"])]
    csv = client.get("/api/explore/export", params={"tld": "fr,com", "status": "registered", "cat": "surname"}).text
    assert "navarro.fr" in csv and "navarro.com" in csv
    # Avec le tri de l'écran : même ordre en tête du fichier, score et avis IA dans les colonnes.
    state.conn.execute("""INSERT INTO domain_ai (name, exploit, resale, quick_low, quick_high, final_low, final_high, comment,
                          rated_at, version) VALUES ('navarro.com', 60, 70, 100, 300, 500, 2000,
                          'À prendre. Nom porté. Acheteur type : agence.', 'x', 5)""")
    state.explorer.ai_changed()
    lines = client.get("/api/explore/export", params={"tld": "fr,com", "status": "registered", "cat": "surname",
                                                      "sort": "resale"}).text.splitlines()
    assert lines[0].lstrip("﻿").startswith("nom;etat;score;verdict;note_revente;note_exploitation;prix_final_bas")
    assert lines[1].startswith("navarro.com;Enregistré;") and ";À prendre;70;60;500;2000;" in lines[1]
    assert client.get("/api/explore/export", params={"sort": "nimporte"}).status_code == 422

    assert client.put("/api/settings/drops", json={"keep_days": 0}).status_code == 422
    assert client.put("/api/settings/drops", json={"keep_days": 45}).json() == {"keep_days": 45, "min_score": 20}
    assert client.put("/api/settings/fr", json={"enabled": False, "url": "https://x/y", "rate": 50}).status_code == 422
    assert client.app.state.store.get("fr_url") != "https://x/y"  # rien d'écrit sur une erreur
    assert client.put("/api/settings/weights", json={"values": {"shape": "NaN"}}).status_code == 422
    assert client.put("/api/settings/fr", json={"enabled": True, "rate": 5}).json()["rate"] == 5
    settings = client.get("/api/settings").json()
    assert settings["drops"]["keep_days"] == 45 and settings["fr"]["rate"] == 5
    assert client.post("/api/drops/check", json={"name": "pas un nom"}).status_code == 422
    client.__exit__(None, None, None)


def test_views_weights_order_url(logged):
    client = logged()
    views = client.put("/api/explore/views", json={"views": [{"name": "Prénoms top", "filters": {"cat": "firstname", "rank": 500}}, {"name": ""}]}).json()
    assert views == [{"name": "Prénoms top", "filters": {"cat": "firstname", "rank": "500"}, "alert": False}]
    assert client.get("/api/explore/views").json() == views
    too_many = {"name": "Trop", "filters": {f"f{i}": "1" for i in range(41)}}
    assert client.put("/api/explore/views", json={"views": [too_many]}).status_code == 422
    weights = client.put("/api/settings/weights", json={"values": {"shape": 5, "seo": 0}}).json()["values"]
    assert weights["shape"] == 2 and weights["seo"] == 0 and weights["demand"] == 1
    assert "gandi" in client.get("/api/settings").json()["order_url"]
    assert client.put("/api/settings/order-url", json={"url": "https://exemple.fr/"}).status_code == 422
    assert client.put("/api/settings/order-url", json={"url": "https://exemple.fr/?d={domain}"}).json()["order_url"].endswith("{domain}")
    assert client.get("/api/settings").json()["order_url"] == "https://exemple.fr/?d={domain}"
    client.__exit__(None, None, None)


def test_weights_and_seo_refresh_the_explorer_at_once(logged):
    client = logged()
    conn = client.app.state.conn
    conn.execute("INSERT INTO fr_names (label, created_on, removed_on) VALUES ('velo', '2010-01-01', '2025-01-01')")
    first = client.get("/api/explore/rank", params={"q": "velo", "mode": "exact"}).json()["items"][0]["score"]
    client.put("/api/settings/weights", json={"values": {"shape": 2}})
    second = client.get("/api/explore/rank", params={"q": "velo", "mode": "exact"}).json()["items"][0]["score"]
    assert second > first  # pas une minute d'attente
    client.__exit__(None, None, None)


def test_fiche_is_the_same_wherever_the_name_comes_from(logged):
    client = logged()
    conn = client.app.state.conn
    conn.execute("INSERT INTO fr_names (label, created_on, removed_on) VALUES ('velo', '2010-01-01', '2025-01-01')")
    conn.execute("INSERT INTO domains (name, added_at, state, registrar, nameservers, epp_statuses, last_checked_at, last_success_at) "
                 "VALUES ('velo.fr', '2026-10-01T00:00:00+00:00', 'available', NULL, '[]', '[]', '2026-10-14T00:00:00+00:00', '2026-10-14T00:00:00+00:00')")
    fiche = client.get("/api/explore/fiche", params={"name": "velo.fr"}).json()
    assert fiche["item"]["status_label"] == "Supprimé, non repris" and fiche["item"]["score"] > 0
    assert fiche["domain_id"] and fiche["rdap"]["available"] is True and "word" in fiche["categories"]
    assert client.get("/api/explore/fiche", params={"name": "pas un nom"}).status_code == 422
    # Dernière vérification en erreur : la fiche n'invente pas un état à partir d'elle.
    conn.execute("UPDATE domains SET check_error = 'délai dépassé', last_success_at = NULL WHERE name = 'velo.fr'")
    assert client.get("/api/explore/fiche", params={"name": "velo.fr"}).json()["rdap"] is None
    client.__exit__(None, None, None)


def test_ai_opinions_round_trip_with_a_token(logged):
    client = logged()
    conn = client.app.state.conn
    conn.execute("INSERT INTO fr_names (label, created_on, removed_on) VALUES ('velo', '2010-01-01', '2025-01-01')")
    conn.execute("INSERT INTO fr_names (label, created_on) VALUES ('mot', '2010-01-01')")
    token = client.post("/api/settings/ai-token").json()["token"]
    assert token.startswith("dw_") and client.app.state.store.get("ai_token_hash") != token
    # Un nom libre vérifié passe avant tous les autres.
    now = "2026-10-01T00:00:00+00:00"
    client.app.state.drops.record("fr", ["ciel", "mai"], [], datetime.fromisoformat(now))
    conn.execute("UPDATE drops SET state = 'available', available_since = ? WHERE name IN ('ciel.fr', 'mai.fr')", (now,))
    client.app.state.explorer.touch("ciel.fr")
    client.app.state.explorer.touch("mai.fr")
    pending = client.get("/api/ai/pending", params={"limit": 20}).json()
    assert pending["version"] == 5 and "revente rapide" in pending["guide"] and len(pending["names"]) == 20
    assert "oral" in pending["format"]["required"] and "radio (entendu une fois" in pending["guide"]
    assert pending["format"]["required"][0] == "name" and pending["format"]["additionalProperties"] is False
    # Les deux libres vérifiés d'abord (entre eux, le meilleur score ; ici presque à égalité).
    assert {n["name"] for n in pending["names"][:2]} == {"mai.fr", "ciel.fr"}
    first = next(n for n in pending["names"] if n["name"] == "mai.fr")
    assert first["status"] == "Libre (vérifié)"
    assert {"categories", "ranks", "taken_elsewhere", "base"} <= set(first)
    many = client.get("/api/ai/pending", params={"limit": 5000}).json()["names"]
    # Les formats que les registres réservent (1 ou 2 caractères jamais vus) ne sont pas notés.
    assert not [n for n in many if len(n["name"].split(".")[0]) <= 2 and n["status"].startswith(("Jamais", "Absent"))]
    velo = next(n for n in many if n["name"] == "velo.fr")
    assert velo["fr_history"]["removed_on"] == "2025-01-01"

    # Hors session : seul le jeton ouvre l'échange, et rien d'autre.
    client.cookies.clear()
    assert client.get("/api/ai/pending").status_code == 401
    assert client.get("/api/ai/pending", headers={"Authorization": "Bearer faux"}).status_code == 401
    headers = {"Authorization": f"Bearer {token}"}
    assert client.get("/api/ai/pending", headers=headers, params={"limit": 5}).status_code == 200
    # Lecture seule : écrans et diagnostic oui ; réglages, écritures et secrets jamais.
    assert client.get("/api/explore", headers=headers).status_code == 200
    diag = client.get("/api/diag", headers=headers).json()
    assert diag["lists"]["firstname"] > 4000 and "recent_errors" in diag and "memory_mb" in diag
    assert token not in json.dumps(diag) and "AUTH_" not in json.dumps(diag)
    assert client.get("/api/settings", headers=headers).status_code == 401
    assert client.post("/api/domains", headers=headers, json={"name": "exemple.fr"}).status_code == 401
    assert client.post("/api/explore/check", headers=headers, json={"name": "velo.fr"}).status_code == 401
    assert client.get("/api/explore", headers={"Authorization": "Bearer faux"}).status_code == 401
    good = {"name": "velo.fr", "exploit": 90, "resale": 90, "quick_low": 2000, "quick_high": 5000,
            "final_low": 10000, "final_high": 30000,
            "comment": "À prendre. Mot que tout le monde tape, rare en 4 lettres. Acheteur type : enseigne vélo."}
    sent = client.post("/api/ai/results", headers=headers, json={"results": [
        good,
        {**good, "exploit": 120},
        {**good, "exploit": 87},
        {**good, "exploit": "90"},
        {**good, "name": "pasunnomconnu.fr"},
        {**good, "name": "velo.xyz"},
        {**good, "quick_high": 1234},
        {**good, "name": "mot.fr", "final_low": 30000, "final_high": 10000},
        {**good, "name": "mot.fr", "quick_high": 50000},
        {**good, "note": "en trop"},
        {**good, "comment": "Très bon nom, à acheter."},
        {**good, "comment": "À prendre. Mot courant — commercial. Acheteur type : enseigne."},
        {**good, "exploit": 40, "resale": 40, "name": "mot.fr"},
        {**good, "comment": "À laisser. Mot courant sans intérêt réel. Acheteur type : personne."},
    ]}).json()
    assert sent["saved"] == 1
    reasons = [r["reason"] for r in sent["rejected"]]
    assert reasons[:8] == ["notes : de 0 à 100, multiples de 5", "notes : de 0 à 100, multiples de 5",
                           "notes et prix : entiers attendus", "nom inconnu", "nom inconnu", "prix hors de l'échelle",
                           "fourchette inversée", "revente rapide plus chère que l'utilisateur final"]
    assert reasons[8].startswith("clés attendues") and "en trop note" in reasons[8]
    assert reasons[9].startswith("commentaire : « À prendre") and reasons[10] == "commentaire : ni emoji ni tiret long"
    assert reasons[11] == "« À prendre » demande une note d'au moins 60"
    assert reasons[12] == "« À laisser » demande des notes de 45 au plus"
    # Grille v5 : sous-notes ; notes et verdict calculés par le NAS, prix dans le palier de la note.
    v5 = {"name": "velo.fr", "oral": 10, "radio": 10, "memo": 10, "sens": 10, "marque": 9, "marche": 8, "ext": 10,
          "acheteurs": 9, "risque": 0, "quick_low": 3000, "quick_high": 8000, "final_low": 15000, "final_high": 50000,
          "comment": "Mot que tout le monde tape, rare en 4 lettres. Acheteur type : enseigne vélo."}
    sent = client.post("/api/ai/results", headers=headers, json={"results": [
        v5, {**v5, "name": "mot.fr", "quick_low": 0, "quick_high": 0, "final_low": 50, "final_high": 100}, {**v5, "name": "mot.fr", "comment": "À prendre. Bon. Acheteur type : x."}]}).json()
    assert sent["saved"] == 1
    assert sent["rejected"][0]["reason"].startswith("prix utilisateur final hors palier")
    assert sent["rejected"][1]["reason"].startswith("commentaire")
    row = client.app.state.conn.execute("SELECT * FROM domain_ai WHERE name = 'velo.fr'").fetchone()
    assert row["version"] == 5 and row["exploit"] == 95 and row["resale"] == 90 and row["comment"].startswith("À prendre. Mot")
    assert json.loads(row["grid"])["oral"] == 10 and row["reviewed_at"] is None
    assert row["status_at"] == 2  # supprimé non repris au moment de l'avis : la mesure a posteriori s'en sert
    # Second passage : le même nom renoté en v5 est marqué revu.
    client.post("/api/ai/results", headers=headers, json={"results": [v5]})
    assert client.app.state.conn.execute("SELECT reviewed_at FROM domain_ai WHERE name = 'velo.fr'").fetchone()[0]
    client.__exit__(None, None, None)

    client = logged()
    fiche = client.get("/api/explore/fiche", params={"name": "velo.fr"}).json()
    assert fiche["ai"]["quick_high"] == 8000 and fiche["ai"]["final_high"] == 50000 and fiche["ai"]["grid"]["oral"] == 10
    ranked = client.get("/api/explore/rank", params={"sort": "ai", "status": ""}).json()["items"]
    assert ranked and ranked[0]["name"] == "velo.fr" and ranked[0]["ai"]["exploit"] == 95
    assert len(ranked) == 50  # un tri ne filtre pas : les noms sans avis suivent, par score
    names = [n["name"] for n in client.get("/api/ai/pending", params={"limit": 5000}).json()["names"]]
    assert "velo.fr" not in names
    # Filtres sur l'avis : verdict, bornes de chaque note et de chaque prix ; compteurs cohérents.
    def rated(**params):
        data = client.get("/api/explore/rank", params={"status": "", **params}).json()
        return [i["name"] for i in data["items"]], sum(data["counts"][k] for k in ("available", "registered", "suspended"))
    assert rated(ai_v="take")[0] == ["velo.fr"]
    assert rated(ai_v="take", ai_quick_high_min="5000", ai_final_high_max="50000", sort="freq")[0] == ["velo.fr"]
    assert rated(ai_v="skip")[0] == [] and rated(ai_exploit_max="50")[0] == []
    assert rated(ai_quick_low_min="5000")[0] == [] and rated(ai_resale_min="90", sort="length")[0] == ["velo.fr"]
    assert client.get("/api/explore/rank", params={"ai_v": "peut-etre"}).status_code == 422
    assert client.get("/api/explore/rank", params={"ai_exploit_min": "beaucoup"}).status_code == 422
    status = client.get("/api/ai/status").json()
    assert status["rated"] == 1 and status["token_set"] and status["pending"] > 0
    client.__exit__(None, None, None)


def test_exchange_script_checks_the_format_before_sending(tmp_path):
    import subprocess
    import sys
    lot = tmp_path / "lot.json"
    lot.write_text(json.dumps({"names": [{"name": "velo.fr"}, {"name": "mai.fr"}]}))
    avis = tmp_path / "avis.json"
    avis.write_text(json.dumps([
        {"name": "velo.fr", "exploit": 90, "resale": 90, "quick_low": 2000, "quick_high": 5000, "final_low": 10000,
         "final_high": 30000, "comment": "À prendre. Mot courant et rare. Acheteur type : enseigne vélo."},
        {"name": "autre.fr", "exploit": 10, "resale": 10, "quick_low": 0, "quick_high": 0, "final_low": 0,
         "final_high": 50, "comment": "À laisser. Mot rare sans usage. Acheteur type : personne."},
    ]))
    out = subprocess.run([sys.executable, "scripts/ai_exchange.py", "check", str(lot), str(avis)],
                         capture_output=True, text=True, check=True).stdout
    assert "1 avis conformes, 2 problèmes" in out and "autre.fr : nom inconnu" in out and "mai.fr : pas d'avis" in out


def test_top_follows_language_and_extensions(logged):
    client = logged()
    client.app.state.conn.execute("INSERT INTO fr_names (label, created_on) VALUES ('assurance', '2001-01-01')")
    every = client.get("/api/explore/top", params={"cat": "sector", "n": 10}).json()
    assert [i["label"] for i in every["items"]][:2] == ["assurance", "insurance"]
    french = client.get("/api/explore/top", params={"cat": "sector", "n": 10, "lang": "fr", "tld": "fr"}).json()
    labels = [i["label"] for i in french["items"]]
    assert "mutuelle" in labels and "loans" not in labels and french["tlds"] == ["fr"]
    assert french["items"][0]["cells"]["fr"]["state"] == "pris"
    assert client.get("/api/explore/top", params={"cat": "nope"}).status_code == 422


def test_language_is_ignored_for_first_names(logged):
    client = logged()
    client.app.state.conn.execute("INSERT INTO fr_names (label, created_on) VALUES ('zz', '2001-01-01')")
    every = client.get("/api/explore/top", params={"cat": "firstname", "n": 500}).json()
    french = client.get("/api/explore/top", params={"cat": "firstname", "n": 500, "lang": "fr"}).json()
    assert len(every["items"]) == len(french["items"]) == 500


def test_recent_first_names_in_top_sort_and_settings(logged, tmp_path):
    import httpx as _httpx
    from app import prenoms
    client = logged()
    st = client.app.state
    st.conn.execute("INSERT INTO fr_names (label, created_on) VALUES ('louise', '2001-01-01')")
    # Sans fichier lu : ni liste ni tri, et le Top le dit.
    assert client.get("/api/explore/rank", params={"cat": "firstname"}).json()["recent"] is None
    assert client.get("/api/explore/top", params={"cat": "firstname_recent", "n": 10}).status_code == 409
    assert client.get("/api/settings").json()["prenoms"]["latest"] is None
    # Fichier fabriqué pour le test, servi par un faux data.gouv.fr.
    text = "sexe;prenom;periode;valeur\n2;LOUISE;2024;900\n1;GABRIEL;2024;1000\n1;XYLOPHONIX;2024;950\n2;JADE;2023;5\n"
    datasets = {"data": [{"title": "Fichier des prénoms", "organization": {"name": "Insee"}, "resources": [
        {"title": "France entière", "url": "https://insee.example/prenoms-nat.csv", "format": "csv", "last_modified": "2025-06-01"}]}]}

    def handler(request):
        if request.url.host == "www.data.gouv.fr":
            return _httpx.Response(200, json=datasets)
        return _httpx.Response(200, content=text.encode())

    st.prenoms._http_factory = lambda: _httpx.AsyncClient(transport=_httpx.MockTransport(handler))
    assert client.post("/api/prenoms/refresh").json() == {"started": True}
    for _ in range(50):
        if not client.get("/api/settings/status").json()["prenoms"]["running"]:
            break
        time.sleep(0.05)
    card = client.get("/api/settings").json()["prenoms"]
    assert card["latest"] == 2024 and card["names"] == 3 and card["untracked"] == 1 and card["error"] is None
    assert client.get("/api/diag").json()["prenoms"]["latest"] == 2024
    top = client.get("/api/explore/top", params={"cat": "firstname_recent", "n": 10, "tld": "fr"}).json()
    assert top["lists"]["firstname_recent"] == "Prénoms 2024" and top["year"] == 2024
    assert [(i["rank"], i["label"], i["count"]) for i in top["items"]] == [(1, "gabriel", 1000), (3, "louise", 900)]
    assert top["items"][1]["cells"]["fr"]["state"] == "pris"
    ranked = client.get("/api/explore/rank", params={"cat": "firstname", "sort": "recent", "tld": "fr", "limit": 2}).json()
    assert ranked["recent"]["year"] == 2024 and ranked["items"][0]["name"] == "gabriel.fr"
    assert "1er prénom donné en 2024" in ranked["items"][0]["reasons"]
    assert client.get("/api/explore/export", params={"cat": "firstname", "sort": "recent", "tld": "fr"}).status_code == 200
    assert client.put("/api/settings/prenoms", json={"url": "http://insee.example/x.csv"}).status_code == 422
    assert client.put("/api/settings/prenoms", json={"url": " https://insee.example/x.csv "}).json()["url"] == "https://insee.example/x.csv"
    client.__exit__(None, None, None)


def test_ai_analysis_on_request(logged):
    client = logged()
    token = client.post("/api/settings/ai-token").json()["token"]
    sent = []

    async def send(text, chat_id=None):
        sent.append(text)

    client.app.state.telegram.send = send
    # Demande depuis la fiche, même pour un nom hors des listes.
    asked = client.post("/api/ai/request", json={"name": "MonProjet.io"}, headers={"host": "domaines.exemple.fr"}).json()
    assert asked["name"] == "monprojet.io" and asked["done_at"] is None
    assert client.get("/api/explore/fiche", params={"name": "monprojet.io"}).json()["ai_request"]["done_at"] is None
    assert [r["name"] for r in client.get("/api/ai/requests").json()["pending"]] == ["monprojet.io"]

    headers = {"Authorization": f"Bearer {token}"}
    lot = client.get("/api/ai/pending", params={"requested": "1"}, headers=headers).json()
    assert [n["name"] for n in lot["names"]] == ["monprojet.io"] and lot["names"][0]["requested"]
    opinion = {"name": "monprojet.io", "exploit": 40, "resale": 30, "quick_low": 0, "quick_high": 0,
               "final_low": 100, "final_high": 300,
               "comment": "À laisser. Composé générique en .io. Acheteur type : start-up du même nom."}
    assert client.post("/api/ai/results", json={"results": [opinion]}, headers=headers).json()["saved"] == 1
    # Telegram : le verdict et le lien direct vers la fiche.
    assert len(sent) == 1 and "monprojet.io" in sent[0] and "À laisser" in sent[0]
    assert "/?fiche=monprojet.io&ai=1" in sent[0] and "domaines.exemple.fr" in sent[0]
    assert client.get("/api/ai/pending", params={"requested": "1"}, headers=headers).json()["names"] == []
    # Notification dans l'appli jusqu'à l'ouverture de la fiche.
    done = client.get("/api/ai/requests").json()["done"]
    assert [d["name"] for d in done] == ["monprojet.io"] and done[0]["comment"].startswith("À laisser")
    assert client.post("/api/ai/requests/seen", json={"name": "monprojet.io"}).json() == {"pending": [], "done": []}

    # Lien ouvert sans session : connexion, puis retour sur la fiche.
    client.cookies.clear()
    login = client.get("/?fiche=monprojet.io&ai=1", follow_redirects=False)
    assert login.headers["location"] == "/login?next=%2F%3Ffiche%3Dmonprojet.io%26ai%3D1"
    back = client.post("/login", data={"username": "moi", "password": PASSWORD, "next": "/?fiche=monprojet.io&ai=1"},
                       follow_redirects=False)
    assert back.headers["location"] == "/?fiche=monprojet.io&ai=1"
    evil = client.post("/login", data={"username": "moi", "password": PASSWORD, "next": "//evil.example"},
                       follow_redirects=False)
    assert evil.headers["location"] == "/"
    client.__exit__(None, None, None)


def test_hidden_drops_leave_the_screen(logged):
    client = logged()
    now = datetime.now(timezone.utc)
    client.app.state.drops.record("fr", ["ciel", "mai"], [], now)
    client.app.state.conn.execute("UPDATE drops SET state = 'available', available_since = ?", (now.isoformat(),))
    names = {i["name"] for i in client.get("/api/drops").json()["items"]}
    assert {"ciel.fr", "mai.fr"} <= names
    assert client.post("/api/drops/hide", json={"names": ["ciel.fr"]}).json() == {"hidden": 1}
    data = client.get("/api/drops").json()
    assert "ciel.fr" not in {i["name"] for i in data["items"]} and data["hidden"] == 1
    client.post("/api/drops/unhide")
    assert "ciel.fr" in {i["name"] for i in client.get("/api/drops").json()["items"]}
    client.__exit__(None, None, None)


def test_portfolio_tracks_purchases_sales_and_reminders(logged):
    from app import portfolio

    client = logged()
    added = []

    async def add(name):
        added.append(name)

    client.app.state.checker.add = add
    soon = (datetime.now(timezone.utc) + timedelta(days=20)).date().isoformat()
    data = client.post("/api/portfolio", json={"name": "ForumDomotique.fr", "buy_price": 12, "renew_price": 12,
                                               "bought_on": "2025-03-01", "expires_at": soon}).json()
    item = data["items"][0]
    assert item["name"] == "forumdomotique.fr" and item["status"] == "owned" and added == ["forumdomotique.fr"]
    assert data["checking"] is True
    assert data["totals"]["invested"] == 12 and data["totals"]["yearly_renewals"] == 12
    assert client.post("/api/portfolio", json={"name": "forumdomotique.fr"}).status_code == 409
    # Rappel 30 jours avant l'échéance, une seule fois.
    conn = client.app.state.conn
    # Pas marqué tant que le message n'est pas parti (échec Telegram : rappel gardé).
    due = portfolio.due_reminders(conn, datetime.now(timezone.utc))
    assert len(due) == 1 and portfolio.due_reminders(conn, datetime.now(timezone.utc)) == due
    texts = portfolio.reminders(conn, datetime.now(timezone.utc))
    assert len(texts) == 1 and "forumdomotique.fr" in texts[0]
    assert portfolio.reminders(conn, datetime.now(timezone.utc)) == []
    # Vente : bénéfice, et prix réel ajouté à la consigne de notation.
    data = client.put(f"/api/portfolio/{item['id']}", json={"status": "sold", "sold_price": 900, "sold_on": "2026-10-01"}).json()
    assert data["items"][0]["profit"] == 888 and data["totals"]["sold_total"] == 900
    token = client.post("/api/settings/ai-token").json()["token"]
    guide = client.get("/api/ai/pending", params={"limit": 1}, headers={"Authorization": f"Bearer {token}"}).json()["guide"]
    assert "forumdomotique.fr : 900 euros (vendu)" in guide
    in_use = client.put(f"/api/portfolio/{item['id']}", json={"status": "in_use"}).json()
    assert in_use["items"][0]["status_label"] == "En exploitation" and in_use["totals"]["kept"] == 1
    assert client.put(f"/api/portfolio/{item['id']}", json={"status": "volé"}).status_code == 422
    assert client.delete(f"/api/portfolio/{item['id']}").json()["items"] == []
    client.__exit__(None, None, None)


def test_diag_log_keeps_exception_cause_without_secrets():
    import logging
    from app import main

    try:
        raise ValueError("https://api.telegram.org/bot123456:ABC-def_9/sendMessage token=abc&x=1")
    except ValueError:
        logging.getLogger("test").error("Tâche en échec", exc_info=True)
    line = main.RECENT.records[-1]["message"]
    assert line.startswith("Tâche en échec : ValueError: ") and "test_app.py" not in line
    assert "123456:ABC" not in line and "token=abc" not in line and "bot<masqué>" in line


def test_bad_inputs_get_422_or_404_not_500(logged):
    """Entrées tordues relevées par la recette API : jamais d'erreur 500."""
    client = logged()
    client.app.state.checker.add = lambda name: __import__("asyncio").sleep(0)
    item = client.post("/api/portfolio", json={"name": "recette.fr"}).json()["items"][0]
    for bad in ({"status": ""}, {"status": None}, {"status": ["owned"]}, {"buy_price": "inf"},
                {"best_offer": "Infinity"}, {"buy_price": [1]}, {"buy_price": {}}, {"buy_price": True}):
        assert client.put(f"/api/portfolio/{item['id']}", json=bad).status_code == 422, bad
        assert client.post("/api/portfolio", json={"name": "autre.fr", **bad}).status_code == 422, bad
    raw = client.put(f"/api/portfolio/{item['id']}", content='{"buy_price": 1e400}', headers={"content-type": "application/json"})
    assert raw.status_code == 422
    huge = 99999999999999999999999
    assert client.put(f"/api/portfolio/{huge}", json={"notes": "x"}).status_code == 404
    assert client.delete(f"/api/portfolio/{huge}").status_code == 404
    assert client.delete("/api/portfolio/987654").status_code == 404
    assert client.get(f"/api/domains/{huge}/history").status_code == 404
    assert client.delete(f"/api/domains/{huge}").status_code == 404
    hidden = client.post("/api/drops/hide", json={"names": ["x" * 5000, "é.fr", "ok.fr"]}).json()["hidden"]
    assert hidden == 1


def test_premium_devalue_toggle_applies_even_when_tlds_are_sent(logged):
    client = logged()
    explorer = client.app.state.explorer
    calls = []
    explorer.refresh_settings = lambda: calls.append(1)
    off = sorted(explorer.premium_tlds_off())
    client.put("/api/settings/premium", json={"devalue": True, "prices": {}, "off": off})
    client.put("/api/settings/premium", json={"devalue": False, "prices": {}, "off": off})
    assert len(calls) == 2


# --- Corrections de l'audit serveur ------------------------------------------------------


def test_telegram_token_is_checked_before_being_saved(logged):
    import httpx as _httpx
    from app.telegram import TelegramError

    client = logged()
    st = client.app.state
    calls = []

    def accept(request):
        calls.append(request.url.path.rsplit("/", 1)[-1])
        return _httpx.Response(200, json={"ok": True, "result": {"username": "monbot"}})

    st.telegram.http = _httpx.AsyncClient(transport=_httpx.MockTransport(accept))
    # Forme invalide (dont un caractère de contrôle collé depuis un terminal) : 422 sans appel, rien d'écrit.
    for raw in ("123:AA\x1b[0mabc", "pas-un-jeton", "123:court"):
        assert client.put("/api/settings/telegram", json={"token": raw}).status_code == 422, raw
    assert calls == [] and st.store.get("telegram_token") == ""
    good = "123456789:" + "A" * 35
    assert client.put("/api/settings/telegram", json={"token": " " + good + "\n"}).json() == {"bot_name": "monbot"}
    assert st.store.get("telegram_token") == good and calls == ["getMe"]
    # Jeton inutilisable déjà en base (ancienne version) : un refus, pas une exception ; le bot et les alertes tiennent.
    st.store.set("telegram_token", "123:AA\x1bx")
    st.store.set("telegram_chat_id", "1")
    assert client.post("/api/alerts/test").status_code == 502
    with pytest.raises(TelegramError):
        client.portal.call(st.telegram._call, st.telegram.http, "getUpdates")
    assert client.get("/api/settings").status_code == 200
    client.__exit__(None, None, None)


def _as_client(app_, host):
    """Même appli, vue depuis une autre adresse TCP (le proxy du NAS, cloudflared ou un visiteur direct)."""

    async def wrapped(scope, receive, send):
        if scope["type"] == "http":
            scope = {**scope, "client": (host, 4000)}
        await app_(scope, receive, send)

    return TestClient(wrapped, follow_redirects=False)


def test_login_lockout_uses_the_address_behind_a_private_proxy(logged, caplog):
    client = logged()
    caplog.set_level("INFO", logger="domain-watch")
    proxy = _as_client(client.app, "172.17.0.1")  # cloudflared ou le proxy du NAS, dans Docker
    for _ in range(5):
        proxy.post("/login", data={"username": "x", "password": "x"}, headers={"cf-connecting-ip": "203.0.113.9"})
    assert "depuis 203.0.113.9" in caplog.text
    # Un autre visiteur par le même proxy n'est pas bloqué ; l'auteur des échecs l'est.
    ok = proxy.post("/login", data={"username": "moi", "password": PASSWORD}, headers={"x-forwarded-for": "198.51.100.7, 172.17.0.1"})
    assert ok.status_code == 303
    locked = proxy.post("/login", data={"username": "moi", "password": PASSWORD}, headers={"cf-connecting-ip": "203.0.113.9"})
    assert locked.status_code == 200 and "Nouvel essai possible" in locked.text
    # Visiteur direct (adresse publique, pas une plage de documentation : Python les dit privées) : ses en-têtes ne font pas foi.
    direct = _as_client(client.app, "93.184.216.34")
    assert direct.post("/login", data={"username": "moi", "password": PASSWORD}, headers={"cf-connecting-ip": "203.0.113.9"}).status_code == 303
    assert set(client.app.state.guard._locked_until) == {"203.0.113.9"}
    client.__exit__(None, None, None)


def test_calendar_url_follows_the_public_address(logged, monkeypatch):
    monkeypatch.delenv("APP_URL", raising=False)
    client = logged()
    proxy = {"host": "domaines.exemple.fr", "x-forwarded-proto": "https"}
    url = client.post("/api/settings/calendar", headers=proxy).json()["calendar_url"]
    assert url.startswith("https://domaines.exemple.fr/calendar/")
    client.get("/", headers=proxy)  # adresse mémorisée pour les liens Telegram : la même sert au calendrier
    assert client.get("/api/settings").json()["calendar_url"] == url
    client.__exit__(None, None, None)


def test_late_portfolio_entry_gets_a_single_reminder(logged):
    import asyncio
    from app import portfolio

    client = logged()
    client.app.state.checker.add = lambda name: asyncio.sleep(0)
    conn = client.app.state.conn
    now = datetime.now(timezone.utc)
    client.post("/api/portfolio", json={"name": "tardif.fr", "expires_at": (now + timedelta(days=4)).date().isoformat()})
    assert len(portfolio.reminders(conn, now)) == 1
    assert portfolio.reminders(conn, now + timedelta(hours=6)) == []
    assert len(json.loads(conn.execute("SELECT reminded FROM portfolio").fetchone()[0])) == 2
    client.__exit__(None, None, None)


def test_out_of_range_number_in_a_setting_is_a_422(logged):
    client = logged()
    for path, body in (("/api/settings/drops", '{"keep_days": 1e400}'),
                       ("/api/settings/premium", '{"devalue": true, "prices": {"com": {"2": 1e400}}}')):
        response = client.put(path, content=body, headers={"content-type": "application/json"})
        assert response.status_code == 422 and '"input"' not in response.text, path
    client.__exit__(None, None, None)


def test_unknown_signal_key_does_not_break_the_domain_list(logged):
    client = logged()
    client.app.state.conn.execute("INSERT INTO domains (name, added_at, state, signals) VALUES ('sig.fr', 'x', 'active', ?)",
                                  (json.dumps({"signals": ["inconnu"]}),))
    assert client.get("/api/domains").json()[0]["signals"] == [{"key": "inconnu", "label": "inconnu"}]
    client.__exit__(None, None, None)


def test_setup_refuses_passwords_bcrypt_would_truncate(client_factory):
    from app.auth import password_problem

    long_one = "a" * 80
    with client_factory() as client:
        page = client.post("/setup", data={"password": long_one, "confirm": long_one}).text
        assert "72 octets" in page and '<pre class="hash">' not in page
    assert password_problem("é" * 40) and password_problem("court")
    assert password_problem("a" * 72) is None


def test_settings_do_not_hammer_telegram_when_the_token_is_refused(logged):
    import httpx as _httpx

    client = logged()
    st = client.app.state
    calls = []

    def refuse(request):
        calls.append(1)
        return _httpx.Response(401, json={"ok": False, "description": "Unauthorized"})

    st.telegram.http = _httpx.AsyncClient(transport=_httpx.MockTransport(refuse))
    st.store.set("telegram_token", "123456789:" + "A" * 35)
    for _ in range(3):
        assert client.get("/api/settings").json()["telegram"]["bot_name"] is None
    assert len(calls) == 1
    st.telegram_failed_at -= 3601  # une heure plus tard : nouvel essai
    client.get("/api/settings")
    assert len(calls) == 2
    client.__exit__(None, None, None)


def test_read_only_token_keeps_portfolio_and_views_closed(logged):
    client = logged()
    token = client.post("/api/settings/ai-token").json()["token"]
    client.cookies.clear()
    headers = {"Authorization": f"Bearer {token}"}
    assert client.get("/api/portfolio", headers=headers).status_code == 401
    assert client.get("/api/explore/views", headers=headers).status_code == 401
    assert client.get("/api/explore/seo", headers=headers).status_code == 401
    assert client.get("/api/explore/top", headers=headers).status_code == 200
    assert client.get("/api/explore/map", headers=headers).status_code == 200
    client.__exit__(None, None, None)


def test_refused_token_never_revives_the_environment_token(client_factory):
    import httpx as _httpx

    hashed = bcrypt.hashpw(PASSWORD.encode(), bcrypt.gensalt(4)).decode()
    with client_factory(AUTH_USERNAME="moi", AUTH_PASSWORD_HASH=hashed, TELEGRAM_BOT_TOKEN="111:" + "e" * 30, TELEGRAM_CHAT_ID="42") as client:
        client.post("/login", data={"username": "moi", "password": PASSWORD})
        st = client.app.state
        assert st.telegram.configured
        assert client.delete("/api/settings/telegram").status_code == 204
        st.telegram.http = _httpx.AsyncClient(transport=_httpx.MockTransport(
            lambda r: _httpx.Response(401, json={"ok": False, "description": "Unauthorized"})))
        assert client.put("/api/settings/telegram", json={"token": "222:" + "f" * 30}).status_code == 422
        assert st.telegram.token == "" and client.get("/api/settings").json()["telegram"]["token_set"] is False

def test_jobs_tolerate_a_busy_loop(client_factory):
    """Boucle bloquée quelques secondes (import .fr) : l'exécution en retard est rattrapée, pas sautée."""
    with client_factory(AUTH_USERNAME="moi", AUTH_PASSWORD_HASH="x") as client:
        jobs = client.app.state.scheduler.get_jobs()
        assert jobs and all(job.misfire_grace_time == 300 and job.coalesce for job in jobs)


def test_portfolio_gives_yearly_figures_sale_links_and_a_csv_export(logged):
    from app import portfolio

    client = logged()
    client.app.state.checker.add = lambda name: __import__("asyncio").sleep(0)
    year = datetime.now(timezone.utc).year
    client.post("/api/portfolio", json={"name": "velo.fr", "buy_price": 100, "renew_price": 10, "bought_on": f"{year - 2}-03-01",
                                        "status": "sold", "sold_on": f"{year - 1}-06-01", "sold_price": 900, "notes": "vendu; vite\nbien"})
    client.post("/api/portfolio", json={"name": "moto.fr", "buy_price": 50, "renew_price": 12, "bought_on": f"{year - 1}-01-15",
                                        "best_offer": 250, "status": "for_sale"})
    client.post("/api/portfolio", json={"name": "perdu.fr", "buy_price": 20, "renew_price": 12, "bought_on": f"{year - 3}-01-15",
                                        "status": "dropped"})
    client.post("/api/portfolio", json={"name": "sansdate.fr", "renew_price": 12})
    data = client.get("/api/portfolio").json()
    years = {y["year"]: y for y in data["years"]}
    assert sorted(years) == [year - 3, year - 2, year - 1, year]
    assert years[year - 2] == {"year": year - 2, "invested": 100, "renewals": 0, "sold": 0, "profit": 0}
    # Renouvellement de velo.fr l'année de sa vente, pas après ; moto.fr renouvelé cette année ; perdu.fr jamais.
    assert years[year - 1] == {"year": year - 1, "invested": 50, "renewals": 10, "sold": 900, "profit": 800}
    assert years[year] == {"year": year, "invested": 0, "renewals": 12, "sold": 0, "profit": 0}
    assert years[year - 3]["invested"] == 20 and years[year - 3]["renewals"] == 0
    item = next(i for i in data["items"] if i["name"] == "moto.fr")
    assert item["sale_links"] == {"sedo": "https://sedo.com/search/?keyword=moto.fr", "dan": "https://dan.com/buy-domain/moto.fr",
                                  "afternic": "https://www.afternic.com/domain/moto.fr"}
    assert portfolio.sale_links("a b.fr")["sedo"].endswith("keyword=a%20b.fr")
    # Export CSV : BOM, point-virgule, une ligne par nom, notes sur une ligne sans point-virgule.
    res = client.get("/api/portfolio/export")
    assert res.status_code == 200 and res.headers["content-type"].startswith("text/csv")
    assert res.headers["content-disposition"] == 'attachment; filename="portefeuille.csv"'
    lines = res.text.splitlines()
    assert lines[0] == "﻿" + ";".join(portfolio.EXPORT_COLUMNS)
    assert len(lines) == 5
    velo = next(line for line in lines if line.startswith("velo.fr;"))
    assert velo == f"velo.fr;Vendu;{year - 2}-03-01;100;10;;;;;{year - 1}-06-01;900;800;;;;;vendu, vite bien"
    token = client.post("/api/settings/ai-token").json()["token"]
    client.cookies.clear()  # le jeton en lecture seule n'ouvre pas le portefeuille
    assert client.get("/api/portfolio/export", headers={"Authorization": f"Bearer {token}"}).status_code == 401

def _opinion(conn, name, verdict, resale, exploit=80):
    conn.execute(
        "INSERT INTO domain_ai (name, exploit, resale, quick_low, quick_high, final_low, final_high, comment, rated_at, version) "
        "VALUES (?, ?, ?, 500, 1000, 3000, 8000, ?, '2026-10-01T00:00:00+00:00', 5)",
        (name, exploit, resale, f"{verdict}. Mot courant, rare en 4 lettres. Acheteur type : enseigne."))


def test_alert_settings_take_alert_and_view_alerts(logged):
    client = logged()
    st = client.app.state
    sent = []

    async def send(text, chat_id=None):
        sent.append(text)

    st.telegram.send = send
    st.store.set("telegram_token", "123:abc")
    st.store.set("telegram_chat_id", "1")
    st.store.set("public_url", "https://nas.exemple")
    # Réglages : seuil entre 0 et 100, activation.
    assert client.get("/api/settings/alerts").json() == {"take_enabled": True, "take_min": 70}
    assert client.put("/api/settings/alerts", json={"take_enabled": True, "take_min": 101}).status_code == 422
    assert client.put("/api/settings/alerts", json={"take_enabled": True, "take_min": 80}).json() == {"take_enabled": True, "take_min": 80}
    assert client.get("/api/settings").json()["alerts"] == {"take_enabled": True, "take_min": 80}
    now = datetime.now(timezone.utc)
    st.conn.execute("INSERT INTO fr_names (label, created_on) VALUES ('mot', '2010-01-01')")  # le .fr a des données
    st.drops.record("fr", ["velo", "mai"], [], now)
    st.conn.execute("UPDATE drops SET state = 'available', available_since = ?", (now.isoformat(),))
    _opinion(st.conn, "velo.fr", "À prendre", 90)
    _opinion(st.conn, "mai.fr", "À prendre", 60)  # sous le seuil
    for name in ("velo.fr", "mai.fr"):
        st.explorer.touch(name)
    views = [{"name": "Quatre lettres", "filters": {"len": "4"}, "alert": True},
             {"name": "Prénoms", "filters": {"cat": "firstname", "min": "99"}, "alert": True},
             {"name": "Sans alerte", "filters": {}}]
    assert [v["alert"] for v in client.put("/api/explore/views", json={"views": views}).json()] == [True, True, False]
    client.portal.call(st.send_alerts)
    # Un message « À prendre » (velo.fr seul), puis un par vue qui a du nouveau (« Prénoms » n'a rien).
    assert len(sent) == 2 and sent[0].startswith("À prendre : 1 nom")
    assert "velo.fr : libre" in sent[0] and "revente 90" in sent[0] and "3000 à 8000 €" in sent[0]
    assert "https://nas.exemple/?fiche=velo.fr" in sent[0] and "Réserver : https://shop.gandi.net" in sent[0] and "mai.fr" not in sent[0]
    assert sent[1].startswith("Vue « Quatre lettres » : 1 nouveau nom") and "velo.fr (libre)" in sent[1] and "mai.fr" not in sent[1]
    assert set(json.loads(st.store.get("view_alert_last"))) == {"Quatre lettres", "Prénoms"}
    # Second passage : rien de nouveau, et les vues attendent une heure.
    client.portal.call(st.send_alerts)
    assert len(sent) == 2
    assert client.get("/api/ai/outcomes").json()["total"] == 0  # tâche de nuit : avis trop récents
    client.__exit__(None, None, None)


def test_decisions_regrets_and_targeted_unhide(logged):
    client = logged()
    st = client.app.state
    added = []

    async def add(name):
        added.append(name)

    st.checker.add = add
    now = datetime.now(timezone.utc)
    st.conn.execute("INSERT INTO fr_names (label, created_on) VALUES ('mot', '2010-01-01')")  # le .fr a des données
    st.drops.record("fr", ["ciel", "mai", "lune"], [], now)
    st.conn.execute("UPDATE drops SET state = 'available', available_since = ?", (now.isoformat(),))
    assert client.post("/api/drops/hide", json={"names": ["ciel.fr", "mai.fr"]}).json() == {"hidden": 2}
    found = client.get("/api/decisions").json()
    assert {(i["name"], i["decision"]) for i in found["items"]} == {("ciel.fr", "ignored"), ("mai.fr", "ignored")}
    assert found["regrets"] == 0 and all(i["outcome"] is None for i in found["items"])
    # Masquage annulé pour un seul nom : sa décision part avec.
    assert client.post("/api/drops/unhide", json={"names": ["mai.fr"]}).json() == {"hidden": 1}
    assert "mai.fr" in {i["name"] for i in client.get("/api/drops").json()["items"]}
    assert [i["name"] for i in client.get("/api/decisions").json()["items"]] == ["ciel.fr"]
    # Le nom ignoré est repris par quelqu'un d'autre : regret, daté.
    st.conn.execute("UPDATE drops SET state = 'taken', state_since = '2026-10-10T00:00:00+00:00' WHERE name = 'ciel.fr'")
    st.explorer.touch("ciel.fr")
    found = client.get("/api/decisions").json()
    assert found["regrets"] == 1 and found["items"][0]["outcome"] == "repris" and found["items"][0]["taken_at"] == "2026-10-10T00:00:00+00:00"
    assert found["items"][0]["state_label"] == "Enregistré"
    # Décision posée à la main, puis « J'ai acheté » par le portefeuille.
    assert client.put("/api/decisions/lune.fr", json={"decision": "peut-être"}).status_code == 422
    assert client.put("/api/decisions/pas un nom", json={"decision": "bought"}).status_code == 422
    items = client.put("/api/decisions/Lune.fr", json={"decision": "ignored", "note": "  trop  cher "}).json()["items"]
    assert {i["name"]: (i["decision"], i["note"]) for i in items}["lune.fr"] == ("ignored", "trop cher")
    client.post("/api/portfolio", json={"name": "velo.fr", "buy_price": 12})
    assert {i["name"]: i["decision"] for i in client.get("/api/decisions").json()["items"]}["velo.fr"] == "bought"
    assert client.delete("/api/decisions/lune.fr").status_code == 204
    assert client.delete("/api/decisions/lune.fr").status_code == 404
    # Tout réafficher : les décisions « ignoré » des noms masqués s'effacent, l'achat reste.
    assert client.post("/api/drops/unhide").json() == {"hidden": 0}
    assert {i["name"] for i in client.get("/api/decisions").json()["items"]} == {"velo.fr"}

def test_security_headers_and_login_redirect_when_already_connected(logged):
    client = logged()
    expected = {"content-security-policy": "frame-ancestors 'none'", "x-content-type-options": "nosniff",
                "referrer-policy": "same-origin", "x-frame-options": "DENY"}
    for path in ("/", "/api/meta", "/api/settings/status", "/login"):
        headers = client.get(path).headers
        assert {k: headers.get(k) for k in expected} == expected, path
    # /logo garde sa CSP propre (SVG sans script), les autres en-têtes s'ajoutent.
    assert client.get("/api/inconnue").headers["x-frame-options"] == "DENY"
    # Déjà connecté : /login renvoie à l'appli, ou à la page demandée si elle est locale.
    assert client.get("/login").headers["location"] == "/"
    assert client.get("/login", params={"next": "/?fiche=velo.fr"}).headers["location"] == "/?fiche=velo.fr"
    assert client.get("/login", params={"next": "https://ailleurs.example"}).headers["location"] == "/"
    client.cookies.clear()
    response = client.get("/login")
    assert response.status_code == 200 and response.headers["x-frame-options"] == "DENY"
    assert client.get("/api/meta").status_code == 401 and client.get("/api/meta").headers["x-frame-options"] == "DENY"
    client.__exit__(None, None, None)


def test_portfolio_check_runs_in_the_background(logged, caplog):
    client = logged()
    started = []

    async def slow_add(name):
        started.append(name)
        await asyncio.sleep(0.05)
        raise RuntimeError("registre muet")

    client.app.state.checker.add = slow_add
    caplog.set_level("WARNING", logger="domain-watch")
    data = client.post("/api/portfolio", json={"name": "velo.fr", "expires_at": "2027-01-01"}).json()
    # Réponse tout de suite, vérification lancée derrière.
    assert data["checking"] is True and data["items"][0]["expiry_source"] == "saisie" and started == ["velo.fr"]
    time.sleep(0.2)
    client.get("/api/meta")  # un tour de boucle : la tâche de fond rend la main
    assert "Portefeuille : vérification de velo.fr : RuntimeError: registre muet" in caplog.text
    # Nom déjà surveillé : rien à vérifier.
    conn = client.app.state.conn
    conn.execute("INSERT INTO domains (name, added_at, expires_at, state) VALUES ('maison.fr', 'x', '2027-06-01T00:00:00+00:00', 'active')")
    data = client.post("/api/portfolio", json={"name": "maison.fr"}).json()
    assert data["checking"] is False and data["items"][0]["expiry_source"] == "RDAP"
    client.__exit__(None, None, None)


def test_light_polls_for_pairing_reserved_and_drops(logged):
    client = logged()
    st = client.app.state
    status = client.get("/api/settings/status").json()
    assert status == {"telegram": {"chat_set": False, "bot_name": None, "pairing": False},
                      "reserved": {"running": False, "at": None, "error": None, "count": 0},
                      "prenoms": {"running": False, "latest": None}}
    st.store.set("telegram_chat_id", "42")
    st.telegram.bot_name = "monbot"
    st.telegram.start_pairing()
    st.conn.execute("INSERT INTO reserved_names (name, source) VALUES ('paris.fr', 'communes')")
    st.store.set("reserved_status", json.dumps({"at": "2026-10-17T06:00:00+00:00", "error": None}))
    status = client.get("/api/settings/status").json()
    assert status["telegram"] == {"chat_set": True, "bot_name": "monbot", "pairing": True}
    assert status["reserved"] == {"running": False, "at": "2026-10-17T06:00:00+00:00", "error": None, "count": 1}
    # Veille : l'état seul, sans la liste, avec l'écart refusé et l'acceptation.
    st.store.set("czds_username", "moi")
    st.store.set("czds_password", "x")
    st.store.set("czds_approved", '["com", "net"]')
    st.store.set("czds_gap_refused", json.dumps({"com": {"expected": 1000, "found": 900, "at": "2026-10-18T06:00:00+00:00"}}))
    light = client.get("/api/drops", params={"items": "0"}).json()
    assert light["items"] is None and light["czds"] is True and light["running"] is False
    assert light["gap_refused"]["com"]["found"] == 900 and light["accept_gap"] == []
    assert client.post("/api/settings/czds/accept-gap", json={"tld": "org"}).status_code == 422
    assert client.post("/api/settings/czds/accept-gap", json={"tld": ".COM"}).json()["accept_gap"] == ["com"]
    assert client.get("/api/drops", params={"items": "0"}).json()["accept_gap"] == ["com"]
    client.__exit__(None, None, None)


def test_diag_lists_missed_and_failed_scheduler_runs(logged):
    from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_MISSED, JobExecutionEvent

    client = logged()
    scheduler = client.app.state.scheduler
    when = datetime.now(timezone.utc)
    scheduler._dispatch_event(JobExecutionEvent(EVENT_JOB_MISSED, "latest", None, when))
    try:
        raise ValueError("bot123456:ABC-def token=secret")
    except ValueError as exc:
        scheduler._dispatch_event(JobExecutionEvent(EVENT_JOB_ERROR, "telegram_flush", None, when, exception=exc))
    diag = client.get("/api/diag").json()
    assert diag["scheduler"]["missed"][0]["job"] == "refresh_latest" and diag["scheduler"]["missed"][0]["scheduled"] == when.replace(microsecond=0).isoformat()
    error = diag["scheduler"]["errors"][0]
    assert error["job"] == "flush" and error["error"].startswith("ValueError: bot<masqué>") and "secret" not in error["error"]
    assert diag["telegram_outbox"] == 0
    client.__exit__(None, None, None)


def test_interface_wires_the_wave_one_routes():
    """L'interface branche ce que les serveurs exposent : routes, libellés et listes (chantier F2b)."""
    from pathlib import Path

    static = Path(__file__).resolve().parent.parent / "app" / "static"
    js = (static / "app.js").read_text(encoding="utf-8")
    html = (static / "index.html").read_text(encoding="utf-8")
    # Routes de la vague 1 branchées, sondages légers compris.
    for route in ("/api/settings/alerts", "/api/ai/outcomes", "/api/decisions", "/api/drops/unhide", "/api/settings/status",
                  "/api/settings/czds/accept-gap", "/api/drops?items=0", "/api/explore/views"):
        assert route in js, route
    assert "/api/portfolio/export" in html
    assert 'this.api("GET", "/api/settings")' not in js.split("async startPairing")[1].split("async removeTelegram")[0]
    # Nouvelles listes : puces de type et gabarit du Top, après Secteurs.
    kinds = [k.split('"')[0] for k in js.split("const EX_TYPES")[1].split("];")[0].split('k: "')[1:]]
    assert kinds[kinds.index("sector"):kinds.index("sector") + 5] == ["sector", "job", "sport", "animal", "food"]
    top = js.split("const TOP_LISTS = [")[1].split("]")[0]
    assert top.startswith('"sector", "job", "sport", "animal", "food"')
    # « Masquer » devient « Ignorer » pour la décision (la case « Masquer les noms faibles » est un filtre, elle reste).
    assert ">Ignorer<" in html and "dropBulk('hide')\" :disabled=\"dropBulkRunning\">Masquer" not in html
    assert "masqué(s)" not in html and "noms ignorés" in js
    # Fiche : pourquoi, ancienne grille, comparables, repères, ligne de temps, avis des autres extensions.
    assert "k.why = opinion.why" in js and "ancienne grille" in html
    for block in ("fiche.comparables", "fiche.references", "fiche.timeline", "o.ai_summary"):
        assert block in html, block
    # Erreurs réseau en français simple, sans console.error ajouté par l'appli.
    assert "Réseau injoignable" in js and "Pas de réponse du registre" in js and "console." not in js
    assert "Réessayer" in js


def test_today_counts_best_names_and_setup(logged):
    """Aujourd'hui : compteurs du jour, 20 meilleurs noms libres ou libérés sous 48 h, étapes de configuration."""
    client = logged()
    st = client.app.state

    async def add(name):
        return None

    st.checker.add = add
    now = datetime.now(timezone.utc)
    st.conn.execute("INSERT INTO fr_names (label, created_on) VALUES ('mot', '2010-01-01')")  # le .fr a des données
    st.drops.record("fr", ["velo", "mai", "ciel", "lune", "chat", "terre"], [], now)
    soon, later = (now + timedelta(hours=24)).isoformat(), (now + timedelta(days=10)).isoformat()
    st.conn.execute("UPDATE drops SET state = 'available', available_since = ? WHERE name IN ('velo.fr', 'chat.fr', 'terre.fr')", (now.isoformat(),))
    st.conn.execute("UPDATE drops SET state = 'available', available_since = ? WHERE name = 'mai.fr'", ((now - timedelta(days=3)).isoformat(),))
    st.conn.execute("UPDATE drops SET state = 'redemption', release_at = ? WHERE name = 'ciel.fr'", (soon,))
    st.conn.execute("UPDATE drops SET state = 'redemption', release_at = ? WHERE name = 'lune.fr'", (later,))
    _opinion(st.conn, "velo.fr", "À prendre", 90, exploit=60)
    _opinion(st.conn, "mai.fr", "À prendre", 60, exploit=95)
    _opinion(st.conn, "lune.fr", "À prendre", 80)  # bientôt libre, mais pas sous 48 h
    _opinion(st.conn, "chat.fr", "À laisser", 20)  # nom faible : jamais en tête d'Aujourd'hui
    st.conn.execute("UPDATE domain_ai SET rated_at = ? WHERE name IN ('velo.fr', 'lune.fr', 'chat.fr')", (now.isoformat(),))
    assert client.post("/api/drops/hide", json={"names": ["terre.fr"]}).json() == {"hidden": 1}
    for name in ("velo.fr", "mai.fr", "ciel.fr", "lune.fr", "chat.fr", "terre.fr"):
        st.explorer.touch(name)
    assert client.get("/api/today", params={"sort": "prix"}).status_code == 422
    data = client.get("/api/today").json()
    # Libérés depuis 24 h : velo et chat (terre est ignoré, mai libéré il y a 3 jours) ; sous 48 h : ciel.
    # Nouveaux « À prendre » : velo (libre) et lune (bientôt libre), avis reçus depuis 24 h.
    assert data["counts"] == {"freed_24h": 2, "coming_48h": 1, "new_take": 2, "regrets": 0}
    names = [i["name"] for i in data["items"]]
    assert names[:2] == ["velo.fr", "mai.fr"] and not {"chat.fr", "terre.fr", "lune.fr"} & set(names)
    score = {i["name"]: i["score"] for i in client.get("/api/drops").json()["items"]}
    assert ("ciel.fr" in names) == (score["ciel.fr"] >= 30)
    # Format des cartes de la Veille : état, avis, score, surveillance, portefeuille.
    first = data["items"][0]
    assert first["state"] == "available" and first["ai"]["resale"] == 90 and "score" in first
    assert first["watched"] is False and first["in_portfolio"] is False
    # Pour exploiter : la note d'exploitation passe mai.fr devant.
    assert [i["name"] for i in client.get("/api/today", params={"sort": "exploit"}).json()["items"]][:2] == ["mai.fr", "velo.fr"]
    # Démarrer en 3 étapes : rien de branché sauf le .fr, absent ici.
    assert [(s["key"], s["done"]) for s in data["setup"]] == [("sources", False), ("telegram", False), ("ai", False)]
    assert client.get("/api/meta").json()["steps"] == data["setup"]
    st.store.set("telegram_token", "123:abc")
    st.store.set("telegram_chat_id", "1")
    assert [s["done"] for s in client.get("/api/today").json()["setup"]][1] is True
    # Regret : un nom ignoré repris par quelqu'un d'autre.
    st.conn.execute("UPDATE drops SET state = 'taken' WHERE name = 'terre.fr'")
    st.explorer.touch("terre.fr")
    assert client.get("/api/today").json()["counts"]["regrets"] == 1
    client.__exit__(None, None, None)


def test_in_portfolio_marks_cards_everywhere(logged):
    """Badge « Portefeuille » : in_portfolio sur la Veille, l'Explorer, Aujourd'hui et la fiche, dès l'achat."""
    client = logged()
    st = client.app.state

    async def add(name):
        return None

    st.checker.add = add
    now = datetime.now(timezone.utc)
    st.conn.execute("INSERT INTO fr_names (label, created_on) VALUES ('mot', '2010-01-01')")
    st.drops.record("fr", ["velo", "mai"], [], now)
    st.conn.execute("UPDATE drops SET state = 'available', available_since = ?", (now.isoformat(),))
    _opinion(st.conn, "velo.fr", "À prendre", 90)
    _opinion(st.conn, "mai.fr", "À prendre", 80)
    for name in ("velo.fr", "mai.fr"):
        st.explorer.touch(name)
    assert client.post("/api/portfolio", json={"name": "mai.fr", "buy_price": 10}).status_code == 200
    drops_items = {i["name"]: i["in_portfolio"] for i in client.get("/api/drops").json()["items"]}
    assert drops_items == {"velo.fr": False, "mai.fr": True}
    assert {i["name"]: i["in_portfolio"] for i in client.get("/api/today").json()["items"]} == drops_items
    ranked = client.get("/api/explore/rank", params={"tld": "fr", "status": "bookable", "sort": "resale", "limit": 200}).json()["items"]
    found = {i["name"]: i["in_portfolio"] for i in ranked if i["name"] in drops_items}
    assert found == drops_items
    fiche = client.get("/api/explore/fiche", params={"name": "mai.fr"}).json()
    assert fiche["in_portfolio"] is True and fiche["item"]["in_portfolio"] is True
    assert client.get("/api/explore/fiche", params={"name": "velo.fr"}).json()["in_portfolio"] is False
    client.__exit__(None, None, None)


def test_interface_home_today_single_fiche_and_settings_order():
    """Chantier F2a : écran Aujourd'hui en tête, fiche unique (plus de tiroir Domaines), Paramètres repliés."""
    from pathlib import Path

    static = Path(__file__).resolve().parent.parent / "app" / "static"
    js = (static / "app.js").read_text(encoding="utf-8")
    html = (static / "index.html").read_text(encoding="utf-8")
    assert "/api/today" in js and "Aujourd'hui" in html and "Démarrer en 3 étapes" in html
    # Aujourd'hui avant Domaines dans la barre latérale et la barre du bas.
    for nav in (html.split('class="side-nav"')[1].split("</nav>")[0], html.split('class="tabbar"')[1].split("</nav>")[0]):
        assert nav.index("Aujourd'hui") < nav.index("Domaines") < nav.index("Veille")
    # Sous-titres d'une ligne sous chaque titre d'écran.
    for sub in ("Les meilleurs noms à prendre maintenant", "Les noms que je surveille", "Ce qui tombe et ce qui va tomber",
                "Tous les noms suivis, pour chercher", "Les noms que je possède", "Sources, alertes, réglages"):
        assert sub in html, sub
    # Plus de tiroir : un nom de Domaines ouvre la fiche commune.
    assert "Détail du domaine" not in html and "openId" not in js and "domainFiche" not in js
    assert "openFiche(d.name)" in html and "Supprimer de la surveillance" in html
    # Paramètres : Sources, Telegram, Alertes, Avis IA, puis le bloc avancé replié, Version en pied.
    settings = html.split('aria-label="Paramètres"')[1].split("</section>")[0]
    order = [settings.index(k) for k in ('id="set-fr"', 'id="set-czds"', 'id="settings-sources"', 'id="set-telegram"',
                                          'id="set-alerts"', 'id="set-ai"', "Afficher les réglages avancés", "<h3>Version</h3>")]
    assert order == sorted(order)
    # Aide : « Par écran » d'abord.
    toc = html.split('class="help-toc')[1].split("</nav>")[0]
    assert toc.index("Par écran") < toc.index("Vocabulaire")


def test_rank_sorts_keep_every_filter(logged):
    """Chaque tri de l'Explorer (chemins différents côté serveur : rank, _by_release, _ordered, _by_frequency,
    _by_ai, _by_seo, _candidates) croisé avec chaque filtre : chaque nom renvoyé respecte tous les filtres."""
    from app import drops as drops_mod, explore, wordlists

    client = logged()
    state = client.app.state
    conn = state.conn
    now = datetime.now(timezone.utc)
    first, sur = wordlists.ranks()["firstname"], wordlists.ranks()["surname"]
    prenoms = sorted(first, key=first.get)[:60]
    noms = sorted(sur, key=sur.get)[:60]
    mots = ["maison", "deal", "chat", "marche", "manger", "lampe", "velo", "ciel", "jardin", "voyage", "mai", "ma", "dea"]
    labels = list(dict.fromkeys(prenoms + noms + mots))
    # .fr : un tiers enregistré, un tiers supprimé (probablement libre), le reste jamais vu ; des noms en rédemption et libres.
    for n, label in enumerate(labels):
        if n % 3 < 2:
            conn.execute("INSERT INTO fr_names (label, created_on, removed_on) VALUES (?, '2010-02-01', ?)",
                         (label, None if n % 3 == 0 else "2026-01-01"))
    for n, label in enumerate(labels[::4]):
        state_ = ["redemption", "pending_delete", "available"][n % 3]
        release = None if state_ == "available" or n % 5 == 4 else (now + timedelta(days=1 + (n * 7) % 25)).isoformat()
        conn.execute("INSERT INTO drops (name, tld, label, kind, removed_on, state, state_since, release_at, available_since, lists) "
                     "VALUES (?, 'fr', ?, 'long', '2026-10-01', ?, ?, ?, ?, '')",
                     (f"{label}.fr", label, state_, now.isoformat(), release, now.isoformat() if state_ == "available" else None))
    # .com : une partie des mêmes noms pris (filtre « pris ailleurs »).
    state.store.set("czds_approved", '["com"]')
    bits = bytearray(drops_mod.BITMAP_BYTES)
    for label in ("ma", "mai", "dea", "chat", "ciel", "velo"):
        i = drops_mod.encode(label)
        bits[i // 8] |= 1 << (i % 8)
    state.drops._save("com", bits, set(labels[::2]))
    # Avis IA de chaque verdict, SEO sur quelques noms.
    for n, label in enumerate(labels[::3]):
        verdict = ["À prendre", "À surveiller", "À laisser"][n % 3]
        resale = {"À prendre": 70, "À surveiller": 50, "À laisser": 30}[verdict] + n % 7
        conn.execute("INSERT INTO domain_ai (name, exploit, resale, quick_low, quick_high, final_low, final_high, comment, rated_at, version) "
                     "VALUES (?, ?, ?, 50, ?, 300, ?, ?, 'x', 5)",
                     (f"{label}.fr", resale - 3, resale, 100 + n * 10, 1000 + n * 50, f"{verdict}. Raison. Acheteur type : x."))
    for n, label in enumerate(labels[1::5]):
        conn.execute("INSERT INTO domain_seo (name, pr, wb_years, checked_at) VALUES (?, ?, ?, 'x')", (f"{label}.fr", n % 6, n % 9))
    state.explorer.invalidate()
    state.explorer.ai_changed()

    fr = state.explorer.status_bytes("fr")
    com = state.explorer.status_bytes("com")
    u = state.explorer.universe
    seo = {r[0]: r for r in conn.execute("SELECT name, pr, wb_years FROM domain_seo")}
    verdicts = {"take": "À prendre", "watch": "À surveiller", "skip": "À laisser"}
    filters = [
        {}, {"q": "ma"}, {"q": "mar", "mode": "contains"}, {"cat": "firstname"}, {"cat": "surname"}, {"cat": "word"},
        {"cat": "letters"}, {"len": "4"}, {"len": "-3"}, {"len": "5"}, {"nat": "noun"}, {"nat": "verb"}, {"lang": "fr"},
        {"lang": "en"}, {"rank": "20", "cat": "firstname"}, {"rank": "500"}, {"taken": "com"}, {"taken": "any"},
        {"min": "35"}, {"junk": "1"}, {"seo": "any"}, {"seo": "pr3"}, {"ai_v": "take"}, {"ai_v": "watch,skip"},
        {"ai_resale_min": "60"}, {"ai_final_high_max": "1200"}, {"premium": "1"}, {"level": ""},
        {"q": "ma", "ai_v": "take,watch"}, {"cat": "surname", "seo": "any"}, {"cat": "firstname", "level": "", "min": "20"},
    ]
    # Bientôt libres : tous les filtres (liste courte, parcours rapides) ; Libres et Tous : les filtres qui
    # passent par d'autres chemins (recherche, liste, SEO, avis IA, pris ailleurs), le parcours complet coûte cher.
    plan = {"coming": filters,
            "bookable": [{"q": "ma"}, {"cat": "firstname"}, {"ai_v": "take"}, {"seo": "any"}, {"taken": "com", "cat": "surname"}],
            "all": [{"q": "ma", "ai_v": "take,watch"}, {"cat": "surname", "level": ""}]}
    checked = 0
    for sort in ("resale", "score", "release", "length", "freq", "seo", "ai"):
        for status, chosen in plan.items():
            codes = explore.STATUS_FILTERS[status]
            for f in chosen:
                params = {"tld": "fr", "status": status, "sort": sort, "limit": 15, "level": "good", **f}
                data = client.get("/api/explore/rank", params=params)
                assert data.status_code == 200, params
                items = data.json()["items"]
                # « Un tri ne filtre pas » : avec SEO ou avis IA, les noms analysés d'abord, puis les autres, mais tous filtrés.
                for it in items:
                    label, i = it["label"], u.index(it["label"])
                    ai_row = conn.execute("SELECT * FROM domain_ai WHERE name = ?", (it["name"],)).fetchone()
                    verdict = ai_row["comment"].split(".")[0] if ai_row else ""
                    n = int(f.get("len") or 0)
                    why = {
                        "état": fr[i] in codes,
                        "recherche": not f.get("q") or (f["q"] in label if f.get("mode") == "contains" else label.startswith(f["q"])),
                        "catégorie": not f.get("cat") or f["cat"] in u.categories(label),
                        "longueur": not n or (len(label) <= -n if n < 0 else len(label) >= 5 if n >= 5 else len(label) == n),
                        "nature": not f.get("nat") or f["nat"] in wordlists.natures().get(label, ()),
                        "langue": not f.get("lang") or label in wordlists.languages()[f["lang"]],
                        "fréquence": not f.get("rank") or (state.explorer.best_rank(label, f.get("cat", "")) or 10**9) <= int(f["rank"]),
                        "pris ailleurs": not f.get("taken") or com[i] in explore.TAKEN_CODES,
                        "score minimum": not f.get("min") or it["score"] >= int(f["min"]),
                        "imprononçables": f.get("junk") == "1" or bool(u.good[i]),
                        "SEO": not f.get("seo") or (it["name"] in seo and (seo[it["name"]][1] >= 3 if f["seo"] == "pr3"
                                                                           else seo[it["name"]][1] > 0 or seo[it["name"]][2] > 0)),
                        "verdict IA": not f.get("ai_v") or verdict in {verdicts[v] for v in f["ai_v"].split(",")},
                        "revente IA": not f.get("ai_resale_min") or (ai_row and ai_row["resale"] >= int(f["ai_resale_min"])),
                        "prix IA": not f.get("ai_final_high_max") or (ai_row and ai_row["final_high"] <= int(f["ai_final_high_max"])),
                        "noms faibles": params["level"] != "good" or (verdict != "À laisser" if verdict else it["score"] >= 30),
                    }
                    assert all(why.values()), (params, label, [k for k, v in why.items() if not v])
                    checked += 1
                if sort == "release":
                    dates = [it["release_at"] or "9" for it in items]
                    assert dates == sorted(dates), params
                if sort == "freq":
                    key = f.get("cat") if f.get("cat") in wordlists.LABELS else ""
                    sizes = {k: len(r) for k, r in wordlists.ranks().items()}
                    pos = [it["ranks"][key] if key else min(r / sizes[k] for k, r in it["ranks"].items()) for it in items]
                    assert pos == sorted(pos), params
                    assert all(it["freq_list"] in it["ranks"] for it in items)
    assert checked > 2000
    # Les plus fréquents, en Prénoms : du plus donné au moins donné (Insee), avec ou sans les noms faibles.
    for level in ("good", ""):
        items = client.get("/api/explore/rank", params={"tld": "fr", "status": "all", "sort": "freq", "cat": "firstname",
                                                        "level": level, "limit": 30}).json()["items"]
        ranks = [it["ranks"]["firstname"] for it in items]
        assert ranks and ranks == sorted(ranks) and all(it["freq_list"] == "firstname" for it in items)
    # Par avis IA, noms faibles masqués : les « À laisser » écartés ne laissent pas leur place à des noms sans avis.
    items = client.get("/api/explore/rank", params={"tld": "fr", "status": "all", "sort": "ai", "level": "good", "limit": 5}).json()["items"]
    assert len(items) == 5 and all(it["ai"] and not it["ai"]["comment"].startswith("À laisser") for it in items)
    client.__exit__(None, None, None)


def test_interface_verdict_badges_sorts_and_fiche_labels():
    """Chantier G1 : verdict IA en pastille partout, tris par fréquence et sur téléphone, fiche sans valeurs brutes."""
    from pathlib import Path

    static = Path(__file__).resolve().parent.parent / "app" / "static"
    js = (static / "app.js").read_text(encoding="utf-8")
    html = (static / "index.html").read_text(encoding="utf-8")
    css = (static / "style.css").read_text(encoding="utf-8")
    # Une seule classe de pastille pour le verdict, sans gras ; plus de verdict en texte gras coloré.
    assert ".verdict {" in css and "font-weight: 400" in css.split(".verdict {")[1].split("}")[0]
    for gone in (".nc-ai", ".fd-ai", ".fiche-comp-ai", ".ex-reason.ai", "lineClass"):
        assert gone not in css + js + html, gone
    # Fiche : le verdict de la carte décision vient de ficheDecision (cls), le gabarit le met en pastille.
    assert html.count('class="verdict"') >= 4 and 'cls: "verdict "' in js
    # Tri « Les plus fréquents » dans la Veille et l'Explorer ; « Libération » grisé hors de Bientôt libres.
    assert html.count('<option value="freq"') == 2 and "Les plus fréquents" in html
    assert ":disabled=\"dropTab !== 'coming'\"" in html and ":disabled=\"ex.status !== 'coming'\"" in html
    # Domaines : tri au menu sur téléphone ; la barre du bas ne remet plus le filtre d'état à zéro.
    assert 'class="dom-sort"' in html and "openDomains(); filter = null" not in html
    # Fiche : catégories et nature traduites même sans écran chargé, rang en clair.
    assert 'noun: "nom commun"' in js and 'letters: "Lettres"' in js and 'firstname: "prénom le plus donné"' in js


def test_cc_dns_extensions_in_explorer_top_fiche_and_veille(logged):
    import json as _json

    client = logged()
    state = client.app.state
    conn = state.conn
    at = "2026-10-18T03:00:00+00:00"
    for name, present, lost in [("assurance.ai", 1, None), ("insurance.ai", 0, None), ("loans.ai", 0, at)]:
        conn.execute("INSERT INTO cc_dns (name, present, checked_at, seen_at, lost_at) VALUES (?, ?, ?, ?, ?)",
                     (name, present, at, at, lost))
    state.store.set("ai_dns_status", _json.dumps({"finished_at": at, "checked": 3, "present": 1}))
    state.cc_dns["ai"].resume_lost()
    ranked = client.get("/api/explore/rank", params={"tld": "ai", "status": "all"}).json()
    assert "ai" in ranked["tlds"] and ranked["absent_label"] == "Absent du DNS, à vérifier"
    labels = {i["label"]: i["status_label"] for i in ranked["items"]}
    assert labels["assurance"] == "Enregistré" and labels["insurance"] == "Absent du DNS, à vérifier"
    top = client.get("/api/explore/top", params={"cat": "sector", "n": 50, "tld": "ai"}).json()
    cells = {i["label"]: i["cells"]["ai"]["state"] for i in top["items"]}
    assert cells["assurance"] == "pris" and cells["insurance"] == "probablement libre" and cells["mutuelle"] == "pas interrogé"
    fiche = client.get("/api/explore/fiche", params={"name": "insurance.ai"}).json()
    assert fiche["item"]["status_label"] == "Absent du DNS, à vérifier" and fiche["cc_dns"]["present"] is False
    # Sorti du DNS : confié à la Veille, avec son extension (puce .ai).
    row = conn.execute("SELECT * FROM drops WHERE name = 'loans.ai'").fetchone()
    assert row is not None and row["next_check_at"]
    conn.execute("UPDATE drops SET state = 'redemption', state_since = ? WHERE name = 'loans.ai'", (at,))
    veille = client.get("/api/drops").json()
    assert [(i["name"], i["tld"]) for i in veille["items"]] == [("loans.ai", "ai")]
    client.__exit__(None, None, None)

def test_interface_fiche_layout():
    """Fiche rangée : en-tête collant avec fermer en icône, carte décision, pictos sur une ligne avec menu « Plus »,
    dates clés résumées, détail en sections repliables ; plus de boutons SEO ni Réservé dans le corps."""
    from pathlib import Path

    static = Path(__file__).resolve().parent.parent / "app" / "static"
    js = (static / "app.js").read_text(encoding="utf-8")
    html = (static / "index.html").read_text(encoding="utf-8")
    css = (static / "style.css").read_text(encoding="utf-8")
    fiche = html.split('<template x-if="fiche">')[1].split('<template id="fiche-other">')[0]
    # En-tête : nom, une ligne d'état, bouton fermer « × » de 44 px avec son libellé accessible.
    assert 'class="fiche-close"' in fiche and 'aria-label="Fermer"' in fiche and ">Fermer<" not in fiche
    assert "ficheWhen(fiche)" in fiche and "width: 44px; height: 44px" in css
    # Ordre : en-tête, carte décision, pictos, dates clés, sections.
    order = [fiche.index(k) for k in ('class="fiche-head"', 'class="fiche-deal"', 'class="fiche-acts"', 'class="fiche-timeline"',
                                      *(f'fsec-title">{t}<' for t in ("Score", "Suivi", "Le nom", "Comparables et repères",
                                                                      "Autres extensions", "Registre et SEO")))]
    assert order == sorted(order)
    assert fiche.count('<details class="fsec') == 6 and fiche.count('class="fsec-sum"') >= 5
    # Tout l'avis IA dans la carte décision : deux notes, verdict, prix, pourquoi, commentaire, « Détail IA »
    # qui déroule la grille ; plus de section Avis IA en dessous.
    deal = fiche.split('class="fiche-deal"')[1].split("</section>")[0]
    for part in ("k.notes", "k.verdict", "k.prices", "k.why", "k.comment", "k.queue", "ficheGridHtml(k.grid)", ">Détail IA<"):
        assert part in deal, part
    assert 'aria-expanded="ficheAiOpen' in deal.replace(":aria-expanded", "aria-expanded").replace(" ? 'true'", "")
    assert 'fsec-title">Avis IA<' not in fiche and "ficheAiHtml" not in js + html
    # Pictos sur une ligne (colonnes égales) ; « Plus » ouvre un menu accessible avec les actions secondaires.
    assert "repeat(5, minmax(0, 1fr))" in css and 'aria-haspopup="menu"' in fiche and 'role="menu"' in fiche
    menu = fiche.split('id="fiche-menu"')[1].split("</div>")[0]
    for label in ("Analyse SEO", "Réservé par registre", "Voir dans Portefeuille"):
        assert label in menu, label
    # Arrêter le suivi : seulement par le picto « Suivi », pas en double dans le menu.
    assert "Arrêter le suivi" not in menu
    assert 'class="fiche-more"' not in fiche and "ficheMenuKey" in js and "ficheMenuClose(true)" in js
    # Picto IA en HTML, sans la petite coche ; dates clés résumées au-delà de 6.
    assert 'class="ia-mark"' in fiche and "ia-check" not in fiche + css
    assert "all.length <= 6" in js and "slice(-3)" in js and "Tout afficher" in fiche
    # Libellés en casse normale, au-dessus de la valeur sous 360 px ; panneau élargi sur ordinateur.
    assert ".fdl dt { color: var(--muted)" in css and "@media (max-width: 359px)" in css
    assert "clamp(400px, 42vw, 520px)" in css
    # Points d'entrée gardés.
    for entry in ("async openFiche(name", "    back() {", "async ficheReload()"):
        assert entry in js, entry

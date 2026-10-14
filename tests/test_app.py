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
        assert "httponly" in cookie and "samesite=strict" in cookie
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
    assert client.get("/api/explore/rank", params={"tld": "fr,xyz"}).status_code == 404
    csv = client.get("/api/explore/export", params={"tld": "fr,com", "status": "registered", "cat": "surname"}).text
    assert "navarro.fr" in csv and "navarro.com" in csv

    assert client.put("/api/settings/drops", json={"keep_days": 0}).status_code == 422
    assert client.put("/api/settings/drops", json={"keep_days": 45}).json() == {"keep_days": 45}
    assert client.put("/api/settings/fr", json={"enabled": True, "rate": 50}).status_code == 422
    assert client.put("/api/settings/fr", json={"enabled": True, "rate": 5}).json()["rate"] == 5
    settings = client.get("/api/settings").json()
    assert settings["drops"]["keep_days"] == 45 and settings["fr"]["rate"] == 5
    assert client.post("/api/drops/check", json={"name": "pas un nom"}).status_code == 422
    client.__exit__(None, None, None)


def test_views_weights_order_url(logged):
    client = logged()
    views = client.put("/api/explore/views", json={"views": [{"name": "Prénoms top", "filters": {"cat": "firstname", "rank": 500}}, {"name": ""}]}).json()
    assert views == [{"name": "Prénoms top", "filters": {"cat": "firstname", "rank": "500"}}]
    assert client.get("/api/explore/views").json() == views
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
    conn.execute("INSERT INTO domains (name, added_at, state, registrar, nameservers, epp_statuses, last_checked_at) "
                 "VALUES ('velo.fr', '2026-10-01T00:00:00+00:00', 'available', NULL, '[]', '[]', '2026-10-14T00:00:00+00:00')")
    fiche = client.get("/api/explore/fiche", params={"name": "velo.fr"}).json()
    assert fiche["item"]["status_label"] == "Supprimé, non repris" and fiche["item"]["score"] > 0
    assert fiche["domain_id"] and fiche["rdap"]["available"] is True and "word" in fiche["categories"]
    assert client.get("/api/explore/fiche", params={"name": "pas un nom"}).status_code == 422
    client.__exit__(None, None, None)

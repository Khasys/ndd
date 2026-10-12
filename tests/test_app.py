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


def test_check_all_also_refreshes_version(logged, monkeypatch):
    client = logged()
    calls = []

    async def fake():
        calls.append(1)

    client.app.state.refresh_latest = fake
    assert client.post("/api/check-all").status_code == 202
    client.get("/health")
    assert calls == [1]
    client.__exit__(None, None, None)


def test_appearance_is_rendered_server_side(logged):
    client = logged()
    page = client.get("/").text
    assert '<html lang="fr">' in page and '<span class="appname">domain-watch</span>' in page
    cfg = client.put("/api/settings/appearance", json={"app_name": "  Mes   domaines ", "theme": "dark"}).json()
    assert cfg["appearance"] == {"app_name": "Mes domaines", "theme": "dark", "logo": False}
    page = client.get("/").text
    assert '<html lang="fr" data-theme="dark">' in page
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

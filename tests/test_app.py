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

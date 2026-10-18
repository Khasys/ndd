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
    assert views == [{"name": "Prénoms top", "filters": {"cat": "firstname", "rank": "500"}}]
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

"""Jetons d'accès : plusieurs par type, libellé, dernière utilisation, révocation, reprise des anciens jetons."""

import sqlite3

from app import db, tokens

from test_app import PASSWORD, client_factory, logged  # noqa: F401  fixtures partagées


def test_tokens_list_rename_and_revoke(logged):
    client = logged()
    a = client.post("/api/tokens", json={"kind": "read", "label": "  Script   perso "}).json()
    b = client.post("/api/tokens", json={"kind": "mcp", "label": ""}).json()
    assert a["token"].startswith("dwr_") and "url" not in a and "/mcp/" in b["url"]
    listing = client.get("/api/tokens").json()
    assert set(listing["kinds"]) == {"ai", "read", "mcp"}
    labels = {t["id"]: (t["label"], t["kind_label"], t["last_used_at"]) for t in listing["items"]}
    assert labels[a["id"]] == ("Script perso", "Lecture seule", None)
    assert labels[b["id"]][0] == "Connecteur MCP"  # libellé vide : celui du type
    assert client.post("/api/tokens", json={"kind": "admin"}).status_code == 422
    assert client.put(f"/api/tokens/{a['id']}", json={"label": "Lecture NAS"}).status_code == 204
    assert client.put("/api/tokens/9999", json={"label": "x"}).status_code == 404
    # Un jeton de lecture lit, n'écrit pas, et sa dernière utilisation est notée.
    client.cookies.clear()
    headers = {"Authorization": f"Bearer {a['token']}"}
    assert client.get("/api/meta", headers=headers).status_code == 200
    assert client.get("/api/ai/pending", headers=headers).status_code == 401
    assert client.get("/api/settings", headers=headers).status_code == 401
    client.post("/login", data={"username": "moi", "password": PASSWORD})
    item = next(t for t in client.get("/api/tokens").json()["items"] if t["id"] == a["id"])
    assert item["label"] == "Lecture NAS" and item["last_used_at"]
    assert client.delete(f"/api/tokens/{a['id']}").status_code == 204
    assert client.delete(f"/api/tokens/{a['id']}").status_code == 404
    client.cookies.clear()
    assert client.get("/api/meta", headers=headers).status_code == 401
    client.__exit__(None, None, None)


def test_calendar_link_is_listed_and_revocable(logged):
    client = logged()
    url = client.post("/api/settings/calendar").json()["calendar_url"]
    row = next(t for t in client.get("/api/tokens").json()["items"] if t["id"] == "calendar")
    assert row["kind"] == "calendar" and row["created_at"] and row["last_used_at"] is None
    path = "/" + url.split("://", 1)[1].split("/", 1)[1]
    assert client.get(path).status_code == 200
    assert next(t for t in client.get("/api/tokens").json()["items"] if t["id"] == "calendar")["last_used_at"]
    assert client.delete("/api/tokens/calendar").status_code == 204
    assert client.get(path).status_code == 404
    assert all(t["id"] != "calendar" for t in client.get("/api/tokens").json()["items"])
    assert client.delete("/api/tokens/abc").status_code == 404
    client.__exit__(None, None, None)


def test_legacy_ai_button_replaces_only_ai_tokens(logged):
    client = logged()
    mcp_token = client.post("/api/tokens", json={"kind": "mcp"}).json()["token"]
    first = client.post("/api/settings/ai-token").json()["token"]
    second = client.post("/api/settings/ai-token").json()["token"]
    conn = client.app.state.conn
    assert tokens.check(conn, ("ai",), first) is None and tokens.check(conn, ("ai",), second)
    assert tokens.check(conn, ("mcp",), mcp_token)
    assert client.get("/api/ai/status").json()["token_set"] is True
    client.__exit__(None, None, None)


def test_old_single_tokens_are_kept_by_the_migration(tmp_path):
    """Les jetons d'avant (empreintes dans settings) passent dans la table : rien à refaire côté tâche ou connecteur."""
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path, isolation_level=None)
    for sql in sorted(db.MIGRATIONS_DIR.glob("*.sql")):
        if sql.name >= "041":
            break
        conn.executescript(sql.read_text())
    conn.execute("INSERT INTO settings (key, value) VALUES ('ai_token_hash', ?), ('mcp_token_hash', ?)",
                 (tokens.hash_token("dw_ancien"), tokens.hash_token("dwm_ancien")))
    conn.executescript((db.MIGRATIONS_DIR / "041_tokens.sql").read_text())
    assert tokens.check(conn, ("ai",), "dw_ancien") and tokens.check(conn, ("mcp",), "dwm_ancien")
    assert conn.execute("SELECT COUNT(*) FROM settings WHERE key LIKE '%token_hash'").fetchone()[0] == 0
    assert [t["label"] for t in tokens.listing(conn)] == ["Connecteur MCP", "Tâche de notation"]

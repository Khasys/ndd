"""Connecteur MCP en lecture seule : jeton, protocole, outils, rien d'écrit, jeton masqué dans les journaux."""

import json
import logging

import pytest

from app import mcp, tokens
from app.main import MCP_READS, READ_PATHS, _MaskMcp, _redact

from test_app import PASSWORD, client_factory, logged  # noqa: F401  fixtures partagées


def rpc(method, params=None, ident=1):
    message = {"jsonrpc": "2.0", "method": method}
    if ident is not None:
        message["id"] = ident
    if params is not None:
        message["params"] = params
    return message


@pytest.fixture
def connected(logged):
    client = logged()
    url = client.post("/api/tokens", json={"kind": "mcp", "label": "ChatGPT"}).json()["url"]
    path = "/" + url.split("://", 1)[1].split("/", 1)[1]
    client.cookies.clear()  # l'assistant n'a pas de session : seule l'adresse secrète compte
    yield client, path
    client.__exit__(None, None, None)


def test_every_tool_reads_a_read_only_screen():
    assert set(MCP_READS) == {mcp.request_for(t["name"], {"query": "a", "id": "a.fr", "focus": "freed", "list": "job"})[0]
                              for t in mcp.tools()}
    assert all(READ_PATHS.match(path) for path in MCP_READS)
    assert all(t["annotations"]["readOnlyHint"] for t in mcp.tools())


def test_token_is_shown_once_and_only_its_hash_is_kept(logged):
    client = logged()
    res = client.post("/api/tokens", json={"kind": "mcp", "label": "ChatGPT"}).json()
    assert res["token"].startswith("dwm_") and res["url"].endswith("/mcp/" + res["token"])
    stored = client.app.state.conn.execute("SELECT hash FROM tokens WHERE id = ?", (res["id"],)).fetchone()[0]
    assert stored == tokens.hash_token(res["token"]) and res["token"] not in json.dumps(client.get("/api/tokens").json())
    assert client.get("/api/settings").json()["mcp"] == {"enabled": True}
    # Le jeton de notation IA n'ouvre pas le connecteur, et inversement.
    ai_token = client.post("/api/settings/ai-token").json()["token"]
    assert tokens.check(client.app.state.conn, ("mcp",), ai_token) is None
    assert tokens.check(client.app.state.conn, ("ai",), res["token"]) is None
    client.__exit__(None, None, None)


def test_wrong_missing_or_revoked_token_is_refused(connected):
    client, path = connected
    assert client.post(path, json=rpc("ping")).status_code == 200
    assert client.post("/mcp/dwm_faux", json=rpc("ping")).status_code == 401
    assert client.post("/mcp", json=rpc("ping")).status_code == 401
    token = path.rsplit("/", 1)[1]
    assert client.post("/mcp", json=rpc("ping"), headers={"Authorization": f"Bearer {token}"}).status_code == 200
    # Révoquer demande une session : l'adresse secrète ne suffit pas.
    token_id = client.app.state.conn.execute("SELECT id FROM tokens WHERE kind = 'mcp'").fetchone()[0]
    assert client.delete(f"/api/tokens/{token_id}").status_code == 401
    client.post("/login", data={"username": "moi", "password": PASSWORD})
    assert client.delete(f"/api/tokens/{token_id}").status_code == 204
    client.cookies.clear()
    assert client.post(path, json=rpc("ping")).status_code == 401


def test_secret_url_opens_nothing_else(connected):
    client, path = connected
    token = path.rsplit("/", 1)[1]
    headers = {"Authorization": f"Bearer {token}"}
    assert client.get("/api/settings", headers=headers).status_code == 401
    assert client.get("/api/explore/fiche", params={"name": "velo.fr"}, headers=headers).status_code == 401
    assert client.post("/api/tokens", json={"kind": "ai"}, headers=headers).status_code == 401
    assert client.get("/api/tokens", headers=headers).status_code == 401
    assert client.get(path).status_code == 405


def test_handshake_and_tool_list(connected):
    client, path = connected
    init = client.post(path, json=rpc("initialize", {"protocolVersion": "2025-03-26", "capabilities": {},
                                                     "clientInfo": {"name": "test", "version": "1"}})).json()
    assert init["result"]["protocolVersion"] == "2025-03-26"
    assert init["result"]["capabilities"] == {"tools": {"listChanged": False}}
    assert client.post(path, json=rpc("notifications/initialized", ident=None)).status_code == 202
    names = [t["name"] for t in client.post(path, json=rpc("tools/list")).json()["result"]["tools"]]
    assert names[:2] == ["search", "fetch"] and len(names) == 7
    assert client.post(path, json=rpc("resources/list")).json()["error"]["code"] == -32601
    bad = client.post(path, content=b"{pas du json", headers={"Content-Type": "application/json"})
    assert bad.status_code == 400 and bad.json()["error"]["code"] == -32700


def test_tools_return_light_json(connected):
    client, path = connected
    conn = client.app.state.conn
    conn.execute("INSERT INTO fr_names (label, created_on, removed_on) VALUES ('velo', '2010-01-01', '2025-01-01')")
    client.app.state.explorer.touch("velo.fr")
    fiche = client.post(path, json=rpc("tools/call", {"name": "fetch", "arguments": {"id": "velo.fr"}})).json()["result"]
    assert fiche["isError"] is False
    text = fiche["content"][0]["text"]
    assert '"id":"velo.fr"' in text and '"rdap":' not in text and "null" not in text
    found = client.post(path, json=rpc("tools/call", {"name": "search", "arguments": {"query": "vel", "tld": "fr"}})).json()
    assert '"id":"velo.fr"' in found["result"]["content"][0]["text"]
    for name, args in (("today", {}), ("drops", {"focus": "coming"}), ("top_list", {"list": "word", "tld": "fr"}),
                       ("ai_rated", {"verdict": "take"}), ("watched", {})):
        result = client.post(path, json=rpc("tools/call", {"name": name, "arguments": args})).json()["result"]
        assert result["isError"] is False, (name, result)
    # Argument refusé par l'écran : erreur lisible pour l'assistant, pas une panne.
    wrong = client.post(path, json=rpc("tools/call", {"name": "today", "arguments": {"period": "1an"}})).json()["result"]
    assert wrong["isError"] is True and "Période inconnue" in wrong["content"][0]["text"]
    missing = client.post(path, json=rpc("tools/call", {"name": "fetch", "arguments": {}})).json()["result"]
    assert missing["isError"] is True
    unknown = client.post(path, json=rpc("tools/call", {"name": "add_domain", "arguments": {"name": "x.fr"}})).json()
    assert unknown["error"]["code"] == -32602


def test_nothing_is_written(connected):
    client, path = connected
    conn = client.app.state.conn
    before = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("domains", "decisions", "portfolio")}
    for name, args in (("fetch", {"id": "velo.fr"}), ("search", {"query": "velo"}), ("watched", {})):
        client.post(path, json=rpc("tools/call", {"name": name, "arguments": args}))
    assert before == {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in before}


def test_compact_drops_noise_and_long_text_is_cut():
    assert mcp.compact({"a": None, "b": [], "rdap": {"x": 1}, "c": {"d": False}, "e": 0, "f": "x"}) == {"e": 0, "f": "x"}
    big = {"items": [{"name": f"nom{i}.fr", "comment": "x" * 500} for i in range(400)]}
    text = mcp.text_of(big)
    assert len(text) <= mcp.MAX_TEXT and text.startswith('{"items":[')


def test_token_is_masked_in_logs():
    assert _redact("POST /mcp/dwm_abc-123 HTTP/1.1") == "POST /mcp/<masqué> HTTP/1.1"
    record = logging.LogRecord("uvicorn.access", logging.INFO, "", 0, '%s - "%s %s HTTP/%s" %d',
                               ("1.2.3.4", "POST", "/mcp/dwm_secret", "1.1", 200), None)
    _MaskMcp().filter(record)
    assert "dwm_secret" not in record.getMessage()

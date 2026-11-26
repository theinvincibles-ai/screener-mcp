"""
Offline tests for the single-user OAuth sign-in on the HTTP transport.

They drive the whole flow a claude.ai connector uses (register, authorize,
passphrase page, token, refresh, call /mcp) against an in-process Starlette
app. No network, no credentials; state goes to a temp directory.
"""

import base64
import hashlib
import json
import secrets
from urllib.parse import parse_qs, urlparse

import pytest
from mcp.server.mcpserver import MCPServer
from starlette.testclient import TestClient

from screener_mcp import server
from screener_mcp.core import oauth
from screener_mcp.core.oauth import LOGIN_PATH, auth_from_env, check_http_auth_env

BASE = "http://localhost:8000"
PASSPHRASE = "correct horse battery"
REDIRECT = "https://claude.ai/api/mcp/auth_callback"


def _app(state_path, monkeypatch):
    monkeypatch.setenv("MCP_AUTH_PASSPHRASE", PASSPHRASE)
    monkeypatch.setenv("MCP_PUBLIC_URL", BASE)
    provider, settings = auth_from_env(state_path)
    app = MCPServer("t", auth_server_provider=provider, auth=settings)

    @app.tool()
    def ping() -> str:
        return "pong"

    app.custom_route(LOGIN_PATH, methods=["GET", "POST"])(provider.login)
    return app.streamable_http_app()


def _register(c):
    r = c.post("/register", json={
        "redirect_uris": [REDIRECT], "client_name": "Claude",
        "token_endpoint_auth_method": "none", "grant_types": ["authorization_code", "refresh_token"],
    })
    assert r.status_code == 201, r.text
    return r.json()["client_id"]


def _start_login(c, client_id):
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    r = c.get("/authorize", params={
        "response_type": "code", "client_id": client_id, "redirect_uri": REDIRECT,
        "code_challenge": challenge, "code_challenge_method": "S256", "state": "xyz",
    }, follow_redirects=False)
    assert r.status_code == 302, r.text
    login = urlparse(r.headers["location"])
    assert login.path == LOGIN_PATH
    return parse_qs(login.query)["req"][0], verifier


def _sign_in(c, req, passphrase=PASSPHRASE):
    return c.post(LOGIN_PATH, content=f"req={req}&passphrase={passphrase.replace(' ', '+')}",
                  headers={"content-type": "application/x-www-form-urlencoded"}, follow_redirects=False)


def _tokens(c, client_id):
    req, verifier = _start_login(c, client_id)
    page = c.get(LOGIN_PATH, params={"req": req})
    assert "Claude" in page.text and "claude.ai" in page.text
    r = _sign_in(c, req)
    assert r.status_code == 302
    back = urlparse(r.headers["location"])
    q = parse_qs(back.query)
    assert f"{back.scheme}://{back.netloc}{back.path}" == REDIRECT and q["state"] == ["xyz"]
    t = c.post("/token", data={
        "grant_type": "authorization_code", "code": q["code"][0], "redirect_uri": REDIRECT,
        "client_id": client_id, "code_verifier": verifier,
    })
    assert t.status_code == 200, t.text
    return t.json()


def _call_mcp(c, token=None):
    headers = {"accept": "application/json, text/event-stream", "content-type": "application/json"}
    if token:
        headers["authorization"] = f"Bearer {token}"
    return c.post("/mcp", headers=headers, json={
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}},
    })


def test_full_sign_in_flow(tmp_path, monkeypatch):
    with TestClient(_app(tmp_path / "oauth.json", monkeypatch), base_url=BASE) as c:
        assert _call_mcp(c).status_code == 401
        meta = c.get("/.well-known/oauth-protected-resource/mcp")
        assert meta.status_code == 200 and meta.json()["resource"] == f"{BASE}/mcp"

        client_id = _register(c)
        tok = _tokens(c, client_id)
        assert _call_mcp(c, tok["access_token"]).status_code == 200
        assert _call_mcp(c, "not-a-token").status_code == 401

        # Refresh rotates: the new pair works, the old refresh token doesn't.
        r = c.post("/token", data={"grant_type": "refresh_token", "refresh_token": tok["refresh_token"],
                                   "client_id": client_id})
        assert r.status_code == 200, r.text
        assert _call_mcp(c, r.json()["access_token"]).status_code == 200
        again = c.post("/token", data={"grant_type": "refresh_token", "refresh_token": tok["refresh_token"],
                                       "client_id": client_id})
        assert again.status_code == 400


def test_wrong_passphrase_and_lockout(tmp_path, monkeypatch):
    with TestClient(_app(tmp_path / "oauth.json", monkeypatch), base_url=BASE) as c:
        client_id = _register(c)
        req, _ = _start_login(c, client_id)
        for _ in range(oauth.MAX_FAILURES):
            assert _sign_in(c, req, "wrong passphrase").status_code == 401
        # Locked out now, even with the right passphrase.
        assert _sign_in(c, req).status_code == 429


def test_expired_or_unknown_login_link(tmp_path, monkeypatch):
    with TestClient(_app(tmp_path / "oauth.json", monkeypatch), base_url=BASE) as c:
        assert c.get(LOGIN_PATH, params={"req": "nope"}).status_code == 400
        assert _sign_in(c, "nope").status_code == 400


def test_tokens_survive_restart_and_are_stored_hashed(tmp_path, monkeypatch):
    state = tmp_path / "oauth.json"
    with TestClient(_app(state, monkeypatch), base_url=BASE) as c:
        tok = _tokens(c, _register(c))
    saved = state.read_text(encoding="utf-8")
    assert tok["access_token"] not in saved and tok["refresh_token"] not in saved
    assert PASSPHRASE not in saved
    assert json.loads(saved)["clients"]

    with TestClient(_app(state, monkeypatch), base_url=BASE) as c:
        assert _call_mcp(c, tok["access_token"]).status_code == 200


def test_default_server_has_no_auth():
    # Without the env vars, the shipped server is unchanged (stdio users unaffected).
    assert server._AUTH is None
    assert server.mcp.settings.auth is None


@pytest.mark.parametrize("env, ok", [
    ({}, True),
    ({"MCP_PUBLIC_URL": "https://x.example"}, False),                                   # public without sign-in
    ({"MCP_AUTH_PASSPHRASE": PASSPHRASE}, False),                                       # sign-in without URL
    ({"MCP_AUTH_PASSPHRASE": "short", "MCP_PUBLIC_URL": "https://x.example"}, False),
    ({"MCP_AUTH_PASSPHRASE": PASSPHRASE, "MCP_PUBLIC_URL": "http://x.example"}, False),
    ({"MCP_AUTH_PASSPHRASE": PASSPHRASE, "MCP_PUBLIC_URL": "https://x.example"}, True),
])
def test_check_http_auth_env(monkeypatch, env, ok):
    monkeypatch.delenv("MCP_AUTH_PASSPHRASE", raising=False)
    monkeypatch.delenv("MCP_PUBLIC_URL", raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    assert (check_http_auth_env() is None) is ok

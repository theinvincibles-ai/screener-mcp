"""FYERS live prices: offline, against mocked FYERS replies (no network, no real keys)."""

import json
import os
import stat
from datetime import datetime
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from starlette.applications import Starlette
from starlette.routing import Route

from screener_mcp import server
from screener_mcp.core import fyers
from screener_mcp.core.fyers_routes import make_routes
from screener_mcp.core.oauth import PassphraseOAuthProvider
from screener_mcp.tools import portfolio as pf

PUBLIC = "https://my-mac.example.ts.net"


@pytest.fixture
def fy(tmp_path, monkeypatch):
    """FYERS configured with dummy keys, token file in a temp dir, fresh caches."""
    monkeypatch.setenv("FYERS_APP_ID", "TESTAPP-100")
    monkeypatch.setenv("FYERS_SECRET_KEY", "not-a-real-secret")
    monkeypatch.setenv("MCP_PUBLIC_URL", PUBLIC)
    monkeypatch.setattr(fyers, "TOKEN_PATH", tmp_path / "fyers_token.json")
    monkeypatch.setattr(fyers, "_MIN_INTERVAL", 0)
    fyers._quote_cache.clear()
    fyers._pending_states.clear()
    return tmp_path


def _quote(lp, prev=None, name="X"):
    return {"lp": lp, "ch": 1.0, "chp": 0.5, "open_price": lp, "high_price": lp, "low_price": lp,
            "prev_close_price": prev if prev is not None else lp, "volume": 1000, "tt": "1795680000",
            "short_name": name}


def _mock_quotes(monkeypatch, handler):
    """Route every FYERS call through `handler(request) -> httpx.Response`."""
    real = httpx.AsyncClient

    def factory(*a, **k):
        k["transport"] = httpx.MockTransport(handler)
        return real(*a, **k)

    monkeypatch.setattr(fyers.httpx, "AsyncClient", factory)


def test_token_expires_at_next_0630_ist():
    ist = fyers.IST
    early = datetime(2026, 11, 26, 5, 0, tzinfo=ist).timestamp()
    late = datetime(2026, 11, 26, 7, 0, tzinfo=ist).timestamp()
    assert datetime.fromtimestamp(fyers.next_expiry(early), ist) == datetime(2026, 11, 26, 6, 30, tzinfo=ist)
    assert datetime.fromtimestamp(fyers.next_expiry(late), ist) == datetime(2026, 11, 27, 6, 30, tzinfo=ist)


def test_token_is_owner_only_and_expires(fy):
    tok = fyers.save_token("abc")
    assert stat.S_IMODE(os.stat(fyers.TOKEN_PATH).st_mode) == 0o600
    assert fyers.load_token()["access_token"] == "abc"
    assert fyers.load_token(now=tok["expires_at"] + 1) is None


async def test_not_configured_and_not_signed_in_errors(fy, monkeypatch):
    monkeypatch.delenv("FYERS_APP_ID")
    env = await server.get_live_quote(["TCS"])
    assert env["status"] == "error" and env["error"]["type"] == "fyers_not_configured"

    monkeypatch.setenv("FYERS_APP_ID", "TESTAPP-100")
    env = await server.get_live_quote(["TCS"])
    assert env["error"]["type"] == "fyers_sign_in_required"
    assert env["error"]["sign_in_url"] == f"{PUBLIC}/fyers/login"


async def test_live_quote_maps_symbols_and_reports_unknown_ones(fy, monkeypatch):
    fyers.save_token("tok123")
    seen = {}

    def handler(request):
        seen["auth"] = request.headers["Authorization"]
        seen["symbols"] = request.url.params["symbols"]
        return httpx.Response(200, json={"s": "ok", "code": 200, "d": [
            {"n": "NSE:TCS-EQ", "s": "ok", "v": _quote(2640.3, 2600.0, "TCS-EQ")},
            {"n": "NSE:NOPE-EQ", "s": "error", "v": {"errmsg": "invalid symbol"}},
        ]})

    _mock_quotes(monkeypatch, handler)
    env = await server.get_live_quote(["tcs", "NOPE"])
    assert seen == {"auth": "TESTAPP-100:tok123", "symbols": "NSE:TCS-EQ,NSE:NOPE-EQ"}
    assert env["status"] == "partial" and env["missing_fields"] == ["NOPE.ltp"]
    q = env["data"]["quotes"][0]
    assert q["symbol"] == "TCS" and q["ltp"] == 2640.3 and q["prev_close"] == 2600.0
    assert q["source"] == "fyers_live" and q["quote_time"].endswith("+05:30")


async def test_implausible_move_is_flagged(fy, monkeypatch):
    fyers.save_token("tok123")
    _mock_quotes(monkeypatch, lambda r: httpx.Response(200, json={"s": "ok", "code": 200, "d": [
        {"n": "NSE:TCS-EQ", "s": "ok", "v": _quote(4000.0, 2600.0)}]}))
    env = await server.get_live_quote(["TCS"])
    assert any("beyond any NSE price band" in w for w in env["warnings"])


async def test_expired_token_is_cleared_and_asks_to_sign_in(fy, monkeypatch):
    fyers.save_token("old")
    _mock_quotes(monkeypatch, lambda r: httpx.Response(200, json={"s": "error", "code": -16, "message": "token expired"}))
    env = await server.get_live_quote(["TCS"])
    assert env["error"]["type"] == "fyers_sign_in_required"
    assert not fyers.TOKEN_PATH.exists()


async def test_portfolio_uses_live_prices_and_falls_back(fy, monkeypatch):
    monkeypatch.setattr(pf, "_PORTFOLIO_PATH", fy / "p.json")
    pf._save({"holdings": {"TCS": {"quantity": 10, "avg_price": 2500}, "OLDCO": {"quantity": 1, "avg_price": 100}}})
    fyers.save_token("tok123")
    _mock_quotes(monkeypatch, lambda r: httpx.Response(200, json={"s": "ok", "code": 200, "d": [
        {"n": "NSE:TCS-EQ", "s": "ok", "v": _quote(2600.0)},
        {"n": "NSE:OLDCO-EQ", "s": "error", "v": {"errmsg": "invalid symbol"}}]}))

    async def screener_price(sym):
        assert sym == "OLDCO", "TCS should come from FYERS, not Screener"
        return 120.0

    monkeypatch.setattr(pf, "_live_price", screener_price)
    env = await server.get_portfolio()
    by_sym = {h["symbol"]: h for h in env["data"]["holdings"]}
    assert by_sym["TCS"]["ltp"] == 2600.0 and by_sym["TCS"]["price_source"] == "fyers_live"
    assert by_sym["OLDCO"]["ltp"] == 120.0 and by_sym["OLDCO"]["price_source"] == "screener_delayed"


async def test_portfolio_says_when_fyers_needs_sign_in(fy, monkeypatch):
    monkeypatch.setattr(pf, "_PORTFOLIO_PATH", fy / "p.json")
    pf._save({"holdings": {"TCS": {"quantity": 1, "avg_price": 2500}}})

    async def screener_price(sym):
        return 2550.0

    monkeypatch.setattr(pf, "_live_price", screener_price)
    env = await server.get_portfolio()
    assert env["data"]["holdings"][0]["price_source"] == "screener_delayed"
    assert any("/fyers/login" in w for w in env["warnings"])


async def test_exchange_auth_code_sends_hash_and_stores_token(fy):
    sent = {}

    def handler(request):
        sent.update(json.loads(request.content))
        return httpx.Response(200, json={"s": "ok", "code": 200, "access_token": "new-token"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await fyers.exchange_auth_code("the-code", http=client)
    assert sent["grant_type"] == "authorization_code" and sent["code"] == "the-code"
    assert sent["appIdHash"] == fyers.app_id_hash() and len(sent["appIdHash"]) == 64
    assert fyers.load_token()["access_token"] == "new-token"


async def test_sign_in_pages_need_passphrase_and_a_fresh_state(fy, monkeypatch):
    auth = PassphraseOAuthProvider("correct horse battery", PUBLIC, state_path=fy / "oauth.json")
    login, callback = make_routes(auth)
    app = Starlette(routes=[Route("/fyers/login", login, methods=["GET", "POST"]),
                            Route("/fyers/callback", callback)])

    async def fake_exchange(code, http=None):
        assert code == "abc"
        return fyers.save_token("tok")

    monkeypatch.setattr(fyers, "exchange_auth_code", fake_exchange)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=PUBLIC) as c:
        assert (await c.get("/fyers/login")).status_code == 200
        assert (await c.post("/fyers/login", data={"passphrase": "wrong"})).status_code == 401
        r = await c.post("/fyers/login", data={"passphrase": "correct horse battery"})
        assert r.status_code == 302
        q = parse_qs(urlparse(r.headers["location"]).query)
        assert q["client_id"] == ["TESTAPP-100"] and q["redirect_uri"] == [f"{PUBLIC}/fyers/callback"]
        state = q["state"][0]

        assert (await c.get("/fyers/callback", params={"auth_code": "abc", "state": "forged"})).status_code == 400
        ok = await c.get("/fyers/callback", params={"auth_code": "abc", "state": state})
        assert ok.status_code == 200 and "FYERS connected" in ok.text
        assert fyers.load_token()["access_token"] == "tok"
        # a state works once
        assert (await c.get("/fyers/callback", params={"auth_code": "abc", "state": state})).status_code == 400

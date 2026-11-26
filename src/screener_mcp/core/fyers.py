"""
Live quotes from FYERS (API v3), using the user's own data-only API app.

Optional: everything here is off unless FYERS_APP_ID, FYERS_SECRET_KEY and
MCP_PUBLIC_URL are set, and callers fall back to Screener's delayed price.

Sign-in. FYERS access tokens expire every day at 06:30 IST, so the user signs
in once a day through two routes on this server:

  /fyers/login     passphrase page (the connector's own passphrase), then a
                   redirect to FYERS's login with a one-time `state`
  /fyers/callback  FYERS sends the browser back here with `auth_code` + `state`;
                   the code is swapped for today's access token, which is kept
                   in ~/.screener-mcp/fyers_token.json (owner-only)

Only quote and history endpoints are called: the app is data-only and this
module has no order code. The app secret and token are never logged.
"""

import asyncio
import hashlib
import json
import logging
import os
import secrets
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import urlencode

import httpx

from .envelope import ToolError

logger = logging.getLogger(__name__)

API_BASE = "https://api-t1.fyers.in"
LOGIN_START_PATH = "/fyers/login"
CALLBACK_PATH = "/fyers/callback"
TOKEN_PATH = Path.home() / ".screener-mcp" / "fyers_token.json"

IST = timezone(timedelta(hours=5, minutes=30))
QUOTE_BATCH = 50              # Standard plan: 50 symbols per quotes request
_MIN_INTERVAL = 0.25          # Standard plan: 5 data requests/second
_QUOTE_TTL = 5.0              # seconds a quote is reused, so bursts don't spend the daily quota
_STATE_TTL = 600              # a login must finish within 10 minutes
_TOKEN_ERROR_CODES = {-8, -15, -16, -17}  # expired / invalid token (FYERS docs)
# A move this large against the previous close is beyond any NSE price band,
# so it's more likely bad data (or a symbol mix-up) than a real move.
SUSPICIOUS_MOVE_PCT = 20.0


class FyersError(Exception):
    def __init__(self, message: str, kind: str = "fyers_error"):
        super().__init__(message)
        self.message = message
        self.kind = kind


# ─── configuration and token ─────────────────────────────────────────────────

def _app_id() -> str:
    return os.getenv("FYERS_APP_ID", "").strip()


def _secret() -> str:
    return os.getenv("FYERS_SECRET_KEY", "").strip()


def _public_url() -> str:
    return os.getenv("MCP_PUBLIC_URL", "").strip().rstrip("/")


def configured() -> bool:
    return bool(_app_id() and _secret() and _public_url())


def redirect_uri() -> str:
    return f"{_public_url()}{CALLBACK_PATH}"


def sign_in_url() -> str:
    return f"{_public_url()}{LOGIN_START_PATH}"


def next_expiry(now: Optional[float] = None) -> float:
    """The next 06:30 IST after `now`: when a token issued now stops working."""
    dt = datetime.fromtimestamp(now if now is not None else time.time(), IST)
    expiry = dt.replace(hour=6, minute=30, second=0, microsecond=0)
    if expiry <= dt:
        expiry += timedelta(days=1)
    return expiry.timestamp()


def load_token(now: Optional[float] = None) -> Optional[dict]:
    """Today's token, or None if there is none or it has expired."""
    try:
        tok = json.loads(TOKEN_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(tok, dict) or not tok.get("access_token"):
        return None
    if tok.get("expires_at", 0) <= (now if now is not None else time.time()):
        return None
    return tok


def save_token(access_token: str, now: Optional[float] = None) -> dict:
    now = now if now is not None else time.time()
    tok = {"access_token": access_token, "issued_at": now, "expires_at": next_expiry(now)}
    TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = TOKEN_PATH.with_name(TOKEN_PATH.name + ".tmp")
    tmp.write_text(json.dumps(tok), encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, TOKEN_PATH)
    return tok


def clear_token() -> None:
    TOKEN_PATH.unlink(missing_ok=True)


def status() -> dict:
    """What the user needs to know: set up? signed in today? until when?"""
    if not configured():
        return {
            "configured": False,
            "connected": False,
            "how_to_set_up": "Set FYERS_APP_ID, FYERS_SECRET_KEY and MCP_PUBLIC_URL (README: 'Live prices from FYERS').",
        }
    tok = load_token()
    out = {"configured": True, "connected": bool(tok), "sign_in_url": sign_in_url()}
    if tok:
        out["connected_until"] = datetime.fromtimestamp(tok["expires_at"], IST).isoformat(timespec="minutes")
    return out


def require_connected() -> dict:
    """The token, or a ToolError telling the user exactly how to connect."""
    if not configured():
        raise ToolError(
            "FYERS live prices aren't set up on this server. Set FYERS_APP_ID, FYERS_SECRET_KEY "
            "and MCP_PUBLIC_URL (README: 'Live prices from FYERS').",
            "fyers_not_configured",
        )
    tok = load_token()
    if not tok:
        raise ToolError(
            f"FYERS needs today's sign-in (tokens expire daily at 6:30 AM IST). Open {sign_in_url()} "
            "to sign in, then ask again.",
            "fyers_sign_in_required",
            sign_in_url=sign_in_url(),
        )
    return tok


# ─── sign-in flow ─────────────────────────────────────────────────────────────

_pending_states: dict[str, float] = {}


def start_login() -> str:
    """FYERS login URL with a fresh one-time state (call after the passphrase check)."""
    now = time.time()
    for s in [s for s, exp in _pending_states.items() if exp < now]:
        del _pending_states[s]
    state = secrets.token_urlsafe(24)
    _pending_states[state] = now + _STATE_TTL
    query = urlencode({
        "client_id": _app_id(), "redirect_uri": redirect_uri(),
        "response_type": "code", "state": state,
    })
    return f"{API_BASE}/api/v3/generate-authcode?{query}"


def take_state(state: str) -> bool:
    """True once for a state this server issued in the last 10 minutes."""
    exp = _pending_states.pop(state or "", None)
    return exp is not None and exp >= time.time()


def app_id_hash() -> str:
    return hashlib.sha256(f"{_app_id()}:{_secret()}".encode("utf-8")).hexdigest()


async def exchange_auth_code(auth_code: str, http: Optional[httpx.AsyncClient] = None) -> dict:
    """Swap the one-time auth code for today's access token and store it."""
    body = {"grant_type": "authorization_code", "appIdHash": app_id_hash(), "code": auth_code}
    async with _maybe_client(http) as client:
        resp = await client.post(f"{API_BASE}/api/v3/validate-authcode", json=body)
    try:
        data = resp.json()
    except ValueError:
        data = {}
    if resp.status_code != 200 or data.get("s") != "ok" or not data.get("access_token"):
        raise FyersError(f"FYERS refused the sign-in ({data.get('message') or f'HTTP {resp.status_code}'}).",
                         "sign_in_failed")
    return save_token(data["access_token"])


# ─── quotes ──────────────────────────────────────────────────────────────────

def fyers_symbol(symbol: str) -> str:
    """'TCS' → 'NSE:TCS-EQ'. A symbol already in FYERS form ('NSE:NIFTY50-INDEX') is kept."""
    s = symbol.strip().upper()
    return s if ":" in s else f"NSE:{s}-EQ"


class _maybe_client:
    """Use the given client, or open (and close) a short-lived one."""

    def __init__(self, client: Optional[httpx.AsyncClient]):
        self._given, self._own = client, None

    async def __aenter__(self) -> httpx.AsyncClient:
        if self._given is not None:
            return self._given
        self._own = httpx.AsyncClient(timeout=15.0)
        return self._own

    async def __aexit__(self, *exc):
        if self._own is not None:
            await self._own.aclose()


_lock = asyncio.Lock()
_last_request = 0.0
_quote_cache: dict[str, tuple[float, dict]] = {}


async def _paced_get(client: httpx.AsyncClient, path: str, params: dict, token: str) -> dict:
    global _last_request
    async with _lock:
        wait = _last_request + _MIN_INTERVAL - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)
        _last_request = time.monotonic()
    resp = await client.get(f"{API_BASE}{path}", params=params,
                            headers={"Authorization": f"{_app_id()}:{token}"})
    if resp.status_code == 429:
        raise FyersError("FYERS rate limit reached; using delayed prices for now.", "rate_limited")
    try:
        data = resp.json()
    except ValueError:
        raise FyersError(f"FYERS returned HTTP {resp.status_code} without data.", "upstream_error")
    if data.get("code") in _TOKEN_ERROR_CODES or resp.status_code == 401:
        clear_token()
        raise FyersError("FYERS sign-in has expired.", "sign_in_required")
    if data.get("s") != "ok":
        raise FyersError(f"FYERS error: {data.get('message') or resp.status_code}.", "upstream_error")
    return data


async def get_quotes(symbols: list[str], http: Optional[httpx.AsyncClient] = None) -> dict[str, dict]:
    """Live quotes keyed by the caller's symbol: {"TCS": {"ok": True, "v": {...}} | {"ok": False, "error": ...}}.

    Raises FyersError when FYERS can't be used at all (no token, expired,
    rate-limited, down); per-symbol problems come back as ok=False entries."""
    tok = load_token()
    if not configured() or not tok:
        raise FyersError("FYERS isn't connected.", "sign_in_required" if configured() else "not_configured")
    wanted = {s: fyers_symbol(s) for s in dict.fromkeys(symbols)}
    now = time.time()
    fresh = {fs: hit[1] for fs in wanted.values() if (hit := _quote_cache.get(fs)) and now - hit[0] < _QUOTE_TTL}
    todo = [fs for fs in dict.fromkeys(wanted.values()) if fs not in fresh]

    async with _maybe_client(http) as client:
        for i in range(0, len(todo), QUOTE_BATCH):
            batch = todo[i:i + QUOTE_BATCH]
            data = await _paced_get(client, "/data/quotes", {"symbols": ",".join(batch)}, tok["access_token"])
            got = {}
            for item in data.get("d") or []:
                name = item.get("n")
                if item.get("s") == "ok" and isinstance(item.get("v"), dict) and item["v"].get("lp") is not None:
                    got[name] = {"ok": True, "v": item["v"]}
                else:
                    v = item.get("v")
                    msg = (v.get("errmsg") if isinstance(v, dict) else None) or "no quote for this symbol"
                    got[name] = {"ok": False, "error": msg}
            for fs in batch:
                entry = got.get(fs, {"ok": False, "error": "not in FYERS's reply"})
                fresh[fs] = entry
                if entry["ok"]:
                    _quote_cache[fs] = (now, entry)
    return {s: fresh[fs] for s, fs in wanted.items()}


def quote_row(symbol: str, entry: dict) -> dict:
    """One quote in the server's own field names, with source and IST time."""
    v = entry["v"]
    tt = v.get("tt")
    try:
        as_of = datetime.fromtimestamp(int(tt), IST).isoformat(timespec="seconds") if tt else None
    except (TypeError, ValueError):
        as_of = None
    return {
        "symbol": symbol.upper(),
        "fyers_symbol": fyers_symbol(symbol),
        "name": v.get("description") or v.get("short_name"),
        "ltp": v.get("lp"),
        "change": v.get("ch"),
        "change_pct": v.get("chp"),
        "open": v.get("open_price"),
        "high": v.get("high_price"),
        "low": v.get("low_price"),
        "prev_close": v.get("prev_close_price"),
        "volume": v.get("volume"),
        "quote_time": as_of,
        "source": "fyers_live",
    }


def suspicious_move(row: dict) -> Optional[str]:
    lp, prev = row.get("ltp"), row.get("prev_close")
    if not lp or not prev:
        return None
    move = (lp - prev) / prev * 100
    if abs(move) > SUSPICIOUS_MOVE_PCT:
        return (f"{row['symbol']}: live price {lp} is {move:+.1f}% from the previous close {prev}, beyond any "
                "NSE price band. Check the symbol before relying on it.")
    return None

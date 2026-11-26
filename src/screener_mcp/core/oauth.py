"""Single-user OAuth sign-in for the HTTP transport.

claude.ai custom connectors (web and mobile) sign in with OAuth, and a bare
streamable-http server has no auth at all. This module makes the server its own
small OAuth authorization server for exactly one person: clients register
dynamically, the owner signs in once on a passphrase page, and the client gets
short-lived access tokens plus rotating refresh tokens.

Off unless MCP_AUTH_PASSPHRASE and MCP_PUBLIC_URL are both set (see
`auth_from_env`). Registered clients and *hashed* tokens are kept in
~/.screener-mcp/oauth.json so a server restart doesn't sign the owner out.
"""

from __future__ import annotations

import hashlib
import hmac
import html
import json
import os
import secrets
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    RefreshToken,
    TokenError,
    construct_redirect_uri,
)
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

STATE_PATH = Path.home() / ".screener-mcp" / "oauth.json"
LOGIN_PATH = "/login"
MIN_PASSPHRASE_LEN = 12

ACCESS_TTL = 60 * 60              # 1 hour; clients refresh silently
REFRESH_TTL = 60 * 60 * 24 * 30   # 30 days of inactivity before signing in again
CODE_TTL = 5 * 60
PENDING_TTL = 10 * 60             # time allowed to type the passphrase

# Brute-force guard for the passphrase page: this many wrong attempts in the
# window locks sign-in until the window passes.
MAX_FAILURES = 5
FAILURE_WINDOW = 15 * 60


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class PassphraseOAuthProvider:
    """OAuthAuthorizationServerProvider for a single owner who signs in with a passphrase."""

    def __init__(self, passphrase: str, public_url: str, state_path: Path = STATE_PATH):
        self._passphrase = passphrase.encode("utf-8")
        self.public_url = public_url.rstrip("/")
        self.resource_url = f"{self.public_url}/mcp"
        self._state_path = state_path
        self._clients: dict[str, OAuthClientInformationFull] = {}
        # Tokens are stored by hash, so a leaked state file can't be replayed.
        self._access: dict[str, AccessToken] = {}
        self._refresh: dict[str, RefreshToken] = {}
        # Short-lived, in memory only: a restart mid-sign-in just means starting again.
        self._pending: dict[str, tuple[str, AuthorizationParams, float]] = {}
        self._codes: dict[str, AuthorizationCode] = {}
        self._failures: list[float] = []
        self._load()

    # ── persistence ──────────────────────────────────────────────────────────

    def _load(self) -> None:
        if not self._state_path.exists():
            return
        data = json.loads(self._state_path.read_text(encoding="utf-8"))
        self._clients = {c["client_id"]: OAuthClientInformationFull.model_validate(c) for c in data.get("clients", [])}
        self._access = {h: AccessToken.model_validate(t) for h, t in data.get("access", {}).items()}
        self._refresh = {h: RefreshToken.model_validate(t) for h, t in data.get("refresh", {}).items()}

    def _save(self) -> None:
        now = time.time()
        self._access = {h: t for h, t in self._access.items() if not t.expires_at or t.expires_at > now}
        self._refresh = {h: t for h, t in self._refresh.items() if not t.expires_at or t.expires_at > now}
        data = {
            "clients": [c.model_dump(mode="json", exclude_none=True) for c in self._clients.values()],
            "access": {h: t.model_dump(mode="json") for h, t in self._access.items()},
            "refresh": {h: t.model_dump(mode="json") for h, t in self._refresh.items()},
        }
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, self._state_path)

    # ── client registration ──────────────────────────────────────────────────

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        return self._clients.get(client_id)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        self._clients[client_info.client_id] = client_info
        self._save()

    # ── authorization ────────────────────────────────────────────────────────

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        now = time.time()
        self._pending = {k: v for k, v in self._pending.items() if v[2] > now}
        request_id = secrets.token_urlsafe(32)
        self._pending[request_id] = (client.client_id, params, now + PENDING_TTL)
        return f"{self.public_url}{LOGIN_PATH}?req={request_id}"

    async def login(self, request: Request) -> Response:
        """GET shows the passphrase page; POST checks it and redirects back to the client."""
        if request.method == "POST":
            form = parse_qs((await request.body()).decode("utf-8"))
            request_id = (form.get("req") or [""])[0]
            passphrase = (form.get("passphrase") or [""])[0]
        else:
            request_id = request.query_params.get("req", "")
            passphrase = None

        pending = self._pending.get(request_id)
        if pending is None or pending[2] < time.time():
            return _page("This sign-in link has expired. Start the connection again from Claude.", status=400)
        client_id, params, _ = pending
        client = self._clients.get(client_id)
        target = urlparse(str(params.redirect_uri)).netloc
        app_name = (client.client_name if client else None) or target

        if passphrase is None:
            return _login_form(request_id, app_name, target)

        now = time.time()
        self._failures = [t for t in self._failures if t > now - FAILURE_WINDOW]
        if len(self._failures) >= MAX_FAILURES:
            return _page("Too many wrong attempts. Wait 15 minutes and try again.", status=429)
        if not hmac.compare_digest(passphrase.encode("utf-8"), self._passphrase):
            self._failures.append(now)
            return _login_form(request_id, app_name, target, error="That passphrase isn't right.", status=401)

        del self._pending[request_id]
        code = secrets.token_urlsafe(32)
        self._codes[code] = AuthorizationCode(
            code=code,
            scopes=params.scopes or [],
            expires_at=now + CODE_TTL,
            client_id=client_id,
            code_challenge=params.code_challenge,
            redirect_uri=params.redirect_uri,
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            resource=params.resource,
            subject="owner",
        )
        return RedirectResponse(
            construct_redirect_uri(str(params.redirect_uri), code=code, state=params.state), status_code=302
        )

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        code = self._codes.get(authorization_code)
        if code is None or code.client_id != client.client_id:
            return None
        return code

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        # Single use: a replayed code finds nothing.
        if self._codes.pop(authorization_code.code, None) is None:
            raise TokenError("invalid_grant", "authorization code was already used")
        return self._issue(client.client_id, authorization_code.scopes)

    # ── tokens ───────────────────────────────────────────────────────────────

    def _issue(self, client_id: str, scopes: list[str]) -> OAuthToken:
        now = int(time.time())
        access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        # Every token is for this server's /mcp endpoint, whatever resource the
        # client named, so the SDK's resource check always has something to match.
        self._access[_hash(access)] = AccessToken(
            token="", client_id=client_id, scopes=scopes, expires_at=now + ACCESS_TTL,
            resource=self.resource_url, subject="owner",
        )
        self._refresh[_hash(refresh)] = RefreshToken(
            token="", client_id=client_id, scopes=scopes, expires_at=now + REFRESH_TTL,
            resource=self.resource_url, subject="owner",
        )
        self._save()
        return OAuthToken(
            access_token=access, expires_in=ACCESS_TTL, refresh_token=refresh,
            scope=" ".join(scopes) or None,
        )

    async def load_access_token(self, token: str) -> AccessToken | None:
        stored = self._access.get(_hash(token))
        if stored is None or (stored.expires_at and stored.expires_at < time.time()):
            return None
        return stored.model_copy(update={"token": token})

    async def load_refresh_token(self, client: OAuthClientInformationFull, refresh_token: str) -> RefreshToken | None:
        stored = self._refresh.get(_hash(refresh_token))
        if stored is None or stored.client_id != client.client_id:
            return None
        if stored.expires_at and stored.expires_at < time.time():
            return None
        return stored.model_copy(update={"token": refresh_token})

    async def exchange_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: RefreshToken, scopes: list[str]
    ) -> OAuthToken:
        # Rotate: the old refresh token stops working once it has been used.
        if self._refresh.pop(_hash(refresh_token.token), None) is None:
            raise TokenError("invalid_grant", "refresh token was already used")
        return self._issue(client.client_id, scopes)

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        h = _hash(token.token)
        self._access.pop(h, None)
        self._refresh.pop(h, None)
        self._save()


# ── settings from the environment ────────────────────────────────────────────


def auth_from_env(state_path: Path | None = None) -> tuple[PassphraseOAuthProvider, AuthSettings] | None:
    """Return the provider and settings when sign-in is configured, else None.

    Needs both MCP_AUTH_PASSPHRASE and MCP_PUBLIC_URL (the https address the
    tunnel or host serves, without /mcp). `check_http_auth_env` reports a
    half-finished setup before the HTTP server starts.
    """
    passphrase = os.getenv("MCP_AUTH_PASSPHRASE", "")
    public_url = os.getenv("MCP_PUBLIC_URL", "").rstrip("/")
    if not passphrase or not public_url:
        return None
    provider = PassphraseOAuthProvider(passphrase, public_url, state_path or STATE_PATH)
    settings = AuthSettings(
        issuer_url=public_url,
        resource_server_url=provider.resource_url,
        validate_token_resource=True,
        client_registration_options=ClientRegistrationOptions(enabled=True),
        revocation_options=RevocationOptions(enabled=True),
    )
    return provider, settings


def check_http_auth_env() -> str | None:
    """An error message if the sign-in env vars are set inconsistently, else None."""
    passphrase = os.getenv("MCP_AUTH_PASSPHRASE", "")
    public_url = os.getenv("MCP_PUBLIC_URL", "")
    if public_url and not passphrase:
        return ("MCP_PUBLIC_URL is set but MCP_AUTH_PASSPHRASE isn't. Refusing to serve a public URL "
                "without sign-in, since anyone could use your Screener.in account.")
    if passphrase and not public_url:
        return "MCP_AUTH_PASSPHRASE is set but MCP_PUBLIC_URL isn't. Set it to your public https address."
    if passphrase and len(passphrase) < MIN_PASSPHRASE_LEN:
        return f"MCP_AUTH_PASSPHRASE must be at least {MIN_PASSPHRASE_LEN} characters."
    if public_url and urlparse(public_url).scheme != "https":
        return "MCP_PUBLIC_URL must start with https://."
    return None


# ── pages ────────────────────────────────────────────────────────────────────

_STYLE = (
    "body{font-family:-apple-system,system-ui,sans-serif;max-width:420px;margin:12vh auto;padding:0 16px;"
    "color:#1a1a1a;background:#fafafa}input,button{font:inherit;width:100%;box-sizing:border-box;"
    "padding:10px;margin-top:8px;border-radius:6px}input{border:1px solid #bbb}"
    "button{border:0;background:#1a1a1a;color:#fff;cursor:pointer}.err{color:#b00020}.muted{color:#666}"
)


def _page(message: str, status: int = 200) -> HTMLResponse:
    return HTMLResponse(
        f"<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width'>"
        f"<title>Screener MCP</title><style>{_STYLE}</style><p>{html.escape(message)}</p>",
        status_code=status,
    )


def _login_form(request_id: str, app_name: str, target: str, error: str | None = None, status: int = 200) -> Response:
    err = f"<p class=err>{html.escape(error)}</p>" if error else ""
    body: dict[str, Any] = {
        "app": html.escape(app_name), "target": html.escape(target), "req": html.escape(request_id, quote=True),
    }
    return HTMLResponse(
        "<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width'>"
        f"<title>Sign in to Screener MCP</title><style>{_STYLE}</style>"
        "<h2>Sign in to Screener MCP</h2>"
        f"<p><b>{body['app']}</b> wants to use your screener.</p>"
        f"<p class=muted>After sign-in you'll be sent to <b>{body['target']}</b>. "
        "If you didn't start this, close the page.</p>"
        f"{err}<form method=post action='{LOGIN_PATH}'>"
        f"<input type=hidden name=req value='{body['req']}'>"
        "<input type=password name=passphrase placeholder=Passphrase autofocus required autocomplete=current-password>"
        "<button type=submit>Sign in</button></form>",
        status_code=status,
        headers={"Cache-Control": "no-store", "X-Frame-Options": "DENY"},
    )

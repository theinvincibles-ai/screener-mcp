"""
HTTP pages for the daily FYERS sign-in (see core/fyers.py for the flow).

/fyers/login asks for the connector passphrase first, so only the owner can
start a sign-in and plant a token; /fyers/callback accepts only a `state` this
server issued in the last 10 minutes.
"""

import html
import logging
from datetime import datetime
from urllib.parse import parse_qs

from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

from . import fyers
from .oauth import _STYLE, PassphraseOAuthProvider

logger = logging.getLogger(__name__)

_HEADERS = {"Cache-Control": "no-store", "X-Frame-Options": "DENY"}


def _page(title: str, message: str, status: int = 200) -> HTMLResponse:
    return HTMLResponse(
        "<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width'>"
        f"<title>{html.escape(title)}</title><style>{_STYLE}</style>"
        f"<h2>{html.escape(title)}</h2><p>{html.escape(message)}</p>",
        status_code=status, headers=_HEADERS,
    )


def _form(error: str | None = None, status: int = 200) -> HTMLResponse:
    err = f"<p class=err>{html.escape(error)}</p>" if error else ""
    return HTMLResponse(
        "<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width'>"
        f"<title>Connect FYERS</title><style>{_STYLE}</style>"
        "<h2>Connect FYERS for today</h2>"
        "<p class=muted>Enter your Screener MCP passphrase. You'll then sign in on FYERS's own page; "
        "your FYERS password never reaches this server.</p>"
        f"{err}<form method=post action='{fyers.LOGIN_START_PATH}'>"
        "<input type=password name=passphrase placeholder=Passphrase autofocus required autocomplete=current-password>"
        "<button type=submit>Continue to FYERS</button></form>",
        status_code=status, headers=_HEADERS,
    )


def make_routes(auth: PassphraseOAuthProvider):
    async def login(request: Request) -> Response:
        if not fyers.configured():
            return _page("FYERS isn't set up", "Set FYERS_APP_ID and FYERS_SECRET_KEY on the server first.", 404)
        if request.method != "POST":
            return _form()
        form = parse_qs((await request.body()).decode("utf-8"))
        problem = auth.check_passphrase((form.get("passphrase") or [""])[0])
        if problem == "locked":
            return _page("Try again later", "Too many wrong attempts. Wait 15 minutes and try again.", 429)
        if problem:
            return _form("That passphrase isn't right.", 401)
        return RedirectResponse(fyers.start_login(), status_code=302, headers=_HEADERS)

    async def callback(request: Request) -> Response:
        q = request.query_params
        if not fyers.take_state(q.get("state", "")):
            return _page("Sign-in expired", "This FYERS sign-in link has expired or was already used. "
                         f"Start again at {fyers.sign_in_url()}.", 400)
        code = q.get("auth_code") or ""
        if not code:
            return _page("FYERS sign-in failed", "FYERS didn't send a sign-in code. Try again.", 400)
        try:
            tok = await fyers.exchange_auth_code(code)
        except fyers.FyersError as e:
            return _page("FYERS sign-in failed", e.message, 502)
        exp = datetime.fromtimestamp(tok["expires_at"], fyers.IST)
        until = f"{exp.strftime('%I:%M %p').lstrip('0')} on {exp.strftime('%a %d %b')}"
        logger.info("FYERS connected")
        return _page("FYERS connected", f"Live prices are on until {until} IST. You can close this page.")

    return login, callback

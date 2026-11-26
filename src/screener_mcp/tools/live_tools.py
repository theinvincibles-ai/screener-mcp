"""Live prices from FYERS: quotes and connection status."""

from ..core import fyers
from ..core.envelope import ToolError, ToolResult

MAX_SYMBOLS = 50


def _symbols(symbols: list[str] | str) -> list[str]:
    raw = symbols.replace(",", " ").split() if isinstance(symbols, str) else symbols
    out = [s.strip().upper() for s in raw or [] if s and s.strip()]
    if not out:
        raise ToolError("Give at least one NSE symbol, e.g. ['TCS', 'INFY'].", "invalid_input")
    if len(out) > MAX_SYMBOLS:
        raise ToolError(f"Up to {MAX_SYMBOLS} symbols per call (got {len(out)}).", "invalid_input")
    return list(dict.fromkeys(out))


def _as_tool_error(e: fyers.FyersError) -> ToolError:
    if e.kind == "sign_in_required":
        fyers.require_connected()  # raises the standard "sign in at <link>" error
    return ToolError(e.message, f"fyers_{e.kind}")


async def get_live_quote(symbols: list[str] | str) -> ToolResult:
    wanted = _symbols(symbols)
    fyers.require_connected()
    try:
        quotes = await fyers.get_quotes(wanted)
    except fyers.FyersError as e:
        raise _as_tool_error(e) from e

    rows, missing, warnings = [], [], []
    for sym in wanted:
        entry = quotes[sym]
        if not entry["ok"]:
            missing.append(f"{sym}.ltp")
            warnings.append(f"{sym}: {entry['error']} (looked up as {fyers.fyers_symbol(sym)}).")
            continue
        row = fyers.quote_row(sym, entry)
        if note := fyers.suspicious_move(row):
            warnings.append(note)
        rows.append(row)
    return ToolResult(
        data={"quotes": rows, "source": "fyers_live", "currency": "INR"},
        warnings=warnings,
        missing_fields=missing,
        reason=f"No live quote for {', '.join(m.split('.')[0] for m in missing)}." if missing else None,
    )


async def fyers_status() -> dict:
    return fyers.status()


async def live_prices(symbols: list[str]) -> tuple[dict[str, dict], str | None]:
    """Best-effort live rows for other tools: ({symbol: row}, note). Never raises;
    the note says why live prices weren't used, for the caller's warnings."""
    if not fyers.configured():
        return {}, None
    if not fyers.load_token():
        return {}, f"Using delayed Screener prices: FYERS needs today's sign-in at {fyers.sign_in_url()}."
    try:
        quotes = await fyers.get_quotes(symbols)
    except fyers.FyersError as e:
        return {}, f"Using delayed Screener prices: {e.message}"
    return {s: fyers.quote_row(s, q) for s, q in quotes.items() if q["ok"]}, None

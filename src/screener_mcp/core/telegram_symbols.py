"""
NSE symbol list for matching tickers and company names in Telegram posts.

The list is NSE's own equity master (mainboard plus SME, since momentum
channels often discuss SME stocks), cached for a week in the Telegram folder.
If NSE can't be reached and nothing is cached, matching falls back to nothing
rather than guessing: callers report that the symbol list is unavailable.
"""

import csv
import io
import logging
import time
from typing import Optional

import httpx

from . import telegram_store as store
from .nse_client import NSE_HEADERS
from .telegram_extract import SymbolIndex

logger = logging.getLogger(__name__)

SOURCES = [
    ("mainboard", "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv"),
    ("sme", "https://nsearchives.nseindia.com/emerge/corporates/content/SME_EQUITY_L.csv"),
]
MAX_AGE = 7 * 24 * 3600

_index: Optional[SymbolIndex] = None
_index_key: Optional[tuple] = None


def parse_csv(text: str) -> list[tuple[str, str]]:
    """(SYMBOL, company name) rows from NSE's EQUITY_L-style CSV."""
    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")))
    rows = []
    for r in reader:
        r = {(k or "").strip().upper(): (v or "").strip() for k, v in r.items()}
        sym, name = r.get("SYMBOL"), r.get("NAME OF COMPANY") or r.get("NAME_OF_COMPANY")
        if sym and name:
            rows.append((sym, name))
    return rows


async def refresh(force: bool = False) -> dict:
    """Download NSE's lists if the cache is missing or older than a week."""
    path = store.symbols_path()
    if not force and path.exists() and time.time() - path.stat().st_mtime < MAX_AGE:
        return {"refreshed": False}
    rows: list[tuple[str, str]] = []
    errors = []
    async with httpx.AsyncClient(headers=NSE_HEADERS, timeout=30, follow_redirects=True) as client:
        for label, url in SOURCES:
            try:
                resp = await client.get(url)
                resp.raise_for_status()
                got = parse_csv(resp.text)
                if not got:
                    raise ValueError("no SYMBOL / NAME OF COMPANY columns")
                rows += got
            except (httpx.HTTPError, ValueError) as e:
                errors.append(f"{label}: {type(e).__name__}")
    if not rows:
        logger.warning("NSE symbol list refresh failed (%s)", "; ".join(errors))
        return {"refreshed": False, "errors": errors}
    store.ensure_dir()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["SYMBOL", "NAME OF COMPANY"])
    w.writerows(sorted(set(rows)))
    path.write_text(buf.getvalue(), encoding="utf-8")
    logger.info("NSE symbol list refreshed: %d symbols", len(rows))
    return {"refreshed": True, "symbols": len(rows), "errors": errors}


def load_index() -> Optional[SymbolIndex]:
    """The cached index, rebuilt when the CSV or aliases file changes."""
    global _index, _index_key
    path, apath = store.symbols_path(), store.aliases_path()
    if not path.exists():
        return None
    key = (str(path), path.stat().st_mtime, apath.stat().st_mtime if apath.exists() else None)
    if _index is None or _index_key != key:
        rows = parse_csv(path.read_text(encoding="utf-8"))
        _index = SymbolIndex.build(rows, store.read_aliases()) if rows else None
        _index_key = key
    return _index


def info() -> dict:
    path = store.symbols_path()
    if not path.exists():
        return {"available": False}
    idx = load_index()
    return {
        "available": idx is not None,
        "symbols": len(idx.names) if idx else 0,
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(path.stat().st_mtime)),
    }

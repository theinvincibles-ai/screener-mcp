"""Telegram channel picks: connection status and the stocks a channel discussed.

These tools report what the channel said, with its own words attached. They do
not judge the calls; analysis and prices come from the strategy engine later.
"""

from datetime import datetime, timedelta, timezone
from typing import Optional

from ..core import telegram_client as tg
from ..core import telegram_store as store
from ..core import telegram_symbols as symbols
from ..core.envelope import ToolError, ToolResult
from ..core.telegram_extract import Post, build_calls

MAX_DAYS = 90
LINK_LOOKBACK_DAYS = 60     # older posts read so an update can find the call it belongs to
MAX_STOCKS = 40
MAX_MENTIONS_PER_STOCK = 5
MAX_TEXT = 400
IST = timezone(timedelta(hours=5, minutes=30))

SETUP_STEPS = [
    "Create an app at https://my.telegram.org (API development tools) to get an api_id and api_hash.",
    "Store them in the Keychain: security add-generic-password -U -s screener-mcp -a telegram-api-id -w "
    "(and the same with -a telegram-api-hash), then restart the service.",
    'Install Telegram support: uv tool install --force "screener-mcp[telegram] @ git+https://github.com/theinvincibles-ai/screener-mcp".',
    "In Terminal on the Mac: scripts/macos/telegram.sh login (Telegram sends a code to your app).",
    "Still in Terminal: scripts/macos/telegram.sh channels, then scripts/macos/telegram.sh follow <id> [--topic <n>].",
]


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _ist(iso: Optional[str]) -> Optional[str]:
    if not iso:
        return None
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(IST).strftime("%Y-%m-%d %H:%M IST")


def _clip(text: str) -> str:
    text = text or ""
    return text if len(text) <= MAX_TEXT else text[:MAX_TEXT - 1] + "…"


async def telegram_status() -> dict:
    channels = store.read_channels()
    signed_in = bool(store.read_session())
    out: dict = {
        "installed": tg.installed(),
        "configured": tg.configured(),
        "signed_in": signed_in,
        "storage": str(store.base_dir()),
        "channels": [],
        "symbol_list": symbols.info(),
        "background_polling": {"running": tg.next_poll_at() is not None, "next_poll_at": _ist(tg.next_poll_at())},
    }
    if store.db_path().exists():
        db = store.get_store()
        counts = db.counts()
        for c in channels:
            cid = int(c["id"])
            st = db.state(cid)
            out["channels"].append({
                "id": cid,
                "title": c.get("title"),
                "topics": [{"id": t, "title": (c.get("topic_titles") or {}).get(str(t))} for t in c.get("topics") or []] or "all",
                "messages_stored": counts.get(cid, {}).get("messages", 0),
                "last_post": _ist(counts.get(cid, {}).get("last_post")),
                "last_successful_poll": _ist(st.get("last_ok_at")),
                "last_error": st.get("last_error"),
                "paused_until": _ist(st.get("paused_until")),
            })
    else:
        out["channels"] = [{"id": int(c["id"]), "title": c.get("title"), "messages_stored": 0} for c in channels]
    todo = []
    if not out["configured"]:
        todo += SETUP_STEPS[:2]
    if not out["installed"]:
        todo.append(SETUP_STEPS[2])
    if not signed_in:
        todo.append(SETUP_STEPS[3])
    if not channels:
        todo.append(SETUP_STEPS[4])
    if todo:
        out["next_steps"] = todo
    out["ready"] = not todo
    return out


def _chat_filter(channel: str, channels: list[dict]) -> Optional[list[int]]:
    channel = (channel or "").strip()
    if not channel:
        return None
    if channel.lstrip("-").isdigit():
        cid = int(channel.lstrip("-").removeprefix("100")) if channel.startswith("-100") else int(channel)
        return [cid]
    hits = [int(c["id"]) for c in channels if channel.lower() in (c.get("title") or "").lower()]
    if not hits:
        raise ToolError(
            f"No followed channel matches {channel!r}.",
            "invalid_input",
            followed=[{"id": int(c["id"]), "title": c.get("title")} for c in channels],
        )
    return hits


def _decorate(rec: dict, titles: dict[int, str], topic_titles: dict[tuple[int, int], str]) -> dict:
    cid, topic = rec.get("chat_id"), rec.get("topic_id")
    rec = dict(rec)
    rec["channel"] = titles.get(cid, str(cid))
    if topic is not None:
        rec["topic"] = topic_titles.get((cid, topic), str(topic))
    rec["link"] = store.message_link(cid, rec["message_id"], topic)
    rec["posted_ist"] = _ist(rec.get("opened_at") or rec.get("at"))
    rec["text"] = _clip(rec.get("text", ""))
    for ev in rec.get("events", []):
        ev["link"] = store.message_link(cid, ev["message_id"], topic)
        ev["posted_ist"] = _ist(ev["at"])
        ev["text"] = _clip(ev["text"])
    return rec


async def get_channel_mentions(days: int = 7, symbol: str = "", channel: str = "") -> ToolResult:
    if not 1 <= int(days) <= MAX_DAYS:
        raise ToolError(f"days must be between 1 and {MAX_DAYS}.", "invalid_input")
    days = int(days)
    warnings: list[str] = []
    channels = store.read_channels()
    if not channels and not store.db_path().exists():
        raise ToolError(
            "No Telegram channel is followed yet.", "telegram_not_set_up", next_steps=SETUP_STEPS,
        )
    if note := await tg.ensure_fresh():
        warnings.append(note)
    elif not (tg.configured() and tg.installed() and store.read_session()):
        warnings.append("Showing stored posts only: Telegram isn't fully set up or signed in here (see telegram_status).")

    index = symbols.load_index()
    if index is None:
        await symbols.refresh()
        index = symbols.load_index()
    if index is None:
        raise ToolError(
            "The NSE symbol list isn't available yet (NSE couldn't be reached), so stocks can't be "
            "picked out of posts. It downloads automatically on the next poll; retry in a few minutes.",
            "symbol_list_unavailable",
        )

    chat_ids = _chat_filter(channel, channels)
    now = datetime.now(timezone.utc)
    since = now - timedelta(days=days)
    rows = store.get_store().messages(_iso(since - timedelta(days=LINK_LOOKBACK_DAYS)), chat_ids)
    posts = [Post(chat_id=r["chat_id"], msg_id=r["msg_id"], posted_at=r["posted_at"], text=r["text"],
                  topic_id=r["topic_id"], reply_to=r["reply_to"], edited_at=r["edited_at"],
                  deleted=bool(r["deleted"]), media=r["media"]) for r in rows]
    found = build_calls(posts, index)

    window = _iso(since)
    want = (symbol or "").strip().upper() or None
    if want and want not in index.names:
        raise ToolError(f"{want} isn't an NSE symbol in the list.", "invalid_input")

    titles = {int(c["id"]): c.get("title") or str(c["id"]) for c in channels}
    topic_titles = {(int(c["id"]), int(t)): title for c in channels for t, title in (c.get("topic_titles") or {}).items()}

    def in_window(rec: dict) -> bool:
        return (rec.get("opened_at") or rec.get("at")) >= window or any(e["at"] >= window for e in rec.get("events", []))

    stocks: dict[str, dict] = {}

    def slot(sym: str) -> dict:
        return stocks.setdefault(sym, {"symbol": sym, "name": index.names.get(sym), "calls": [],
                                       "updates_without_a_call": [], "mentions": [], "mention_count": 0,
                                       "last_activity": ""})

    for call in found["calls"]:
        if (want and call["symbol"] != want) or not in_window(call):
            continue
        s = slot(call["symbol"])
        s["calls"].append(_decorate(call, titles, topic_titles))
        s["last_activity"] = max([s["last_activity"], call["opened_at"], *[e["at"] for e in call["events"]]])
    for ev in found["unlinked_events"]:
        if (want and ev["symbol"] != want) or ev["at"] < window:
            continue
        s = slot(ev["symbol"])
        s["updates_without_a_call"].append(_decorate(ev, titles, topic_titles))
        s["last_activity"] = max(s["last_activity"], ev["at"])
    for ev in found["mentions"]:
        if (want and ev["symbol"] != want) or ev["at"] < window:
            continue
        s = slot(ev["symbol"])
        s["mention_count"] += 1
        s["mentions"].append(_decorate(ev, titles, topic_titles))
        s["last_activity"] = max(s["last_activity"], ev["at"])

    ordered = sorted(stocks.values(), key=lambda s: s["last_activity"], reverse=True)
    if len(ordered) > MAX_STOCKS:
        warnings.append(f"{len(ordered)} stocks were discussed; showing the {MAX_STOCKS} most recent. Pass symbol= for one stock.")
        ordered = ordered[:MAX_STOCKS]
    missing: list[str] = []
    for s in ordered:
        s["mentions"] = s["mentions"][-MAX_MENTIONS_PER_STOCK:]
        s["last_activity"] = _ist(s["last_activity"])
        for c in s["calls"]:
            missing += [f"{c['symbol']}@{c['message_id']}.{m}" for m in c["missing"]]

    in_window_rows = [r for r in rows if r["posted_at"] >= window]
    image_only = sum(1 for r in in_window_rows if r["media"] and not (r["text"] or "").strip())
    if image_only:
        warnings.append(f"{image_only} post(s) were images with no caption; charts and screenshots aren't read yet.")
    deleted = sum(1 for r in in_window_rows if r["deleted"])
    if deleted:
        warnings.append(f"{deleted} post(s) were later deleted by the channel; they are kept and marked deleted.")

    data = {
        "window": {"from": _ist(window), "to": _ist(_iso(now)), "days": days},
        "channels": [{"id": cid, "title": titles.get(cid, str(cid)),
                      "posts_in_window": sum(1 for r in in_window_rows if r["chat_id"] == cid)}
                     for cid in sorted({r["chat_id"] for r in rows} | set(chat_ids or titles))],
        "stocks": ordered,
        "how_to_read": (
            "calls = posts that gave a trade (entry, stop or targets) with every later update attached; "
            "updates_without_a_call = target/stop/exit/cancel posts whose original call wasn't found; "
            "mentions = discussion without levels. Levels are exactly as written; a level the post didn't "
            "give is null and listed in the call's 'missing'. This is the channel's view, not a recommendation."
        ),
    }
    return ToolResult(
        data=data,
        warnings=warnings,
        missing_fields=missing,
        reason=(f"{len(missing)} level(s) or timeframe(s) were not stated in the posts; they are null, not guessed."
                if missing else None),
    )

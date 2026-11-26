"""
Read-only Telegram reader for the channels you follow, using your own account.

Optional: off unless TELEGRAM_API_ID and TELEGRAM_API_HASH are set (from your
app at my.telegram.org, kept in the Keychain) and you have signed in once with
`screener-mcp-telegram login`. Telethon is only imported when needed, so the
core server runs without it (`pip install "screener-mcp[telegram]"`).

What it does: fetch new posts from followed channels (and only the followed
forum topics), re-check the last two days for edits and deletions, and store
everything in the local SQLite file (core/telegram_store.py). It never sends,
joins, reads receipts or marks anything as read. The session string and the
message text are never logged.
"""

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from . import telegram_store as store
from .envelope import ToolError

logger = logging.getLogger(__name__)
logging.getLogger("telethon").setLevel(logging.WARNING)

IST = timezone(timedelta(hours=5, minutes=30))
BACKFILL_DAYS = int(os.getenv("TELEGRAM_BACKFILL_DAYS", "30"))
RECHECK_HOURS = 48          # window re-read for edits and deletions
MAX_PER_POLL = 2000         # per channel, so a first run can't run away
STALE_AFTER = 5 * 60        # a tool call triggers a fresh pull if older than this
INSTALL_HINT = 'Telegram support isn\'t installed. Run: pip install "screener-mcp[telegram]" (or uv tool install --force "screener-mcp[telegram] @ git+https://github.com/theinvincibles-ai/screener-mcp").'


def _api() -> tuple[int, str] | None:
    raw_id, api_hash = os.getenv("TELEGRAM_API_ID", "").strip(), os.getenv("TELEGRAM_API_HASH", "").strip()
    if not raw_id or not api_hash:
        return None
    try:
        return int(raw_id), api_hash
    except ValueError:
        return None


def configured() -> bool:
    return _api() is not None


def installed() -> bool:
    try:
        import telethon  # noqa: F401
    except ImportError:
        return False
    return True


def poll_interval(now: Optional[datetime] = None) -> int:
    """Seconds to the next poll: 15 min in Indian market hours, else hourly."""
    override = os.getenv("TELEGRAM_POLL_MINUTES")
    if override:
        return max(60, int(float(override) * 60))
    t = (now or datetime.now(IST)).astimezone(IST)
    market = t.weekday() < 5 and (8, 45) <= (t.hour, t.minute) < (16, 0)
    return 15 * 60 if market else 60 * 60


# ─── message conversion (pure, tested offline with stand-in objects) ─────────

def _iso(dt: Optional[datetime]) -> Optional[str]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _media_kind(msg: Any) -> Optional[str]:
    if getattr(msg, "photo", None):
        return "photo"
    if getattr(msg, "document", None):
        return "document"
    if getattr(msg, "web_preview", None):
        return "link_preview"
    media = getattr(msg, "media", None)
    return type(media).__name__ if media else None


def topic_and_parent(msg: Any, forum: bool) -> tuple[Optional[int], Optional[int]]:
    """(forum topic id, id of the post it replies to). In a forum, a post's
    reply header points at the topic's first message when it isn't a reply to
    anyone; that is the topic, not a parent. Topic 1 is "General"."""
    rh = getattr(msg, "reply_to", None)
    reply_id = getattr(rh, "reply_to_msg_id", None) if rh else None
    top_id = getattr(rh, "reply_to_top_id", None) if rh else None
    if not forum:
        return None, reply_id
    if rh is None or not getattr(rh, "forum_topic", False):
        return 1, reply_id
    if top_id:
        return top_id, reply_id
    return reply_id, None


def to_row(msg: Any, chat_id: int, forum: bool) -> dict:
    topic, parent = topic_and_parent(msg, forum)
    return {
        "chat_id": chat_id,
        "msg_id": msg.id,
        "topic_id": topic,
        "posted_at": _iso(msg.date),
        "edited_at": _iso(getattr(msg, "edit_date", None)),
        "text": getattr(msg, "message", None) or "",
        "media": _media_kind(msg),
        "reply_to": parent,
    }


# ─── client ───────────────────────────────────────────────────────────────────

_client = None
_client_lock = asyncio.Lock()
_poll_lock = asyncio.Lock()
_loop_task: Optional[asyncio.Task] = None
_next_poll_at: Optional[str] = None


def _require_ready() -> tuple[int, str, str]:
    if not installed():
        raise ToolError(INSTALL_HINT, "telegram_not_installed")
    api = _api()
    if not api:
        raise ToolError(
            "Telegram isn't set up on this server. Store your my.telegram.org api_id and api_hash in the "
            "Keychain (README: 'Telegram channels'), then restart the service.",
            "telegram_not_configured",
        )
    session = store.read_session()
    if not session:
        raise ToolError(
            "Telegram isn't signed in. On the Mac, run `screener-mcp-telegram login` once in Terminal.",
            "telegram_not_signed_in",
        )
    return api[0], api[1], session


def make_client(session: str, api_id: int, api_hash: str):
    from telethon import TelegramClient
    from telethon.sessions import StringSession

    # The session lives in memory here; only the CLI writes the session file,
    # so the server and a CLI command never fight over a lock.
    return TelegramClient(StringSession(session), api_id, api_hash,
                          device_model="screener-mcp", app_version="read-only", receive_updates=False)


async def _get_client():
    global _client
    async with _client_lock:
        api_id, api_hash, session = _require_ready()
        if _client is None:
            _client = make_client(session, api_id, api_hash)
        if not _client.is_connected():
            await _client.connect()
        if not await _client.is_user_authorized():
            await _client.disconnect()
            _client = None
            raise ToolError(
                "The Telegram sign-in has expired or was revoked. Run `screener-mcp-telegram login` again on the Mac.",
                "telegram_not_signed_in",
            )
        return _client


def _peer(ch: dict):
    from telethon.tl import types

    return types.InputPeerChannel(int(ch["id"]), int(ch["access_hash"]))


async def _poll_channel(client, ch: dict, db: store.MessageStore) -> dict:
    from telethon import errors
    from telethon.tl import types

    chat_id, forum = int(ch["id"]), bool(ch.get("forum"))
    topics = set(ch.get("topics") or [])
    st = db.state(chat_id)
    now = datetime.now(timezone.utc)
    if st.get("paused_until") and st["paused_until"] > store.now_iso():
        return {"chat_id": chat_id, "skipped": "paused", "paused_until": st["paused_until"]}

    peer = _peer(ch)
    rows, last = [], int(st.get("last_msg_id") or 0)
    try:
        if last:
            it = client.iter_messages(peer, offset_id=last, reverse=True, limit=MAX_PER_POLL)
        else:
            it = client.iter_messages(peer, offset_date=now - timedelta(days=BACKFILL_DAYS), reverse=True, limit=MAX_PER_POLL)
        async for m in it:
            if isinstance(m, types.MessageService):
                last = max(last, m.id)
                continue
            row = to_row(m, chat_id, forum)
            last = max(last, m.id)
            if topics and row["topic_id"] not in topics:
                continue
            rows.append(row)
        new = db.upsert(rows)

        # Edits and deletions in the recent window: a deleted losing call must
        # still be counted, so it is flagged, not removed.
        since = (now - timedelta(hours=RECHECK_HOURS)).isoformat(timespec="seconds").replace("+00:00", "Z")
        ids = [i for i in db.recent_ids(chat_id, since) if i not in {r["msg_id"] for r in rows}]
        edited, gone = [], []
        for i in range(0, len(ids), 100):
            batch = ids[i:i + 100]
            got = await client.get_messages(peer, ids=batch)
            for mid, m in zip(batch, got):
                if m is None or isinstance(m, types.MessageEmpty):
                    gone.append(mid)
                elif getattr(m, "edit_date", None):
                    edited.append(to_row(m, chat_id, forum))
        db.upsert(edited)
        deleted = db.mark_deleted(chat_id, gone)
        db.save_state(chat_id, last_msg_id=last, last_poll_at=store.now_iso(), last_ok_at=store.now_iso(),
                      last_error=None, paused_until=None)
        logger.info("telegram poll chat=%s new=%d edited=%d deleted=%d", chat_id, new, len(edited), deleted)
        return {"chat_id": chat_id, "new": new, "edited": len(edited), "deleted": deleted}
    except errors.FloodWaitError as e:
        until = (now + timedelta(seconds=e.seconds + 5)).isoformat(timespec="seconds").replace("+00:00", "Z")
        db.save_state(chat_id, last_poll_at=store.now_iso(), last_error=f"Telegram asked to wait {e.seconds}s", paused_until=until)
        logger.warning("telegram poll chat=%s flood wait %ss", chat_id, e.seconds)
        return {"chat_id": chat_id, "error": "flood_wait", "paused_until": until}
    except (errors.ChannelPrivateError, errors.ChannelInvalidError) as e:
        db.save_state(chat_id, last_poll_at=store.now_iso(), last_error=f"No access to this channel ({type(e).__name__})")
        logger.warning("telegram poll chat=%s no access: %s", chat_id, type(e).__name__)
        return {"chat_id": chat_id, "error": "no_access"}
    except (OSError, ConnectionError, asyncio.TimeoutError) as e:
        db.save_state(chat_id, last_poll_at=store.now_iso(), last_error=f"Network error ({type(e).__name__})")
        logger.warning("telegram poll chat=%s network: %s", chat_id, type(e).__name__)
        return {"chat_id": chat_id, "error": "network"}


async def poll_all() -> list[dict]:
    """One pass over every followed channel. Raises ToolError if not set up."""
    channels = store.read_channels()
    if not channels:
        return []
    async with _poll_lock:
        client = await _get_client()
        db = store.get_store()
        out = []
        for ch in channels:
            out.append(await _poll_channel(client, ch, db))
        try:
            from . import telegram_symbols
            await telegram_symbols.refresh()
        except Exception as e:  # the symbol list is best-effort; the cache keeps working
            logger.warning("NSE symbol refresh failed: %s", type(e).__name__)
        return out


async def ensure_fresh() -> Optional[str]:
    """Pull now if the newest successful poll is older than STALE_AFTER.
    Returns a warning for the caller, or None. Never raises."""
    if not (configured() and installed() and store.read_session() and store.read_channels()):
        return None
    start_background_polling()  # e.g. signed in after the server started
    db = store.get_store()
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=STALE_AFTER)).isoformat(timespec="seconds").replace("+00:00", "Z")
    states = [db.state(int(c["id"])) for c in store.read_channels()]
    if all((s.get("last_ok_at") or "") >= cutoff for s in states):
        return None
    try:
        results = await asyncio.wait_for(poll_all(), timeout=90)
    except ToolError as e:
        return f"Showing stored posts only: {e.message}"
    except Exception as e:
        return f"Showing stored posts only: the Telegram pull failed ({type(e).__name__})."
    bad = [r for r in results if r.get("error")]
    if bad:
        return f"Telegram pull had problems for {len(bad)} channel(s); see telegram_status."
    return None


async def _loop() -> None:
    global _next_poll_at
    while True:
        try:
            await poll_all()
        except ToolError as e:
            logger.info("telegram poller idle: %s", e.error_type)
        except Exception as e:
            logger.warning("telegram poll failed: %s", type(e).__name__)
        wait = poll_interval()
        _next_poll_at = (datetime.now(timezone.utc) + timedelta(seconds=wait)).isoformat(timespec="seconds").replace("+00:00", "Z")
        await asyncio.sleep(wait)


def start_background_polling() -> bool:
    """Start the poller once per process, if Telegram is set up. Idempotent."""
    global _loop_task
    if _loop_task is not None and not _loop_task.done():
        return True
    if not (configured() and installed() and store.read_session()):
        return False
    _loop_task = asyncio.get_running_loop().create_task(_loop(), name="telegram-poller")
    return True


def next_poll_at() -> Optional[str]:
    return _next_poll_at if _loop_task is not None and not _loop_task.done() else None

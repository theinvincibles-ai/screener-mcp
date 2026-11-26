"""
Local storage for the Telegram channel reader. Everything stays on this machine.

  ~/.screener-mcp/telegram/            folder, owner-only (0700)
    telegram.session                   sign-in session string, owner-only (0600);
                                       anyone holding it can act as your account
    channels.json                      channels (and forum topics) to follow,
                                       written by `screener-mcp-telegram follow`
    aliases.json                       optional: {"mazagon": "MAZDOCK", ...}
    messages.db                        SQLite: posts, edits, deletions, poll state
    nse_symbols.csv                    cached NSE equity list for ticker matching

Logs written by this module carry ids and counts only, never message text.
"""

import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

from .local_store import read_json, write_json

SCHEMA_VERSION = 1


def base_dir() -> Path:
    # Read at call time so tests (and users) can point it elsewhere.
    return Path(os.getenv("SCREENER_TELEGRAM_DIR") or Path.home() / ".screener-mcp" / "telegram")


def ensure_dir() -> Path:
    d = base_dir()
    d.mkdir(parents=True, exist_ok=True)
    os.chmod(d, 0o700)
    return d


def session_path() -> Path:
    return base_dir() / "telegram.session"


def channels_path() -> Path:
    return base_dir() / "channels.json"


def aliases_path() -> Path:
    return base_dir() / "aliases.json"


def db_path() -> Path:
    return base_dir() / "messages.db"


def symbols_path() -> Path:
    return base_dir() / "nse_symbols.csv"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


# ─── session and channel config ──────────────────────────────────────────────

def read_session() -> Optional[str]:
    try:
        s = session_path().read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return s or None


def write_session(value: str) -> None:
    ensure_dir()
    p = session_path()
    tmp = p.with_name(p.name + ".tmp")
    # Create owner-only from the start, so the secret is never world-readable.
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(value)
    os.replace(tmp, p)
    os.chmod(p, 0o600)


def delete_session() -> None:
    session_path().unlink(missing_ok=True)


def read_channels() -> list[dict]:
    data = read_json(channels_path(), {"channels": []})
    return [c for c in data.get("channels", []) if isinstance(c, dict) and c.get("id")]


def write_channels(channels: list[dict]) -> None:
    ensure_dir()
    write_json(channels_path(), {"channels": channels})
    os.chmod(channels_path(), 0o600)


def read_aliases() -> dict[str, str]:
    data = read_json(aliases_path(), {})
    return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}


def message_link(chat_id: int, msg_id: int, topic_id: Optional[int] = None) -> str:
    """t.me link that opens the post for a member of the private channel."""
    if topic_id and topic_id != 1:
        return f"https://t.me/c/{chat_id}/{topic_id}/{msg_id}"
    return f"https://t.me/c/{chat_id}/{msg_id}"


# ─── message database ────────────────────────────────────────────────────────

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS messages (
    chat_id    INTEGER NOT NULL,
    msg_id     INTEGER NOT NULL,
    topic_id   INTEGER,
    posted_at  TEXT NOT NULL,
    edited_at  TEXT,
    text       TEXT NOT NULL DEFAULT '',
    media      TEXT,
    reply_to   INTEGER,
    deleted    INTEGER NOT NULL DEFAULT 0,
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (chat_id, msg_id)
);
CREATE INDEX IF NOT EXISTS messages_by_time ON messages (posted_at);
CREATE TABLE IF NOT EXISTS poll_state (
    chat_id      INTEGER PRIMARY KEY,
    last_msg_id  INTEGER NOT NULL DEFAULT 0,
    last_poll_at TEXT,
    last_ok_at   TEXT,
    last_error   TEXT,
    paused_until TEXT
);
"""


class MessageStore:
    """Thin SQLite wrapper. One connection per instance, guarded by a lock so the
    background poller and tool calls can share it."""

    def __init__(self, path: Optional[Path] = None):
        self.path = path or db_path()
        if self.path != Path(":memory:"):
            self.path.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.chmod(self.path.parent, 0o700)
            except OSError:
                pass
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(self.path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            self._db.executescript(_SCHEMA)
            self._db.execute("INSERT OR IGNORE INTO meta VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))
            self._db.commit()
        if self.path != Path(":memory:"):
            os.chmod(self.path, 0o600)

    def close(self) -> None:
        self._db.close()

    def upsert(self, rows: Iterable[dict]) -> int:
        """Insert new posts or update edited ones. Returns rows written."""
        fetched = now_iso()
        n = 0
        with self._lock:
            for r in rows:
                self._db.execute(
                    """INSERT INTO messages (chat_id, msg_id, topic_id, posted_at, edited_at, text, media, reply_to, deleted, fetched_at)
                       VALUES (:chat_id, :msg_id, :topic_id, :posted_at, :edited_at, :text, :media, :reply_to, 0, :fetched)
                       ON CONFLICT (chat_id, msg_id) DO UPDATE SET
                         topic_id = excluded.topic_id, edited_at = excluded.edited_at, text = excluded.text,
                         media = excluded.media, reply_to = excluded.reply_to, deleted = 0, fetched_at = excluded.fetched_at""",
                    {"topic_id": None, "edited_at": None, "media": None, "reply_to": None, **r, "text": r.get("text") or "", "fetched": fetched},
                )
                n += 1
            self._db.commit()
        return n

    def mark_deleted(self, chat_id: int, msg_ids: Iterable[int]) -> int:
        """Keep the row (a deleted losing call must still count), flag it deleted."""
        ids = list(msg_ids)
        if not ids:
            return 0
        with self._lock:
            cur = self._db.executemany(
                "UPDATE messages SET deleted = 1 WHERE chat_id = ? AND msg_id = ? AND deleted = 0",
                [(chat_id, i) for i in ids],
            )
            self._db.commit()
            return cur.rowcount

    def recent_ids(self, chat_id: int, since_iso: str) -> list[int]:
        with self._lock:
            rows = self._db.execute(
                "SELECT msg_id FROM messages WHERE chat_id = ? AND posted_at >= ? AND deleted = 0 ORDER BY msg_id",
                (chat_id, since_iso),
            ).fetchall()
        return [r[0] for r in rows]

    def messages(self, since_iso: str, chat_ids: Optional[list[int]] = None) -> list[dict]:
        q = "SELECT * FROM messages WHERE posted_at >= ?"
        args: list = [since_iso]
        if chat_ids:
            q += f" AND chat_id IN ({','.join('?' * len(chat_ids))})"
            args += chat_ids
        q += " ORDER BY posted_at, msg_id"
        with self._lock:
            return [dict(r) for r in self._db.execute(q, args).fetchall()]

    def counts(self) -> dict[int, dict]:
        with self._lock:
            rows = self._db.execute(
                "SELECT chat_id, COUNT(*) n, MIN(posted_at) first, MAX(posted_at) last FROM messages GROUP BY chat_id"
            ).fetchall()
        return {r["chat_id"]: {"messages": r["n"], "first_post": r["first"], "last_post": r["last"]} for r in rows}

    def state(self, chat_id: int) -> dict:
        with self._lock:
            r = self._db.execute("SELECT * FROM poll_state WHERE chat_id = ?", (chat_id,)).fetchone()
        return dict(r) if r else {"chat_id": chat_id, "last_msg_id": 0, "last_poll_at": None,
                                  "last_ok_at": None, "last_error": None, "paused_until": None}

    def save_state(self, chat_id: int, **fields) -> None:
        st = {**self.state(chat_id), **fields}
        with self._lock:
            self._db.execute(
                """INSERT INTO poll_state (chat_id, last_msg_id, last_poll_at, last_ok_at, last_error, paused_until)
                   VALUES (:chat_id, :last_msg_id, :last_poll_at, :last_ok_at, :last_error, :paused_until)
                   ON CONFLICT (chat_id) DO UPDATE SET last_msg_id = excluded.last_msg_id,
                     last_poll_at = excluded.last_poll_at, last_ok_at = excluded.last_ok_at,
                     last_error = excluded.last_error, paused_until = excluded.paused_until""",
                st,
            )
            self._db.commit()


_store: Optional[MessageStore] = None
_store_lock = threading.Lock()


def get_store() -> MessageStore:
    global _store
    with _store_lock:
        if _store is None or _store.path != db_path():
            ensure_dir()
            _store = MessageStore()
        return _store

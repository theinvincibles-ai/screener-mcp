"""
Offline tests for the Telegram channel reader: stock and level extraction, call
grouping, the local store, message conversion and the two tools. No Telegram,
no NSE: Telethon is never imported here, and the NSE list is a trimmed fixture.
"""

import os
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from screener_mcp.core import telegram_client as tg
from screener_mcp.core import telegram_store as store
from screener_mcp.core import telegram_symbols as symbols
from screener_mcp.core.envelope import to_envelope
from screener_mcp.core.telegram_extract import (
    Post,
    SymbolIndex,
    build_calls,
    extract_message,
    parse_levels,
    parse_timeframe,
)
from screener_mcp.tools import telegram_tools

FIXTURE = Path(__file__).parent / "fixtures" / "nse_equity_sample.csv"


@pytest.fixture(scope="module")
def index() -> SymbolIndex:
    rows = symbols.parse_csv(FIXTURE.read_text(encoding="utf-8"))
    return SymbolIndex.build(rows, {"mazagon": "MAZDOCK"})


@pytest.fixture
def tgdir(tmp_path, monkeypatch):
    """A private Telegram folder with the fixture symbol list and no API keys."""
    monkeypatch.setenv("SCREENER_TELEGRAM_DIR", str(tmp_path / "telegram"))
    monkeypatch.delenv("TELEGRAM_API_ID", raising=False)
    monkeypatch.delenv("TELEGRAM_API_HASH", raising=False)
    store.ensure_dir()
    store.symbols_path().write_text(FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")
    yield tmp_path / "telegram"
    if store._store is not None:
        store._store.close()
        store._store = None


# ─── symbols ──────────────────────────────────────────────────────────────────

def test_parse_csv_reads_nse_columns_with_bom_and_spaces():
    rows = symbols.parse_csv("﻿" + FIXTURE.read_text(encoding="utf-8"))
    assert ("BEL", "Bharat Electronics Limited") in rows
    assert len(rows) == 7


def test_symbols_found_by_tag_symbol_name_and_alias(index):
    assert index.find("#bel looks good") == [("BEL", "tag")]
    assert index.find("NSE: TCS and $INFY") == [("TCS", "tag"), ("INFY", "tag")]
    assert index.find("SUZLON breaking out") == [("SUZLON", "symbol")]
    assert index.find("Tata Motors near support") == [("TATAMOTORS", "name")]
    assert index.find("mazagon flying") == [("MAZDOCK", "name")]


def test_trading_words_are_not_tickers(index):
    # STAR is a real symbol but also a word; bare capitals don't count, a tag does.
    assert index.find("STAR performer, BUY now, SL tight") == []
    assert index.find("#STAR on watch") == [("STAR", "tag")]
    assert index.find("bel is lower case, not a ticker") == []


def test_unknown_tags_are_reported(index):
    assert index.unknown_tags("#XYZCORP and #BEL") == ["XYZCORP"]


# ─── levels, timeframe, events ────────────────────────────────────────────────

def test_levels_entry_range_stop_and_targets():
    lv = parse_levels("Buy above 310-315 SL 298 targets 330/345/360")
    assert (lv["entry_low"], lv["entry_high"], lv["stop"]) == (310, 315, 298)
    assert lv["targets"] == [330, 345, 360]


def test_levels_with_stock_between_buy_and_price_and_rupee_commas():
    lv = parse_levels("Buy TCS at ₹3,500 stop loss 3,400 T1 3,700 T2 3,850")
    assert (lv["entry_low"], lv["stop"], lv["targets"]) == (3500, 3400, [3700, 3850])


def test_unstated_levels_stay_none():
    lv = parse_levels("BEL looking strong")
    assert lv == {"entry_low": None, "entry_high": None, "stop": None, "targets": [], "cmp": None}


def test_target_hit_number_is_not_a_target_price():
    assert parse_levels("Target 1 done")["targets"] == []


@pytest.mark.parametrize("text,expected", [
    ("BTST call", "btst"), ("intraday only", "intraday"), ("swing 2-3 weeks", "swing"),
    ("positional 3 months", "positional"), ("long term investment", "long_term"), ("no horizon", None),
])
def test_timeframe(text, expected):
    assert parse_timeframe(text) == expected


@pytest.mark.parametrize("text,event", [
    ("#BEL buy 310 SL 298 tgt 340", "entry"),
    ("#BEL SL hit, exit", "stop_hit"),
    ("#BEL T2 achieved 🎯", "target_hit"),
    ("#BEL book partial profits", "exit"),
    ("#BEL call cancelled, don't buy", "cancel"),
    ("#BEL trail SL to 320", "update"),
    ("#BEL chart looks interesting", "mention"),
    ("Avoid chasing, buy #BEL at 310 SL 298", "entry"),
])
def test_event_classification(index, text, event):
    segs = extract_message(text, index)
    assert [s.event for s in segs] == [event]


def test_multi_stock_post_splits_levels_by_line(index):
    segs = extract_message("TCS buy 3500 SL 3400 TGT 3700\nINFY buy 1500 sl 1450 tgt 1600", index)
    assert [(s.symbols, s.levels["stop"]) for s in segs] == [(["TCS"], 3400), (["INFY"], 1450)]


def test_two_stocks_on_one_line_get_no_levels(index):
    segs = extract_message("Buy TCS and INFY, SL 1450 tgt 1600\nBEL watch", index)
    assert segs[0].symbols == ["TCS", "INFY"]
    assert segs[0].levels_ambiguous and segs[0].levels["stop"] is None


def test_reply_without_stock_inherits_parent(index):
    assert extract_message("Target 1 done ✅", index, inherited=["BEL"])[0].symbols == ["BEL"]
    assert extract_message("Target 1 done ✅", index, inherited=["BEL", "TCS"]) == []


# ─── calls across posts ───────────────────────────────────────────────────────

def _t(day: int, hour: int = 4) -> str:
    return f"2026-11-{day:02d}T{hour:02d}:00:00Z"


def test_calls_record_every_update_and_close(index):
    posts = [
        Post(1, 10, _t(10), "#BEL buy 310-315 SL 298 targets 330/345/360 swing"),
        Post(1, 11, _t(11), "Target 1 done ✅", reply_to=10),
        Post(1, 12, _t(12), "BEL T3 achieved 🎯"),
        Post(1, 20, _t(12), "#TCS buy 3500 SL 3400 TGT 3700", deleted=True),
        Post(1, 21, _t(13), "TCS SL hit"),
        Post(1, 30, _t(13), "INFY SL hit"),
        Post(1, 40, _t(14), "#SUZLON buy 60 SL 55 tgt 70"),
        Post(1, 41, _t(14, 6), "SUZLON call cancelled"),
        Post(1, 50, _t(14), "", media="photo"),
        Post(1, 60, _t(15), "Mazagon looking strong"),
    ]
    out = build_calls(posts, index)
    calls = {c["symbol"]: c for c in out["calls"]}

    bel = calls["BEL"]
    assert bel["status"] == "closed_target"
    assert [e["type"] for e in bel["events"]] == ["target_hit", "target_hit"]
    assert bel["timeframe"] == "swing" and bel["missing"] == []

    # A losing call deleted by the channel still counts.
    assert calls["TCS"]["status"] == "closed_stop" and calls["TCS"]["deleted"] is True
    assert calls["TCS"]["missing"] == ["timeframe"]
    assert calls["SUZLON"]["status"] == "cancelled"

    assert [(e["symbol"], e["type"]) for e in out["unlinked_events"]] == [("INFY", "stop_hit")]
    assert [m["symbol"] for m in out["mentions"]] == ["MAZDOCK"]
    assert out["image_only_posts"] == 1


def test_partial_exit_keeps_call_open(index):
    out = build_calls([
        Post(1, 1, _t(10), "#BEL buy 310 SL 298 tgt 340"),
        Post(1, 2, _t(11), "#BEL book partial profits"),
    ], index)
    assert out["calls"][0]["status"] == "open"
    assert out["calls"][0]["events"][0]["partial"] is True


# ─── message conversion ───────────────────────────────────────────────────────

def _msg(mid, text="", reply=None, date=None, photo=None):
    return SimpleNamespace(id=mid, message=text, reply_to=reply, date=date or datetime(2026, 11, 20, 4, tzinfo=timezone.utc),
                           edit_date=None, photo=photo, document=None, web_preview=None, media=photo)


def test_topic_and_parent_in_forum_and_plain_channel():
    in_topic = SimpleNamespace(reply_to_msg_id=7, reply_to_top_id=None, forum_topic=True)
    reply_in_topic = SimpleNamespace(reply_to_msg_id=55, reply_to_top_id=7, forum_topic=True)
    assert tg.topic_and_parent(_msg(1, reply=in_topic), forum=True) == (7, None)
    assert tg.topic_and_parent(_msg(2, reply=reply_in_topic), forum=True) == (7, 55)
    assert tg.topic_and_parent(_msg(3), forum=True) == (1, None)   # General topic
    plain_reply = SimpleNamespace(reply_to_msg_id=9, reply_to_top_id=None, forum_topic=False)
    assert tg.topic_and_parent(_msg(4, reply=plain_reply), forum=False) == (None, 9)


def test_to_row_marks_media_and_utc_time():
    row = tg.to_row(_msg(5, photo=object()), chat_id=123, forum=False)
    assert row["media"] == "photo" and row["text"] == ""
    assert row["posted_at"] == "2026-11-20T04:00:00Z"


def test_poll_interval_market_hours():
    ist = timezone(timedelta(hours=5, minutes=30))
    assert tg.poll_interval(datetime(2026, 11, 26, 10, 0, tzinfo=ist)) == 15 * 60    # Thursday 10 AM
    assert tg.poll_interval(datetime(2026, 11, 26, 20, 0, tzinfo=ist)) == 60 * 60    # evening
    assert tg.poll_interval(datetime(2026, 11, 28, 10, 0, tzinfo=ist)) == 60 * 60    # Saturday


# ─── local store ──────────────────────────────────────────────────────────────

@pytest.mark.skipif(os.name == "nt", reason="POSIX permissions")
def test_store_files_are_owner_only(tgdir):
    store.write_session("secret-session-string")
    db = store.get_store()
    for p in (store.session_path(), store.db_path()):
        assert stat.S_IMODE(p.stat().st_mode) == 0o600, p
    assert stat.S_IMODE(tgdir.stat().st_mode) == 0o700
    assert store.read_session() == "secret-session-string"
    db.close()
    store._store = None


def test_store_edits_and_deletions(tgdir):
    db = store.get_store()
    db.upsert([{"chat_id": 1, "msg_id": 1, "posted_at": _t(20), "text": "#BEL buy 310"}])
    db.upsert([{"chat_id": 1, "msg_id": 1, "posted_at": _t(20), "text": "#BEL buy 312", "edited_at": _t(20, 5)}])
    assert db.mark_deleted(1, [1]) == 1
    rows = db.messages(_t(1))
    assert len(rows) == 1 and rows[0]["text"] == "#BEL buy 312" and rows[0]["deleted"] == 1
    db.save_state(1, last_msg_id=1, last_ok_at=_t(20))
    assert db.state(1)["last_msg_id"] == 1


# ─── tools ────────────────────────────────────────────────────────────────────

async def test_status_lists_next_steps_when_not_set_up(tgdir):
    st = await telegram_tools.telegram_status()
    assert st["ready"] is False and not st["configured"] and not st["signed_in"]
    assert any("my.telegram.org" in s for s in st["next_steps"])
    assert st["symbol_list"]["symbols"] == 7


async def test_mentions_without_channels_is_a_clear_error(tgdir):
    env = await _call_safely(telegram_tools.get_channel_mentions)
    assert env["status"] == "error" and env["error"]["type"] == "telegram_not_set_up"


async def test_mentions_reports_calls_with_missing_levels(tgdir):
    store.write_channels([{"id": 1, "access_hash": 2, "title": "Momentum Picks", "forum": False, "topics": None}])
    now = datetime.now(timezone.utc)
    iso = lambda d: (now - timedelta(days=d)).isoformat(timespec="seconds").replace("+00:00", "Z")  # noqa: E731
    store.get_store().upsert([
        {"chat_id": 1, "msg_id": 1, "posted_at": iso(3), "text": "#BEL buy 310 SL 298 swing"},
        {"chat_id": 1, "msg_id": 2, "posted_at": iso(2), "text": "SL hit", "reply_to": 1},
        {"chat_id": 1, "msg_id": 3, "posted_at": iso(1), "text": "", "media": "photo"},
        {"chat_id": 1, "msg_id": 4, "posted_at": iso(40), "text": "#TCS buy 3500 SL 3400 tgt 3700"},
    ])
    env = to_envelope(await telegram_tools.get_channel_mentions(days=7))
    assert env["status"] == "partial"
    assert env["missing_fields"] == ["BEL@1.targets"]
    stocks = env["data"]["stocks"]
    assert [s["symbol"] for s in stocks] == ["BEL"]          # TCS is outside the 7 days
    call = stocks[0]["calls"][0]
    assert call["status"] == "closed_stop" and call["targets"] == []
    assert call["link"] == "https://t.me/c/1/1" and call["channel"] == "Momentum Picks"
    assert any("images with no caption" in w for w in env["warnings"])

    only = to_envelope(await telegram_tools.get_channel_mentions(days=60, symbol="TCS"))
    assert [s["symbol"] for s in only["data"]["stocks"]] == ["TCS"]


async def _call_safely(fn, *a):
    from screener_mcp.core.envelope import ToolError, error_envelope
    try:
        return to_envelope(await fn(*a))
    except ToolError as e:
        return error_envelope(e.message, e.error_type, **e.details)


# ─── poller against a stand-in client (needs Telethon for its error types) ────

class _FakeClient:
    def __init__(self, history, alive):
        self.history, self.alive, self.calls = history, alive, []

    def iter_messages(self, peer, **kw):
        self.calls.append(kw)
        start = kw.get("offset_id", 0)

        async def gen():
            for m in self.history:
                if m.id > start:
                    yield m
        return gen()

    async def get_messages(self, peer, ids):
        return [self.alive.get(i) for i in ids]


async def test_poll_stores_new_posts_edits_and_deletions(tgdir):
    pytest.importorskip("telethon")
    now = datetime.now(timezone.utc)
    m1 = _msg(1, "#BEL buy 310 SL 298", date=now - timedelta(hours=3))
    m2 = _msg(2, "#TCS buy 3500 SL 3400", date=now - timedelta(hours=2))
    ch = {"id": 9, "access_hash": 1, "title": "x", "forum": False, "topics": None}
    db = store.get_store()

    first = _FakeClient([m1, m2], {})
    assert (await tg._poll_channel(first, ch, db))["new"] == 2
    assert "offset_date" in first.calls[0]          # first run backfills by date

    edited = _msg(1, "#BEL buy 312 SL 298", date=m1.date)
    edited.edit_date = now
    m3 = _msg(3, "BEL T1 done", date=now)
    second = _FakeClient([m1, m2, m3], {1: edited})  # 2 is gone: deleted by the channel
    res = await tg._poll_channel(second, ch, db)
    assert second.calls[0]["offset_id"] == 2        # then only newer posts
    assert (res["new"], res["edited"], res["deleted"]) == (1, 1, 1)
    rows = {r["msg_id"]: r for r in db.messages("2000-01-01T00:00:00Z")}
    assert rows[1]["text"] == "#BEL buy 312 SL 298" and rows[2]["deleted"] == 1 and 3 in rows
    assert db.state(9)["last_msg_id"] == 3


async def test_poll_only_keeps_followed_topics(tgdir):
    pytest.importorskip("telethon")
    now = datetime.now(timezone.utc)
    t5 = SimpleNamespace(reply_to_msg_id=5, reply_to_top_id=None, forum_topic=True)
    t6 = SimpleNamespace(reply_to_msg_id=6, reply_to_top_id=None, forum_topic=True)
    msgs = [_msg(10, "#BEL buy 310 SL 298", reply=t5, date=now), _msg(11, "#TCS buy 1 SL 0.5", reply=t6, date=now)]
    ch = {"id": 9, "access_hash": 1, "forum": True, "topics": [5]}
    res = await tg._poll_channel(_FakeClient(msgs, {}), ch, store.get_store())
    assert res["new"] == 1
    assert [r["topic_id"] for r in store.get_store().messages("2000-01-01T00:00:00Z")] == [5]
    assert store.get_store().state(9)["last_msg_id"] == 11

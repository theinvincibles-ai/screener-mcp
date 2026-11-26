"""The user's portfolio and notes exist nowhere else: a damaged file must be an
error that changes nothing, never "empty" followed by an overwrite."""

import json

from screener_mcp import server
from screener_mcp.core import local_store
from screener_mcp.tools import notebook as nb
from screener_mcp.tools import portfolio as pf


def test_write_keeps_previous_version_as_backup(tmp_path):
    path = tmp_path / "data.json"
    local_store.write_json(path, {"v": 1})
    local_store.write_json(path, {"v": 2})
    assert json.loads(path.read_text(encoding="utf-8")) == {"v": 2}
    assert json.loads(local_store.backup_path(path).read_text(encoding="utf-8")) == {"v": 1}
    assert [p.name for p in tmp_path.iterdir() if p.suffix == ".tmp"] == []


def test_failed_write_leaves_original_untouched(tmp_path, monkeypatch):
    path = tmp_path / "data.json"
    local_store.write_json(path, {"v": 1})

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(local_store.os, "replace", boom)
    try:
        local_store.write_json(path, {"v": 2})
    except OSError:
        pass
    assert json.loads(path.read_text(encoding="utf-8")) == {"v": 1}
    assert [p.name for p in tmp_path.iterdir() if p.suffix == ".tmp"] == []


def test_missing_file_reads_as_default(tmp_path):
    assert local_store.read_json(tmp_path / "nope.json", {"holdings": {}}) == {"holdings": {}}


async def test_corrupt_portfolio_is_an_error_and_is_not_overwritten(tmp_path, monkeypatch):
    path = tmp_path / "portfolio.json"
    monkeypatch.setattr(pf, "_PORTFOLIO_PATH", path)
    pf._save({"holdings": {"TCS": {"quantity": 10, "avg_price": 3000}}})
    pf._save({"holdings": {"TCS": {"quantity": 12, "avg_price": 3000}}})
    path.write_text('{"holdings": {"TCS": {"quanti', encoding="utf-8")  # cut off mid-write

    env = await server.add_portfolio_stock("INFY", 5, 1500)
    assert env["status"] == "error"
    assert env["error"]["type"] == "local_data_unreadable"
    assert env["error"]["backup"].endswith("portfolio.json.bak")
    # the damaged file and the backup are both left exactly as they were
    assert path.read_text(encoding="utf-8") == '{"holdings": {"TCS": {"quanti'
    assert json.loads(local_store.backup_path(path).read_text(encoding="utf-8"))["holdings"]["TCS"]["quantity"] == 10

    env = await server.get_portfolio()
    assert env["status"] == "error" and env["error"]["type"] == "local_data_unreadable"


async def test_corrupt_note_is_not_overwritten_by_append_and_is_skipped_in_list(tmp_path, monkeypatch):
    monkeypatch.setattr(nb, "_BASE_DIR", tmp_path)
    nb._save(tmp_path / "TCS" / "good0001.json",
             {"note_id": "good0001", "symbol": "TCS", "entries": [{"timestamp": "2026-11-01", "text": "ok"}]})
    bad = tmp_path / "TCS" / "bad00001.json"
    bad.write_text("{not json", encoding="utf-8")

    env = await server.notebook_ai("append", note_id="bad00001", content="more")
    assert env["status"] == "error" and env["error"]["type"] == "local_data_unreadable"
    assert bad.read_text(encoding="utf-8") == "{not json"

    env = await server.notebook_ai("list")
    report = env["data"]["report"]
    assert "good0001" in report and "bad00001" in report and "could not be read" in report

from pathlib import Path

from autobot.codex import SessionStore


def test_session_store_round_trip(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "state" / "sessions.sqlite3")
    assert store.get("group:1") is None
    store.put("group:1", "thread-1")
    assert store.get("group:1") == "thread-1"
    store.put("group:1", "thread-2")
    assert store.get("group:1") == "thread-2"
    store.delete("group:1")
    assert store.get("group:1") is None
    store.close()

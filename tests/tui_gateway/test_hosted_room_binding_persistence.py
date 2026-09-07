"""Room bindings obey canonical storage without losing their identity."""
import hashlib
import json

import pytest

from gateway.hosted_room_driver import TaskIdentity
from gateway.hosted_rooms import MAX_ROOM_ID_CHARS
from hermes_state import SessionDB
from tui_gateway.hosted_room_driver import _task_session_title


def task(room_id, thread_id="thread-1", member_id="ops"):
    return {"identity": TaskIdentity(room_id, "task-1", thread_id, "turn-1"),
            "payload": {"target_member_id": member_id, "session_scope": "thread_member_v1"}}


def test_full_coordinates_persist_and_resolve_after_reopen(tmp_path):
    coordinates = [
        ("r00000000-0000-4000-8000-000000000001", "thread-1", "ops"),
        ("r" * (MAX_ROOM_ID_CHARS - 1) + "a", "thread-1", "ops"),
        ("r" * (MAX_ROOM_ID_CHARS - 1) + "b", "thread-1", "ops"),
        ("r" * (MAX_ROOM_ID_CHARS - 1) + "b", "thread-2", "ops"),
        ("r" * (MAX_ROOM_ID_CHARS - 1) + "b", "thread-2", "lead"),
    ]
    path = tmp_path / "sessions.db"
    database = SessionDB(path)
    try:
        for index, values in enumerate(coordinates):
            session_id = f"native-binding-{index}"
            database.create_session(session_id, source="bot_room")
            title = _task_session_title(task(*values))
            database.set_session_title(session_id, title)
            assert database.get_session_title(session_id) == title
    finally:
        database.close()
    database = SessionDB(path)
    try:
        for index, values in enumerate(coordinates):
            title = _task_session_title(task(*values))
            assert database.get_session_by_title(title)["id"] == f"native-binding-{index}"
    finally:
        database.close()


def test_existing_short_scoped_bindings_are_not_orphaned(tmp_path):
    legacy_digest = hashlib.sha256(json.dumps(["thread-1", "ops"], ensure_ascii=True).encode()).hexdigest()
    old_title = f"Group: room-1 | scope:{legacy_digest}"
    database = SessionDB(tmp_path / "legacy.db")
    try:
        database.create_session("already-existing", source="bot_room")
        database.set_session_title("already-existing", old_title)
        assert database.get_session_by_title(_task_session_title(task("room-1")))["id"] == "already-existing"
    finally:
        database.close()

"""Read state is monotonic, durable and distinct from delivery."""
import pytest

from gateway import hosted_rooms as rooms
from tests.tui_gateway.test_groups_history import room_service, original, call
import tui_gateway.server as srv


def test_room_and_thread_read_bounds_survive_reopen_without_replay_unread(room_service):
    service, room = room_service
    actor = {"kind": "user", "id": "incoming"}
    first = original(service, room, "first", actor=actor)
    own = original(service, room, "own")
    second = original(service, room, "second", actor=actor, thread="t2")
    assert call("groups.read.get")["unread_count"] == 2
    marked = call("groups.read.mark", thread_id="t1", through_seq=own["seq"])
    assert marked["unread_count"] == 0
    assert call("groups.read.get", thread_id="t2")["unread_count"] == 1
    # A thread-local mark does not hide an unread sibling in the room feed.
    assert call("groups.read.get")["unread_count"] == 1
    latest = call("groups.read.mark", through_seq=second["seq"])
    assert latest["unread_count"] == 0
    assert call("groups.read.mark", through_seq=first["seq"])["through_seq"] == latest["through_seq"]
    from gateway.hosted_room_history import read_cursor
    reopened = read_cursor(service.db_path, room_id="room", reader={"kind": "user", "id": "desktop"}, thread_id="t2")
    assert reopened["through_seq"] == latest["through_seq"]
    assert reopened["unread_count"] == 0
    original(service, room, "second", actor=actor, thread="t2")
    assert call("groups.read.get")["unread_count"] == 0
    assert "room_read_cursors_v1" in call("groups.capabilities")["features"]


def test_invalid_marks_and_forged_reader_do_not_advance_other_scopes(room_service):
    service, room = room_service
    source = original(service, room, actor={"kind": "user", "id": "incoming"})
    for params in ({"through_seq": True}, {"through_seq": source["seq"] + 1},
                   {"through_seq": 0, "thread_id": "missing"}):
        assert "groups.read.mark" in srv._methods
        assert "error" in srv._methods["groups.read.mark"](1, {"room_id": "room", **params})
    assert call("groups.read.get")["through_seq"] == 0
    mark = call("groups.read.mark", through_seq=source["seq"], reader={"kind": "user", "id": "forged"})
    assert mark["reader"] == {"kind": "user", "id": "desktop"}
    from gateway.hosted_room_history import read_cursor
    assert read_cursor(service.db_path, room_id="room", reader={"kind": "user", "id": "forged"})["through_seq"] == 0

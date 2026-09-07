"""Replies keep a durable original-event parent in the correct room thread."""
import pytest

from gateway import hosted_rooms as rooms
from tests.tui_gateway.test_groups_history import room_service, original, call
import tui_gateway.server as srv


def test_reply_inherits_durable_parent_thread_and_retains_tombstoned_parent(room_service):
    service, room = room_service
    parent = original(service, room)
    reply = call("groups.send", event_id="reply", payload={"text": "Reply", "parent_event_id": parent["event_id"]})["event"]
    assert reply["payload"]["thread_id"] == parent["payload"]["thread_id"]
    assert reply["payload"]["parent_event_id"] == parent["event_id"]
    call("groups.message.delete", event_id="delete-parent", target_event_id=parent["event_id"], expected_revision=parent["seq"])
    second = call("groups.send", event_id="reply-after-delete", payload={"text": "Discuss historical parent",
                                                                          "parent_event_id": parent["event_id"]})["event"]
    assert second["payload"]["thread_id"] == reply["payload"]["thread_id"]
    projected = call("groups.history", thread_id=parent["payload"]["thread_id"])["messages"]
    assert next(m for m in projected if m["event_id"] == parent["event_id"])["deleted"]
    assert next(m for m in projected if m["event_id"] == second["event_id"])["parent_event_id"] == parent["event_id"]
    assert "thread_parent_references_v1" in call("groups.capabilities")["features"]


def test_reply_rejects_missing_parent_and_cross_thread_coordinates_before_append(room_service):
    service, room = room_service
    parent = original(service, room)
    before = rooms.room_state(service.db_path, room_id="room")["latest_seq"]
    for payload in ({"text": "bad", "parent_event_id": "absent"},
                    {"text": "bad", "parent_event_id": parent["event_id"], "thread_id": "other-thread"}):
        result = srv._methods["groups.send"](1, {"room_id": "room", "event_id": "bad", "payload": payload})
        assert "error" in result
    assert rooms.room_state(service.db_path, room_id="room")["latest_seq"] == before

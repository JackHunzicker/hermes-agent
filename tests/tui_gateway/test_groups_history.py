"""Real RPC/storage contracts for immutable shared-message mutations."""
import json
import sqlite3

import pytest

from gateway import hosted_rooms as rooms
from tui_gateway import methods_groups
from tui_gateway.hosted_room_service import HostedRoomService
from tests.tui_gateway.hosted_room_service_fixtures import _server
import tui_gateway.server as srv


@pytest.fixture
def room_service(tmp_path, monkeypatch):
    service = HostedRoomService(_server(), db_path=tmp_path / "state.db")
    service.local_profiles = lambda: ("default", "ops")
    monkeypatch.setattr(srv, "get_hosted_room_service", lambda: service)
    monkeypatch.setattr(rooms, "default_db_path", lambda: service.db_path)
    room = service.create_room(room_id="room", name="Shared history", members=[
        {"member_id": p, "profile": p, "handle": p} for p in ("default", "ops")])
    yield service, room


def call(method, **params):
    assert method in srv._methods, f"missing connected RPC: {method}"
    reply = srv._methods[method](1, {"room_id": "room", **params})
    assert "error" not in reply, reply
    return reply["result"]


def original(service, room, event_id="original", actor=None, thread="t1"):
    return rooms.append_event(service.db_path, room_id="room", event_id=event_id,
        kind="message.user", actor=actor or {"kind": "user", "id": "desktop"},
        payload={"text": "Original exact\r\nβ bytes", "thread_id": thread},
        authority_gateway_id=room["authority_gateway_id"], authority_epoch=room["authority_epoch"])


def test_registered_mutations_preserve_originals_and_project_snapshot_search(room_service):
    service, room = room_service
    source = original(service, room)
    before = json.dumps(source, sort_keys=True)
    params = dict(event_id="edit-1", target_event_id=source["event_id"],
                  expected_revision=source["seq"], text="Corrected β text")
    edited = call("groups.message.edit", **params)
    assert edited["message"]["text"] == params["text"]
    assert edited["message"]["actor"] == source["actor"]
    assert call("groups.message.edit", **params)["event"] == {**edited["event"], "idempotent": True}
    assert "error" in srv._methods["groups.message.edit"](2, {"room_id": "room", **params,
        "event_id": "stale", "text": "lost update"})
    reaction = call("groups.message.react", event_id="react", target_event_id=source["event_id"],
                    reaction="👍", present=True)
    assert reaction["message"]["reactions"] == [{"reaction": "👍", "actors": [source["actor"]]}]
    assert call("groups.message.react", event_id="react", target_event_id=source["event_id"],
                reaction="👍", present=True)["idempotent"]
    snapshot = call("groups.history.search", query="CORRECTED β")
    assert [m["event_id"] for m in snapshot["messages"]] == [source["event_id"]]
    deleted = call("groups.message.delete", event_id="delete", target_event_id=source["event_id"],
                   expected_revision=edited["message"]["revision"])
    assert deleted["message"]["deleted"] and deleted["message"]["text"] is None
    assert call("groups.history.search", query="corrected")["messages"] == []
    assert call("groups.history.search", query="corrected", snapshot_seq=snapshot["snapshot_seq"])["messages"]
    assert call("groups.history")["messages"][0]["deleted"]
    assert "error" in srv._methods["groups.message.edit"](3, {"room_id": "room", **params,
        "event_id": "after-delete", "expected_revision": deleted["message"]["revision"]})
    reloaded = next(e for e in rooms.read_events(service.db_path, room_id="room")["events"]
                    if e["event_id"] == source["event_id"])
    assert json.dumps(reloaded, sort_keys=True) == before
    other = original(service, room, "foreign", actor={"kind": "user", "id": "other"})
    assert "error" in srv._methods["groups.message.delete"](4, {"room_id": "room",
        "event_id": "forged", "target_event_id": other["event_id"], "expected_revision": other["seq"],
        "actor": other["actor"]})
    features = call("groups.capabilities")["features"]
    assert "message_mutations_v1" in features
    assert "groups.history" in srv._LONG_HANDLERS


def test_legacy_reader_is_fenced_by_mutations_outside_its_requested_page(room_service):
    service, room = room_service
    source = original(service, room)
    assert call("groups.log", limit=1)["events"]
    call("groups.message.edit", event_id="edit", target_event_id=source["event_id"],
         expected_revision=source["seq"], text="replacement")
    reply = srv._methods["groups.log"](1, {"room_id": "room", "limit": 1})
    assert reply["error"]["data"]["reason"] == "room_reader_upgrade_required"
    assert reply["error"]["data"]["required_features"] == ["message_mutations_v1"]
    assert "error" in srv._methods["groups.log"](1, {"room_id": "room", "supported_features": None})
    page = call("groups.log", supported_features=["message_mutations_v1"])
    assert {e["kind"] for e in page["events"]} >= {"message.user", "message.edited"}
    from gateway.platforms.api_server_room_controls import _visible_events
    with pytest.raises(rooms.HostedRoomError, match="reader"):
        _visible_events("room")

"""Scoped controls must preserve unrelated work and reject stale attempts."""
from types import SimpleNamespace

import pytest

from gateway import hosted_room_driver as driver, hosted_rooms
from tui_gateway.hosted_room_service import HostedRoomService
from tests.tui_gateway.hosted_room_service_fixtures import _server
from tests.tui_gateway.test_hosted_room_stop_replay import _room, _queue


@pytest.mark.parametrize("mode", ["legacy_bounded", "event_driven"])
def test_thread_stop_is_durable_scoped_and_replay_safe(tmp_path, monkeypatch, mode):
    db = tmp_path / "state.db"
    room = _room(db)
    service = HostedRoomService(_server(), db_path=db)
    from gateway.hosted_room_responder_policy import DEFAULT_POLICY, update_policy
    update_policy(service, room_id="room-1", event_id="policy", expected_revision=room["revision"],
                  policy={**DEFAULT_POLICY, "mode": mode})
    first = _queue(db, task_id="first", event_id="user-1", text="First")
    other = _queue(db, task_id="other", event_id="user-2", text="Other")
    from tui_gateway import methods_groups, methods_groups_controls
    monkeypatch.setattr(methods_groups, "get_hosted_room_service", lambda: service)
    server = SimpleNamespace(_methods={}, _ok=lambda rid, result: {"result": result},
                             _err=lambda rid, code, message, data=None: {"error": message})
    methods_groups_controls.register(server)
    result = server._methods["groups.stop_scope"]("rpc-1", {"room_id": "room-1",
        "cancel_id": "stop-1", "scope": {"kind": "thread", "thread_id": first.thread_id}})["result"]
    assert result["tasks"][0]["status"] == "cancelled"
    assert driver.get_task(db, other)["status"] == "queued"
    restarted = HostedRoomService(_server(), db_path=db)
    snapshot = restarted._policy_snapshot(restarted._room("room-1"))
    assert snapshot.events and snapshot.events[0]["payload"]["thread_id"] == other.thread_id
    replay = restarted.stop_scope("room-1", cancel_id="stop-1", scope={
        "kind": "thread", "thread_id": first.thread_id})
    assert replay["through_seq"] == result["through_seq"] and replay["idempotent"]
    with pytest.raises(ValueError):
        restarted.stop_scope("room-1", cancel_id="stop-1", scope={
            "kind": "thread", "thread_id": other.thread_id})
    restarted.stop_room("room-1", cancel_id="all")
    assert driver.get_task(db, other)["status"] == "cancelled"
    room = restarted._room("room-1")
    hosted_rooms.append_event(db, room_id="room-1", event_id="resume", kind="message.user",
        actor={"kind": "user", "id": "owner"}, payload={"text": "Resume", "thread_id": first.thread_id},
        authority_gateway_id=room["authority_gateway_id"], authority_epoch=room["authority_epoch"])
    resumed = restarted._policy_snapshot(restarted._room("room-1"))
    assert any(event["event_id"] == "resume" for event in resumed.events)


def test_exact_task_stop_rejects_changed_attempt_and_wrong_thread(tmp_path):
    db = tmp_path / "state.db"
    _room(db)
    identity = _queue(db, task_id="first", event_id="user-1", text="First")
    service = HostedRoomService(_server(), db_path=db)
    scope = {"kind": "task", "task_id": identity.task_id, "thread_id": identity.thread_id,
             "execution_generation": 0, "cancel_generation": 0}
    for changed in ({"execution_generation": 1}, {"thread_id": "other"}):
        with pytest.raises(ValueError):
            service.stop_scope("room-1", cancel_id="bad", scope={**scope, **changed})
        assert driver.get_task(db, identity)["status"] == "queued"
    result = service.stop_scope("room-1", cancel_id="exact", scope=scope)
    assert result["tasks"][0]["task_id"] == identity.task_id
    assert result["tasks"][0]["status"] == "cancelled"
    assert service.stop_scope("room-1", cancel_id="exact", scope=scope)["idempotent"]
    # Persisted Stop survives a crash between log admission and the driver call.
    later = _queue(db, task_id="later", event_id="user-2", text="Later")
    from gateway.hosted_room_scoped_controls import append_stop
    append_stop(db, service._room("room-1"), "crashed", {"kind": "task",
        "thread_id": later.thread_id, "task_id": later.task_id,
        "execution_generation": 0, "cancel_generation": 0})
    restarted = HostedRoomService(_server(), db_path=db)
    restarted._apply_scoped_stop_fences("room-1")
    assert driver.get_task(db, later)["status"] == "cancelled"

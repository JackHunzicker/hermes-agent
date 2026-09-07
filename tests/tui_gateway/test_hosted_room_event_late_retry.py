"""Late committed attempts reopen event routing without changing frozen inputs."""
from copy import deepcopy
import pytest
from gateway import hosted_room_driver as driver, hosted_rooms as rooms
from tests.tui_gateway.test_hosted_room_retry_handoff import _service, _send, _queued, _finish
from tests.tui_gateway.test_hosted_room_responder_policy import POLICY, rpc_for


def test_late_retry_after_idle_routes_new_mention_once(tmp_path):
    service, binding = _service(tmp_path)
    room = rooms.room_state(service.db_path, room_id="workshop")
    policy = {**POLICY, "mode": "event_driven", "leader_member_id": "writer"}
    assert "result" in rpc_for(service)["groups.policy.update"](1, {"room_id": "workshop", "event_id": "policy",
        "expected_revision": room["revision"], "policy": policy})
    _send(service, "first", "@writer begin")
    original = _queued(service)[0]
    frozen = deepcopy(original["payload"])
    service.runtime._process_room(binding)
    assert driver.list_tasks(service.db_path, room_id="workshop", status="deferred")
    service.prepare_room(binding)
    assert not _queued(service)
    retry = service.retry_room_task("workshop", task_id=original["identity"].task_id, retry_id="retry")
    _finish(service, binding, retry, "@reviewer new late result")
    queued = _queued(service)
    assert len(queued) == 1 and queued[0]["payload"]["target_profile"] == "reviewer"
    assert "new late result" in queued[0]["payload"]["prompt"]
    assert next(t for t in driver.list_tasks(service.db_path, room_id="workshop") if t["identity"] == original["identity"])["payload"] == frozen
    service.prepare_room(binding)
    assert _queued(service) == queued
    assert len([e for e in service._events("workshop") if e["kind"] == "message.user"]) == 1


def test_configured_leader_cannot_be_retired_until_policy_changes(tmp_path):
    from tests.tui_gateway.test_hosted_room_membership import service_at, MEMBERS
    service = service_at(tmp_path / "state.db")
    room = service.create_room(room_id="room-1", name="Leader", members=MEMBERS)
    updated = rpc_for(service)["groups.policy.update"](1, {"room_id": "room-1", "event_id": "policy",
        "expected_revision": room["revision"], "policy": {**POLICY, "mode": "event_driven"}})["result"]["room"]
    with pytest.raises(rooms.RoomConflictError, match="before removing its leader"):
        service.update_members(room_id="room-1", event_id="roster", expected_revision=updated["revision"],
            members=[MEMBERS[0], MEMBERS[2]])
    assert rooms.room_state(service.db_path, room_id="room-1")["members"] == updated["members"]
    changed = rpc_for(service)["groups.policy.update"](1, {
        "room_id": "room-1", "event_id": "change-leader", "expected_revision": updated["revision"],
        "policy": {**POLICY, "mode": "event_driven", "leader_member_id": MEMBERS[0]["member_id"]},
    })["result"]["room"]
    removed = service.update_members(room_id="room-1", event_id="roster", expected_revision=changed["revision"],
        members=[MEMBERS[0], MEMBERS[2]])
    assert {m["member_id"] for m in removed["members"]} == {MEMBERS[0]["member_id"], MEMBERS[2]["member_id"]}
    assert MEMBERS[1]["member_id"] in {m["member_id"] for m in removed["retired_members"]}

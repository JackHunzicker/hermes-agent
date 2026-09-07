"""Accepted canonical inputs survive bounded checkpoint eviction and roster changes."""
from gateway import hosted_rooms as rooms, hosted_room_discussion as discussion
from gateway.hosted_room_history import mutate_message
from tests.tui_gateway.test_hosted_room_membership import service_at, MEMBERS
from tests.tui_gateway.test_hosted_room_responder_policy import POLICY, rpc_for


def test_oldest_pending_input_survives_notice_flood_and_restart(tmp_path):
    db = tmp_path / "state.db"
    service = service_at(db)
    room = service.create_room(room_id="room-1", name="Backlog", members=MEMBERS)
    assert "result" in rpc_for(service)["groups.policy.update"](1, {"room_id": "room-1", "event_id": "policy",
        "expected_revision": room["revision"], "policy": {**POLICY, "mode": "event_driven"}})
    first = service.send(room_id="room-1", event_id="old", payload={"text": "@ops oldest input", "thread_id": "t"})
    service.send(room_id="room-1", event_id="new", payload={"text": "@ops later input", "thread_id": "t"})
    for index in range(30):
        mutate_message(db, room_id="room-1", event_id=f"reaction-{index}", target_event_id="new", actor=first["actor"],
            operation="react", reaction="check", present=bool(index % 2),
            authority_gateway_id=room["authority_gateway_id"], authority_epoch=room["authority_epoch"])
    room = rooms.room_state(db, room_id="room-1")
    service._policy_snapshot(room)
    reopened = service_at(db)
    snapshot = reopened._policy_snapshot(room)
    task = discussion.plan_next_task(room, snapshot.events, local_profiles=reopened.local_profiles(),
        initial_watermarks=snapshot.watermarks, freeze_input_context=True).task
    assert task.member.member_id == "ops"
    assert task.payload["input_context"]["event_seqs"] == [first["seq"]]
    assert "oldest input" in task.payload["prompt"]


def test_retired_historical_mention_does_not_block_new_ingress(tmp_path):
    db = tmp_path / "state.db"
    service = service_at(db)
    room = service.create_room(room_id="room-1", name="Roster", members=MEMBERS)
    changed = rpc_for(service)["groups.policy.update"](1, {"room_id": "room-1", "event_id": "policy",
        "expected_revision": room["revision"], "policy": {**POLICY, "mode": "event_driven",
            "default_responder": "all", "leader_member_id": None}})["result"]["room"]
    rooms.append_event(db, room_id="room-1", event_id="old", kind="message.user", actor={"kind": "user", "id": "desktop"},
        payload={"text": "@ops old input", "thread_id": "t"}, authority_gateway_id=room["authority_gateway_id"], authority_epoch=room["authority_epoch"])
    service.update_members(room_id="room-1", event_id="roster", expected_revision=changed["revision"], members=[MEMBERS[0], MEMBERS[2]])
    assert service.send(room_id="room-1", event_id="new", payload={"text": "@review new input", "thread_id": "t"})["event_id"] == "new"

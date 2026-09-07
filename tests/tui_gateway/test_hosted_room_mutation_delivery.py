"""Mutation notices travel through real policy and frozen-task reconstruction."""
from gateway import hosted_rooms as rooms, hosted_room_discussion as discussion
from gateway.hosted_room_history import mutate_message
from tests.tui_gateway.test_hosted_room_membership import service_at, MEMBERS
from tests.tui_gateway.test_hosted_room_responder_policy import POLICY, rpc_for


def test_mutation_notice_is_new_attributed_input_and_reconstructs_after_restart(tmp_path):
    db = tmp_path / "state.db"
    service = service_at(db)
    room = service.create_room(room_id="room-1", name="Mutation delivery", members=MEMBERS)
    changed = rpc_for(service)["groups.policy.update"](1, {"room_id": "room-1", "event_id": "policy",
        "expected_revision": room["revision"], "policy": {**POLICY, "mode": "event_driven"}})
    assert "result" in changed, changed
    source = service.send(room_id="room-1", event_id="u", payload={"text": "@ops Original", "thread_id": "t"})
    room = rooms.room_state(db, room_id="room-1")
    initial = discussion.plan_next_task(room, service._events("room-1"), local_profiles=service.local_profiles(),
                                        freeze_input_context=True).task
    publication = discussion.plan_publication(room, service._events("room-1"), initial, status="settled",
        result={"text": "PASS"}, local_profiles=service.local_profiles())
    for event in publication.events:
        rooms.append_event(db, **event.append_kwargs("room-1"))
    edited = mutate_message(db, room_id="room-1", event_id="edit", target_event_id="u", actor=source["actor"],
        operation="edit", text="Corrected information", expected_revision=source["seq"],
        authority_gateway_id=room["authority_gateway_id"], authority_epoch=room["authority_epoch"])
    room = rooms.room_state(db, room_id="room-1")
    snapshot = service._policy_snapshot(room)
    planned = discussion.plan_next_task(room, snapshot.events, local_profiles=service.local_profiles(),
        initial_watermarks=snapshot.watermarks, freeze_input_context=True).task
    assert "Message edited: u" in planned.payload["prompt"]
    assert "Corrected information" in planned.payload["prompt"]
    assert edited["event"]["seq"] in planned.payload["input_context"]["event_seqs"]
    reopened = service_at(db)
    rebuilt = discussion.reconstruct_task_plan(room, reopened._events("room-1"),
        {"identity": planned.identity, "payload": planned.payload}, local_profiles=reopened.local_profiles())
    assert rebuilt == planned
    raw = reopened._events("room-1")
    assert [e for e in raw if e["kind"] == "message.user"] == [source]
    assert next(e for e in raw if e["event_id"] == "edit")["actor"] == source["actor"]

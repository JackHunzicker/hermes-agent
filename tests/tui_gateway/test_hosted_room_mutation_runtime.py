"""No model calls: the existing service driver delivers durable notices once."""
import time
from gateway import hosted_room_driver as driver, hosted_rooms as rooms
from tests.tui_gateway.test_hosted_room_participant_tools import participant
from tui_gateway.hosted_room_driver import HostedRoomBinding
from tests.tui_gateway.hosted_room_service_fixtures import _FakeRPC, _wait_for
from tests.tui_gateway.test_hosted_room_membership import service_at, MEMBERS
from tests.tui_gateway.test_hosted_room_responder_policy import POLICY, rpc_for


def test_default_room_keeps_mutation_notice_in_next_runtime_input(participant):
    from gateway.hosted_room_history import mutate_message
    service, room, source, task, session = participant
    mutation = mutate_message(service.db_path, room_id=room["room_id"], event_id="correct-source",
        target_event_id=source["event_id"], actor=source["actor"], operation="edit",
        authority_gateway_id=room["authority_gateway_id"], authority_epoch=room["authority_epoch"],
        expected_revision=source["seq"], text="@ops corrected instructions")
    assert mutation["message"]["text"] == "@ops corrected instructions"
    driver.settle_task(service.db_path, session["_test_attempt"], settlement_id="old-input-done",
                       status="settled", result={"text": "PASS"}, clock=time.time)
    binding = HostedRoomBinding(room["room_id"], room["authority_gateway_id"], room["authority_epoch"])
    service.prepare_room(binding)
    service.send(room_id=room["room_id"], event_id="next-human", payload={"text": "@ops continue", "thread_id": task["identity"].thread_id})
    tasks = driver.list_tasks(service.db_path, room_id=room["room_id"], status="queued")
    assert tasks
    print("Post-edit queued prompt:", tasks[0]["payload"]["prompt"])
    assert "Message edited:" in tasks[0]["payload"]["prompt"] and "corrected instructions" in tasks[0]["payload"]["prompt"], \
        "Acknowledged edit is absent even from the default-policy participant's next runtime input"



class NoticeRPC(_FakeRPC):
    def __init__(self):
        super().__init__()
        self.calls = []

    def submit(self, **kwargs):
        self.calls.append(kwargs)
        kwargs["on_terminal"]({"status": "settled", "text": "PASS"})
        return {"accepted": True}


def configure(service, rpc):
    service.rpc = rpc
    service.runtime.rpc = rpc
    service.runtime.poll_interval_seconds = 0.05
    service.runtime.active_poll_interval_seconds = 0.02


def test_idle_mutations_reach_each_member_once_and_reopen_preserves_watermarks(tmp_path):
    db = tmp_path / "state.db"
    service, rpc = service_at(db), NoticeRPC()
    room = service.create_room(room_id="room-1", name="Notices", members=MEMBERS)
    changed = rpc_for(service)["groups.policy.update"](1, {"room_id": "room-1", "event_id": "policy",
        "expected_revision": room["revision"], "policy": {**POLICY, "mode": "event_driven", "max_turns_per_window": 16}})
    assert "result" in changed, changed
    configure(service, rpc)
    source = service.send(room_id="room-1", event_id="u", payload={"text": "@ops Initial", "thread_id": "t"})
    service.start()
    try:
        _wait_for(lambda: len(rpc.calls) == 1 and not service.status("room-1")["working"], timeout=10)
        changed = rpc_for(service)["groups.message.edit"](2, {"room_id": "room-1", "event_id": "edit",
            "target_event_id": "u", "expected_revision": source["seq"], "text": "Correction after idle"})
        assert "result" in changed, changed
        _wait_for(lambda: len(rpc.calls) >= 4 and not service.status("room-1")["working"], timeout=10)
        notices = [call for call in rpc.calls if "Message edited: u" in call["prompt"]]
        assert len(notices) == len(MEMBERS)
        assert {call["profile"] for call in notices} == {m["profile"] for m in MEMBERS}
        assert all("Correction after idle" in call["prompt"] for call in notices)
        for method, event_id, params, notice, expected_count in (
            ("groups.message.react", "reaction", {"reaction": "👍", "present": True}, "Reaction added", 7),
            ("groups.message.delete", "delete", {"expected_revision": changed["result"]["message"]["revision"]},
             "Message deleted: u", 10),
        ):
            result = rpc_for(service)[method](3, {"room_id": "room-1", "event_id": event_id,
                                                "target_event_id": "u", **params})
            assert "result" in result, result
            _wait_for(lambda: len(rpc.calls) >= expected_count and not service.status("room-1")["working"], timeout=10)
            # Recent context may retain prior notices; each new notice reaches all recipients.
            batch = rpc.calls[expected_count - len(MEMBERS):expected_count]
            assert {call["profile"] for call in batch} == {m["profile"] for m in MEMBERS}
            assert all(notice in call["prompt"] for call in batch)
    finally:
        assert service.stop(timeout=3)
    reopened = service_at(db)
    configure(reopened, rpc)
    room = rooms.room_state(db, room_id="room-1")
    snapshot = reopened._policy_snapshot(room)
    from gateway.hosted_room_discussion import plan_next_task
    decision = plan_next_task(room, snapshot.events, local_profiles=reopened.local_profiles(),
                              initial_watermarks=snapshot.watermarks, freeze_input_context=True)
    assert decision.task is None
    assert [e for e in reopened._events("room-1") if e["kind"] == "message.user"] == [source]

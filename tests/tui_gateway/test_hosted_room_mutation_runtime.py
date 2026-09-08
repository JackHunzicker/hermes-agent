"""No model calls: the existing service driver delivers durable notices once."""
import json
import time
import pytest
from gateway import hosted_room_driver as driver, hosted_rooms as rooms
from tests.tui_gateway.test_hosted_room_participant_tools import participant
from tui_gateway.hosted_room_driver import HostedRoomBinding
from tests.tui_gateway.hosted_room_service_fixtures import _FakeRPC, _wait_for
from tests.tui_gateway.test_hosted_room_membership import service_at, MEMBERS
from tests.tui_gateway.test_hosted_room_responder_policy import POLICY, rpc_for


@pytest.mark.parametrize("operation", ["edit", "delete"])
@pytest.mark.parametrize("terminal_text", ["PASS", "I used the old instructions"])
def test_default_room_keeps_mutation_notice_in_next_runtime_input(participant, operation, terminal_text):
    from gateway.hosted_room_history import mutate_message
    service, room, source, task, session = participant
    mutation = mutate_message(service.db_path, room_id=room["room_id"], event_id="correct-source",
        target_event_id=source["event_id"], actor=source["actor"], operation=operation,
        authority_gateway_id=room["authority_gateway_id"], authority_epoch=room["authority_epoch"],
        expected_revision=source["seq"], **({"text": "@ops corrected instructions"} if operation == "edit" else {}))
    assert mutation["event"]["kind"] == {"edit": "message.edited", "delete": "message.deleted"}[operation]
    driver.settle_task(service.db_path, session["_test_attempt"], settlement_id="old-input-done",
                       status="settled", result={"text": terminal_text}, clock=time.time)
    binding = HostedRoomBinding(room["room_id"], room["authority_gateway_id"], room["authority_epoch"])
    service.prepare_room(binding)
    service.send(room_id=room["room_id"], event_id="next-human", payload={"text": "@ops continue", "thread_id": task["identity"].thread_id})
    tasks = driver.list_tasks(service.db_path, room_id=room["room_id"], status="queued")
    assert tasks
    notice = "Message edited:" if operation == "edit" else "Message deleted:"
    assert notice in tasks[0]["payload"]["prompt"], "Acknowledged mutation is absent from the next runtime input"
    driver.release_lease(service.db_path, session["_test_lease"], clock=time.time)
    rpc = NoticeRPC()
    configure(service, rpc)
    service.start()
    try:
        _wait_for(lambda: len(rpc.calls) == 1 and not service.status(room["room_id"])["working"], timeout=10)
        prompt = rpc.calls[0]["prompt"]
        records = [json.loads(line) for line in prompt.splitlines() if line.strip().startswith('{')]
        correction = next(record for record in records if record["event_id"] == mutation["event"]["event_id"])
        assert correction["actor"] == source["actor"]
        assert notice in correction["content"]
        if operation == "edit":
            assert "@ops corrected instructions" in correction["content"]
        service.send(room_id=room["room_id"], event_id="after-correction", payload={"text": "@ops again", "thread_id": "thread-tools"})
        _wait_for(lambda: len(rpc.calls) == 2 and not service.status(room["room_id"])["working"], timeout=10)
        assert notice not in rpc.calls[1]["prompt"]
    finally:
        assert service.stop(timeout=3)
    assert next(e for e in service._events(room["room_id"]) if e["event_id"] == source["event_id"]) == source
    assert "@ops inspect" in task["payload"]["prompt"]
    assert notice not in task["payload"]["prompt"]



@pytest.mark.parametrize("budget", ["lines", "ascii_bytes", "escaped_unicode_bytes"])
def test_legacy_mutation_backlog_is_bounded_without_losing_oldest_notice(participant, budget):
    from gateway.hosted_room_history import mutate_message
    from gateway.hosted_room_policy_checkpoint import MAX_THREAD_TRANSCRIPT_EVENTS
    service, room, source, _, session = participant
    revision = source["seq"]
    mutations = []
    count = MAX_THREAD_TRANSCRIPT_EVENTS + 2 if budget == "lines" else 3
    body = {"lines": "", "ascii_bytes": "x" * 60000, "escaped_unicode_bytes": "🚀" * 5000}[budget]
    for index in range(count):
        result = mutate_message(service.db_path, room_id=room["room_id"], event_id=f"edit-{index}",
            target_event_id=source["event_id"], actor=source["actor"], operation="edit", text=f"correction {index} {body}",
            expected_revision=revision, authority_gateway_id=room["authority_gateway_id"], authority_epoch=room["authority_epoch"])
        revision = result["message"]["revision"]
        mutations.append(result["event"])
    driver.settle_task(service.db_path, session["_test_attempt"], settlement_id="old-input-done",
        status="settled", result={"text": "PASS"}, clock=time.time)
    binding = HostedRoomBinding(room["room_id"], room["authority_gateway_id"], room["authority_epoch"])
    service.prepare_room(binding)
    service.send(room_id=room["room_id"], event_id="next", payload={"text": "@ops next", "thread_id": "thread-tools"})
    queued = driver.list_tasks(service.db_path, room_id=room["room_id"], status="queued")[0]
    records = [json.loads(line) for line in queued["payload"]["prompt"].splitlines() if line.strip().startswith('{')]
    assert mutations[0]["event_id"] in {r["event_id"] for r in records}, "oldest accepted correction was skipped"
    assert len(records) <= MAX_THREAD_TRANSCRIPT_EVENTS
    represented = {record["event_id"] for record in records}
    consumed = set(queued["payload"]["input_context"]["event_seqs"])
    assert all(event["event_id"] in represented for event in mutations if event["seq"] in consumed)
    assert len(queued["payload"]["prompt"].encode("utf-8")) <= driver.MAX_PROMPT_BYTES
    with service.policy_checkpoint._connect() as conn:
        retained = conn.execute("SELECT COUNT(*) FROM hosted_room_policy_transcript WHERE room_id=? AND kind LIKE 'message.%'",
            (room["room_id"],)).fetchone()[0]
    assert retained <= MAX_THREAD_TRANSCRIPT_EVENTS
    driver.release_lease(service.db_path, session["_test_lease"], clock=time.time)
    rpc = NoticeRPC()
    configure(service, rpc)
    service.start()
    try:
        _wait_for(lambda: len(rpc.calls) == 1 and not service.status(room["room_id"])["working"], timeout=10)
    finally:
        assert service.stop(timeout=3)
    from tui_gateway.hosted_room_service import HostedRoomService
    from tests.tui_gateway.hosted_room_service_fixtures import _server
    reopened = HostedRoomService(_server(), db_path=service.db_path)
    reopened.local_profiles = service.local_profiles
    configure(reopened, rpc)
    reopened.send(room_id=room["room_id"], event_id="next-batch", payload={"text": "@ops continue", "thread_id": "thread-tools"})
    reopened.start()
    try:
        _wait_for(lambda: len(rpc.calls) == 2 and not reopened.status(room["room_id"])["working"], timeout=10)
    finally:
        assert reopened.stop(timeout=3)
    delivered = [json.loads(line)["event_id"] for call in rpc.calls for line in call["prompt"].splitlines()
                 if line.strip().startswith('{')]
    assert all(delivered.count(event["event_id"]) == 1 for event in mutations)


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

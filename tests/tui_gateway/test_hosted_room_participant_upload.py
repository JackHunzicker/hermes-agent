"""Core participant uploads cross the real submit proof and terminal publication path."""
import base64
import threading
import time
import pytest

from gateway import hosted_room_driver as driver
from tui_gateway import server
from tui_gateway.hosted_room_driver import HostedRoomBinding
from tests.tui_gateway.test_hosted_room_participant_tools import participant, call


def test_internal_submit_upload_publishes_exact_bytes_for_another_member(participant):
    service, room, _, task, session = participant
    attempt = session["_test_attempt"]
    data = b"# Shared findings\r\nUTF-8: \xc3\xa9\n"
    receipts = []

    def submit(rid, params):
        active = {"source": "bot_room", "history_lock": threading.RLock()}
        proof, callback = params["_hosted_task"], params["_hosted_terminal_callback"]
        assert server._hosted_submit_error(rid, active, proof, callback) is None
        error, _ = server._lock_in_submit_turn(rid, params["session_id"], active,
            params["text"], params, False, None, proof)
        assert error is None
        token = server._current_runtime_session_record.set(active)
        try:
            uploaded = call(operation="upload", name="findings.md", data_base64=base64.b64encode(data).decode())
            assert uploaded["ok"] is True, uploaded
            assert uploaded["publication"] == "on_turn_completion"
            assert call(operation="upload", name="findings.md", data_base64=base64.b64encode(data).decode())["artifact_id"] == uploaded["artifact_id"]
            revised = call(operation="upload", name="findings.md",
                data_base64=base64.b64encode(data + b"Revised\n").decode())
            assert revised["ok"] is True, revised
            assert revised["artifact_id"] != uploaded["artifact_id"]
            assert revised["sha256"] != uploaded["sha256"]
            assert call(operation="search", query="inspect")["messages"]
            callback({"status": "settled", "text": "@default review both versions of the shared findings."})
        finally:
            server._current_runtime_session_record.reset(token)
        return {"result": {"accepted": True}}

    service.rpc.server._methods["prompt.submit"] = submit
    service.rpc.bind_artifact_scope(task=task["identity"], execution_generation=attempt.execution_generation,
        member_id="ops", authority_gateway_id=room["authority_gateway_id"],
        authority_epoch=room["authority_epoch"], profile="ops")
    service.rpc.submit(profile="ops", session_id="internal-tools", prompt=task["payload"]["prompt"],
        source="bot_room", task=task["identity"], execution_generation=attempt.execution_generation,
        on_terminal=receipts.append)
    assert receipts[0]["artifacts"]["items"][0]["size"] == len(data)
    driver.settle_task(service.db_path, attempt, settlement_id="upload-finished", status="settled", result=receipts[0], clock=time.time)
    service.prepare_room(HostedRoomBinding(room["room_id"], room["authority_gateway_id"], room["authority_epoch"]))
    message = next(e for e in service._events(room["room_id"]) if e["kind"] == "message.member")
    attachments = message["payload"]["attachments"]
    assert len(attachments) == 2
    assert message["actor"]["id"] == "ops"
    assert message["payload"]["thread_id"] == task["identity"].thread_id
    assert {item["name"] for item in attachments} == {"findings.md"}
    assert len({item["attachment_id"] for item in attachments}) == 2
    stored_versions = {
        service.attachments.read(room_id=room["room_id"], attachment_id=item["attachment_id"],
            recipient_member_id="default", event_id=message["event_id"]).data
        for item in attachments
    }
    assert stored_versions == {data, data + b"Revised\n"}
    queued = driver.list_tasks(service.db_path, room_id=room["room_id"], status="queued")
    assert len(queued) == 1
    assert queued[0]["payload"]["target_member_id"] == "default"
    assert queued[0]["payload"]["attachments"] == attachments
    from gateway.hosted_room_policy_checkpoint import HostedRoomPolicyCheckpoint
    service.policy_checkpoint = HostedRoomPolicyCheckpoint(service.db_path)
    events = service._events(room["room_id"])
    service.prepare_room(HostedRoomBinding(room["room_id"], room["authority_gateway_id"], room["authority_epoch"]))
    assert service._events(room["room_id"]) == events
    assert driver.list_tasks(service.db_path, room_id=room["room_id"], status="queued") == queued
    assert call(operation="upload", name="late.md", data_base64=base64.b64encode(data).decode())["ok"] is False


def test_capabilities_distinguish_connected_core_tools_from_other_runtimes(participant, monkeypatch):
    service, _, _, _, _ = participant
    monkeypatch.setattr(server, "get_hosted_room_service", lambda: service)
    reply = server._methods["groups.capabilities"](1, {})
    assert reply["result"]["participant_tools"] == {"core": True, "native": False, "peer": False}
    assert {"participant_tools_v1", "participant_messages_v1"} <= set(reply["result"]["features"])


def test_rejected_upload_guard_cannot_retire_previous_attempt_bytes(participant):
    from dataclasses import replace
    from gateway.hosted_room_artifacts import RoomArtifactOutbox, RoomArtifactScope
    service, _, _, _, session = participant
    scope = RoomArtifactScope.from_mapping({k: v for k, v in session["_hosted_room_task"].items() if k not in {"thread_id", "turn_id"}})
    outbox = RoomArtifactOutbox(service.db_path)
    prior = outbox.put_bytes(scope=scope, data=b"earlier", source_name="prior.txt")
    def refuse(conn):
        raise ValueError("attempt stopped")
    with pytest.raises(ValueError, match="attempt stopped"):
        outbox.put_bytes(scope=replace(scope, execution_generation=scope.execution_generation+1),
            data=b"later", source_name="later.txt", write_guard=refuse)
    assert outbox.read(scope, prior["artifact_id"])[1] == b"earlier"

"""Room input targets the existing live waiter without creating another turn."""
import threading
import time

import pytest

from gateway import hosted_room_driver as state, hosted_rooms
from tui_gateway import server, methods_groups
from tui_gateway.hosted_room_service import HostedRoomService
from tests.tui_gateway.test_hosted_room_stop_replay import _room, _queue


def _waiting(tmp_path, monkeypatch, *, batch=False):
    db = tmp_path / "state.db"
    _room(db)
    identity = _queue(db, task_id="task", event_id="thread", text="Question")
    service = HostedRoomService(server, db_path=db)
    binding = service.bindings()[0]
    lease = state.acquire_lease(db, room_id=identity.room_id, gateway_id=binding.gateway_id,
        authority_epoch=binding.authority_epoch, process_generation=service.runtime.process_generation,
        ttl_seconds=60, clock=time.time)
    service.runtime._leases[identity.room_id] = lease
    state.start_task(db, identity, lease, expected_cancel_generation=0, clock=time.time)
    event, other = threading.Event(), threading.Event()
    marker = {"room_id": identity.room_id, "thread_id": identity.thread_id,
              "task_id": identity.task_id, "execution_generation": 1, "member_id": "one"}
    monkeypatch.setattr(server, "_sessions", {"live": {"session_key": "stored", "running": True,
        "history_lock": threading.Lock(), "_hosted_room_task": marker}})
    monkeypatch.setattr(server, "_pending", {"question": ("live", event), "other": ("elsewhere", other)})
    monkeypatch.setattr(server, "_pending_prompt_payloads", {
        "question": ("clarify.request", {"request_id": "question", "question": "Choose", "choices": ["a", "b"]})})
    monkeypatch.setattr(server, "_answers", {})
    monkeypatch.setattr(server, "_batch_clarify", {"question": {"qids": ["q1", "q2"], "answers": {}}} if batch else {})
    monkeypatch.setattr(methods_groups, "get_hosted_room_service", lambda: service)
    task = state.get_task(db, identity)
    service.runtime._report_pending_action(binding, task, session_id="live",
        info=service.rpc.info(profile="one", session_id="live", source="bot_room"))
    request = {**marker, "request_id": "question", "command_id": "input-1", "answer": "a"}
    return service, request, event, other


def test_input_observation_exact_rpc_response_and_loss_replay(tmp_path, monkeypatch):
    service, request, event, other = _waiting(tmp_path, monkeypatch)
    actions = service.status("room-1")["pending_actions"]
    assert actions[0]["kind"] == "input" and actions[0]["thread_id"] == "thread"
    for changed in ({"execution_generation": 2}, {"thread_id": "wrong"}, {"request_id": "other"}):
        result = server._methods["groups.input.respond"]("rpc", {**request, **changed})
        assert "error" in result and not event.is_set() and not other.is_set()
    result = server._methods["groups.input.respond"]("rpc", request)["result"]
    assert result["complete"] and event.is_set() and not other.is_set()
    assert server._answers["question"] == "a"
    server._pending.pop("question")
    assert "pending_input" not in service.rpc.info(profile="one", session_id="live", source="bot_room")
    restarted = HostedRoomService(server, db_path=service.db_path)
    monkeypatch.setattr(methods_groups, "get_hosted_room_service", lambda: restarted)
    replay = server._methods["groups.input.respond"]("again", request)["result"]
    assert replay["idempotent"]
    assert "error" in server._methods["groups.input.respond"]("changed", {**request, "answer": "b"})


def test_batch_input_preserves_partial_answers_and_stop_rejects_remaining(tmp_path, monkeypatch):
    service, request, event, other = _waiting(tmp_path, monkeypatch, batch=True)
    response = server._methods["groups.input.respond"]("rpc", {**request, "question_id": "q1"})["result"]
    assert not response["complete"] and not event.is_set()
    assert server._batch_clarify["question"]["answers"] == {"q1": "a"}
    from gateway.hosted_room_scoped_controls import append_stop
    append_stop(service.db_path, service._room("room-1"), "stop", {"kind": "thread", "thread_id": "thread"})
    result = server._methods["groups.input.respond"]("rpc", {
        **request, "command_id": "input-2", "question_id": "q2"})
    assert "error" in result and not event.is_set() and not other.is_set()

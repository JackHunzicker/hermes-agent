"""An explicit approval thread must match the live persisted task."""
from gateway import hosted_room_driver as state
from tui_gateway import server
from tests.tui_gateway.test_hosted_room_scoped_input import _waiting
from tests.tui_gateway.hosted_room_service_fixtures import _FakeRPC
import pytest


@pytest.mark.parametrize("stop", [False, True])
def test_approval_thread_and_stop_fences_precede_target_callback(tmp_path, monkeypatch, stop):
    service, request, _, _ = _waiting(tmp_path, monkeypatch)
    rpc = _FakeRPC()
    service.rpc = rpc
    monkeypatch.setattr(server, "get_hosted_room_service", lambda: service)
    task = state.list_tasks(service.db_path, room_id="room-1")[0]
    service.runtime._report_pending_action(service.bindings()[0], task, session_id="live", info={
        "pending_approval": {"request_id": "approval-1", "choices": ["once", "deny"]}})
    action = service.status("room-1")["pending_actions"][0]
    assert action["thread_id"] == "thread"
    params = {key: request[key] for key in ("room_id", "member_id", "thread_id", "task_id", "execution_generation")}
    params.update(request_id="approval-1", choice="once")
    result = server._methods["groups.approve"]("rpc", {**params, "thread_id": "other"})
    assert "error" in result and not rpc.approvals
    from gateway.hosted_room_scoped_controls import append_stop
    if stop:
        append_stop(service.db_path, service._room("room-1"), "stop", {"kind": "thread", "thread_id": "thread"})
    result = server._methods["groups.approve"]("rpc", params)
    if stop:
        assert "error" in result and not rpc.approvals
    else:
        assert result["result"]["approved"]
        assert rpc.approvals == [{"session_id": "live", "request_id": "approval-1", "choice": "once"}]

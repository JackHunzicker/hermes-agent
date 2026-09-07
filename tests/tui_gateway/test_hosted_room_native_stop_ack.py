"""Native local completion is not proof that the native turn acknowledged Stop."""
from types import SimpleNamespace

import pytest

from gateway import hosted_room_driver as state
from tui_gateway import server
from tui_gateway.hosted_room_driver import _find_terminal_receipt, _target_interruption_from_info
from tui_gateway.hosted_room_driver import _task_session_title as task_session_title
from tui_gateway.hosted_room_server_rpc import HostedRoomServerRPC
from tests.tui_gateway.test_auto_continue import _session, _patch_local_interrupt, emits, marker_home, turn_env
from tests.tui_gateway.test_hosted_room_driver_runtime import (
    BINDING, FakeSessionRPC, _admit, _identity, _runtime, db,
)


@pytest.mark.parametrize("ack", [False, True, None, "finalizer-error"],
                         ids=["unacknowledged", "acknowledged", "legacy", "finalizer-error"])
@pytest.mark.parametrize("stop", [True, False], ids=["stop-requested", "target-interruption"])
def test_native_result_through_production_turn_retains_uncertainty(
    db, monkeypatch, emits, turn_env, ack, stop,
):
    finalizer_error = ack == "finalizer-error"
    if finalizer_error:
        ack = False
        def fail_absorb(*args, **kwargs):
            raise RuntimeError("injected finalizer error after native result")
        monkeypatch.setattr(server, "_absorb_turn_result", fail_absorb)
    rpc = FakeSessionRPC(auto_complete=False)
    runtime = _runtime(db, rpc, lease_ttl_seconds=60)
    identity = _identity()
    _admit(db, identity)
    lease = runtime._ensure_lease(BINDING)
    attempt = state.start_task(db, identity, lease, expected_cancel_generation=0, clock=runtime.clock)
    if stop:
        state.begin_task_cancel(db, identity, cancel_id="stop-1", expected_cancel_generation=0, clock=runtime.clock)
    result = {"interrupted": True, "completed": False, "final_response": "partial native text"}
    if ack is not None:
        result.update(native_terminal_acknowledged=ack, codex_thread_id="native-thread", codex_turn_id="native-turn")
    agent = SimpleNamespace(session_id="session-key", run_conversation=lambda *a, **k: result,
                            clear_interrupt=lambda: None)
    session = _session(agent=agent, running=True, _hosted_room_task={
        "task_id": identity.task_id, "execution_generation": attempt.execution_generation,
        "thread_id": identity.thread_id, "turn_id": identity.turn_id,
    })
    live_rpc = HostedRoomServerRPC(server)
    if ack is not None:
        agent.api_mode = "codex_app_server"
    _patch_local_interrupt(monkeypatch, session)
    agent.interrupt = lambda: None
    def run_native(*args, **kwargs):
        if stop and ack is not None:
            response = live_rpc.interrupt(profile="ops", session_id="sid", source="bot_room",
                                          expected_task_id=identity.task_id)
            assert response["status"] == "stopping"
            assert response["native_terminal_acknowledged"] is False
            rpc.add_session(title=task_session_title(state.get_task(db, identity)))
            rpc.info = lambda **kw: {"active": True, "task_id": identity.task_id}
            rpc.interrupt = lambda **kw: response
            assert runtime._finish_stop(BINDING, state.get_task(db, identity), lease) is False
            assert state.get_task(db, identity)["result"]["native_terminal_acknowledged"] is False
        return result
    agent.run_conversation = run_native
    # Real turn admission/invocation/finalizer and callback; only native computation is injected.
    monkeypatch.setattr(server, "_sessions", {"sid": session})
    receipts = []
    def on_terminal(receipt):
        receipts.append(receipt)
        runtime._on_terminal(BINDING, attempt, receipt)
    assert server._run_prompt_submit("rid", "sid", session, "work", terminal_callback=on_terminal)
    assert len(receipts) == 1
    task = state.get_task(db, identity)  # independent SQLite reopen
    if ack is False:
        assert task["status"] == ("stopping" if stop else "indeterminate")
        assert task["result"]["native_terminal_acknowledged"] is False
        assert task["result"]["codex_thread_id"] == "native-thread"
        assert task["result"]["codex_turn_id"] == "native-turn"
        if not finalizer_error:
            complete = next(payload for event, _, payload in emits if event == "message.complete")
            assert complete["native_terminal_acknowledged"] is False
        assert receipts[0]["status"] == "indeterminate"
        info = live_rpc.info(profile="ops", session_id="sid", source="bot_room")
        assert info["native_terminal_acknowledged"] is False
        assert info["task_id"] == identity.task_id
        assert info["execution_generation"] == attempt.execution_generation
        assert _target_interruption_from_info(info, identity, attempt.execution_generation) is None
        # Neither absent/idle local session nor an expired recovery defer window is an ACK.
        runtime._drop_lease(BINDING.room_id)
        recovered = _runtime(db, FakeSessionRPC(), lease_ttl_seconds=60, indeterminate_defer_seconds=0.01,
                             clock=lambda: lease.expires_at + 60)
        recovered._run_cycle()
        reopened = state.get_task(db, identity)
        assert reopened["status"] == task["status"]
        assert reopened["result"] == task["result"]
        assert not any(name == "submit" for name, _ in recovered.rpc.calls)
        if not stop:
            new_lease = recovered._ensure_lease(BINDING)
            with pytest.raises(state.InvalidTaskTransitionError, match="native"):
                state.defer_indeterminate_task(db, identity, new_lease,
                    expected_execution_generation=attempt.execution_generation, expected_cancel_generation=0,
                    reason="member_unavailable", clock=recovered.clock)
            with pytest.raises(state.InvalidTaskTransitionError, match="native"):
                recovered.retry_indeterminate(identity, retry_id="explicit-but-unproven")
            assert state.get_task(db, identity)["result"] == task["result"]
            recovered.rpc.add_session(title=task_session_title(task))
            recovered.rpc.info = lambda **kw: {"active": False, "status": "interrupted",
                "task_id": identity.task_id, "execution_generation": attempt.execution_generation,
                "native_terminal_acknowledged": True, "codex_thread_id": "native-thread",
                "codex_turn_id": "native-turn"}
            recovered._run_cycle()
            assert state.get_task(db, identity)["status"] == "failed"
            assert state.get_task(db, identity)["result"]["native_terminal_acknowledged"] is True
    elif stop:
        if state.get_task(db, identity)["status"] == "stopping":
            runtime._finish_stop(BINDING, state.get_task(db, identity), lease)
        assert state.get_task(db, identity)["status"] == "cancelled"


@pytest.mark.parametrize("ack", [False, True, None], ids=["unacknowledged", "acknowledged", "legacy"])
def test_recovery_cannot_promote_negative_native_proof(db, ack):
    identity = _identity()
    rpc = FakeSessionRPC(auto_complete=False)
    runtime = _runtime(db, rpc, lease_ttl_seconds=60)
    _admit(db, identity)
    lease = runtime._ensure_lease(BINDING)
    attempt = state.start_task(db, identity, lease, expected_cancel_generation=0, clock=runtime.clock)
    task = state.begin_task_cancel(db, identity, cancel_id="stop-1", expected_cancel_generation=0, clock=runtime.clock)
    for coordinate, wrong in (("task_id", "other-task"), ("execution_generation", 99),
                              ("thread_id", "other-thread"), ("turn_id", "other-turn")):
        for native_ack in (False, True):
            runtime._on_terminal(BINDING, attempt, {
                "status": "cancelled", "native_terminal_acknowledged": native_ack, coordinate: wrong})
            assert state.get_task(db, identity) == task
    proof = {"task_id": identity.task_id, "execution_generation": attempt.execution_generation}
    if ack is not None:
        proof["native_terminal_acknowledged"] = ack
    info = {**proof, "active": False, "status": "interrupted"}
    row = {**proof, "role": "assistant", "status": "failed", "reason_code": "target_interrupted",
           "codex_thread_id": "native-thread", "codex_turn_id": "native-turn"}
    rpc.add_session(title=task_session_title(task), history=[row])
    rpc.info = lambda **kwargs: info
    assert (_target_interruption_from_info(info, identity, 1) is None) is (ack is False)
    assert (_find_terminal_receipt([row], identity, 1) is None) is (ack is False)
    if ack is False:
        legacy_row = {key: value for key, value in row.items() if key != "native_terminal_acknowledged"}
        assert _find_terminal_receipt([legacy_row, row], identity, 1) is None
        assert runtime._finish_stop(BINDING, task, lease) is False
        persisted = state.get_task(db, identity)
        assert persisted["status"] == "stopping"
        assert persisted["result"]["native_terminal_acknowledged"] is False
        assert persisted["result"]["codex_thread_id"] == "native-thread"
        assert persisted["result"]["codex_turn_id"] == "native-turn"
        rpc.info = lambda **kwargs: {"active": False, "task_id": None}
        rpc.states.clear()
        rpc.sessions.clear()
        assert runtime._finish_stop(BINDING, persisted, lease) is False
        # A reconnect which omits native proof cannot wash the durable negative evidence.
        rpc.add_session(title=task_session_title(task), history=[{k: v for k, v in row.items()
                                                               if k != "native_terminal_acknowledged"}])
        assert runtime._finish_stop(BINDING, state.get_task(db, identity), lease) is False
        # A later exact native terminal acknowledgement may resolve the same Stop.
        rpc.states[next(iter(rpc.states))]["history"] = []
        rpc.info = lambda **kwargs: {**info, "native_terminal_acknowledged": True}
        assert runtime._finish_stop(BINDING, state.get_task(db, identity), lease) is True
        resolved = state.get_task(db, identity)
        assert resolved["status"] == "cancelled"
        assert resolved["result"]["native_terminal_acknowledged"] is True
    else:
        assert runtime._finish_stop(BINDING, task, lease) is True

"""The target API must not erase native Stop uncertainty before RoomLink observes it."""
import asyncio

import pytest
from aiohttp.test_utils import TestClient, TestServer

from tests.gateway.test_api_server_runs import _make_adapter, _make_slow_agent, _create_runs_app


@pytest.mark.asyncio
@pytest.mark.parametrize("ack", [False, True, None])
async def test_native_stop_result_remains_truthful_in_target_run_status(monkeypatch, ack):
    adapter = _make_adapter()
    agent, ready, interrupted = _make_slow_agent()
    def run(*args, **kwargs):
        ready.set()
        assert interrupted.wait(5)
        return {"interrupted": True, "final_response": "partial", **(
            {"native_terminal_acknowledged": ack, "codex_thread_id": "native-thread", "codex_turn_id": "native-turn"}
            if ack is not None else {})}
    agent.run_conversation.side_effect = run
    monkeypatch.setattr(adapter, "_create_agent", lambda **kwargs: agent)
    async with TestClient(TestServer(_create_runs_app(adapter))) as client:
        response = await client.post("/v1/runs", json={"input": "work"})
        run_id = (await response.json())["run_id"]
        task = adapter._active_run_tasks[run_id]
        assert await asyncio.to_thread(ready.wait, 5)
        response = await client.post(f"/v1/runs/{run_id}/stop")
        assert response.status == 200
        await asyncio.wait_for(task, 5)
        response = await client.get(f"/v1/runs/{run_id}")
        status = await response.json()
    assert status["status"] == ("interrupted" if ack is False else "cancelled")
    if ack is not None:
        assert status["native_terminal_acknowledged"] is ack
        assert status["codex_thread_id"] == "native-thread"
        assert status["codex_turn_id"] == "native-turn"

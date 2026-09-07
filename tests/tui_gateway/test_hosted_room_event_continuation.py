"""Existing room worker drains event continuations across durable rate windows."""
import threading
import time
import sqlite3
from gateway import hosted_rooms
from tests.tui_gateway.hosted_room_service_fixtures import _FakeRPC, _wait_for
from tests.tui_gateway.test_hosted_room_membership import service_at, MEMBERS
from tests.tui_gateway.test_hosted_room_responder_policy import POLICY, rpc_for


class RelayRPC(_FakeRPC):
    def __init__(self):
        super().__init__()
        self.calls = []
    def submit(self, **kw):
        self.calls.append(kw)
        target = "review" if kw["profile"] == "ops" else "ops"
        kw["on_terminal"]({"status": "settled", "text": "@" + target + " continue" if len(self.calls) < 36 else "Finished."})
        return {"accepted": True}


def configure(service, rpc, clock):
    service.rpc = rpc
    service.runtime.rpc = rpc
    service.runtime.clock = lambda: clock[0]
    service.runtime.poll_interval_seconds = 0.05
    service.runtime.active_poll_interval_seconds = 0.02


def test_event_continuation_restarts_after_cooldown_without_human_send(tmp_path):
    db = tmp_path / "state.db"
    clock = [time.time()]
    service = service_at(db)
    room = service.create_room(room_id="room-1", name="Relay", members=MEMBERS)
    policy = {**POLICY, "mode": "event_driven", "max_turns_per_window": 4, "window_seconds": 60}
    changed = rpc_for(service)["groups.policy.update"](1, {"room_id": "room-1", "event_id": "policy",
        "expected_revision": room["revision"], "policy": policy})
    assert "result" in changed, changed
    rpc = RelayRPC()
    configure(service, rpc, clock)
    service.send(room_id="room-1", event_id="u1", payload={"text": "@ops begin", "thread_id": "t"})
    service.start()
    try:
        for batch in range(1, 10):
            _wait_for(lambda: len(rpc.calls) >= batch * 4, timeout=10)
            _wait_for(lambda: not service.status("room-1")["working"], timeout=10)
            assert len(rpc.calls) == batch * 4
            if batch < 9:
                assert service.status("room-1")["continuation"]["state"] == "cooldown"
                assert service.stop(timeout=3)
                service = service_at(db)
                clock[0] += 61
                configure(service, rpc, clock)
                service.start()
        events = service._events("room-1")
        assert len([e for e in events if e["kind"] == "message.user"]) == 1
        assert len({call["task"].task_id for call in rpc.calls}) == 36
        assert len([e for e in events if e["kind"] == "message.member"]) == 36
        with sqlite3.connect(db) as conn:
            assert conn.execute("SELECT COUNT(*) FROM hosted_room_policy_events WHERE room_id='room-1'").fetchone()[0] <= 64
    finally:
        assert service.stop(timeout=3)


def test_busy_input_queue_preserves_every_acknowledged_message_and_rejects_overflow(tmp_path):
    db = tmp_path / "state.db"
    service = service_at(db)
    room = service.create_room(room_id="room-1", name="Queue", members=MEMBERS)
    policy = {**POLICY, "mode": "event_driven", "max_turns_per_window": 32, "window_seconds": 60}
    assert "result" in rpc_for(service)["groups.policy.update"](1, {"room_id": "room-1", "event_id": "policy",
        "expected_revision": room["revision"], "policy": policy})
    class BusyRPC(_FakeRPC):
        def __init__(self):
            super().__init__()
            self.calls, self.started, self.release = [], threading.Event(), threading.Event()
            self.drained = threading.Event()
        def submit(self, **kw):
            self.calls.append(kw)
            if len(self.calls) == 1:
                self.started.set()
                assert self.release.wait(60)
            kw["on_terminal"]({"status": "settled", "text": "Acknowledged."})
            if len(self.calls) == 24:
                self.drained.set()
            return {"accepted": True}
    rpc = BusyRPC()
    configure(service, rpc, [time.time()])
    service.send(room_id="room-1", event_id="u0", payload={"text": "@ops queued-0", "thread_id": "t"})
    service.start()
    try:
        assert rpc.started.wait(10)
        for index in range(1, 24):
            service.send(room_id="room-1", event_id="u" + str(index),
                         payload={"text": "@ops queued-" + str(index), "thread_id": "t"})
        import pytest
        with pytest.raises(hosted_rooms.RoomConflictError, match="queue is full"):
            service.send(room_id="room-1", event_id="overflow", payload={"text": "@ops overflow", "thread_id": "t"})
        rpc.release.set()
        assert rpc.drained.wait(60), service.runtime.status()
        _wait_for(lambda: not service.status("room-1")["working"], timeout=10)
        assert len(rpc.calls) == 24
        events = service._events("room-1")
        users = [e for e in events if e["kind"] == "message.user"]
        assert len(users) == 24 and all(e["event_id"] != "overflow" for e in users)
        for event in users:
            assert any(event["event_id"] in call["prompt"] for call in rpc.calls)
        assert len({call["task"].task_id for call in rpc.calls}) == 24
    finally:
        rpc.release.set()
        assert service.stop(timeout=3)

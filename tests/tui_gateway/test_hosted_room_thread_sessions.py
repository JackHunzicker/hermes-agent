"""Local runtime scopes use the room's durable thread/member coordinates."""
from gateway import hosted_rooms
from tui_gateway.hosted_room_service import HostedRoomService
from tests.tui_gateway.hosted_room_service_fixtures import _server
from tests.tui_gateway.test_hosted_room_driver_runtime import FakeSessionRPC


def test_local_threads_do_not_share_runtime_history_and_resume_same_scope(tmp_path):
    db = tmp_path / "state.db"
    rpc = FakeSessionRPC()

    def service():
        value = HostedRoomService(_server(), db_path=db)
        value.rpc = value.runtime.rpc = rpc
        value.local_profiles = lambda: ("default", "ops")
        return value

    first = service()
    first.create_room(room_id="room-1", name="Threads", members=[
        {"member_id": p, "profile": p, "handle": p} for p in ("default", "ops")])

    def send(value, event_id, thread_id):
        value.send(room_id="room-1", event_id=event_id, payload={"text": "@ops report", "thread_id": thread_id})
        value.runtime._process_room(value.bindings()[0])
        submits = [params for method, params in rpc.calls if method == "submit"]
        assert submits and submits[-1]["task"].thread_id == thread_id
        return submits[-1]["session_id"]

    thread_one = send(first, "u1", "thread-one")
    thread_two = send(first, "u2", "thread-two")
    assert thread_two != thread_one, "different room threads shared the same native history"
    # The synchronous harness has no worker-loop finally block to release leases.
    first.runtime._release_idle_leases()
    assert first.stop(timeout=1)
    resumed = service()
    assert send(resumed, "u3", "thread-one") == thread_one
    assert send(resumed, "u4", "thread-two") == thread_two
    assert len(rpc.sessions) == 2
    assert resumed.stop(timeout=1)
    assert len([e for e in hosted_rooms.read_events(db, room_id="room-1")["events"] if e["kind"] == "message.member"]) == 4

"""The reader guard and returned log must describe the same SQLite snapshot."""
import concurrent.futures
import threading

from gateway import hosted_rooms as rooms
from gateway import hosted_room_capabilities as capabilities


def test_mutation_cannot_commit_between_reader_guard_and_legacy_page(tmp_path, monkeypatch):
    db = tmp_path / "state.db"
    rooms.create_room(db, room_id="r", name="Snapshot", members=[], authority_gateway_id="g")
    rooms.append_event(db, room_id="r", event_id="u", kind="message.user", actor={"kind": "user", "id": "desktop"},
                       payload={"text": "original", "thread_id": "t"}, authority_gateway_id="g", authority_epoch=1)
    original_guard = capabilities.require_reader
    start = threading.Event()
    completed = threading.Event()

    def append_after_guard():
        assert start.wait(5)
        rooms.append_event(db, room_id="r", event_id="e", kind="message.edited", actor={"kind": "user", "id": "desktop"},
                           payload={"text": "changed", "thread_id": "t", "target_event_id": "u", "expected_revision": 1},
                           authority_gateway_id="g", authority_epoch=1)
        completed.set()

    def synchronized_guard(conn, room_id, supported):
        original_guard(conn, room_id, supported)
        start.set()
        # A writer has time to run, but cannot commit until the snapshot reader closes.
        assert not completed.wait(2), "mutation committed between reader negotiation and page read"

    monkeypatch.setattr(capabilities, "require_reader", synchronized_guard)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(append_after_guard)
        page = rooms.read_events(db, room_id="r", supported_features=[])
        future.result(timeout=5)
    assert completed.is_set()
    assert [e["event_id"] for e in page["events"]] == ["u"]

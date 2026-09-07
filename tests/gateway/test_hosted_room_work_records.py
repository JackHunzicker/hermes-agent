"""Consistent, bounded metadata without prompt/result/private-state copying."""

import json
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from gateway import hosted_room_driver as driver
from gateway import hosted_room_link_records as links
from gateway import hosted_rooms as rooms
from gateway import hosted_room_work_records as records

HOME = "install:home"
MEMBERS = [{"member_id": "ops", "profile": "ops", "target": {"kind": "local", "profile": "ops"}}]
IDENTITY = driver.TaskIdentity("room", "task", "thread", "turn")


@pytest.fixture
def source(tmp_path):
    db = tmp_path / "source.db"
    rooms.create_room(db, room_id="room", name="Workshop", members=MEMBERS, authority_gateway_id=HOME)
    rooms.append_event(db, room_id="room", event_id="hello", kind="message.user",
                       actor={"kind": "user", "id": "owner"}, payload={"text": "PRIVATE_MESSAGE"},
                       authority_gateway_id=HOME, authority_epoch=1)
    driver.admit_task(db, IDENTITY, payload={"prompt": "PRIVATE_PROMPT /private/workspace API_KEY",
                     "target_profile": "ops", "source_event_seq": 1}, clock=lambda: 100)
    return db


def capture(db):
    return records.capture(db, room_id="room", local_gateway_id=HOME)


def test_phase_revisions_do_not_depend_on_history_and_survive_reopen(source):
    first = capture(source)
    assert capture(source) == first
    held = driver.acquire_lease(source, room_id="room", gateway_id=HOME, authority_epoch=1,
                                process_generation="process", ttl_seconds=30, clock=lambda: 100)
    attempt = driver.start_task(source, IDENTITY, held, expected_cancel_generation=0, clock=lambda: 100)
    second = capture(source)
    assert second["history"] == first["history"]
    assert second["revision"] == first["revision"] + 1
    assert second["tasks"][0]["phase"] == "running"
    driver.settle_task(source, attempt, settlement_id="settled", status="settled",
                       result={"text": "PRIVATE_RESULT", "path": "/private/result"}, clock=lambda: 100)
    third = capture(source)
    assert third["revision"] == second["revision"] + 1
    assert third["tasks"][0]["settlement_id"] == "settled"
    encoded = json.dumps(third)
    for private in ("PRIVATE_MESSAGE", "PRIVATE_PROMPT", "PRIVATE_RESULT", "/private", "API_KEY", "result_json", "prompt"):
        assert private not in encoded
    assert third["limitations"] == records.LIMITATIONS


@pytest.mark.parametrize("failure", ["bound", "unsupported"])
def test_incomplete_capture_is_explicit_not_a_truncated_complete_list(source, monkeypatch, failure):
    if failure == "bound":
        monkeypatch.setattr(records, "MAX_TASKS", 0)
    else:
        with rooms._transaction(source, immediate=True) as conn:
            conn.execute("UPDATE hosted_room_driver_tasks SET payload_json=?", ('{"field_private_state":true}',))
    result = capture(source)
    assert result["availability"] == "unavailable"
    assert result["reason"] == ("bounds_exceeded" if failure == "bound" else "unsupported_task")
    assert result["tasks"] == result["receipts"] == []
    assert capture(source)["revision"] == result["revision"]


def test_capture_preserves_close_fact_before_stop_event(source):
    first = capture(source)
    links.begin_room_link_retirement(source, room_id="room", authority_gateway_id=HOME, authority_epoch=1)
    second = capture(source)
    assert second["history"] == first["history"]
    assert second["stop"] == {"closing": True, "revocation_complete": False, "seq": 0, "cancel_id": None}
    rooms.request_room_stop(source, room_id="room", cancel_id="stop", expected_gateway_id=HOME, expected_epoch=1)
    assert capture(source)["stop"]["cancel_id"] == "stop"


def test_capture_is_one_sqlite_view_while_another_writer_changes_phase(source, monkeypatch):
    entered, release, writing = threading.Event(), threading.Event(), threading.Event()
    original = records._capture_tasks
    def paused(*args):
        result = original(*args)
        entered.set()
        assert release.wait(5)
        return result
    monkeypatch.setattr(records, "_capture_tasks", paused)
    with ThreadPoolExecutor(max_workers=2) as pool:
        snapshot = pool.submit(capture, source)
        assert entered.wait(5)
        def mutate():
            writing.set()
            links.begin_room_link_retirement(source, room_id="room", authority_gateway_id=HOME, authority_epoch=1)
        writer = pool.submit(mutate)
        assert writing.wait(5)
        release.set()
        result = snapshot.result(timeout=5)
        writer.result(timeout=5)
    assert result["stop"]["closing"] is False
    assert capture(source)["stop"]["closing"] is True


def test_capture_waits_for_the_acknowledged_history_prefix(source):
    with pytest.raises(records.WorkRecordPrefixError):
        records.capture(source, room_id="room", local_gateway_id=HOME, through_seq=0)


def test_missing_task_store_is_not_claimed_to_be_empty(tmp_path):
    db = tmp_path / "empty.db"
    rooms.create_room(db, room_id="room", name="Empty", members=MEMBERS, authority_gateway_id=HOME)
    assert capture(db)["reason"] == "task_store_missing"


def test_store_capacity_is_hard_bounded(source, monkeypatch):
    monkeypatch.setattr(records, "MAX_STORE_BYTES", 10)
    with pytest.raises(records.WorkRecordCapacityError):
        capture(source)

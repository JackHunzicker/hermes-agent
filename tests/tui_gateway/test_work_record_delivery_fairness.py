"""Active conversation traffic must not starve an anchored passive record."""

import io
import json
import sqlite3
import urllib.error

import pytest

from gateway import hosted_room_driver as driver
from gateway import hosted_room_peer as peer
from gateway import hosted_room_work_records as records
from gateway import hosted_rooms as rooms
from tests.tui_gateway.test_hosted_room_replication import (
    HOME, KEY, SECRET, TARGET, add_profile, append, pair, save_link,  # noqa: F401
)
from tui_gateway import hosted_room_replication as publisher


@pytest.fixture
def copying(pair, monkeypatch):
    link = save_link(pair.source, permissions=("replicate", records.PERMISSION))
    claims = peer.decode_room_grant(SECRET, link.grant, permission=records.PERMISSION)
    rooms.reserve_peer_room(pair.target, claims=claims, expires_at=claims["status_expires_at"])
    pair.records = []
    pair.lose_record_ack = False
    pair.record_error = None

    def transport(request, *, timeout):
        # Neither history nor record transport may retain the capture write lock.
        with sqlite3.connect(pair.source, timeout=0.1) as conn:
            conn.execute("BEGIN IMMEDIATE")
        if not request.full_url.endswith("/work-records"):
            return pair.http(request, timeout=timeout)
        record = json.loads(request.data)["record"]
        pair.records.append(record)
        if pair.record_error:
            raise pair.record_error
        result = records.ingest(
            pair.target, record=record,
            token=request.get_header("Authorization").removeprefix("HermesRoom "),
            secret=SECRET, target_install_id=TARGET, target_profile="reviewer",
        )
        if pair.lose_record_ack:
            pair.lose_record_ack = False
            raise TimeoutError("record ACK lost after committed target write")
        return io.BytesIO(json.dumps(result).encode())

    monkeypatch.setattr("hermes_cli.urllib_security.open_credentialed_url", transport)
    pair.pub = publisher.HostedRoomReplicationPublisher(pair.source)
    return pair


def add_task(source, suffix):
    append(source, f"message-{suffix}")
    seq = rooms.room_state(source, room_id="room")["latest_seq"]
    driver.admit_task(
        source, driver.TaskIdentity("room", f"task-{suffix}", "thread", f"turn-{suffix}"),
        payload={"prompt": "private task body", "source_event_seq": seq,
                 "target_profile": "reviewer", "target_member_id": "reviewer"},
        clock=lambda: 100,
    )


@pytest.mark.parametrize("turns", [5, 20, 100])
def test_busy_history_delivers_current_task_snapshots_without_quiet_turn(copying, turns):
    for turn in range(turns):
        add_task(copying.source, str(turn))
        copying.pub._publish_one(KEY)
        if turn >= 1:
            # With one page per turn, a healthy target must receive a snapshot
            # within the next turn, not wait indefinitely for global quiet.
            latest = rooms.room_state(copying.source, room_id="room")["latest_seq"]
            assert copying.records
            assert latest - copying.records[-1]["history"]["seq"] <= 2
    assert len(copying.records[-1]["tasks"]) >= turns - 2
    assert "private task body" not in json.dumps(copying.records)


def test_history_anchor_survives_new_events_and_lost_ack_across_restart(copying, monkeypatch):
    monkeypatch.setattr(publisher, "PAGE_LIMIT", 1)
    for turn in range(4):
        add_task(copying.source, str(turn))
    copying.http.lose_ack = True
    copying.pub._publish_one(KEY)
    with rooms._transaction(copying.source) as conn:
        pending = conn.execute(f"SELECT record_json FROM {records.PENDING_TABLE}").fetchone()
        assert pending is not None
        frozen = json.loads(pending[0])
    copying.pub = publisher.HostedRoomReplicationPublisher(copying.source)
    # New tasks/events on every retry must not move the frozen snapshot's anchor.
    for turn in range(frozen["history"]["seq"] + 2):
        add_task(copying.source, f"growing-{turn}")
        copying.pub._publish_one(KEY)
        if copying.records:
            break
    assert copying.records[0] == frozen
    assert copying.http.requests[0] == copying.http.requests[1]


def test_lost_record_ack_retries_identical_record_while_history_keeps_growing(copying):
    add_task(copying.source, "first")
    copying.pub._publish_one(KEY)
    copying.lose_record_ack = True
    add_task(copying.source, "second")
    copying.pub._publish_one(KEY)
    assert copying.records
    frozen = copying.records[-1]
    copying.pub = publisher.HostedRoomReplicationPublisher(copying.source)
    add_task(copying.source, "third")
    copying.pub._publish_one(KEY)
    assert copying.records[-1] == frozen
    assert copying.records[-2] == frozen
    assert copying.http.requests[-1][1]["page"]["cursor"] == rooms.room_state(
        copying.source, room_id="room")["latest_seq"]
    add_task(copying.source, "fourth")
    copying.pub._publish_one(KEY)
    add_task(copying.source, "fifth")
    copying.pub._publish_one(KEY)
    assert copying.records[-1]["revision"] > frozen["revision"]


def test_quiet_history_suppresses_unchanged_records(copying):
    add_task(copying.source, "only")
    copying.pub._publish_one(KEY)
    copying.pub._publish_one(KEY)
    assert copying.records
    delivered = list(copying.records)
    history = list(copying.http.requests)
    for _ in range(3):
        copying.pub._publish_one(KEY)
    assert copying.records == delivered
    assert copying.http.requests == history


def test_empty_group_history_is_created_before_first_record(copying):
    empty = copying.source.with_name("empty.db")
    members = rooms.room_state(copying.source, room_id="room")["members"]
    rooms.create_room(empty, room_id="room", name="Empty", members=members, authority_gateway_id=HOME)
    save_link(empty, permissions=("replicate", records.PERMISSION))
    copying.source = empty
    copying.pub = publisher.HostedRoomReplicationPublisher(empty)
    copying.pub._publish_one(KEY)
    assert copying.records == []
    assert copying.http.requests[-1][1]["page"]["cursor"] == 0
    copying.pub._publish_one(KEY)
    assert copying.records[-1]["history"]["seq"] == 0
    assert copying.pub.status()["work_records"][0]["status"] == "acked"


def test_unavailable_record_transport_does_not_stop_history(copying):
    copying.record_error = TimeoutError("record endpoint unavailable")
    for turn in range(5):
        add_task(copying.source, str(turn))
        copying.pub._publish_one(KEY)
    assert copying.records
    assert all(record == copying.records[0] for record in copying.records)
    assert copying.http.requests[-1][1]["page"]["cursor"] == rooms.room_state(
        copying.source, room_id="room")["latest_seq"]


@pytest.mark.parametrize("busy", [False, True], ids=["quiet-control", "continuous-history"])
def test_recovered_alternate_gets_a_work_attempt(pair, monkeypatch, busy):
    add_profile(pair)
    for member, profile in (("reviewer", "reviewer"), ("z-other", "default")):
        link = save_link(pair.source, member_id=member, profile=profile,
                         permissions=("replicate", records.PERMISSION))
        claims = peer.decode_room_grant(SECRET, link.grant, permission=records.PERMISSION)
        rooms.reserve_peer_room(pair.target, claims=claims, expires_at=claims["status_expires_at"])

    recovered = False
    attempts, bodies = [], []

    def transport(request, *, timeout):
        if not request.full_url.endswith("/work-records"):
            return pair.http(request, timeout=timeout)
        token = request.get_header("Authorization").removeprefix("HermesRoom ")
        claims = peer.decode_room_grant(SECRET, token, permission=records.PERMISSION)
        member = claims["member_id"]
        record = json.loads(request.data)["record"]
        attempts.append(member)
        bodies.append(record)
        if member == "reviewer" or not recovered:
            raise urllib.error.HTTPError(request.full_url, 503, "unavailable", {},
                io.BytesIO(b'{"error":{"code":"unavailable"}}'))
        reply = records.ingest(pair.target, record=record, token=token, secret=SECRET,
                               target_install_id=TARGET, target_profile=claims["target_profile"])
        return io.BytesIO(json.dumps(reply).encode())

    monkeypatch.setattr("hermes_cli.urllib_security.open_credentialed_url", transport)
    pub = publisher.HostedRoomReplicationPublisher(pair.source)
    pub._publish_one(KEY)
    pub._publish_one(KEY)
    pub._publish_one(("room", "z-other"))
    assert attempts == ["reviewer", "z-other"]
    assert {row["work_record_status"] for row in pub.status()["routes"]} == {"unavailable"}
    frozen = bodies[0]
    recovered = True
    pub = publisher.HostedRoomReplicationPublisher(pair.source)
    start = len(attempts)
    for turn in range(6):
        if busy:
            append(pair.source, f"continuing-{turn}")
        pub._publish_one(("room", "z-other" if turn % 2 == 0 else "reviewer"))
        if pub.status()["work_records"][0]["status"] == "acked":
            break
    assert all(body == frozen for body in bodies)
    if busy:
        assert pair.http.requests[-1][1]["page"]["cursor"] == rooms.room_state(
            pair.source, room_id="room")["latest_seq"]
    assert pub.status()["work_records"][0]["status"] == "acked", attempts[start:]

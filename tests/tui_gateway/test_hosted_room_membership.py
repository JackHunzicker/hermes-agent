"""Membership changes keep historical authors while fencing new work."""

import sqlite3

import pytest

from gateway import hosted_room_discussion as discussion, hosted_room_driver as driver, hosted_rooms
from tui_gateway.hosted_room_service import HostedRoomService
from tests.tui_gateway.hosted_room_service_fixtures import _server


@pytest.mark.parametrize("with_attachment", [False, True])
def test_send_roster_race_commits_neither_message_nor_attachment(tmp_path, monkeypatch, with_attachment):
    db = tmp_path / "state.db"
    service = service_at(db)
    room = service.create_room(room_id="room-1", name="Review", members=MEMBERS)
    payload = {"text": "@ops inspect", "thread_id": "t1"}
    stored = None
    if with_attachment:
        stored = service.put_attachment(room_id="room-1", upload_id="race-upload",
            kind="file", name="notes.txt", mime="text/plain", data=b"notes")
        payload["attachments"] = [{key: stored[key]
            for key in ("attachment_id", "kind", "name", "size", "mime")}]
    append = hosted_rooms.append_event

    def change_roster_before_append(*args, **kwargs):
        service.update_members(room_id="room-1", event_id="remove-ops",
            expected_revision=room["revision"], members=[MEMBERS[0], MEMBERS[2]])
        return append(*args, **kwargs)

    monkeypatch.setattr(hosted_rooms, "append_event", change_roster_before_append)
    with pytest.raises(hosted_rooms.HostedRoomError):
        service.send(room_id="room-1", event_id="raced-send", payload=payload)
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT event_id FROM hosted_room_events WHERE event_id='raced-send'").fetchone() is None
        if stored:
            state, event_id, expiry = conn.execute(
                "SELECT state,event_id,expires_at FROM hosted_room_attachments WHERE attachment_id=?",
                (stored["attachment_id"],)).fetchone()
            assert state == "uploaded"
            assert event_id is None
            assert expiry is not None
    monkeypatch.setattr(hosted_rooms, "append_event", append)
    payload["text"] = "@review inspect"
    event = service.send(room_id="room-1", event_id="raced-send", payload=payload)
    assert event["event_id"] == "raced-send"
    assert service.send(room_id="room-1", event_id="raced-send", payload=payload)["idempotent"] is True



MEMBERS = [
    {"member_id": name, "profile": name, "handle": name}
    for name in ("default", "ops", "review")
]


def service_at(db):
    service = HostedRoomService(_server(), db_path=db)
    service.local_profiles = lambda: ("default", "ops", "review", "new")
    return service


def test_membership_restart_preserves_author_and_fences_removed_admission(tmp_path):
    db = tmp_path / "state.db"
    service = service_at(db)
    room = service.create_room(room_id="room-1", name="Review", members=MEMBERS)
    hosted_rooms.append_event(db, room_id="room-1", event_id="u1", kind="message.user",
        actor={"kind": "user", "id": "desktop"}, authority_gateway_id=room["authority_gateway_id"],
        authority_epoch=room["authority_epoch"], payload={"text": "@ops report", "thread_id": "t1"})
    task = discussion.plan_next_task(room, service._events("room-1"), local_profiles=service.local_profiles()).task
    publication = discussion.plan_publication(room, service._events("room-1"), task,
        status="settled", result={"text": "Original author report"}, local_profiles=service.local_profiles())
    for event in publication.events:
        hosted_rooms.append_event(db, **event.append_kwargs("room-1"))
    original = service._events("room-1")[1]

    changed = service.update_members(room_id="room-1", event_id="members-1", expected_revision=room["revision"],
                                     members=[MEMBERS[0], MEMBERS[2]])
    assert [m["member_id"] for m in changed["members"]] == ["default", "review"]
    assert changed["retired_members"][0]["member_id"] == "ops"
    restarted = service_at(db)
    durable = hosted_rooms.room_state(db, room_id="room-1")
    assert durable["retired_members"] == changed["retired_members"]
    assert restarted._events("room-1")[1] == original
    reconstructed = discussion.reconstruct_task_plan(durable, restarted._events("room-1"),
        {"identity": task.identity, "payload": task.payload}, local_profiles=restarted.local_profiles())
    assert reconstructed.member == task.member
    # A policy computed before the membership transaction cannot admit removed work afterward.
    with pytest.raises(driver.RoomUnavailableError, match="member"):
        driver.admit_task(db, task.identity, payload=task.payload, clock=lambda: 100)
    restarted.send(room_id="room-1", event_id="u2", payload={"text": "@review summarize", "thread_id": "t1"})
    snapshot = restarted._policy_snapshot(hosted_rooms.room_state(db, room_id="room-1"))
    planned = discussion.plan_next_task(durable, snapshot.events, local_profiles=restarted.local_profiles(),
                                       initial_watermarks=snapshot.watermarks)
    assert planned.task.member.member_id == "review"
    assert "@ops" in planned.task.payload["prompt"]
    assert "Original author report" in planned.task.payload["prompt"]
    replay = restarted.update_members(room_id="room-1", event_id="members-1", expected_revision=room["revision"],
                                      members=[MEMBERS[0], MEMBERS[2]])
    assert replay["idempotent"] is True
    with pytest.raises(hosted_rooms.RoomConflictError, match="revision"):
        restarted.update_members(room_id="room-1", event_id="members-stale", expected_revision=room["revision"],
                                 members=[MEMBERS[0], MEMBERS[2]])
    with pytest.raises(hosted_rooms.RoomConflictError, match="retired"):
        restarted.update_members(room_id="room-1", event_id="reuse", expected_revision=changed["revision"], members=MEMBERS)

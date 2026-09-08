"""Late-receipt indexing upgrades derived caches, never accepted inputs or history."""

import json
import sqlite3
import time
from types import SimpleNamespace
import threading

import pytest

from gateway import hosted_room_discussion as discussion
from gateway import hosted_room_driver as driver
from gateway import hosted_rooms as rooms
from gateway.hosted_room_policy_checkpoint import HostedRoomPolicyCheckpoint
from tui_gateway.hosted_room_service import HostedRoomService

PROFILES = ("writer", "reviewer")


def log(db):
    return rooms.read_events(db, room_id="upgrade-room")["events"]


def append(db, event_id, kind, payload, actor_kind="user"):
    return rooms.append_event(db, room_id="upgrade-room", event_id=event_id, kind=kind,
        actor={"kind": actor_kind, "id": "home"}, authority_gateway_id="home", authority_epoch=1, payload=payload)


def legacy_cache(db):
    room = rooms.create_room(db, room_id="upgrade-room", name="Upgrade", authority_gateway_id="home",
        members=[{"member_id": profile, "profile": profile, "handle": profile} for profile in PROFILES])
    append(db, "source", "message.user", {"text": "@writer Prepare the plan.", "thread_id": "thread"})
    original = discussion.plan_next_task(room, log(db), local_profiles=PROFILES, freeze_input_context=True).task
    deferred = discussion.plan_publication(room, log(db), original, status="deferred", execution_generation=1,
        result={"reason": "member_unavailable"}, local_profiles=PROFILES)
    for event in deferred.events:
        rooms.append_event(db, **event.append_kwargs("upgrade-room"))
    append(db, "silent", "room.activity", {"status": "settled", "reason_code": "silent_round",
        "thread_id": "thread", "discussion_event_id": "source"}, "gateway")
    completed = discussion.plan_publication(room, log(db), original, status="settled",
        result={"text": "The committed plan is ready."}, local_profiles=PROFILES)
    rooms.append_event(db, **completed.events[0].append_kwargs("upgrade-room"))
    append(db, "newer", "message.user", {"text": "@reviewer Use the earlier plan.", "thread_id": "thread"})
    checkpoint = HostedRoomPolicyCheckpoint(db)
    before = checkpoint.snapshot(room_id="upgrade-room", latest_seq=log(db)[-1]["seq"])
    late = rooms.append_event(db, **completed.events[-1].append_kwargs("upgrade-room"))
    # Field version 3 consumed a late receipt without indexing it once newer
    # input prevented reopening the old discussion. Reproduce its persisted state.
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE hosted_room_policy_cursors SET through_seq=? WHERE room_id='upgrade-room'", (late["seq"],))
        conn.execute("UPDATE hosted_room_policy_transcript_state SET schema_version=3 WHERE room_id='upgrade-room'")
    assert not checkpoint.publication_exists(room_id="upgrade-room", task_id=original.identity.task_id,
                                             status="settled", execution_generation=2)
    planned = discussion.plan_next_task(room, before.events, local_profiles=PROFILES,
                                       initial_watermarks=before.watermarks, freeze_input_context=True).task
    assert "The committed plan is ready." not in planned.payload["prompt"]
    return room, original, planned


def test_version_three_rebuilds_late_receipts_and_context_without_changing_history(tmp_path):
    db = tmp_path / "state.db"
    room, original, old_plan = legacy_cache(db)
    history = log(db)
    checkpoint = HostedRoomPolicyCheckpoint(db)
    updated = checkpoint.snapshot(room_id="upgrade-room", latest_seq=history[-1]["seq"])
    assert checkpoint.publication_exists(room_id="upgrade-room", task_id=original.identity.task_id,
                                         status="settled", execution_generation=2)
    planned = discussion.plan_next_task(room, updated.events, local_profiles=PROFILES,
        initial_watermarks=updated.watermarks, freeze_input_context=True).task
    assert "The committed plan is ready." in planned.payload["prompt"]
    assert planned.identity.task_id != old_plan.identity.task_id
    assert log(db) == history
    assert HostedRoomPolicyCheckpoint(db).snapshot(room_id="upgrade-room", latest_seq=history[-1]["seq"]) == updated


@pytest.mark.parametrize("status", ["queued", "running", "indeterminate"])
def test_upgrade_preserves_an_already_admitted_frozen_turn(tmp_path, monkeypatch, status):
    monkeypatch.setattr(rooms, "local_authority_gateway_id", lambda: "home")
    db = tmp_path / "state.db"
    _, _, old_plan = legacy_cache(db)
    clock = [time.time()]
    admitted = driver.admit_task(db, old_plan.identity, payload=old_plan.payload, clock=lambda: clock[0])
    if status != "queued":
        lease = driver.acquire_lease(db, room_id="upgrade-room", gateway_id="home", authority_epoch=1,
            process_generation="prior", ttl_seconds=1, clock=lambda: clock[0])
        driver.start_task(db, old_plan.identity, lease, expected_cancel_generation=0, clock=lambda: clock[0])
        if status == "indeterminate":
            clock[0] += 2
            current = driver.acquire_lease(db, room_id="upgrade-room", gateway_id="home", authority_epoch=1,
                process_generation="current", ttl_seconds=60, clock=lambda: clock[0])
            driver.recover_room(db, current, clock=lambda: clock[0])
    history = log(db)
    server = SimpleNamespace(_methods={}, _sessions={}, _sessions_lock=threading.Lock())
    service = HostedRoomService(server, db_path=db)
    service.local_profiles = lambda: PROFILES
    service.runtime.clock = lambda: clock[0]
    service.prepare_room(service.bindings()[0])
    existing = driver.get_task_for_turn(db, old_plan.identity)
    assert existing["status"] == status
    assert existing["identity"] == admitted["identity"]
    assert existing["payload"] == admitted["payload"]
    assert log(db) == history
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM hosted_room_driver_tasks WHERE room_id='upgrade-room'").fetchone()[0] == 1


def policy_service(db):
    server = SimpleNamespace(_methods={}, _sessions={}, _sessions_lock=threading.Lock())
    service = HostedRoomService(server, db_path=db)
    service.local_profiles = lambda: PROFILES
    return service


@pytest.mark.parametrize("operation", ["edit", "delete", "react", "participant"])
@pytest.mark.parametrize("cache_version", [None, 5], ids=["current-cache", "orphaned-v5-cache"])
def test_pre_policy_source_routes_fresh_input_once_across_restart(tmp_path, monkeypatch, operation, cache_version):
    from gateway.hosted_room_history import mutate_message
    from gateway.hosted_room_responder_policy import DEFAULT_POLICY, update_policy

    monkeypatch.setattr(rooms, "local_authority_gateway_id", lambda: "home")
    db = tmp_path / "state.db"
    service = policy_service(db)
    room = service.create_room(room_id="upgrade-room", name="Upgrade", members=[
        {"member_id": profile, "profile": profile, "handle": profile} for profile in PROFILES])
    target = append(db, "old-target", "message.user", {"text": "Retired instructions", "thread_id": "thread"})
    source = append(db, "latest-source", "message.user", {"text": "Old source context", "thread_id": "thread"})
    service._policy_snapshot(service._room("upgrade-room"))
    changed = update_policy(service, room_id="upgrade-room", event_id="first-policy",
        expected_revision=room["revision"], policy={**DEFAULT_POLICY, "mode": "event_driven"})
    old_correction = mutate_message(db, room_id="upgrade-room", event_id="old-correction", target_event_id=target["event_id"],
        actor=target["actor"], operation="edit", text="Also retired by the next policy",
        expected_revision=target["seq"], authority_gateway_id="home", authority_epoch=1)
    changed = update_policy(service, room_id="upgrade-room", event_id="latest-policy",
        expected_revision=changed["revision"], policy={**DEFAULT_POLICY, "mode": "event_driven"})
    service.prepare_room(service.bindings()[0])
    assert not driver.list_tasks(db, room_id="upgrade-room")
    if operation == "participant":
        fresh = rooms.append_event(db, room_id="upgrade-room", event_id="fresh-input", kind="message.participant",
            actor={"kind": "member", "id": "writer"}, authority_gateway_id="home", authority_epoch=1, payload={
            "text": "@reviewer Fresh handoff", "thread_id": "thread", "member_id": "writer",
            "task_id": "historical-task", "execution_generation": 1, "mention_member_ids": ["reviewer"]})
        recipients = ("reviewer",)
    else:
        fresh = mutate_message(db, room_id="upgrade-room", event_id="fresh-input", target_event_id=target["event_id"],
            actor=target["actor"], operation=operation, text="Fresh correction", expected_revision=old_correction["message"]["revision"],
            reaction="ack", present=True, authority_gateway_id="home", authority_epoch=1)["event"]
        recipients = PROFILES

    if cache_version is not None:
        service._policy_snapshot(service._room("upgrade-room"))
        # Version 5 durably consumed the notice but had no projected source/thread.
        with sqlite3.connect(db) as conn:
            for table in ("hosted_room_policy_threads", "hosted_room_policy_events"):
                conn.execute(f"DELETE FROM {table} WHERE room_id='upgrade-room'")
            conn.execute("UPDATE hosted_room_policy_transcript_state SET schema_version=? WHERE room_id='upgrade-room'",
                         (cache_version,))
    accepted_history = log(db)
    delivered = []
    for member_id in recipients:
        # Restart before admission and after each recipient's durable terminal.
        service = policy_service(db)
        binding = service.bindings()[0]
        room = service._room("upgrade-room")
        snapshot = service._policy_snapshot(room)
        if not delivered:
            assert log(db) == accepted_history
        assert any(event["event_id"] == source["event_id"] for event in snapshot.events)
        planned = discussion.plan_next_task(room, snapshot.events, local_profiles=PROFILES,
            initial_watermarks=snapshot.watermarks, freeze_input_context=True).task
        full = discussion.plan_next_task(room, log(db), local_profiles=PROFILES, freeze_input_context=True).task
        assert planned is not None, "accepted post-policy input has no eligible source anchor"
        assert planned == full
        assert planned.payload["source_event_seq"] == source["seq"]
        assert planned.payload["input_context"] == {"watermark": changed["event"]["seq"], "event_seqs": [fresh["seq"]]}
        records = [json.loads(line) for line in planned.payload["prompt"].splitlines() if line.strip().startswith("{")]
        assert [record["event_id"] for record in records] == [fresh["event_id"]]
        assert records[0]["actor"] == fresh["actor"]
        assert planned.payload["target_profile"] == member_id
        service.prepare_room(binding)
        queued = driver.list_tasks(db, room_id="upgrade-room", status="queued")
        assert len(queued) == 1
        frozen = queued[0]
        reopened = HostedRoomPolicyCheckpoint(db)
        replay = reopened.events_for_task(room_id="upgrade-room", source_event_seq=source["seq"],
            input_context=frozen["payload"]["input_context"], task_id=frozen["identity"].task_id)
        assert discussion.reconstruct_task_plan(room, replay, frozen, local_profiles=PROFILES) == planned
        lease = service.runtime._ensure_lease(binding)
        attempt = driver.start_task(db, frozen["identity"], lease, expected_cancel_generation=0, clock=time.time)
        driver.settle_task(db, attempt, status="settled", settlement_id=f"done:{member_id}",
            result={"text": "PASS"}, clock=time.time)
        service.prepare_room(binding)
        driver.release_lease(db, lease, clock=time.time)
        delivered.append(frozen["identity"].task_id)

    service = policy_service(db)
    service.prepare_room(service.bindings()[0])
    assert not driver.list_tasks(db, room_id="upgrade-room", status="queued")
    assert {task["identity"].task_id for task in driver.list_tasks(db, room_id="upgrade-room")} == set(delivered)
    assert [event for event in log(db) if event["kind"] == "message.user"] == [target, source]


@pytest.mark.parametrize("scope", ["room", "thread"])
def test_pre_policy_anchor_recovery_does_not_revive_stopped_work(tmp_path, monkeypatch, scope):
    from gateway.hosted_room_history import mutate_message
    from gateway.hosted_room_responder_policy import DEFAULT_POLICY, update_policy

    monkeypatch.setattr(rooms, "local_authority_gateway_id", lambda: "home")
    db = tmp_path / "state.db"
    service = policy_service(db)
    room = service.create_room(room_id="upgrade-room", name="Stopped", members=[
        {"member_id": profile, "profile": profile, "handle": profile} for profile in PROFILES])
    source = append(db, "source", "message.user", {"text": "Old work", "thread_id": "thread"})
    if scope == "room":
        service.stop_room("upgrade-room", cancel_id="stop")
    else:
        service.stop_scope("upgrade-room", cancel_id="stop", scope={"kind": "thread", "thread_id": "thread"})
    update_policy(service, room_id="upgrade-room", event_id="policy", expected_revision=room["revision"],
        policy={**DEFAULT_POLICY, "mode": "event_driven"})
    mutate_message(db, room_id="upgrade-room", event_id="edit", target_event_id=source["event_id"],
        actor=source["actor"], operation="edit", text="Correction", expected_revision=source["seq"],
        authority_gateway_id="home", authority_epoch=1)
    reopened = policy_service(db)
    history = log(db)
    room = reopened._room("upgrade-room")
    assert discussion.plan_next_task(room, history, local_profiles=PROFILES).task is None
    reopened.prepare_room(reopened.bindings()[0])
    assert not driver.list_tasks(db, room_id="upgrade-room")
    assert log(db) == history

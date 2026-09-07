"""Participant operations under the existing admitted task and room authority."""
from contextlib import contextmanager
import hashlib
import json
import time

from gateway import hosted_rooms as rooms, hosted_room_driver as driver


class ParticipantScopeError(rooms.HostedRoomError):
    reason = "participant_scope_unavailable"


def current_participant():
    from tui_gateway.server import _current_runtime_session_record
    session = _current_runtime_session_record.get()
    if not isinstance(session, dict) or session.get("source") != "bot_room":
        raise ParticipantScopeError("Room tools require an admitted Group Chat turn")
    proof = session.get("_hosted_room_task")
    if not isinstance(proof, dict):
        raise ParticipantScopeError("Room participant scope is unavailable")
    return dict(proof)


def require_participant(conn, proof, *, now=None):
    """Check the complete producer fence in the caller's storage transaction."""
    timestamp = time.time() if now is None else now
    try:
        identity = driver.TaskIdentity(proof["room_id"], proof["task_id"], proof["thread_id"], proof["turn_id"])
        epoch, generation = proof["authority_epoch"], proof["execution_generation"]
        if type(epoch) is not int or type(generation) is not int or min(epoch, generation) < 1:
            raise ParticipantScopeError("Room participant generation is invalid")
        local = rooms.local_authority_gateway_id()
        if any(proof[key] != local for key in ("home_install_id", "target_install_id", "authority_gateway_id")):
            raise ParticipantScopeError("Participant tools require the room's execution host")
        row = rooms._room_row(conn, rooms._SELECT_ROOM, (identity.room_id,), identity.room_id)
        rooms.room_safety._raise_if_quarantined(conn, identity.room_id)
        rooms._require_authority(row, local, epoch, "Room participant authority changed")
        if row["disbanded_at"] is not None:
            raise ParticipantScopeError("Room is no longer active")
        room = rooms._room_from_row(row)
        member = next((m for m in room["members"] if m["member_id"] == proof["member_id"]), None)
        if member is None or member["profile"] != proof["target_profile"]:
            raise ParticipantScopeError("Room participant is no longer active")
        task = driver._load_task(conn, identity)
        payload = json.loads(task["payload_json"])
        if (task["status"] != "running" or task["execution_generation"] != generation
                or payload.get("target_member_id") != member["member_id"]
                or payload["target_profile"] != member["profile"]):
            raise ParticipantScopeError("Room participant attempt is stale or stopped")
        lease_row = conn.execute(driver._SELECT_LEASE, (identity.room_id,)).fetchone()
        if lease_row is None:
            raise ParticipantScopeError("Room participant lease is unavailable")
        lease = driver.DriverLease(identity.room_id, task["run_gateway_id"], epoch,
            task["run_process_generation"], task["run_lease_generation"], lease_row["expires_at"])
        driver.require_active_lease_in_transaction(conn, lease, now=timestamp)
        stop = conn.execute("SELECT 1 FROM hosted_room_events WHERE room_id=? "
            "AND kind='room.stop_requested' AND seq>? LIMIT 1", (identity.room_id, task["source_event_seq"])).fetchone()
        if stop is not None:
            raise ParticipantScopeError("Room participant was stopped")
        from gateway.hosted_room_scoped_controls import pending_stop
        if pending_stop(conn, {"identity": identity, "payload": payload, "execution_generation": generation}):
            raise ParticipantScopeError("Room participant was stopped in this thread or task")
        return room, member, task
    except (KeyError, TypeError, StopIteration, driver.DriverStateError) as exc:
        raise ParticipantScopeError("Room participant identity or attempt is unavailable") from exc


@contextmanager
def participant_transaction(proof):
    with rooms._transaction(rooms.default_db_path(), immediate=True) as conn:
        room, member, task = require_participant(conn, proof)
        yield conn, room, member, task


def participant_members(proof, args):
    with participant_transaction(proof) as (_, room, member, task):
        return {"room_id": room["room_id"], "thread_id": task["thread_id"],
            "self_member_id": member["member_id"], "members": room["members"],
            "retired_members": room.get("retired_members", [])}


def participant_history(proof, args):
    from gateway.hosted_room_history import history_page
    all_threads = args.get("all_threads", False)
    if type(all_threads) is not bool or (all_threads and "thread_id" in args):
        raise rooms.HostedRoomError("Choose one thread or boolean all_threads")
    with participant_transaction(proof) as (conn, room, _, task):
        return history_page(rooms.default_db_path(), room_id=room["room_id"],
            thread_id=None if all_threads else args.get("thread_id", task["thread_id"]),
            _conn=conn, **{k: args[k] for k in ("after_seq", "limit", "snapshot_seq", "query") if k in args})


def participant_upload(proof, args):
    from gateway.hosted_room_artifacts import RoomArtifactOutbox, RoomArtifactScope, RoomArtifactError
    from gateway.hosted_room_attachments import decode_content_base64, AttachmentError
    try:
        with participant_transaction(proof):
            scope = RoomArtifactScope.from_mapping({k: v for k, v in proof.items() if k not in {"thread_id", "turn_id"}})
        data = decode_content_base64(args["data_base64"])
        artifact = RoomArtifactOutbox(rooms.default_db_path()).put_bytes(scope=scope, data=data,
            source_name=args["name"], name=args["name"], write_guard=lambda conn: require_participant(conn, proof))
    except (RoomArtifactError, AttachmentError) as exc:
        raise rooms.HostedRoomError(str(exc)) from exc
    return {k: artifact[k] for k in ("artifact_id", "name", "size", "sha256")} | {"publication": "on_turn_completion"}


def participant_send(proof, args):
    from gateway.hosted_room_discussion import MAX_USER_TEXT_BYTES
    from gateway.hosted_room_responder_policy import validate_mentions
    event_key = rooms._event_id(args.get("event_id"))
    text = args.get("text")
    if not isinstance(text, str) or not text.strip() or len(text.encode("utf-8")) > MAX_USER_TEXT_BYTES:
        raise rooms.HostedRoomError("Participant message must contain bounded text")
    with participant_transaction(proof) as (conn, room, member, task):
        active = {m["member_id"] for m in room["members"]}
        mentions = args.get("mention_member_ids", [])
        if (not isinstance(mentions, list) or len(mentions) > len(active)
                or any(not isinstance(m, str) or m not in active for m in mentions)
                or len(set(mentions)) != len(mentions)):
            raise rooms.HostedRoomError("Explicit mention member is unavailable")
        selected = set(mentions) | set(validate_mentions(text, room["members"]))
        payload = {"text": text, "thread_id": task["thread_id"], "member_id": member["member_id"],
            "task_id": task["task_id"], "execution_generation": task["execution_generation"],
            "mention_member_ids": [m["member_id"] for m in room["members"] if m["member_id"] in selected]}
        parent_id = args.get("parent_event_id")
        if parent_id is not None:
            parent = rooms._load_event(conn, room["room_id"], rooms._event_id(parent_id))
            if (parent is None or parent["kind"] not in {"message.user", "message.member", "message.participant"}
                    or json.loads(parent["payload_json"]).get("thread_id") != task["thread_id"]):
                raise rooms.HostedRoomError("Reply parent is not a message in this room thread")
            payload["parent_event_id"] = parent_id
        actor = {"kind": "member", "id": member["member_id"], "profile": member["profile"]}
        if member.get("display_name"):
            actor["display_name"] = member["display_name"]
        actor, actor_json = rooms._validate_actor(actor, kind="message.participant")
        payload_json = rooms._payload_json(payload)
        seed = json.dumps([task["task_id"], task["execution_generation"], event_key], separators=(",", ":"))
        event_id = "participant:" + hashlib.sha256(seed.encode()).hexdigest()
        existing = rooms._load_event(conn, room["room_id"], event_id)
        if existing is not None:
            if rooms._event_content(existing) != ("message.participant", actor_json, room["authority_epoch"], payload_json):
                raise rooms.EventConflictError("Participant event_id already has different content")
            return {"event": rooms._event_from_row(existing, idempotent=True), "accepted": True}
        sent = conn.execute("SELECT COUNT(*) FROM hosted_room_events WHERE room_id=? AND kind='message.participant' "
            "AND json_extract(payload_json,'$.task_id')=? AND json_extract(payload_json,'$.execution_generation')=?",
            (room["room_id"], task["task_id"], task["execution_generation"])).fetchone()[0]
        if sent >= 16:
            raise rooms.HostedRoomError("Participant turn message limit reached; finish this turn")
        from gateway.hosted_room_event_policy import require_input_capacity
        require_input_capacity(conn, room["room_id"], "message.participant", payload)
        row = rooms._room_row(conn, rooms._SELECT_ROOM_WITH_BYTES, (room["room_id"],), room["room_id"])
        seq, timestamp = int(row["next_seq"]), time.time()
        size = rooms._insert_event(conn, row, room["room_id"], seq, event_id, "message.participant",
            actor_json, room["authority_epoch"], payload_json, timestamp)
        rooms._fenced_update(conn, "UPDATE hosted_rooms SET next_seq=?,event_bytes=event_bytes+?,updated_at=? "
            "WHERE room_id=? AND next_seq=?", (seq+1, size, timestamp, room["room_id"], seq),
            RuntimeError("Participant event lost its append fence"))
        event = rooms._event_from_row(rooms._load_event(conn, room["room_id"], event_id))
    # Existing room worker owns continuation; tools never start another driver.
    from tui_gateway.methods_groups import get_hosted_room_service
    service = get_hosted_room_service()
    if service is not None and service.db_path == rooms.default_db_path():
        service.wakeup()
    return {"event": event, "accepted": True}

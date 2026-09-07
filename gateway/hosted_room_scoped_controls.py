"""Persisted selectors for controls which must never expand to another thread."""
from __future__ import annotations

import hashlib
from collections.abc import Mapping

from gateway import hosted_rooms


def validate_scope(scope):
    if not isinstance(scope, Mapping) or scope.get("kind") not in {"thread", "task"}:
        raise ValueError("invalid_stop_scope")
    fields = {"kind", "thread_id"}
    if scope["kind"] == "task":
        fields |= {"task_id", "execution_generation", "cancel_generation"}
    if set(scope) != fields:
        raise ValueError("invalid_stop_scope_fields")
    result = dict(scope)
    for key in fields - {"kind", "execution_generation", "cancel_generation"}:
        result[key] = hosted_rooms._validate_identifier(scope[key], label=key, max_chars=128)
    for key in fields & {"execution_generation", "cancel_generation"}:
        if type(scope[key]) is not int or scope[key] < 0:
            raise ValueError(f"invalid_{key}")
    return result


def stop_event_id(cancel_id):
    cancel_id = hosted_rooms._validate_identifier(cancel_id, label="cancel_id", max_chars=128)
    return "scoped-stop:" + hashlib.sha256(cancel_id.encode()).hexdigest()[:32]


def existing_stop(db_path, room_id, cancel_id, scope):
    with hosted_rooms._transaction(db_path) as conn:
        row = hosted_rooms._load_event(conn, room_id, stop_event_id(cancel_id))
        if row is None:
            return None
        event = hosted_rooms._event_from_row(row, idempotent=True)
    expected = {"cancel_id": cancel_id, **{k: v for k, v in scope.items() if k != "kind"}}
    if event["kind"] != f"{scope['kind']}.stop_requested" or event["payload"] != expected:
        raise hosted_rooms.EventConflictError("cancel_id already has another stop scope")
    return event


def append_stop(db_path, room, cancel_id, scope):
    return hosted_rooms.append_event(
        db_path, room_id=room["room_id"], event_id=stop_event_id(cancel_id),
        kind=f"{scope['kind']}.stop_requested",
        actor={"kind": "gateway", "id": room["authority_gateway_id"]},
        authority_gateway_id=room["authority_gateway_id"], authority_epoch=room["authority_epoch"],
        payload={"cancel_id": cancel_id, **{k: v for k, v in scope.items() if k != "kind"}})


def thread_stop_seq(conn, room_id, thread_id):
    row = conn.execute("""SELECT MAX(seq) FROM hosted_room_events
        WHERE room_id=? AND kind='thread.stop_requested'
        AND json_extract(payload_json, '$.thread_id')=?""", (room_id, thread_id)).fetchone()
    return int(row[0] or 0)


def pending_stop(conn, task):
    identity = task["identity"]
    return conn.execute("""SELECT seq, payload_json FROM hosted_room_events
        WHERE room_id=? AND seq>? AND (
            (kind='thread.stop_requested' AND json_extract(payload_json, '$.thread_id')=?)
            OR (kind='task.stop_requested' AND json_extract(payload_json, '$.task_id')=?
                AND json_extract(payload_json, '$.execution_generation')=?))
        ORDER BY seq DESC LIMIT 1""",
        (identity.room_id, task["payload"]["source_event_seq"], identity.thread_id,
         identity.task_id, task["execution_generation"])).fetchone()


def task_receipt(task):
    identity = task["identity"]
    return {"room_id": identity.room_id, "thread_id": identity.thread_id,
            "task_id": identity.task_id, "status": task["status"],
            "execution_generation": int(task["execution_generation"]),
            "cancel_generation": int(task["cancel_generation"])}


def require_active_task(conn, *, room_id, member_id, thread_id, task_id, execution_generation):
    from gateway import hosted_room_driver as state
    row = conn.execute("SELECT * FROM hosted_room_driver_tasks WHERE room_id=? AND task_id=?",
        (room_id, task_id)).fetchone()
    if row is None:
        raise ValueError("scoped_task_not_found")
    task = state._task_from_row(row)
    if (task["identity"].thread_id != thread_id or task["execution_generation"] != execution_generation
        or task["status"] != "running"
        or task["payload"].get("target_member_id", task["payload"].get("target_profile")) != member_id):
        raise ValueError("scoped_task_attempt_changed")
    room_stop = conn.execute("SELECT MAX(seq) FROM hosted_room_events WHERE room_id=? AND kind='room.stop_requested'",
        (room_id,)).fetchone()[0]
    if int(room_stop or 0) > int(task["payload"]["source_event_seq"]) or pending_stop(conn, task) is not None:
        raise ValueError("scoped_task_stopped")
    return task

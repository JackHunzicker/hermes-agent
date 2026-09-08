"""Event-driven routing and bounded derived state using the existing room checkpoint."""
from __future__ import annotations
import json
import hashlib
from functools import partial
from gateway import hosted_rooms as rooms
from gateway.hosted_room_responder_policy import DEFAULT_POLICY, responders, validate_mentions, _MENTION, _field
from gateway.hosted_rooms_common import table_exists

MESSAGE_KINDS = {"message.user", "message.member", "message.participant"}
NOTICE_KINDS = {"message.edited", "message.deleted", "message.reaction"}


def policy_for(conn, room_id):
    row = conn.execute("SELECT responder_policy_json FROM hosted_rooms WHERE room_id=?", (room_id,)).fetchone()
    return (json.loads(row[0]) if row else None) or DEFAULT_POLICY


def continuation_state(conn, room_id, now):
    policy = policy_for(conn, room_id)
    if policy["mode"] != "event_driven":
        return {"state": "legacy_bounded"}
    rows = conn.execute("SELECT created_at FROM hosted_room_driver_tasks WHERE room_id=? AND created_at>? "
                        "ORDER BY created_at DESC LIMIT ?",
                        (room_id, now - policy["window_seconds"], policy["max_turns_per_window"])).fetchall() if table_exists(conn, "hosted_room_driver_tasks") else []
    until = min(float(row[0]) for row in rows) + policy["window_seconds"] if len(rows) >= policy["max_turns_per_window"] else None
    return {"state": "cooldown" if until is not None else "ready", "cooldown_until": until,
            "max_turns_per_window": policy["max_turns_per_window"], "window_seconds": policy["window_seconds"]}


def require_admission_budget(conn, room_id, now, error):
    if continuation_state(conn, room_id, now)["state"] == "cooldown":
        raise error("room continuation cooldown is active")


def service_state(service, room_id):
    with rooms._transaction(service.db_path) as conn:
        return continuation_state(conn, room_id, service.runtime.clock())


def plan(room, events, *, initial_watermarks, freeze_input_context):
    from gateway import hosted_room_discussion as d
    from gateway.hosted_room_history import policy_events
    raw = d._validated_events(events, room=room)
    validated = d._validated_events(policy_events(events), room=room)
    stopped = max((e.seq for e in raw if e.kind == "room.stop_requested"), default=0)
    thread_stops = {str(e.payload["thread_id"]): e.seq for e in raw if e.kind == "thread.stop_requested"}
    policy_seq = max((e.seq for e in raw if e.kind == "room.policy_changed"), default=0)
    sources = {str(e.payload["thread_id"]): e for e in raw if e.kind == "message.user"}
    committed = {str(e.payload["message_event_id"]) for e in raw if e.kind == "turn.settled" and e.payload.get("message_event_id")}
    watermarks = dict(initial_watermarks or {})
    for event in raw:
        if event.kind in d._TERMINAL_EVENT_KINDS:
            key = (str(event.payload["thread_id"]), str(event.payload["member_id"]))
            watermarks[key] = max(watermarks.get(key, 0), int(event.payload["seen_through_seq"]))
    idle = None
    for source in sorted(sources.values(), key=lambda e: e.seq):
        thread_id = str(source.payload["thread_id"])
        stop_floor = max(stopped, thread_stops.get(thread_id, 0))
        routing_floor = max(stop_floor, policy_seq)
        # Policy changes retire input, not the durable identity of its thread.
        if source.seq <= stop_floor:
            continue
        decide = partial(d.DiscussionDecision, discussion_event_id=source.event_id, source_event_seq=source.seq, thread_id=thread_id)
        messages = tuple(e for e in validated if e.payload.get("thread_id") == thread_id and
                         (e.kind in {"message.user", "message.participant"} or e.kind == "message.member" and e.event_id in committed))
        candidates = []
        for member_index, member in enumerate(room.members):
            watermark = max(watermarks.get((thread_id, member.member_id), 0), routing_floor)
            targets = []
            for event in messages:
                if event.seq <= max(watermark, routing_floor) or event.payload.get("member_id") == member.member_id:
                    continue
                if member.member_id in event.payload.get("mention_member_ids", ()):
                    targets.append(event)
                    continue
                # A mutation notice must reach each participant once; it never becomes a user source.
                original_kind = next((e.kind for e in raw if e.seq == event.seq), event.kind)
                if original_kind in NOTICE_KINDS or member in event_responders(event.payload["text"], room.members,
                        room.responder_policy, default=original_kind == "message.user"):
                    targets.append(event)
            if not targets:
                continue
            candidates.append((targets[0].seq, member_index, member, watermark))
        for maximum, member_index, member, watermark in sorted(candidates):
            seen, delta, attachments = d._bounded_task_delta(messages, watermark=watermark, maximum_seq=maximum)
            if not delta:
                continue
            prompt = d._build_prompt(room=room, member=member, messages=messages, watermark=watermark, seen_through_seq=seen)
            return decide("task", "new_room_events", task=d._make_task_plan(room=room, discussion_event=source,
                member=member, member_index=member_index, round_index=0, seen_through_seq=seen, prompt=prompt,
                attachments=attachments, input_context=d.validate_task_input({"watermark": watermark,
                    "event_seqs": [e.seq for e in delta]}) if freeze_input_context else None,
                session_scope="thread_member_v1" if freeze_input_context and d._peer_id(member) is None else None))
        idle = decide("settled", "no_pending_events")
    return idle or d.DiscussionDecision("idle", "no_pending_user_event")


def _trim(checkpoint, conn, room_id, thread_id):
    cutoff = conn.execute("""SELECT seq FROM hosted_room_policy_transcript WHERE room_id=? AND thread_id=?
        AND kind IN ('message.user','message.member','message.participant','message.edited','message.deleted','message.reaction')
        ORDER BY seq DESC LIMIT 1 OFFSET 23""", (room_id, thread_id)).fetchone()
    if cutoff:
        conn.execute("DELETE FROM hosted_room_policy_transcript WHERE room_id=? AND thread_id=? AND seq<?",
                     (room_id, thread_id, int(cutoff[0])))
    # Source rows remain pinned; terminal reconstruction uses frozen raw event references.
    conn.execute("""DELETE FROM hosted_room_policy_events WHERE room_id=? AND thread_id=?
        AND seq NOT IN (SELECT seq FROM hosted_room_policy_events WHERE room_id=? AND thread_id=?
                       ORDER BY seq DESC LIMIT 48)
        AND seq NOT IN (SELECT latest_user_seq FROM hosted_room_policy_threads WHERE room_id=? AND thread_id=?)""",
        (room_id, thread_id, room_id, thread_id, room_id, thread_id))


def apply_event(checkpoint, conn, event):
    if event["kind"] == "room.policy_changed":
        conn.execute("DELETE FROM hosted_room_policy_threads WHERE room_id=?", (event["room_id"],))
        conn.execute("DELETE FROM hosted_room_policy_events WHERE room_id=?", (event["room_id"],))
        return True
    if policy_for(conn, event["room_id"])["mode"] != "event_driven":
        if event["kind"] == "message.participant" or event["kind"] in NOTICE_KINDS:
            checkpoint._store_transcript_event(conn, event=event, thread_id=event["payload"]["thread_id"])
            return True
        return False
    kind, payload, room_id = event["kind"], event["payload"], event["room_id"]
    thread_id = payload.get("thread_id")
    if kind == "room.activity":
        conn.execute("UPDATE hosted_room_policy_threads SET completed=1 WHERE room_id=? AND thread_id=?",
                     (room_id, thread_id))
        return True
    if kind == "message.user":
        checkpoint._apply_user_message(conn, event, payload)
    elif kind in {"message.member", "turn.settled", "turn.failed", "turn.cancelled", "turn.deferred"}:
        source = conn.execute("SELECT * FROM hosted_room_events WHERE room_id=? AND event_id=? AND kind='message.user'",
                              (room_id, payload["discussion_event_id"])).fetchone()
        if source is not None:
            checkpoint._store_active_event(conn, event=rooms._event_from_row(source), thread_id=thread_id,
                                           discussion_event_id=payload["discussion_event_id"])
        previous = conn.execute("SELECT seen_through_seq FROM hosted_room_policy_watermarks WHERE room_id=? AND thread_id=? AND member_id=?",
                                (room_id, thread_id, payload["member_id"])).fetchone()
        checkpoint._apply_discussion_event(conn, event, payload)
        if kind.startswith("turn."):
            conn.execute("UPDATE hosted_room_policy_watermarks SET seen_through_seq=? WHERE room_id=? AND thread_id=? AND member_id=?",
                         (max(int(previous[0]) if previous else 0, int(payload["seen_through_seq"])), room_id, thread_id, payload["member_id"]))
    elif kind == "message.participant" or kind in NOTICE_KINDS:
        _restore_source(checkpoint, conn, event)
        checkpoint._store_transcript_event(conn, event=event, thread_id=thread_id)
    else:
        return False
    if kind != "message.member":
        from gateway.hosted_room_scoped_controls import thread_stop_seq
        cursor = conn.execute("SELECT stopped_through_seq FROM hosted_room_policy_cursors WHERE room_id=?", (room_id,)).fetchone()
        fence = max(int(cursor[0]) if cursor else 0, thread_stop_seq(conn, room_id, thread_id))
        conn.execute("UPDATE hosted_room_policy_threads SET completed=0 WHERE room_id=? AND thread_id=? AND latest_user_seq>?",
                     (room_id, thread_id, fence))
    _trim(checkpoint, conn, room_id, thread_id)
    return True


def _restore_source(checkpoint, conn, event):
    """Recover one identity anchor after policy reset without reviving stopped work."""
    room_id, thread_id = event["room_id"], event["payload"]["thread_id"]
    if conn.execute("SELECT 1 FROM hosted_room_policy_threads WHERE room_id=? AND thread_id=?",
                    (room_id, thread_id)).fetchone():
        return
    from gateway.hosted_room_scoped_controls import thread_stop_seq
    cursor = conn.execute("SELECT stopped_through_seq FROM hosted_room_policy_cursors WHERE room_id=?",
                          (room_id,)).fetchone()
    fence = max(int(cursor[0]) if cursor else 0, thread_stop_seq(conn, room_id, thread_id))
    source = conn.execute("""SELECT * FROM hosted_room_events WHERE room_id=? AND kind='message.user'
        AND json_extract(payload_json, '$.thread_id')=? AND seq>? AND seq<? ORDER BY seq DESC LIMIT 1""",
        (room_id, thread_id, fence, event["seq"])).fetchone()
    if source is not None:
        source = rooms._event_from_row(source)
        checkpoint._apply_user_message(conn, source, source["payload"])


def augment_references(conn, room_id, events, thread_id, *, pending_kinds=None):
    """Retained terminals require their original source and message to validate replay."""
    by_seq = {e["seq"]: e for e in events}
    # Legacy recipients can remain idle for different bounded discussions. Recover
    # each recipient's oldest accepted tool/notice prefix, not only the slowest one.
    recipients = [None] if pending_kinds is None else [m["member_id"] for m in json.loads(conn.execute(
        "SELECT members_json FROM hosted_rooms WHERE room_id=?", (room_id,)).fetchone()[0])]
    pending = [event for recipient in recipients for event in pending_events(
        conn, room_id, thread_id, limit=24, kinds=pending_kinds, member_id=recipient)]
    for event in pending:
        by_seq[event["seq"]] = event
        if event["kind"] == "message.member":
            row = conn.execute("SELECT * FROM hosted_room_events WHERE room_id=? AND kind='turn.settled' "
                "AND json_extract(payload_json, '$.message_event_id')=?", (room_id, event["event_id"])).fetchone()
            if row:
                terminal = rooms._event_from_row(row)
                by_seq[terminal["seq"]] = terminal
    events = list(by_seq.values())
    ids = {e["payload"][key] for e in events if e["kind"].startswith("turn.")
           for key in ("discussion_event_id", "message_event_id") if e["payload"].get(key)}
    for event_id in ids:
        row = conn.execute("SELECT * FROM hosted_room_events WHERE room_id=? AND event_id=?", (room_id, event_id)).fetchone()
        if row:
            event = rooms._event_from_row(row)
            by_seq[event["seq"]] = event
    return [by_seq[seq] for seq in sorted(by_seq)]


def require_input_capacity(conn, room_id, kind, payload):
    """Reject overflow before acknowledging ingress; accepted messages keep their turn."""
    if kind not in {"message.user", "message.participant"}:
        return
    policy = policy_for(conn, room_id)
    if policy["mode"] != "event_driven":
        return
    if len(pending_events(conn, room_id, payload.get("thread_id"), limit=24,
                         kinds={"message.user", "message.participant"})) >= 24:
        raise rooms.RoomConflictError("room input queue is full; wait for pending turns")


def pending_events(conn, room_id, thread_id, *, limit, kinds=None, member_id=None):
    """Recover the oldest unconsumed canonical references after checkpoint eviction."""
    policy = policy_for(conn, room_id)
    row = conn.execute("SELECT members_json FROM hosted_rooms WHERE room_id=?", (room_id,)).fetchone()
    members = json.loads(row[0])
    active = {m["member_id"] for m in members}
    marks = {r[0]: int(r[1]) for r in conn.execute(
        "SELECT member_id, seen_through_seq FROM hosted_room_policy_watermarks WHERE room_id=? AND thread_id=?",
        (room_id, thread_id))} if table_exists(conn, "hosted_room_policy_watermarks") else {}
    from gateway.hosted_room_scoped_controls import thread_stop_seq
    stop = conn.execute("SELECT MAX(seq) FROM hosted_room_events WHERE room_id=? "
        "AND kind IN ('room.stop_requested','room.policy_changed')", (room_id,)).fetchone()[0]
    fence = max(int(stop or 0), thread_stop_seq(conn, room_id, thread_id))
    result = []
    for row in conn.execute("SELECT * FROM hosted_room_events WHERE room_id=? "
            "AND json_extract(payload_json, '$.thread_id')=? AND seq>? ORDER BY seq", (room_id, thread_id, fence)):
        event = rooms._event_from_row(row)
        kind, message = event["kind"], event["payload"]
        if kind not in (kinds or MESSAGE_KINDS | NOTICE_KINDS):
            continue
        if kind == "message.member" and conn.execute("SELECT 1 FROM hosted_room_events WHERE room_id=? "
                "AND kind='turn.settled' AND json_extract(payload_json, '$.message_event_id')=? LIMIT 1",
                (room_id, event["event_id"])).fetchone() is None:
            continue
        targets = active if kind in NOTICE_KINDS else set(message.get("mention_member_ids", ())) | {
            _field(m, "member_id") for m in event_responders(message["text"], members, policy, default=kind == "message.user")}
        targets = (targets & active) - {message.get("member_id")}
        if member_id is not None:
            targets &= {member_id}
        if any(marks.get(target, 0) < event["seq"] for target in targets):
            result.append(event)
            if len(result) >= limit:
                break
    return result


def event_responders(text, members, policy, *, default):
    try:
        return responders(text, members, policy, default=default)
    except rooms.HostedRoomError:
        targets = set()
        for mention in _MENTION.finditer(text):
            try:
                targets.update(validate_mentions(mention.group(0), members))
            except rooms.HostedRoomError:
                continue
        return tuple(m for m in members if _field(m, "member_id") in targets)


def publish_unavailable_mentions(service, room, events):
    for event in events:
        if event["kind"] != "message.member":
            continue
        for mention in _MENTION.finditer(event["payload"]["text"]):
            try:
                validate_mentions(mention.group(0), room["members"])
            except rooms.HostedRoomError as exc:
                digest = hashlib.sha256((event["event_id"] + mention.group(0)).encode()).hexdigest()
                rooms.append_event(service.db_path, room_id=room["room_id"], event_id="mention-unavailable:" + digest,
                    kind="member.unavailable", actor={"kind": "gateway", "id": room["authority_gateway_id"]},
                    payload={"thread_id": event["payload"]["thread_id"], "source_event_id": event["event_id"],
                             "mention": mention.group(0), "reason": "explicit_mention_unavailable", "text": str(exc)},
                    authority_gateway_id=room["authority_gateway_id"], authority_epoch=room["authority_epoch"])

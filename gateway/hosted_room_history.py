"""Current shared-message views over the immutable hosted-room event log."""
from contextlib import nullcontext
import hashlib
import json

from gateway import hosted_rooms as rooms

MESSAGE_KINDS = frozenset({"message.user", "message.member", "message.participant"})
MUTATION_KINDS = frozenset({"message.edited", "message.deleted", "message.reaction"})
_OPERATIONS = {"edit": "message.edited", "delete": "message.deleted", "react": "message.reaction"}


def _text(value, label, *, max_bytes=64 * 1024):
    if not isinstance(value, str) or not value.strip() or len(value.encode("utf-8")) > max_bytes:
        raise rooms.HostedRoomError(f"{label} must be nonempty text of at most {max_bytes} UTF-8 bytes")
    return value


def _actor_key(actor):
    return actor["kind"], actor["id"]


def project_events(events):
    """Project a complete ordered snapshot without changing its source records."""
    messages = {}
    reactions = {}
    for event in events:
        kind, payload = event["kind"], event["payload"]
        if kind in MESSAGE_KINDS:
            event_id = event["event_id"]
            messages[event_id] = {
                "event_id": event_id, "seq": event["seq"],
                "thread_id": payload.get("thread_id", event_id),
                "parent_event_id": payload.get("parent_event_id"), "actor": dict(event["actor"]),
                "original_text": payload.get("text", ""), "text": payload.get("text", ""),
                "deleted": False, "revision": event["seq"], "updated_at": event["created_at"],
                "attachments": payload.get("attachments", []), "reactions": [],
            }
            reactions[event_id] = {}
        elif kind in MUTATION_KINDS:
            target = payload["target_event_id"]
            message = messages.get(target)
            if message is None:
                raise rooms.HostedRoomError("message mutation target is absent from snapshot")
            if kind == "message.reaction":
                members = reactions[target].setdefault(payload["reaction"], {})
                key = _actor_key(event["actor"])
                if payload["present"]:
                    members[key] = dict(event["actor"])
                else:
                    members.pop(key, None)
            else:
                message.update(text=payload.get("text"), deleted=kind == "message.deleted",
                               revision=event["seq"], updated_at=event["created_at"])
    for event_id, message in messages.items():
        message["reactions"] = [{"reaction": reaction, "actors": [actors[k] for k in sorted(actors)]}
                                for reaction, actors in sorted(reactions[event_id].items()) if actors]
    return list(messages.values())


def _load_projection(conn, room_id, snapshot_seq):
    return project_events([rooms._event_from_row(row) for row in conn.execute(
        f"SELECT {rooms._EVENT_COLUMNS} FROM hosted_room_events WHERE room_id=? AND seq<=? ORDER BY seq",
        (room_id, snapshot_seq))])


def _snapshot(conn, room_id, *, include_disbanded=False):
    row = rooms._room_row(conn, rooms._SELECT_ROOM_WITH_BYTES, (room_id,), room_id)
    if row["disbanded_at"] is not None and not include_disbanded:
        raise rooms.RoomNotFoundError("hosted room not found")
    return row


def history_page(db_path, *, room_id, thread_id=None, after_seq=0, limit=100,
                 snapshot_seq=None, query=None, include_disbanded=False, _conn=None):
    room_id = rooms._room_id(room_id)
    after_seq = rooms._non_negative(after_seq, "after_seq")
    limit = rooms._bounded_limit(limit, 100)
    if thread_id is not None:
        thread_id = rooms._event_id(thread_id)
    if query is not None:
        query = _text(query, "query", max_bytes=2048)
        if len(query) > 512:
            raise rooms.HostedRoomError("query must be at most 512 characters")
    with nullcontext(_conn) if _conn is not None else rooms._transaction(db_path) as conn:
        if _conn is None:
            conn.execute("BEGIN")
        room = _snapshot(conn, room_id, include_disbanded=include_disbanded)
        latest = int(room["next_seq"]) - 1
        snapshot = latest if snapshot_seq is None else rooms._non_negative(snapshot_seq, "snapshot_seq")
        if not after_seq <= snapshot <= latest:
            raise rooms.HostedRoomError("history cursor is ahead of its snapshot")
        messages = _load_projection(conn, room_id, snapshot)
        if thread_id is not None and not any(m["thread_id"] == thread_id for m in messages):
            raise rooms.HostedRoomError("thread not found in room snapshot")
        candidates = [m for m in messages if m["seq"] > after_seq
                      and (thread_id is None or m["thread_id"] == thread_id)
                      and (query is None or (not m["deleted"] and query.casefold() in m["text"].casefold()))]
        page, size = [], 0
        for message in candidates[:limit]:
            encoded_size = len(json.dumps(message, ensure_ascii=False).encode("utf-8"))
            if encoded_size > rooms.MAX_LOG_PAGE_BYTES - 1024:
                raise rooms.HostedRoomError("projected message exceeds history page limit")
            if page and size + encoded_size > rooms.MAX_LOG_PAGE_BYTES - 1024:
                break
            page.append(message)
            size += encoded_size
        has_more = len(candidates) > len(page)
        return {"messages": page, "cursor": page[-1]["seq"] if has_more else snapshot,
                "snapshot_seq": snapshot, "latest_seq": latest, "has_more": has_more,
                "authority": {"gateway_id": room["authority_gateway_id"], "epoch": room["authority_epoch"]}}


def _mutation_payload(operation, *, target_event_id, expected_revision, text, reaction, present):
    if operation not in _OPERATIONS:
        raise rooms.HostedRoomError("unknown message mutation")
    payload = {"target_event_id": rooms._event_id(target_event_id)}
    if operation == "react":
        reaction = _text(reaction, "reaction", max_bytes=128)
        if len(reaction) > 64 or type(present) is not bool:
            raise rooms.HostedRoomError("reaction requires at most 64 characters and boolean present")
        payload.update(reaction=reaction, present=present)
    else:
        payload["expected_revision"] = rooms._require_positive_int(expected_revision, "expected_revision")
        if operation == "edit":
            payload["text"] = _text(text, "text")
    return payload


def _active_actor(room, actor):
    if actor["kind"] == "member" and not any(m.get("member_id") == actor["id"]
                                              for m in json.loads(room["members_json"])):
        raise rooms.HostedRoomError("removed room member cannot mutate messages")


def mutate_message(db_path, *, room_id, event_id, target_event_id, actor, operation,
                   authority_gateway_id, authority_epoch, expected_revision=None, text=None,
                   reaction=None, present=None):
    room_id, event_id = rooms._room_id(room_id), rooms._event_id(event_id)
    payload = _mutation_payload(operation, target_event_id=target_event_id, expected_revision=expected_revision,
                                text=text, reaction=reaction, present=present)
    kind = _OPERATIONS[operation]
    actor, actor_json = rooms._validate_actor(actor, kind=kind)
    authority_epoch = rooms._require_positive_int(authority_epoch, "authority_epoch")
    with rooms._transaction(db_path, immediate=True) as conn:
        rooms.room_safety._raise_if_quarantined(conn, room_id)
        room = _snapshot(conn, room_id)
        rooms._require_authority(room, authority_gateway_id, authority_epoch, "stale hosted room authority")
        _active_actor(room, actor)
        messages = _load_projection(conn, room_id, int(room["next_seq"]) - 1)
        target = next((m for m in messages if m["event_id"] == target_event_id), None)
        if target is None:
            raise rooms.HostedRoomError("message target not found in room")
        payload["thread_id"] = target["thread_id"]
        payload_json = rooms._payload_json(payload)
        previous = rooms._load_event(conn, room_id, event_id)
        if previous is not None:
            if rooms._event_content(previous) != (kind, actor_json, authority_epoch, payload_json):
                raise rooms.EventConflictError("event_id already exists with different mutation")
            return {"event": rooms._event_from_row(previous, idempotent=True),
                    "message": target, "idempotent": True}
        if operation != "react" and _actor_key(actor) != _actor_key(target["actor"]):
            raise rooms.HostedRoomError("only the original author may edit or delete a message")
        if target["deleted"]:
            raise rooms.RoomConflictError("message has been deleted")
        if operation != "react" and target["revision"] != expected_revision:
            raise rooms.RoomConflictError("message revision changed; reload before editing")
        now, seq = rooms._now(None), int(room["next_seq"])
        size = rooms._insert_event(conn, room, room_id, seq, event_id, kind, actor_json,
                                   authority_epoch, payload_json, now)
        conn.execute("UPDATE hosted_rooms SET next_seq=next_seq+1,event_bytes=event_bytes+?,updated_at=? WHERE room_id=?",
                     (size, now, room_id))
        event = rooms._event_from_row(rooms._load_event(conn, room_id, event_id))
        current = next(m for m in _load_projection(conn, room_id, seq) if m["event_id"] == target_event_id)
        return {"event": event, "message": current, "idempotent": False}


def mutation_event_id(client_event_id):
    return "mutation:" + hashlib.sha256(rooms._event_id(client_event_id).encode()).hexdigest()


def read_cursor(db_path, *, room_id, reader, thread_id=None, through_seq=None):
    """Read/advance one viewer's durable bounds; receipt delivery is unrelated."""
    room_id = rooms._room_id(room_id)
    if not isinstance(reader, dict) or reader.get("kind") not in {"user", "member"}:
        raise rooms.HostedRoomError("reader must be a user or member actor")
    reader, _ = rooms._validate_actor(reader, kind="message.user" if reader["kind"] == "user" else "message.member")
    scope = "" if thread_id is None else rooms._event_id(thread_id)
    if through_seq is not None:
        through_seq = rooms._non_negative(through_seq, "through_seq")
    with rooms._transaction(db_path, immediate=True) as conn:
        room = _snapshot(conn, room_id)
        latest = int(room["next_seq"]) - 1
        if through_seq is not None and through_seq > latest:
            raise rooms.HostedRoomError("read cursor is ahead of room history")
        messages = _load_projection(conn, room_id, latest)
        if scope and not any(m["thread_id"] == scope for m in messages):
            raise rooms.HostedRoomError("thread not found in room")
        conn.execute("""CREATE TABLE IF NOT EXISTS hosted_room_read_cursors (
            room_id TEXT NOT NULL REFERENCES hosted_rooms(room_id) ON DELETE CASCADE,
            reader_kind TEXT NOT NULL, reader_id TEXT NOT NULL, thread_id TEXT NOT NULL,
            through_seq INTEGER NOT NULL CHECK(through_seq>=0),
            PRIMARY KEY(room_id, reader_kind, reader_id, thread_id))""")
        key = (room_id, reader["kind"], reader["id"])
        if through_seq is not None:
            _active_actor(room, reader)
            conn.execute("""INSERT INTO hosted_room_read_cursors VALUES (?,?,?,?,?)
                ON CONFLICT(room_id,reader_kind,reader_id,thread_id) DO UPDATE
                SET through_seq=MAX(through_seq,excluded.through_seq)""", (*key, scope, through_seq))
        bounds = {row["thread_id"]: int(row["through_seq"]) for row in conn.execute(
            "SELECT thread_id,through_seq FROM hosted_room_read_cursors WHERE room_id=? AND reader_kind=? AND reader_id=?", key)}
        room_bound = bounds.get("", 0)
        unread = sum(1 for m in messages if not m["deleted"] and _actor_key(m["actor"]) != _actor_key(reader)
                     and (not scope or m["thread_id"] == scope)
                     and m["seq"] > max(room_bound, bounds.get(m["thread_id"], 0)))
        return {"room_id": room_id, "thread_id": thread_id, "reader": reader,
                "through_seq": max(room_bound, bounds.get(scope, 0)), "latest_seq": latest, "unread_count": unread}


def policy_events(events):
    """Append-only context notices; original native turns and stored events stay intact.

    Source anchors must be selected from raw events before this transcript view.
    The mutation retains its own immutable sequence, actor and event identifier.
    """
    from gateway.hosted_room_discussion import MAX_USER_TEXT_BYTES, _truncate_utf8_text
    result = []
    for event in events:
        if event["kind"] not in MUTATION_KINDS:
            result.append(event)
            continue
        payload, actor = event["payload"], event["actor"]
        target = payload["target_event_id"]
        if event["kind"] == "message.edited":
            notice = f"[Message edited: {target}. Prior delivery remains historical.]\n{payload['text']}"
        elif event["kind"] == "message.deleted":
            notice = f"[Message deleted: {target}. Its original remains in the audit log; do not treat it as current.]"
        else:
            action = "added" if payload["present"] else "removed"
            notice = f"[Reaction {action} on message {target}: {payload['reaction']}]"
        notice = _truncate_utf8_text(notice, max_bytes=MAX_USER_TEXT_BYTES,
            suffix="\n[Notice truncated; read the original mutation event for its complete text.]")
        projected_payload = {"text": notice, "thread_id": payload["thread_id"]}
        if actor["kind"] == "member":
            projected_payload["member_id"] = actor["id"]
        result.append({**event, "kind": "message.user" if actor["kind"] == "user" else "message.participant",
                       "payload": projected_payload})
    return result

"""Persisted responder choice and bounded continuation policy on the room log."""
from __future__ import annotations

import json
import re
from collections.abc import Mapping

from gateway import hosted_rooms as rooms
from gateway.hosted_rooms_common import table_exists

DEFAULT_POLICY = {"mode": "legacy_bounded", "default_responder": "all", "leader_member_id": None,
                  "max_turns_per_window": 10, "window_seconds": 60}
_MENTION = re.compile(r"(?<![\w@])@([A-Za-z0-9][A-Za-z0-9._:-]*)")


def _field(member, name):
    return member.get(name) if isinstance(member, Mapping) else getattr(member, name)


def validate_mentions(text, members):
    """Resolve explicit handles/immutable IDs, refusing unknown or retired targets."""
    members = tuple(members)
    aliases = {str(_field(m, "member_id")).casefold(): _field(m, "member_id") for m in members}
    # Preserve existing @handle meaning in stored rooms, regardless of roster order.
    # Immutable IDs are aliases only where no handle already owns that spelling.
    aliases.update({str(_field(m, "handle")).casefold(): _field(m, "member_id") for m in members})
    requested = set()
    for match in _MENTION.finditer(str(text)):
        token = match.group(1).rstrip(".:").casefold()
        if token in {"all", "everyone"}:
            requested.update(_field(m, "member_id") for m in members)
        elif token in aliases:
            requested.add(aliases[token])
        else:
            raise rooms.HostedRoomError(f"Explicit mention is unavailable: @{token}")
    return tuple(_field(m, "member_id") for m in members if _field(m, "member_id") in requested)


def normalize_policy(value, members):
    if not isinstance(value, Mapping) or set(value) != set(DEFAULT_POLICY):
        raise rooms.HostedRoomError("responder policy requires exactly the supported policy fields")
    result = dict(value)
    if value["mode"] not in {"legacy_bounded", "event_driven"}:
        raise rooms.HostedRoomError("unsupported responder policy mode")
    if value["default_responder"] not in {"all", "leader", "mentions_only"}:
        raise rooms.HostedRoomError("unsupported default responder")
    for key, maximum in (("max_turns_per_window", 32), ("window_seconds", 3600)):
        if type(value[key]) is not int or not 1 <= value[key] <= maximum:
            raise rooms.HostedRoomError(f"invalid responder policy {key}")
    leader = value["leader_member_id"]
    ids = {_field(m, "member_id") for m in members}
    if leader is not None and (not isinstance(leader, str) or leader not in ids):
        raise rooms.HostedRoomError("leader member is unavailable")
    if value["default_responder"] == "leader" and leader is None:
        raise rooms.HostedRoomError("leader responder requires leader_member_id")
    return result


def responders(text, members, policy, *, default=True):
    selected = validate_mentions(text, members)
    if selected:
        return tuple(m for m in members if _field(m, "member_id") in selected)
    if not default or policy["default_responder"] == "mentions_only":
        return ()
    if policy["default_responder"] == "leader":
        return tuple(m for m in members if _field(m, "member_id") == policy["leader_member_id"])
    return tuple(members)


def update_policy(service, *, room_id, event_id, expected_revision, policy):
    room_id, event_id = rooms._room_id(room_id), rooms._event_id(event_id)
    rooms._require_positive_int(expected_revision, "expected_revision")
    with service._policy_lock, rooms._transaction(service.db_path, immediate=True) as conn:
        rooms.room_safety._raise_if_quarantined(conn, room_id)
        row = rooms._room_row(conn, rooms._SELECT_ROOM_WITH_BYTES, (room_id,), room_id)
        rooms._require_authority(row, rooms.local_authority_gateway_id(), int(row["authority_epoch"]), "stale hosted room authority")
        if row["disbanded_at"] is not None:
            raise rooms.RoomNotFoundError("hosted room not found")
        members = json.loads(row["members_json"])
        normalized = normalize_policy(policy, members)
        payload = rooms._payload_json({"policy": normalized, "expected_revision": expected_revision})
        existing = rooms._load_event(conn, room_id, event_id)
        if existing is not None:
            if existing["kind"] != "room.policy_changed" or existing["payload_json"] != payload:
                raise rooms.EventConflictError("event_id already exists with different immutable content")
            return {**rooms._room_from_row(row, idempotent=True), "event": rooms._event_from_row(existing, idempotent=True)}
        if int(row["revision"]) != expected_revision:
            raise rooms.RoomConflictError("room revision changed; reload before editing policy")
        if any(m.get("target", {}).get("kind") == "peer" for m in members):
            raise rooms.RoomConflictError("peer responder policy replication is unsupported")
        if table_exists(conn, "hosted_room_driver_tasks") and conn.execute(
            "SELECT 1 FROM hosted_room_driver_tasks WHERE room_id=? AND status IN "
            "('queued','running','indeterminate','deferred','stopping') LIMIT 1", (room_id,)).fetchone():
            raise rooms.RoomConflictError("Stop outstanding room work before changing policy")
        now = rooms._now(None)
        seq = int(row["next_seq"])
        size = rooms._insert_event(conn, row, room_id, seq, event_id, "room.policy_changed",
            rooms._system_actor_json("room-control"), int(row["authority_epoch"]), payload, now)
        conn.execute("UPDATE hosted_rooms SET responder_policy_json=?, revision=revision+1, next_seq=next_seq+1, "
                     "event_bytes=event_bytes+?, updated_at=? WHERE room_id=?",
                     (rooms._payload_json(normalized), size, now, room_id))
        updated = conn.execute(rooms._SELECT_ROOM, (room_id,)).fetchone()
        result = {**rooms._room_from_row(updated), "event": rooms._event_from_row(rooms._load_event(conn, room_id, event_id))}
    service.wakeup()
    return result

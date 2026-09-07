"""Revision-fenced membership mutations on the existing hosted-room store."""
from dataclasses import asdict
import json

from gateway import hosted_rooms as rooms
from gateway.hosted_rooms_common import table_exists


class MembershipBusyError(rooms.RoomConflictError):
    reason = "room_membership_busy"


class MembershipUnsupportedError(rooms.RoomConflictError):
    reason = "room_membership_peer_unsupported"


def require_active_member(conn, room_id, payload, *, error):
    """Fence stale policy admissions after an identity was retired.

    Legacy generic driver tasks need not have a Discussion member. Retired
    identities, however, are never allowed to start work under either spelling.
    """
    row = conn.execute("SELECT retired_members_json FROM hosted_rooms WHERE room_id=?", (room_id,)).fetchone()
    retired = json.loads(row[0]) if row else []
    member_id = payload.get("target_member_id")
    if any(m["member_id"] == member_id or (member_id is None and m["profile"] == payload.get("target_profile"))
           for m in retired):
        raise error("removed room member cannot start new work")


def update_members(db_path, *, room_id, event_id, expected_revision, members, local_profiles, authority_gateway_id):
    from gateway.hosted_room_discussion import validate_roster

    room_id, event_id = rooms._room_id(room_id), rooms._event_id(event_id)
    rooms._require_positive_int(expected_revision, "expected_revision")
    normalized = [asdict(m) for m in validate_roster(members, local_profiles=local_profiles)]
    # Match create_room's stable optional-field serialization.
    for member in normalized:
        if not member["display_name"]:
            member.pop("display_name")
    _, members_json = rooms._validate_members(normalized)
    payload_json = rooms._payload_json({"members": normalized, "expected_revision": expected_revision})
    with rooms._transaction(db_path, immediate=True) as conn:
        rooms.room_safety._raise_if_quarantined(conn, room_id)
        row = rooms._room_row(conn, rooms._SELECT_ROOM_WITH_BYTES, (room_id,), room_id)
        if row["disbanded_at"] is not None:
            raise rooms.RoomNotFoundError("hosted room not found")
        rooms._require_authority(row, authority_gateway_id, int(row["authority_epoch"]), "stale hosted room authority")
        previous_event = rooms._load_event(conn, room_id, event_id)
        if previous_event is not None:
            if previous_event["kind"] != "room.members_changed" or previous_event["payload_json"] != payload_json:
                raise rooms.EventConflictError("event_id already exists with different membership")
            return {**rooms._room_from_row(row, idempotent=True), "event": rooms._event_from_row(previous_event, idempotent=True)}
        if row["revision"] != expected_revision:
            raise rooms.RoomConflictError("room revision changed; reload before editing membership")
        previous = json.loads(row["members_json"])
        retired = json.loads(row["retired_members_json"])
        if any(m.get("target", {}).get("kind") == "peer" for m in (*previous, *normalized)):
            raise MembershipUnsupportedError("peer membership changes require route revocation; not supported yet")
        by_id = {m["member_id"]: m for m in previous}
        retired_ids = {m["member_id"].casefold() for m in retired}
        for member in normalized:
            if member["member_id"].casefold() in retired_ids:
                raise rooms.RoomConflictError("retired member IDs cannot be reused")
            old = by_id.get(member["member_id"])
            if old is not None and old != member:
                raise rooms.RoomConflictError("member identity is immutable; use a new member ID")
        if table_exists(conn, "hosted_room_driver_tasks") and conn.execute(
            "SELECT 1 FROM hosted_room_driver_tasks WHERE room_id=? AND status IN "
            "('queued','running','indeterminate','deferred','stopping') LIMIT 1", (room_id,)).fetchone():
            raise MembershipBusyError("Stop outstanding room work before changing membership")
        active_ids = {m["member_id"] for m in normalized}
        policy = json.loads(row["responder_policy_json"])
        if policy.get("leader_member_id") is not None and policy["leader_member_id"] not in active_ids:
            raise rooms.RoomConflictError("Change responder policy before removing its leader")
        retired.extend(m for m in previous if m["member_id"] not in active_ids)
        _, retired_json = rooms._validate_members(retired)
        timestamp = rooms._now(None)
        seq = int(row["next_seq"])
        event_bytes = rooms._insert_event(conn, row, room_id, seq, event_id, "room.members_changed",
            rooms._system_actor_json("room-control"), int(row["authority_epoch"]), payload_json, timestamp)
        conn.execute("UPDATE hosted_rooms SET members_json=?, retired_members_json=?, revision=revision+1, "
                     "next_seq=next_seq+1, event_bytes=event_bytes+?, updated_at=? WHERE room_id=?",
                     (members_json, retired_json, event_bytes, timestamp, room_id))
        updated = conn.execute(rooms._SELECT_ROOM, (room_id,)).fetchone()
        return {**rooms._room_from_row(updated), "event": rooms._event_from_row(rooms._load_event(conn, room_id, event_id))}

"""Durable reply references resolve against original events, including tombstones."""
from gateway import hosted_rooms as rooms
from gateway.hosted_room_history import MESSAGE_KINDS


def resolve_reply_payload(conn, *, room_id, payload):
    if not isinstance(payload, dict) or "parent_event_id" not in payload:
        return payload
    parent_id = rooms._event_id(payload["parent_event_id"])
    parent = rooms._load_event(conn, room_id, parent_id)
    if parent is None or parent["kind"] not in MESSAGE_KINDS:
        raise rooms.HostedRoomError("reply parent is not a message in this room")
    source = rooms._event_from_row(parent)
    thread_id = source["payload"].get("thread_id", parent_id)
    if "thread_id" in payload and payload["thread_id"] != thread_id:
        raise rooms.HostedRoomError("reply thread differs from its parent")
    return {**payload, "parent_event_id": parent_id, "thread_id": thread_id}

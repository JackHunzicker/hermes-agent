"""Room participant capabilities gated by the caller's admitted session scope."""
import json

from gateway import hosted_room_participants as participants
from gateway import hosted_rooms
from tools.registry import registry


GROUP_ROOM_SCHEMA = {
    "name": "group_room",
    "description": "Read or search shared Group Chat history, inspect its roster, reply in your current thread, or upload a file for publication when your turn completes. Identity comes from your admitted turn.",
    "parameters": {"type": "object", "additionalProperties": False,
        "properties": {
            "operation": {"type": "string", "enum": ["members", "history", "search", "send", "upload"]},
            "event_id": {"type": "string", "description": "Stable key for retrying the same send."},
            "text": {"type": "string"}, "parent_event_id": {"type": "string"},
            "mention_member_ids": {"type": "array", "items": {"type": "string"}},
            "query": {"type": "string"}, "thread_id": {"type": "string"},
            "all_threads": {"type": "boolean"}, "after_seq": {"type": "integer", "minimum": 0},
            "limit": {"type": "integer", "minimum": 1, "maximum": 100},
            "snapshot_seq": {"type": "integer", "minimum": 0},
            "name": {"type": "string"}, "data_base64": {"type": "string"},
        }, "required": ["operation"]},
}

_OPERATIONS = {
    "members": (participants.participant_members, set(), set()),
    "send": (participants.participant_send, {"event_id", "text"}, {"parent_event_id", "mention_member_ids"}),
    "history": (participants.participant_history, set(), {"thread_id", "all_threads", "after_seq", "limit", "snapshot_seq"}),
    "search": (participants.participant_history, {"query"}, {"thread_id", "all_threads", "after_seq", "limit", "snapshot_seq"}),
    "upload": (participants.participant_upload, {"name", "data_base64"}, set()),
}


def handle_group_room(args, **kwargs):
    try:
        proof = participants.current_participant()
        if not isinstance(args, dict) or args.get("operation") not in _OPERATIONS:
            raise hosted_rooms.HostedRoomError("Unknown room participant operation")
        handler, required, optional = _OPERATIONS[args["operation"]]
        fields = set(args) - {"operation"}
        if not required <= fields <= required | optional:
            raise hosted_rooms.HostedRoomError("Invalid fields for room participant operation")
        return json.dumps({"ok": True, **handler(proof, args)}, ensure_ascii=False)
    except hosted_rooms.HostedRoomError as exc:
        return json.dumps({"ok": False, "error": str(exc), "reason": getattr(exc, "reason", "participant_operation_invalid")})


registry.register(name="group_room", toolset="bot_room", schema=GROUP_ROOM_SCHEMA,
                  handler=handle_group_room, emoji="💬")

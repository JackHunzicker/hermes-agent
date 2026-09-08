"""Reader negotiation for event semantics that older clients would misrepresent."""
from gateway import hosted_rooms as rooms

EVENT_FEATURES = {
    "message.edited": "message_mutations_v1",
    "message.deleted": "message_mutations_v1",
    "message.reaction": "message_mutations_v1",
    "message.participant": "participant_messages_v1",
    "thread.stop_requested": "scoped_stop_v1",
    "task.stop_requested": "scoped_stop_v1",
    "room.policy_changed": "responder_policy_v1",
}


class RoomReaderUpgradeRequired(rooms.HostedRoomError):
    reason = "room_reader_upgrade_required"

    def __init__(self, required_features):
        self.required_features = sorted(required_features)
        super().__init__("room reader requires features: " + ", ".join(self.required_features))

    @property
    def data(self):
        return {"reason": self.reason, "required_features": self.required_features, "action": "upgrade_client"}


def require_peer_writers(conn, room_id, kind):
    """Gate semantic appends under the same write lock as membership and routes."""
    import json
    from gateway.hosted_room_peer import GatewayRoomCatalog, HostedRoomPeerError

    feature = EVENT_FEATURES.get(kind)
    if feature is None:
        return
    row = conn.execute("SELECT members_json FROM hosted_rooms WHERE room_id=?", (room_id,)).fetchone()
    members = json.loads(row[0])
    routes = {r["member_id"]: r for r in conn.execute(
        "SELECT member_id,target_profile,catalog_json FROM hosted_room_links WHERE room_id=?", (room_id,))}
    for member in members:
        target = member.get("target", {})
        if target.get("kind") != "peer":
            continue
        route = routes.get(member["member_id"])
        try:
            catalog = GatewayRoomCatalog.from_mapping(json.loads(route["catalog_json"])) if route else None
        except (ValueError, TypeError, HostedRoomPeerError):
            catalog = None
        if (catalog is None or catalog.installation_id != target.get("installation_id")
                or catalog.execution_policy.target_profile != member["profile"]
                or route["target_profile"] != member["profile"]
                or feature not in (catalog.supported_features or ())):
            raise RoomReaderUpgradeRequired({feature})


def require_reader(conn, room_id, supported_features):
    if not isinstance(supported_features, (list, tuple)) or not all(isinstance(f, str) for f in supported_features):
        raise rooms.HostedRoomError("supported_features must be a list of strings")
    kinds = conn.execute("SELECT DISTINCT kind FROM hosted_room_events WHERE room_id=?", (room_id,))
    missing = {EVENT_FEATURES[row[0]] for row in kinds if row[0] in EVENT_FEATURES} - set(supported_features)
    if missing:
        raise RoomReaderUpgradeRequired(missing)

"""Shared history RPCs using server-owned viewer identity and the room authority."""
from .method_ctx import HandlerRegistry

_registry = HandlerRegistry()
METHODS = ("groups.history", "groups.history.search", "groups.message.edit",
           "groups.message.delete", "groups.message.react", "groups.read.get", "groups.read.mark")
FEATURES = ("message_history_projection_v1", "message_history_search_v1", "message_mutations_v1", "room_read_cursors_v1")


def _register_handler(name):
    @_registry.method(name)
    def handler(rid, params, _name=name):
        from gateway import hosted_room_history as history, hosted_rooms as rooms
        from gateway.hosted_room_capabilities import RoomReaderUpgradeRequired
        try:
            if _name.startswith("groups.read."):
                if _name.endswith("mark") and params.get("through_seq") is None:
                    raise rooms.HostedRoomError("through_seq is required")
                state = history.read_cursor(rooms.default_db_path(), room_id=params.get("room_id"),
                    reader={"kind": "user", "id": "desktop"}, thread_id=params.get("thread_id"),
                    through_seq=params.get("through_seq") if _name.endswith("mark") else None)
                return _ok(rid, state)
            if _name.startswith("groups.history"):
                if _name.endswith("search") and not params.get("query"):
                    raise rooms.HostedRoomError("query is required")
                page = history.history_page(rooms.default_db_path(), **{
                    k: v for k, v in params.items() if k in {
                        "room_id", "thread_id", "after_seq", "limit", "snapshot_seq", "include_disbanded", "query"}})
                return _ok(rid, page)
            service = get_hosted_room_service()
            if service is None:
                return _err(rid, 4123, "hosted room driver is unavailable")
            room = service._owned_room(params.get("room_id"))
            result = history.mutate_message(service.db_path, room_id=room["room_id"],
                event_id=history.mutation_event_id(params.get("event_id")),
                target_event_id=params.get("target_event_id"), operation=_name.rsplit(".", 1)[1],
                actor={"kind": "user", "id": "desktop"},
                authority_gateway_id=room["authority_gateway_id"], authority_epoch=room["authority_epoch"],
                **{k: params[k] for k in ("expected_revision", "text", "reaction", "present") if k in params})
            service.runtime.wakeup()
            return _ok(rid, result)
        except RoomReaderUpgradeRequired as exc:
            return _err(rid, 4160, str(exc), exc.data)
        except rooms.HostedRoomError as exc:
            return _err(rid, 4160, str(exc), {"reason": getattr(exc, "reason", "room_history_invalid")})
        except (TypeError, ValueError) as exc:
            return _err(rid, 4160, str(exc))


for _name in METHODS:
    _register_handler(_name)


def register(server):
    _registry.install(server)

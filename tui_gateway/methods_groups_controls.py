"""Room controls whose explicit scope must survive client/server version skew."""
from tui_gateway.method_ctx import HandlerRegistry

_registry = HandlerRegistry()
METHODS = ("groups.stop_scope", "groups.input.respond")
FEATURES = ("scoped_stop_v1", "scoped_input_v1", "scoped_approval_v1")


@_registry.method("groups.stop_scope")
def stop_scope(rid, params):
    from tui_gateway.methods_groups import get_hosted_room_service
    service = get_hosted_room_service()
    if service is None:
        return _err(rid, 4115, "hosted room driver is unavailable")
    try:
        result = service.stop_scope(str(params.get("room_id") or ""),
            cancel_id=params.get("cancel_id"), scope=params.get("scope"))
        return _ok(rid, result)
    except (ValueError, RuntimeError) as exc:
        return _err(rid, 5120, str(exc), {"reason": str(exc)})


def register(server):
    _registry.install(server)


@_registry.method("groups.input.respond")
def respond_input(rid, params):
    from tui_gateway.methods_groups import get_hosted_room_service
    service = get_hosted_room_service()
    if service is None:
        return _err(rid, 4115, "hosted room driver is unavailable")
    try:
        return _ok(rid, service.respond_room_input(params))
    except (ValueError, RuntimeError) as exc:
        return _err(rid, 5121, str(exc), {"reason": str(exc)})

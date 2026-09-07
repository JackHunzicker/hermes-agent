"""Opaque binding titles remain fenced before and after canonical persistence."""
from contextlib import nullcontext

import pytest

from hermes_state import SessionDB
import tui_gateway.server as server
from tests.tui_gateway.test_hosted_room_binding_persistence import task
from tui_gateway.hosted_room_driver import _task_session_title


@pytest.mark.parametrize("persisted", [False, True])
def test_direct_prompt_cannot_bypass_opaque_binding(tmp_path, monkeypatch, persisted):
    db = SessionDB(tmp_path / "sessions.db")
    title = _task_session_title(task("r00000000-0000-4000-8000-000000000001"))
    live = {"id": "live-id", "session_key": "stored-id", "source": "bot_room",
            "profile_home": None, "pending_hidden": True}
    if persisted:
        db.create_session("stored-id", source="bot_room")
        db.set_session_title("stored-id", title)
    else:
        live["pending_title"] = title
    monkeypatch.setattr(server, "_sess_nowait", lambda _params, _rid: (live, None))
    monkeypatch.setattr(server, "_session_db", lambda _session: nullcontext(db))
    monkeypatch.setattr(server, "_ensure_active_session_slot", lambda *_: "normal admission reached")
    try:
        response = server._methods["prompt.submit"]("direct-request", {"session_id": "live-id", "text": "continue"})
        assert response["error"]["code"] == 4122
        assert "managed by its gateway" in response["error"]["message"]
    finally:
        db.close()

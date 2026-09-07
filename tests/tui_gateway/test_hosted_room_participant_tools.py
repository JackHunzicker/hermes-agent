"""Real registry calls use the admitted room attempt, never model-supplied identity."""
import json
import time

import pytest

from gateway import hosted_room_driver as driver, hosted_rooms as rooms
from model_tools import get_tool_definitions, handle_function_call
from tests.tui_gateway.hosted_room_service_fixtures import _server
from tui_gateway.hosted_room_service import HostedRoomService
from tui_gateway.hosted_room_driver import HostedRoomBinding
from tui_gateway import server


@pytest.fixture
def participant(tmp_path, monkeypatch, request):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    service = HostedRoomService(_server(), db_path=tmp_path / "state.db")
    service.local_profiles = lambda: ("default", "ops")
    room = service.create_room(room_id="room-tools", name="Tools", members=[
        {"member_id": p, "profile": p, "handle": p} for p in ("default", "ops")])
    if getattr(request, "param", None) == "event_driven":
        from gateway.hosted_room_responder_policy import DEFAULT_POLICY, update_policy
        update_policy(service, room_id=room["room_id"], event_id="policy", expected_revision=room["revision"],
            policy={**DEFAULT_POLICY, "mode": "event_driven"})
    source = service.send(room_id=room["room_id"], event_id="source",
                          payload={"text": "@ops inspect", "thread_id": "thread-tools"})
    task = driver.list_tasks(service.db_path, room_id=room["room_id"])[0]
    lease = driver.acquire_lease(service.db_path, room_id=room["room_id"],
        gateway_id=room["authority_gateway_id"], authority_epoch=room["authority_epoch"],
        process_generation="tool-test", ttl_seconds=120, clock=time.time)
    attempt = driver.start_task(service.db_path, task["identity"], lease,
        expected_cancel_generation=task["cancel_generation"], clock=time.time)
    scope = {"room_id": room["room_id"], "thread_id": task["identity"].thread_id,
        "turn_id": task["identity"].turn_id, "task_id": task["identity"].task_id,
        "member_id": "ops", "target_profile": "ops",
        "execution_generation": attempt.execution_generation,
        "home_install_id": room["authority_gateway_id"], "target_install_id": room["authority_gateway_id"],
        "authority_gateway_id": room["authority_gateway_id"], "authority_epoch": room["authority_epoch"]}
    session = {"source": "bot_room", "_hosted_room_task": scope, "_test_attempt": attempt}
    token = server._current_runtime_session_record.set(session)
    try:
        yield service, room, source, task, session
    finally:
        server._current_runtime_session_record.reset(token)


def call(**args):
    return json.loads(handle_function_call("group_room", args, task_id="not-authority"))


def test_default_room_delivers_accepted_participant_handoff(participant):
    service, room, _, task, session = participant
    sent = call(operation="send", event_id="handoff-proof", text="@default inspect the handoff")
    assert sent["ok"] is True, sent
    assert sent["event"]["payload"]["mention_member_ids"] == ["default"]
    driver.settle_task(service.db_path, session["_test_attempt"], settlement_id="sender-done",
                       status="settled", result={"text": "PASS"}, clock=time.time)
    binding = HostedRoomBinding(room["room_id"], room["authority_gateway_id"], room["authority_epoch"])
    service.prepare_room(binding)
    tasks = driver.list_tasks(service.db_path, room_id=room["room_id"], status="queued")
    print("Accepted participant event:", sent["event"]["event_id"], "queued recipients:", [t["payload"]["target_member_id"] for t in tasks])
    assert any(t["payload"]["target_member_id"] == "default" and "inspect the handoff" in t["payload"]["prompt"] for t in tasks), \
        "Accepted @default participant handoff was not delivered in the default room policy"


def test_participant_registry_send_is_attributed_idempotent_and_attempt_fenced(participant):
    service, room, source, task, session = participant
    definitions = get_tool_definitions(enabled_toolsets=["bot_room"], quiet_mode=True)
    assert any(t["function"]["name"] == "group_room" for t in definitions)
    assert not any(t["function"]["name"] == "group_room" for t in
                   get_tool_definitions(enabled_toolsets=["terminal"], quiet_mode=True))
    roster = call(operation="members")
    assert roster["ok"] is True, roster
    assert roster["self_member_id"] == "ops"
    assert roster["members"] == room["members"]
    args = dict(operation="send", event_id="tool-send", text="@default review these findings",
                parent_event_id=source["event_id"], mention_member_ids=["default"])
    sent = call(**args)
    assert sent["ok"] is True, sent
    event = sent["event"]
    assert event["actor"] == {"kind": "member", "id": "ops", "profile": "ops"}
    assert event["payload"]["task_id"] == task["identity"].task_id
    assert event["payload"]["thread_id"] == "thread-tools"
    assert event["payload"]["parent_event_id"] == source["event_id"]
    assert call(**args)["event"]["event_id"] == event["event_id"]
    assert call(**{**args, "text": "changed"})["ok"] is False
    before = service._events(room["room_id"])
    assert call(**args, actor={"kind": "member", "id": "default"})["ok"] is False
    session["_hosted_room_task"] = {**session["_hosted_room_task"], "execution_generation": 99}
    assert call(operation="members")["ok"] is False
    assert call(**{**args, "event_id": "stale"})["ok"] is False
    assert service._events(room["room_id"]) == before


def test_participant_tool_without_runtime_context_cannot_choose_a_room():
    token = server._current_runtime_session_record.set(None)
    try:
        result = call(operation="members", room_id="room-tools", member_id="ops")
        assert result.get("ok") is False
        assert result.get("reason") == "participant_scope_unavailable"
    finally:
        server._current_runtime_session_record.reset(token)


def test_room_session_selects_participant_tools_at_initialization(monkeypatch):
    monkeypatch.delenv("HERMES_DESKTOP", raising=False)
    monkeypatch.setenv("HERMES_TUI_TOOLSETS", "terminal")
    selected = server._load_enabled_toolsets("bot_room")
    assert "bot_room" in selected
    assert "bot_room" not in server._load_enabled_toolsets("desktop")
    assert any(t["function"]["name"] == "group_room" for t in
               get_tool_definitions(enabled_toolsets=selected, quiet_mode=True))


def test_participant_history_search_reads_current_projection_in_its_room(participant):
    from gateway.hosted_room_history import mutate_message
    service, room, source, _, _ = participant
    sent = call(operation="send", event_id="finding", text="original finding")["event"]
    mutate_message(service.db_path, room_id=room["room_id"], event_id="edit-finding",
        target_event_id=sent["event_id"], actor=sent["actor"], operation="edit",
        authority_gateway_id=room["authority_gateway_id"], authority_epoch=room["authority_epoch"],
        expected_revision=sent["seq"], text="corrected finding")
    page = call(operation="history")
    assert page["ok"] is True, page
    assert [m["event_id"] for m in page["messages"]] == [source["event_id"], sent["event_id"]]
    found = call(operation="search", query="corrected")
    assert [m["event_id"] for m in found["messages"]] == [sent["event_id"]]
    assert call(operation="search", query="original")["messages"] == []
    assert call(operation="history", room_id="other")["ok"] is False
    assert call(operation="history", all_threads="not-a-boolean")["ok"] is False


@pytest.mark.parametrize("participant", ["event_driven"], indirect=True)
def test_participant_reply_respects_the_same_pending_input_bound(participant):
    service, room, _, _, _ = participant
    for index in range(23):
        service.send(room_id=room["room_id"], event_id=f"busy-{index}",
            payload={"text": f"@ops queued {index}", "thread_id": "thread-tools"})
    before = service._events(room["room_id"])
    reply = call(operation="send", event_id="overflow", text="@default overflow")
    assert reply["ok"] is False, reply
    assert "queue is full" in reply["error"]
    assert service._events(room["room_id"]) == before

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
    session = {"source": "bot_room", "_hosted_room_task": scope, "_test_attempt": attempt, "_test_lease": lease}
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
    assert any(t["payload"]["target_member_id"] == "default" and "inspect the handoff" in t["payload"]["prompt"] for t in tasks), \
        "Accepted @default participant handoff was not delivered in the default room policy"
    driver.release_lease(service.db_path, session["_test_lease"], clock=time.time)
    from tests.tui_gateway.test_hosted_room_mutation_runtime import NoticeRPC, configure
    from tests.tui_gateway.hosted_room_service_fixtures import _wait_for
    from gateway.hosted_room_discussion import reconstruct_task_plan
    reopened = HostedRoomService(_server(), db_path=service.db_path)
    reopened.local_profiles = service.local_profiles
    frozen = tasks[0]
    events = reopened.policy_checkpoint.events_for_task(room_id=room["room_id"],
        source_event_seq=frozen["payload"]["source_event_seq"], input_context=frozen["payload"]["input_context"])
    rebuilt = reconstruct_task_plan(room, events, frozen, local_profiles=reopened.local_profiles())
    assert rebuilt.payload == frozen["payload"]
    rpc = NoticeRPC()
    configure(reopened, rpc)
    reopened.start()
    try:
        _wait_for(lambda: len(rpc.calls) == 1 and not reopened.status(room["room_id"])["working"], timeout=10)
    finally:
        assert reopened.stop(timeout=3)
    assert rpc.calls[0]["profile"] == "default"
    records = [json.loads(line) for line in rpc.calls[0]["prompt"].splitlines() if line.strip().startswith('{')]
    handoff = next(record for record in records if record["event_id"] == sent["event"]["event_id"])
    assert handoff["actor"] == sent["event"]["actor"]
    assert handoff["content"] == "@ops: @default inspect the handoff"
    for _ in range(3):
        reopened.prepare_room(binding)
    assert not driver.list_tasks(service.db_path, room_id=room["room_id"], status="queued")
    assert rooms.room_state(service.db_path, room_id=room["room_id"])["responder_policy"] == room["responder_policy"]


def test_legacy_tool_handoff_chain_retains_round_bound_and_pending_delivery(participant):
    from gateway.hosted_room_discussion import MAX_DISCUSSION_ROUNDS, MAX_DISCUSSION_MEMBERS
    service, room, _, task, session = participant
    binding = HostedRoomBinding(room["room_id"], room["authority_gateway_id"], room["authority_epoch"])
    admitted = []
    for index in range(MAX_DISCUSSION_ROUNDS * MAX_DISCUSSION_MEMBERS + 1):
        member = task["payload"]["target_member_id"]
        target = "default" if member == "ops" else "ops"
        args = dict(operation="send", event_id=f"handoff-{index}", text=f"handoff {index}", mention_member_ids=[target])
        sent = call(**args)
        assert sent["ok"] is True, sent
        assert call(**args)["event"]["event_id"] == sent["event"]["event_id"]
        driver.settle_task(service.db_path, session["_test_attempt"], settlement_id=f"done-{index}",
            status="settled", result={"text": "PASS"}, clock=time.time)
        admitted.append(task)
        service.prepare_room(binding)
        queued = driver.list_tasks(service.db_path, room_id=room["room_id"], status="queued")
        if not queued:
            break
        task = queued[0]
        assert task["payload"]["target_member_id"] == target
        assert f"handoff {index}" in task["payload"]["prompt"]
        attempt = driver.start_task(service.db_path, task["identity"], session["_test_lease"],
            expected_cancel_generation=task["cancel_generation"], clock=time.time)
        session["_test_attempt"] = attempt
        session["_hosted_room_task"] = {**session["_hosted_room_task"],
            "task_id": task["identity"].task_id, "turn_id": task["identity"].turn_id,
            "member_id": target, "target_profile": target, "execution_generation": attempt.execution_generation}
    else:
        pytest.fail("participant handoffs escaped the legacy round bound")
    events = service._events(room["room_id"])
    terminals = [event for event in events if event["kind"] == "turn.settled"]
    assert max(e["payload"]["round_index"] for e in terminals) < MAX_DISCUSSION_ROUNDS
    assert len({(e["payload"]["round_index"], e["payload"]["member_id"]) for e in terminals}) == len(admitted)
    assert any(e["kind"] == "room.activity" and e["payload"]["status"] == "bounded" for e in events)
    for _ in range(3):
        service.prepare_room(binding)
    assert not driver.list_tasks(service.db_path, room_id=room["room_id"], status="queued")
    service.send(room_id=room["room_id"], event_id="continue-bounded", payload={"text": f"@{target} continue", "thread_id": "thread-tools"})
    queued = driver.list_tasks(service.db_path, room_id=room["room_id"], status="queued")
    assert queued[0]["payload"]["target_member_id"] == target
    assert sent["event"]["seq"] in queued[0]["payload"]["input_context"]["event_seqs"]


def test_peer_policy_excludes_local_participant_schema(participant, monkeypatch):
    from gateway.hosted_room_execution_policy import execution_policy_mapping
    from tools import approval
    monkeypatch.setattr(approval, "_YOLO_MODE_FROZEN", False)
    policy = execution_policy_mapping(target_profile="ops", config={
        "platform_toolsets": {"api_server": ["terminal", "bot_room"]}, "approvals": {"mode": "manual"}})
    peer_names = {t["function"]["name"] for t in get_tool_definitions(
        enabled_toolsets=policy["enabled_toolsets"], quiet_mode=True)}
    assert "group_room" not in peer_names
    assert "share_group_file" in peer_names
    assert "group_room" in {t["function"]["name"] for t in get_tool_definitions(
        enabled_toolsets=server._load_enabled_toolsets("bot_room"), quiet_mode=True)}
    assert call(operation="members")["ok"] is True


def test_participant_registry_send_is_attributed_idempotent_and_attempt_fenced(participant, monkeypatch):
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
    # Old raw-log clients must negotiate this kind, even outside their requested page.
    monkeypatch.setattr(server, "get_hosted_room_service", lambda: service)
    legacy = server._methods["groups.log"](1, {"room_id": room["room_id"], "limit": 1})
    assert legacy["error"]["data"]["reason"] == "room_reader_upgrade_required"
    assert legacy["error"]["data"]["required_features"] == ["participant_messages_v1"]
    capable = server._methods["groups.log"](2, {"room_id": room["room_id"],
        "supported_features": ["participant_messages_v1"]})
    assert next(e for e in capable["result"]["events"] if e["event_id"] == event["event_id"]) == event
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

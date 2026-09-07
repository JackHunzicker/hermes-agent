"""Responder policy is persisted, revision fenced, and used by real admission."""
from types import SimpleNamespace
import pytest
from gateway import hosted_rooms, hosted_room_discussion as discussion
from tui_gateway import methods_groups
from tests.tui_gateway.test_hosted_room_membership import service_at, MEMBERS

POLICY = {"mode": "legacy_bounded", "default_responder": "leader", "leader_member_id": "ops",
          "max_turns_per_window": 4, "window_seconds": 60}


def rpc_for(service):
    server = SimpleNamespace(_methods={}, get_hosted_room_service=lambda: service,
        _ok=lambda rid, result: {"id": rid, "result": result},
        _err=lambda rid, code, message, data=None: {"id": rid, "error": {"code": code, "message": message, "data": data}})
    methods_groups.register(server)
    return server._methods


def test_registered_policy_update_replays_and_routes_after_restart(tmp_path):
    db = tmp_path / "state.db"
    service = service_at(db)
    room = service.create_room(room_id="room-1", name="Review", members=MEMBERS)
    params = {"room_id": "room-1", "event_id": "policy-1", "expected_revision": room["revision"], "policy": POLICY}
    methods = rpc_for(service)
    changed = methods["groups.policy.update"](1, params)["result"]["room"]
    assert changed["responder_policy"] == POLICY
    assert changed["revision"] > room["revision"]
    service = service_at(db)
    methods = rpc_for(service)
    replay = methods["groups.policy.update"](2, params)["result"]["room"]
    assert replay["idempotent"] is True
    assert "error" in methods["groups.policy.update"](3, {**params, "event_id": "stale"})
    assert "error" in methods["groups.policy.update"](4, {**params, "policy": {**POLICY, "leader_member_id": "review"}})
    service.send(room_id="room-1", event_id="u1", payload={"text": "Give an update", "thread_id": "t"})
    snapshot = service._policy_snapshot(hosted_rooms.room_state(db, room_id="room-1"))
    decision = discussion.plan_next_task(hosted_rooms.room_state(db, room_id="room-1"), snapshot.events,
        local_profiles=service.local_profiles(), initial_watermarks=snapshot.watermarks)
    assert decision.task.member.member_id == "ops"


@pytest.mark.parametrize("mention", ["missing", "retired"])
def test_explicit_unavailable_mention_does_not_dispatch_everyone(tmp_path, mention):
    service = service_at(tmp_path / "state.db")
    service.create_room(room_id="room-1", name="Review", members=MEMBERS)
    with pytest.raises(ValueError, match="unavailable.*" + mention):
        service.send(room_id="room-1", event_id="u1", payload={"text": "@" + mention + " report", "thread_id": "t"})
    assert not [e for e in service._events("room-1") if e["kind"] == "message.user"]


@pytest.mark.parametrize("reverse_roster", [False, True])
def test_existing_handle_wins_over_immutable_id_alias_after_reopen(tmp_path, reverse_roster):
    from gateway import hosted_room_driver as driver

    db = tmp_path / "state.db"
    roster = [{"member_id": "member-a", "profile": "default", "handle": "ops"},
              {"member_id": "ops", "profile": "ops", "handle": "reviewer"}]
    if reverse_roster:
        roster.reverse()
    service = service_at(db)
    service.create_room(room_id="alias-room", name="Aliases", members=roster)
    service = service_at(db)
    service.send(room_id="alias-room", event_id="request",
                 payload={"text": "@ops check this", "thread_id": "t"})
    tasks = driver.list_tasks(db, room_id="alias-room")
    assert [t["payload"]["target_member_id"] for t in tasks] == ["member-a"]
    members = discussion.validate_roster(roster, local_profiles=service.local_profiles())
    from gateway.hosted_room_responder_policy import validate_mentions
    assert validate_mentions("@member-a use an unambiguous ID", members) == ("member-a",)
    assert validate_mentions("@reviewer use the other handle", members) == ("ops",)

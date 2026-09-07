"""Real HTTP, signatures, passive SQLite retention and publisher consumption."""

import asyncio
import copy
import json

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway import hosted_room_driver as driver
from gateway import hosted_room_links as links
from gateway import hosted_room_peer as peer
from gateway import hosted_room_replicas as replicas
from gateway import hosted_room_work_records as records
from gateway import hosted_rooms as rooms
from tests.gateway.test_api_server_room_replicas import HOME, TARGET, KEY, MEMBERS, setup  # noqa: F401
from tui_gateway.hosted_room_peer_http import PeerRunsHTTPClient, PeerRunsHTTPError
from tui_gateway.hosted_room_replication import HostedRoomReplicationPublisher

TASK = driver.TaskIdentity("room", "task", "thread", "turn")


def seed(source):
    driver.admit_task(source, TASK, payload={"target_profile": "default", "target_member_id": "reviewer",
                      "prompt": "PRIVATE_PROMPT /private/workspace", "source_event_seq": 1}, clock=lambda: 100)


def capture(source):
    return records.capture(source, room_id="room", local_gateway_id=HOME)


async def invitation(http, **overrides):
    body = {"room_id": "room", "home_install_id": HOME, "authority_gateway_id": HOME,
            "authority_epoch": 1, "member_id": "reviewer", "replication": True, "work_records": True}
    body.update(overrides)
    response = await http.post("/v1/room-members/invitations", json=body, headers={"Authorization": f"Bearer {KEY}"})
    assert response.status == 201, await response.text()
    reply = await response.json()
    assert reply["work_records_version"] == records.VERSION
    return reply


async def history(client, token, source):
    return await asyncio.to_thread(client.replicate_page, grant=token, target_profile="default", room_id="room",
                                   room_name="Workshop", members=MEMBERS, page=rooms.read_events(source, room_id="room", include_disbanded=True))


async def deliver(client, token, record):
    return await asyncio.to_thread(client.replicate_work_records, grant=token, target_profile="default", record=record)


@pytest.mark.asyncio
async def test_explicit_records_are_passively_retained_and_summarized(setup):
    source, target, app = setup
    seed(source)
    async with TestClient(TestServer(app)) as http:
        grant = (await invitation(http))["grant"]
        client = PeerRunsHTTPClient(base_url=str(http.make_url("/")), api_key="", timeout_seconds=3)
        await history(client, grant, source)
        first = capture(source)
        assert (await deliver(client, grant, first))["revision"] == first["revision"]
        assert await deliver(client, grant, first) == await deliver(client, grant, first)
        summary = replicas.replica_state(target, room_id="room")["work_records"]
        assert summary["tasks"][0]["task_id"] == TASK.task_id
        assert summary["phases"] == {"queued": 1}
        assert summary["source_loss_safe"] is False
        assert "PRIVATE_PROMPT" not in json.dumps(summary)
        assert rooms.list_rooms(target) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("auth", ["history_only", "bearer", "missing"])
async def test_old_or_broad_auth_cannot_deliver_records(setup, auth):
    source, _, app = setup
    seed(source)
    async with TestClient(TestServer(app)) as http:
        token = (await invitation(http, work_records=False))["grant"]
        headers = {"history_only": {"Authorization": f"HermesRoom {token}"},
                   "bearer": {"Authorization": f"Bearer {KEY}"}, "missing": {}}[auth]
        response = await http.post("/v1/room-members/work-records", json={"record": capture(source)}, headers=headers)
        assert response.status == 401


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["digest", "revision", "prefix", "extra", "authority", "roster"])
async def test_revision_scope_prefix_and_privacy_schema_reject_conflicts(setup, mutation):
    source, target, app = setup
    seed(source)
    async with TestClient(TestServer(app)) as http:
        grant = (await invitation(http))["grant"]
        client = PeerRunsHTTPClient(base_url=str(http.make_url("/")), api_key="", timeout_seconds=3)
        await history(client, grant, source)
        first = capture(source)
        await deliver(client, grant, first)
        bad = copy.deepcopy(first)
        if mutation == "digest":
            bad["digest"] = "a" * 64
        elif mutation == "revision":
            bad["tasks"][0]["phase"] = "running"
        elif mutation == "prefix":
            bad["history"]["event_sha256"] = "a" * 64
        elif mutation == "extra":
            bad["tasks"][0]["prompt"] = "MUST_NOT_STORE"
        elif mutation == "authority":
            bad["authority"]["epoch"] = 2
        else:
            bad["roster_sha256"] = "a" * 64
        if mutation != "digest":
            bad["digest"] = records.digest({k: v for k, v in bad.items() if k not in {"revision", "digest"}})
        with pytest.raises(PeerRunsHTTPError):
            await deliver(client, grant, bad)
        assert replicas.replica_state(target, room_id="room")["work_records"]["digest"] == first["digest"]


@pytest.mark.asyncio
async def test_revocation_is_rechecked_after_http_auth_before_target_write(setup, monkeypatch):
    source, target, app = setup
    seed(source)
    async with TestClient(TestServer(app)) as http:
        grant = (await invitation(http))["grant"]
        client = PeerRunsHTTPClient(base_url=str(http.make_url("/")), api_key="", timeout_seconds=3)
        await history(client, grant, source)
        record = capture(source)
        claims = peer.decode_room_grant(peer.gateway_room_grant_secret(), grant, permission=records.PERMISSION)
        original = records.validate
        def revoke(value):
            checked = original(value)
            monkeypatch.setattr(records, "validate", original)
            rooms.revoke_room_grant_scope(target, claims=claims, expires_at=claims["status_expires_at"])
            return checked
        monkeypatch.setattr(records, "validate", revoke)
        with pytest.raises(PeerRunsHTTPError) as error:
            await deliver(client, grant, record)
        assert error.value.status_code == 401
        assert replicas.replica_state(target, room_id="room")["work_records"]["availability"] == "not_retained"


@pytest.mark.asyncio
async def test_canonical_disband_reclaims_records_and_rejects_late_delivery(setup):
    source, target, app = setup
    seed(source)
    async with TestClient(TestServer(app)) as http:
        grant = (await invitation(http))["grant"]
        client = PeerRunsHTTPClient(base_url=str(http.make_url("/")), api_key="", timeout_seconds=3)
        await history(client, grant, source)
        record = capture(source)
        await deliver(client, grant, record)
        rooms.disband_room(source, room_id="room", expected_gateway_id=HOME, expected_epoch=1)
        await history(client, grant, source)
        with pytest.raises(PeerRunsHTTPError):
            await deliver(client, grant, record)
        state = replicas.replica_state(target, room_id="room")
        assert state["work_records"]["availability"] == "not_retained"
        assert state["disbanded_at"] is not None


@pytest.mark.asyncio
async def test_publisher_loss_restart_and_task_only_change_use_same_pending_record(setup, monkeypatch):
    source, target, app = setup
    seed(source)
    received = []
    @web.middleware
    async def lose_ack(request, handler):
        response = await handler(request)
        if request.path.endswith("/work-records") and response.status == 200:
            with rooms._transaction(target) as conn:
                received.append(json.loads(conn.execute(f"SELECT record_json FROM {records.TARGET_TABLE} WHERE room_id='room'").fetchone()[0]))
            if len(received) == 1:
                request.transport.close()
        return response
    app.middlewares.append(lose_ack)
    async with TestClient(TestServer(app)) as http:
        reply = await invitation(http)
        links.save_room_link(source, links.make_stored_link(
            room_id="room", member_id="reviewer", target_url=str(http.make_url("/")), target_profile="default",
            grant=reply["grant"], catalog=peer.GatewayRoomCatalog.from_mapping(reply["catalog"]),
            cancellation_scope_id="cancel", trace_id="trace"))
        with monkeypatch.context() as scope:
            scope.setattr(rooms, "local_authority_gateway_id", lambda: HOME)
            first = HostedRoomReplicationPublisher(source)
        key = ("room", "reviewer")
        await asyncio.to_thread(first._publish_one, key)  # history
        await asyncio.to_thread(first._publish_one, key)  # records: accepted, reply lost
        assert first.status()["work_records"][0]["status"] == "unavailable"
        held = driver.acquire_lease(source, room_id="room", gateway_id=HOME, authority_epoch=1,
                                    process_generation="process", ttl_seconds=30, clock=lambda: 100)
        driver.start_task(source, TASK, held, expected_cancel_generation=0, clock=lambda: 100)
        with monkeypatch.context() as scope:
            scope.setattr(rooms, "local_authority_gateway_id", lambda: HOME)
            second = HostedRoomReplicationPublisher(source)
        await asyncio.to_thread(second._publish_one, key)
        assert received[0] == received[1]
        await asyncio.to_thread(second._publish_one, key)
        assert received[2]["revision"] > received[1]["revision"]
        assert received[2]["history"] == received[1]["history"]
        assert replicas.replica_state(target, room_id="room")["work_records"]["phases"] == {"running": 1}
        await asyncio.to_thread(second._publish_one, key)
        assert len(received) == 3
        assert second.status()["source_loss_safe"] is False

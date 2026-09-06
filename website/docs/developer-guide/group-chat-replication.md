---
title: Participant History Copies
description: Opt-in passive Group Chat history delivery and its recovery limits.
---

# Participant History Copies

A hosted Group Chat has one authority gateway. This opt-in layer copies its ordered history to participating gateways without using Desktop or invoking a Bot. It is a prerequisite for surviving authority-host loss, not automatic takeover.

## Enable Through The Existing API

This initial implementation is API-first. Existing Group Chats and invitations keep their current permissions unless explicitly opted in.

1. Read `groups.capabilities` on the target gateway. Look for `authenticated_replication` before requesting the new option.
2. Add the exact boolean `replication: true` to an ordinary target-side `groups.peer.invite` request. Keep the existing room, authority, epoch, member and profile coordinates. The HTTP equivalent is `POST /v1/room-members/invitations`.
3. Register the resulting scoped grant and catalog on the home with the existing `groups.peer.register` flow. Do not retain a broad API key in that route.
4. The running hosted-room service discovers the durable route and copies bounded pages. Inspect the home `groups.state` response's `driver_status.replication` and the participant's `groups.replica_state` for coverage.

Grant values are secrets. Never put them in examples, logs, screenshots or issue reports. Ordinary invitations do not gain replica-write access, and replication does not enable execution or change a Bot's approval policy. Grant refresh retains the original permissions and authorization horizon.

## Delivery And Recovery

- The existing RoomLink client sends UTF-8 JSON over verified HTTPS, with plain HTTP allowed only on loopback. Credentials are not forwarded to arbitrary redirects.
- The recipient verifies its target-issued grant and exact group/member/authority scope. Reservation and revocation are checked again in the same SQLite transaction as the history write.
- Two bounded background workers copy pages independently of Bot turns. Profiles sharing one participant gateway use shared target coverage and an OS file lock, so their deliveries cannot overtake an unresolved page.
- The pending page coordinates are durable before transmission. A lost response or publisher restart retries that page before newer history. Matching duplicates do not create new events; gaps and conflicting history are not silently discarded.
- The original membership remains fixed. Committed rename events can update the copied name; a terminal disband cannot be followed by more events.
- An unavailable participant backs off without holding the Bot driver's locks. Optional publisher failures are reported separately from ordinary Group Chat execution.

Coordination files under `room_replication_locks` contain no credentials. Do not unlink them while services can be running: replacing a locked file can create two independent lock owners. They are local process coordination, not a distributed authority lease.

## What A Copy Does Not Prove

`source_loss_safe` deliberately remains false. Acknowledged replica coverage describes copied history, not a quorum-committed room or proof that every accepted source write survived.

The source's unreplicated tail can be lost if it disappears. Accepted execution receipts, pending external effects, cancellation, permission revocation and the policy cursor need a complete recovery contract before any successor can run work. Unknown outcomes must not become automatic retries.

Files are not mirrored by this layer. A copied attachment reference does not prove that its bytes are available on another gateway. Likewise, revoking a route before its final disband page is delivered can leave an explicitly incomplete passive copy; no synthetic canonical tombstone is invented.

Neither `groups.promote` nor `groups.demote` is enabled here. A timeout, an operator confirmation flag or a higher epoch number is not proof that the old coordinator can no longer commit. Automatic continuation requires a separately verified, globally exclusive authority decision.

## Focused Validation

Use the canonical `scripts/run_tests.sh` runner with the repository's installed test environment. The receiver and publisher tests use disposable SQLite stores, real signatures and reservations, real loopback HTTP, dropped acknowledgments and separate publisher processes. They do not use production credentials or provider calls.

Relevant owners are `test_hosted_room_replica_ingress.py`, `test_api_server_room_replicas.py`, `test_hosted_room_replication.py` and `test_hosted_room_replication_http.py`, plus the existing room, grant, RPC and peer-HTTP regressions. Field acceptance across independently hosted gateway processes remains a separate gate from these tests.

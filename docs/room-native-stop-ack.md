# Native Stop acknowledgement at the room boundary

`CodexAppServerSession` returns `terminal_acknowledged`; `agent/codex_runtime.py`
exports it as `native_terminal_acknowledged`, alongside `codex_thread_id` and
`codex_turn_id`. A returned `interrupted` flag is not native terminal proof.

## Contract

- The production TUI finalizer and its exception path retain native evidence in
  the hosted callback. Explicit false produces an `indeterminate` receipt, not
  `cancelled`. The local session RPC distinguishes delivery of an interrupt
  request (`stopping`, false) from the later native terminal acknowledgement.
- The existing SQLite room task row owns uncertainty. A running task becomes
  `indeterminate`; an already stopping task remains `stopping`. `result_json`
  retains false and the available native identifiers across runtime restart.
  No new ledger or schema migration is introduced.
- Callback coordinates, task execution/cancel generations, and the current
  driver lease fence uncertainty writes. Negative evidence cannot be replaced
  by idle/absent-session inference, a proof-less history row, automatic deferral,
  or explicit Retry. The latter is deliberately unsupported until an exact
  positive native terminal receipt is available.
- Existing reconciliation may consume a later exact true acknowledgement.
  A confirmed Stop completes cancellation; an independently interrupted target
  turn is a failure. Native true and legacy nonnative cancellation retain their
  established behavior. A conflicting callback cannot settle another attempt.
- Target API Runs preserve native proof instead of projecting false as cancelled
  or completed. RoomLink HTTP status/history retain those fields. Recovery of a
  known native-uncertain peer attempt is receipt-only, never admission replay.

## Qualification and limits

Regression coverage uses production callback/finalizer paths, real SQLite room
transitions and reopen, runtime recovery, local interrupt handlers, injected
native results, and local HTTP fixtures. These tests do not establish live-vendor
or physical-client acceptance.

Run the focused regressions from the repository root with its development environment:

```sh
scripts/run_tests.sh -j 4 --file-retries 0 \
  tests/tui_gateway/test_hosted_room_native_stop_ack.py \
  tests/gateway/test_api_server_native_stop_ack.py
```

The native terminal evidence producer is supplied by the native Codex continuity
and control changes in NousResearch/hermes-agent#105502. This room-side consumer
must be integrated with that producer before claiming end-to-end native Stop
acknowledgement.

A missing native acknowledgement remains unresolved; this patch does not invent
proof by killing a local process or attempting a vendor lookup. Old peers which
already discarded the field cannot reconstruct it retroactively. Native vendor
sessions, physical Desktop/Android clients, other parity features, and production
configuration/account changes are outside this qualification.

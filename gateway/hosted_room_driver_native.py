"""Native acknowledgement evidence in the existing fenced room task row."""
import json
from collections.abc import Mapping

from gateway import hosted_room_driver as state


def require_native_ack(row, proof=None):
    """Use inside the task transaction: negative evidence cannot be erased by a probe."""
    previous = json.loads(row["result_json"] or "null")
    ack = proof.get("native_terminal_acknowledged") if isinstance(proof, Mapping) else None
    if ack is False or (isinstance(previous, Mapping)
                        and previous.get("native_terminal_acknowledged") is False and ack is not True):
        raise state.InvalidTaskTransitionError("native terminal acknowledgement remains unresolved")


def record_native_uncertainty(
    db_path, identity, lease, *, expected_execution_generation,
    expected_cancel_generation, result, clock,
):
    """Retain negative native proof without completing Stop or authorizing replay."""
    state._expected_generations(
        lease, identity, expected_execution_generation, expected_cancel_generation)
    if not isinstance(result, Mapping) or result.get("native_terminal_acknowledged") is not False:
        raise state.DriverValidationError("negative native acknowledgement is required")
    now = state._timestamp(clock)
    with state._transaction(db_path) as conn:
        state._require_active_lease(conn, lease, now=now)
        row = state._load_task(conn, identity)
        previous = json.loads(row["result_json"] or "null")
        merged = dict(previous) if isinstance(previous, Mapping) else {}
        merged.update({key: value for key, value in result.items() if value is not None and value != ""})
        sql = state._task_update(
            "status=CASE WHEN status='stopping' THEN 'stopping' ELSE 'indeterminate' END, "
            "result_json=?, indeterminate_at=COALESCE(indeterminate_at, ?), updated_at=?",
            "status IN ('running', 'stopping', 'indeterminate') AND " + state._GENERATION_FENCE)
        state.fenced_update(conn, sql, (state._canonical_json(merged), now, now,
            identity.room_id, identity.task_id, expected_execution_generation, expected_cancel_generation),
            state.StaleTaskError("native acknowledgement evidence belongs to a stale task attempt"))
        return state._task_from_row(state._load_task(conn, identity))

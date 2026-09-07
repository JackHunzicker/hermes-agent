"""Resolve an existing clarification waiter under its server-owned room scope."""
from collections.abc import Mapping


def respond_exact(server, *, session_id, proof, request):
    session = server._sessions.get(session_id)
    if session is None:
        raise ValueError("input_session_expired")
    with session["history_lock"]:
        marker = session.get("_hosted_room_task")
        if not session.get("running") or not isinstance(marker, Mapping) or any(
                marker.get(key) != value for key, value in proof.items()):
            raise ValueError("input_task_attempt_changed")
        with server._prompt_lock:
            request_id = request["request_id"]
            entry = server._pending.get(request_id)
            kind, _ = server._pending_prompt_payloads.get(request_id, (None, None))
            if not entry or entry[0] != session_id or kind != "clarify.request" or entry[1].is_set():
                raise ValueError("input_request_expired")
            question_id = request.get("question_id")
            batch = server._batch_clarify.get(request_id)
            if batch is not None:
                if question_id not in batch["qids"]:
                    raise ValueError("input_question_not_found")
                batch["answers"][question_id] = request["answer"]
                remaining = [qid for qid in batch["qids"] if qid not in batch["answers"]]
            else:
                if question_id:
                    raise ValueError("input_question_not_found")
                server._answers[request_id] = request["answer"]
                remaining = []
            if not remaining:
                entry[1].set()
            return {"complete": not remaining}

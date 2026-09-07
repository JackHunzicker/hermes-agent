"""Exact hosted task controls composed with the existing room driver."""
from gateway import hosted_room_driver as state
from gateway import hosted_room_scoped_controls as controls


class HostedRoomScopedControlsMixin:
    def stop_scope(self, room_id, *, cancel_id, scope):
        scope = controls.validate_scope(scope)
        room = self._owned_room(room_id)
        with self._policy_lock:
            event = controls.existing_stop(self.db_path, room_id, cancel_id, scope)
            tasks = [task for task in state.list_tasks(self.db_path, room_id=room_id)
                     if task["identity"].thread_id == scope["thread_id"]]
            if scope["kind"] == "task":
                tasks = [task for task in tasks if task["identity"].task_id == scope["task_id"]]
                if not tasks:
                    raise ValueError("task_scope_not_found")
                if int(tasks[0]["execution_generation"]) != scope["execution_generation"]:
                    raise state.StaleTaskError("task_attempt_changed")
                if event is None and any(int(tasks[0][key]) != scope[key]
                                         for key in ("execution_generation", "cancel_generation")):
                    raise state.StaleTaskError("task_attempt_changed")
            elif not tasks:
                with self.policy_checkpoint._connect() as conn:
                    exists = conn.execute("""SELECT 1 FROM hosted_room_events WHERE room_id=?
                        AND kind='message.user' AND json_extract(payload_json, '$.thread_id')=? LIMIT 1""",
                        (room_id, scope["thread_id"])).fetchone()
                if exists is None:
                    raise ValueError("thread_scope_not_found")
            if event is None:
                event = controls.append_stop(self.db_path, room, cancel_id, scope)
            receipts = []
            for task in tasks:
                if int(task["payload"]["source_event_seq"]) >= int(event["seq"]):
                    continue
                if task["status"] not in state.TERMINAL_STATUSES:
                    expected = ({"expected_execution_generation": scope["execution_generation"],
                                 "expected_cancel_generation": scope["cancel_generation"]}
                                if scope["kind"] == "task" else {})
                    task = self.runtime.cancel(task["identity"], cancel_id=cancel_id, **expected)
                    if task["status"] == "cancelled":
                        self._discard_cancelled_task_artifacts(room_id, task)
                receipts.append(controls.task_receipt(task))
        self.runtime.wakeup()
        return {"room_id": room_id, "cancel_id": cancel_id, "scope": scope,
                "through_seq": int(event["seq"]), "tasks": receipts,
                "idempotent": bool(event.get("idempotent", False))}

    def _apply_scoped_stop_fences(self, room_id):
        import json
        for task in state.list_tasks(self.db_path, room_id=room_id):
            if task["status"] in state.TERMINAL_STATUSES:
                continue
            with self.policy_checkpoint._connect() as conn:
                stop = controls.pending_stop(conn, task)
            if stop is not None:
                self.runtime.cancel(task["identity"],
                    cancel_id=str(task.get("cancel_id") or json.loads(stop["payload_json"])["cancel_id"]))

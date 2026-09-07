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
                    cancel_id=str(task.get("cancel_id") or json.loads(stop["payload_json"])["cancel_id"]),
                    expected_execution_generation=int(task["execution_generation"]),
                    expected_cancel_generation=int(task["cancel_generation"]))

    def respond_room_input(self, request):
        import json
        import time
        from gateway import hosted_rooms

        fields = {"room_id", "member_id", "thread_id", "task_id", "execution_generation",
                  "request_id", "command_id", "answer"}
        if not isinstance(request, dict) or not fields <= set(request) or set(request) - fields - {"question_id"}:
            raise ValueError("invalid_input_request_fields")
        if type(request["execution_generation"]) is not int or request["execution_generation"] < 1:
            raise ValueError("invalid_execution_generation")
        if not isinstance(request["answer"], str) or len(request["answer"].encode("utf-8")) > 32768:
            raise ValueError("input_answer_exceeds_limit")
        for key in fields - {"execution_generation", "answer"}:
            hosted_rooms._validate_identifier(request[key], label=key, max_chars=128)
        room_id, member_id = request["room_id"], request["member_id"]
        room = self._owned_room(room_id)
        if not any(str(member.get("member_id") or member.get("profile")) == member_id for member in room["members"]):
            raise ValueError("input_member_unavailable")
        if self._member_is_peer(room_id, member_id):
            raise ValueError("scoped_input_unsupported_for_peer")
        encoded = json.dumps(request, sort_keys=True, separators=(",", ":"))
        with self._policy_lock, state._transaction(self.db_path) as conn:
            state._require_room_authority(conn, room_id, room["authority_gateway_id"], room["authority_epoch"])
            roster = json.loads(conn.execute("SELECT members_json FROM hosted_rooms WHERE room_id=?", (room_id,)).fetchone()[0])
            if not any(str(member.get("member_id") or member.get("profile")) == member_id for member in roster):
                raise ValueError("input_member_unavailable")
            conn.execute("""CREATE TABLE IF NOT EXISTS hosted_room_input_receipts (
                room_id TEXT NOT NULL, command_id TEXT NOT NULL, request_json TEXT NOT NULL,
                result_json TEXT NOT NULL, created_at REAL NOT NULL, PRIMARY KEY(room_id, command_id))""")
            conn.execute("DELETE FROM hosted_room_input_receipts WHERE created_at<?", (time.time() - 30 * 86400,))
            receipt = conn.execute("SELECT * FROM hosted_room_input_receipts WHERE room_id=? AND command_id=?",
                (room_id, request["command_id"])).fetchone()
            if receipt is not None:
                if receipt["request_json"] != encoded:
                    raise ValueError("input_command_conflict")
                return {**json.loads(receipt["result_json"]), "idempotent": True}
            if conn.execute("SELECT COUNT(*) FROM hosted_room_input_receipts WHERE room_id=?", (room_id,)).fetchone()[0] >= 4096:
                raise ValueError("room_input_receipt_limit")
            row = conn.execute("SELECT * FROM hosted_room_driver_tasks WHERE room_id=? AND task_id=?",
                (room_id, request["task_id"])).fetchone()
            if row is None:
                raise ValueError("input_task_not_found")
            task = controls.require_active_task(conn, **{key: request[key] for key in (
                "room_id", "member_id", "thread_id", "task_id", "execution_generation")})
            lease = self.runtime._leases.get(room_id)
            if lease is None or (row["run_gateway_id"], row["run_process_generation"], row["run_lease_generation"]) != (
                    lease.gateway_id, lease.process_generation, lease.lease_generation):
                raise ValueError("input_task_lease_changed")
            state.require_active_lease_in_transaction(conn, lease, now=time.time())
            action = self._pending_actions.get((room_id, member_id))
            if action is None or action.get("kind") != "input" or any(
                    action.get(key) != request[key] for key in ("task_id", "execution_generation", "request_id", "thread_id")):
                raise ValueError("input_request_expired")
            proof = {key: request[key] for key in ("room_id", "member_id", "thread_id", "task_id", "execution_generation")}
            applied = self.rpc.respond_input(session_id=action["session_id"], proof=proof, request=request)
            result = {**proof, "request_id": request["request_id"], "resolved": True,
                      "complete": applied["complete"], "idempotent": False}
            conn.execute("INSERT INTO hosted_room_input_receipts VALUES (?, ?, ?, ?, ?)",
                (room_id, request["command_id"], encoded, json.dumps(result, sort_keys=True), time.time()))
        if result["complete"]:
            with self._policy_lock:
                if self._pending_actions.get((room_id, member_id)) == action:
                    self._pending_actions.pop((room_id, member_id), None)
        self.runtime.wakeup()
        return result

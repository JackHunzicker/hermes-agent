"""A single expanded projection must obey the same response byte ceiling."""
import pytest

from gateway import hosted_rooms as rooms
from gateway.hosted_room_history import history_page


def test_oversized_single_message_projection_fails_before_returning_unbounded_page(tmp_path, monkeypatch):
    db = tmp_path / "state.db"
    rooms.create_room(db, room_id="r", name="Bounded", members=[], authority_gateway_id="g")
    rooms.append_event(db, room_id="r", event_id="u", kind="message.user", actor={"kind": "user", "id": "desktop"},
        payload={"text": "β" * 600, "thread_id": "t"}, authority_gateway_id="g", authority_epoch=1)
    # Projection includes original+current text and may also accumulate reactions.
    monkeypatch.setattr(rooms, "MAX_LOG_PAGE_BYTES", 2048)
    with pytest.raises(rooms.HostedRoomError, match="exceeds history page limit"):
        history_page(db, room_id="r")


def test_maximum_accepted_edit_remains_deliverable_with_its_notice_header(tmp_path):
    from gateway.hosted_room_history import mutate_message, policy_events
    from gateway.hosted_room_discussion import validate_user_payload, MAX_USER_TEXT_BYTES
    db = tmp_path / "state.db"
    rooms.create_room(db, room_id="r", name="Notice bound", members=[], authority_gateway_id="g")
    source = rooms.append_event(db, room_id="r", event_id="u", kind="message.user", actor={"kind": "user", "id": "desktop"},
        payload={"text": "original", "thread_id": "t"}, authority_gateway_id="g", authority_epoch=1)
    text = "β" * (MAX_USER_TEXT_BYTES // 2)
    edited = mutate_message(db, room_id="r", event_id="e", target_event_id="u", operation="edit",
        actor=source["actor"], text=text, expected_revision=source["seq"], authority_gateway_id="g", authority_epoch=1)
    notice = policy_events([edited["event"]])[0]
    normalized = validate_user_payload(notice["payload"])
    assert normalized["text"].startswith("[Message edited: u.")
    assert "truncated" in normalized["text"]
    assert notice["actor"] == source["actor"] and notice["event_id"] == "e"
    assert edited["event"]["payload"]["text"] == text

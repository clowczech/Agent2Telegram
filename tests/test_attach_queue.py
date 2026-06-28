"""Tests for attach-mode inbound turn queueing."""
import threading
import unittest

from agent2telegram.attach import AttachBridge
from agent2telegram.config import Config


class _FakeClient:
    def __init__(self):
        self.actions = []
        self.deleted = []
        self.sent = []

    def send_chat_action(self, chat_id, action="typing"):
        self.actions.append((chat_id, action))

    def send_message(self, chat_id, text, parse_mode=None):
        self.sent.append((chat_id, text))

    def delete_message(self, chat_id, message_id):
        self.deleted.append((chat_id, message_id))


class _FakeSession:
    def __init__(self):
        self.injected = []

    def inject(self, text):
        self.injected.append(text)

    def _capture(self):
        return ""


def _message(text, *, update_id=1, message_id=10, edited=False):
    key = "edited_message" if edited else "message"
    return {
        "update_id": update_id,
        key: {
            "chat": {"id": 7},
            "from": {"id": 7},
            "message_id": message_id,
            "text": text,
        },
    }


def _bridge():
    b = object.__new__(AttachBridge)
    b.cfg = Config(agent="generic", token="1:2", allowed_user_ids=[7], tmux_session="a2t")
    b.tg = _FakeClient()
    b._allowed = {7}
    b._owner_chat = 7
    b._turn_end = None
    b._session = _FakeSession()
    b._turn_active = threading.Event()
    b._init_inbound_queue()
    b._turn_from_tg = False
    b._transcript = None
    b._last_activity = 0.0
    b._status = {"mid": None, "shown": ""}
    b._last_typing = 0.0
    b._typing_count = 0
    b._turn_started = 0.0
    b._max_gap = 0.0
    b._last_pane_warning = 0.0
    b._status_path = None
    b._seen_tools = set()
    b._tui_seen = set()
    b._turn_text_sent = True
    b._pending_turn_end = False
    b._marker = "[tg]"
    return b


class AttachInboundQueueTests(unittest.TestCase):
    def test_message_during_active_turn_waits_until_finish_turn(self):
        b = _bridge()
        b._turn_active.set()

        b._handle(_message("second turn"))

        self.assertEqual(b._session.injected, [])
        self.assertEqual([item["text"] for item in b._inbound_queue], ["second turn"])

        b._turn_from_tg = False
        b._finish_turn()

        self.assertEqual(b._session.injected, ["second turn"])
        self.assertTrue(b._turn_active.is_set())
        self.assertEqual(list(b._inbound_queue), [])

    def test_queued_messages_are_injected_fifo_one_per_turn(self):
        b = _bridge()
        b._turn_active.set()

        b._handle(_message("first queued", update_id=1, message_id=11))
        b._handle(_message("second queued", update_id=2, message_id=12))

        self.assertEqual(b._session.injected, [])

        b._turn_from_tg = False
        b._finish_turn()
        self.assertEqual(b._session.injected, ["first queued"])
        self.assertEqual([item["text"] for item in b._inbound_queue], ["second queued"])

        b._finish_turn()
        self.assertEqual(b._session.injected, ["first queued", "second queued"])
        self.assertEqual(list(b._inbound_queue), [])

    def test_edit_updates_same_queued_message_before_injection(self):
        b = _bridge()
        b._turn_active.set()

        b._handle(_message("old text", update_id=1, message_id=11))
        b._handle(_message("new text", update_id=2, message_id=11, edited=True))

        self.assertEqual([item["text"] for item in b._inbound_queue], ["new text"])

        b._turn_from_tg = False
        b._finish_turn()

        self.assertEqual(b._session.injected, ["new text"])


if __name__ == "__main__":
    unittest.main()

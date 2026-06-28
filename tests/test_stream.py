"""Tests for stream-mode turn completion and durable Telegram sends."""
import tempfile
import threading
import unittest
from pathlib import Path

from agent2telegram.config import Config
from agent2telegram.stream import StreamBridge


class _FakeClient:
    def __init__(self, *, fail_times=0):
        self.actions = []
        self.deleted = []
        self.sent = []
        self.fail_times = fail_times
        self.on_send = None

    def send_chat_action(self, chat_id, action="typing"):
        self.actions.append((chat_id, action))

    def send_message(self, chat_id, text, parse_mode=None):
        if self.on_send:
            self.on_send()
        if self.fail_times:
            self.fail_times -= 1
            raise RuntimeError("telegram down")
        self.sent.append((chat_id, text))

    def delete_message(self, chat_id, message_id):
        self.deleted.append((chat_id, message_id))


def _bridge(tmpdir, client=None):
    b = object.__new__(StreamBridge)
    b.cfg = Config(agent="codex", token="1:2", allowed_user_ids=[7], mode="stream")
    b.tg = client or _FakeClient()
    b._allowed = {7}
    b._marker = "[tg]"
    b._origin = "Telegram:"
    b._origins = ("Telegram:", "[TG]")
    b._owner_chat = 7
    b._signal = None
    b._turn_end = None
    b._stop = threading.Event()
    b._sent_path = Path(tmpdir) / "stream_sent.txt"
    b._sent_keys = set()
    b._queue_path = Path(tmpdir) / "stream_outbound_queue.jsonl"
    b._pending_send = []
    b._turn_active = threading.Event()
    b._init_inbound_queue()
    b._turn_from_tg = True
    b._transcript = None
    b._last_activity = 0.0
    b._status = {"mid": None, "shown": ""}
    b._last_typing = 0.0
    b._typing_count = 0
    b._turn_started = 0.0
    b._max_gap = 0.0
    b._status_path = None
    b._seen_tools = set()
    b._tui_seen = set()
    b._turn_text_sent = False
    b._pending_turn_end = False
    b._thread_id = None
    b._proc_lock = threading.Lock()
    return b


def _agent_message(text, *, key="msg-1"):
    return {"type": "item.completed", "item": {"type": "agent_message", "id": key, "text": text}}


class StreamBridgeTests(unittest.TestCase):
    def test_finish_turn_does_not_require_attach_only_state(self):
        with tempfile.TemporaryDirectory() as d:
            b = _bridge(d)
            b._turn_active.set()

            b._finish_turn()

            self.assertFalse(b._turn_active.is_set())
            self.assertEqual(list(b._inbound_queue), [])

    def test_stream_send_marks_ledger_only_after_confirmed_send(self):
        with tempfile.TemporaryDirectory() as d:
            client = _FakeClient()
            b = _bridge(d, client)
            ledger_seen_during_send = []
            client.on_send = lambda: ledger_seen_during_send.append(
                ("msg-1" in b._sent_keys, b._sent_path.exists())
            )

            b._handle_stream_event(_agent_message("[tg] hello"))

            self.assertEqual(ledger_seen_during_send, [(False, False)])
            self.assertEqual(client.sent, [(7, "hello")])
            self.assertIn("msg-1", b._sent_keys)
            self.assertEqual(b._sent_path.read_text("utf-8"), "msg-1\n")
            self.assertEqual(b._pending_send, [])

    def test_failed_stream_send_stays_queued_for_redelivery(self):
        with tempfile.TemporaryDirectory() as d:
            client = _FakeClient(fail_times=1)
            b = _bridge(d, client)

            b._handle_stream_event(_agent_message("[tg] retry me", key="msg-retry"))

            self.assertEqual(client.sent, [])
            self.assertNotIn("msg-retry", b._sent_keys)
            self.assertFalse(b._sent_path.exists())
            self.assertEqual(b._pending_send, [{"text": "retry me", "key": "msg-retry"}])
            self.assertIn("msg-retry", b._queue_path.read_text("utf-8"))

            b._flush_pending()

            self.assertEqual(client.sent, [(7, "retry me")])
            self.assertIn("msg-retry", b._sent_keys)
            self.assertEqual(b._sent_path.read_text("utf-8"), "msg-retry\n")
            self.assertEqual(b._pending_send, [])


if __name__ == "__main__":
    unittest.main()

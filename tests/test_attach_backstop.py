"""Tests for attach-mode turn-end backstop delivery."""
import tempfile
import threading
import time
import unittest
from pathlib import Path

from agent2telegram import attach as attach_mod
from agent2telegram.attach import AttachBridge
from agent2telegram.config import Config


class _FakeClient:
    def __init__(self):
        self.sent = []
        self.deleted = []

    def send_message(self, chat_id, text, parse_mode=None):
        self.sent.append((chat_id, text))

    def delete_message(self, chat_id, message_id):
        self.deleted.append((chat_id, message_id))


def _bridge(tmpdir):
    b = object.__new__(AttachBridge)
    b.cfg = Config(agent="generic", token="1:2", allowed_user_ids=[7], tmux_session="a2t")
    b.tg = _FakeClient()
    b._owner_chat = 7
    b._marker = "[tg]"
    b._signal = Path(tmpdir) / "answer.txt"
    b._turn_end = None
    b._transcript = Path(tmpdir) / "transcript.jsonl"
    b._turn_active = threading.Event()
    b._turn_active.set()
    b._turn_from_tg = True
    b._turn_text_sent = False
    b._pending_turn_end = False
    b._turn_started = time.monotonic() - 1.0
    b._typing_count = 7
    b._max_gap = 0.0
    b._status = {"mid": None, "shown": ""}
    b._status_path = None
    b._seen_tools = set()
    b._stop = threading.Event()
    b._sent_keys = set()
    b._pending_send = []
    b._queue_path = None
    return b


class AttachBackstopTests(unittest.TestCase):
    def setUp(self):
        self._retry_delay = attach_mod.BACKSTOP_RETRY_DELAY
        attach_mod.BACKSTOP_RETRY_DELAY = 0.0

    def tearDown(self):
        attach_mod.BACKSTOP_RETRY_DELAY = self._retry_delay

    def test_retry_reads_transcript_after_initial_empty_result(self):
        with tempfile.TemporaryDirectory() as d:
            b = _bridge(d)
            seen = []
            answers = iter(["", "[tg] final from transcript"])

            def last_text():
                return next(answers)

            def drain():
                seen.append("drain")

            b._last_assistant_text = last_text
            b._drain_transcript = drain

            b._finish_turn()

            self.assertEqual(seen, ["drain"])
            self.assertEqual(b.tg.sent, [(7, "final from transcript")])
            self.assertTrue(b._turn_text_sent)
            self.assertFalse(b._turn_active.is_set())

    def test_fallback_sends_signal_file_when_transcript_stays_empty(self):
        with tempfile.TemporaryDirectory() as d:
            b = _bridge(d)
            b._signal.write_text("[tg] final from signal", "utf-8")
            b._last_assistant_text = lambda: ""
            b._drain_transcript = lambda: None

            b._finish_turn()

            self.assertEqual(b.tg.sent, [(7, "final from signal")])
            self.assertTrue(b._turn_text_sent)
            self.assertFalse(b._signal.exists())

    def test_empty_transcript_and_signal_logs_error_without_crashing(self):
        with tempfile.TemporaryDirectory() as d:
            b = _bridge(d)
            b._last_assistant_text = lambda: ""
            b._drain_transcript = lambda: None

            with self.assertLogs("agent2telegram.attach", level="ERROR") as logs:
                b._finish_turn()

            self.assertEqual(b.tg.sent, [])
            self.assertFalse(b._turn_active.is_set())
            self.assertTrue(any("Telegram turn ended without an answer" in line for line in logs.output))
            self.assertTrue(any("typing_count=7" in line for line in logs.output))

    def test_already_sent_turn_text_is_not_sent_again(self):
        with tempfile.TemporaryDirectory() as d:
            b = _bridge(d)
            b._turn_text_sent = True
            b.tg.sent.append((7, "already sent"))

            def unexpected_read():
                raise AssertionError("backstop should not read transcript after text was sent")

            b._last_assistant_text = unexpected_read

            b._finish_turn()

            self.assertEqual(b.tg.sent, [(7, "already sent")])
            self.assertFalse(b._turn_active.is_set())


if __name__ == "__main__":
    unittest.main()

"""Tests for attach-mode inbound handling."""
import tempfile
import threading
import unittest
from pathlib import Path

from agent2telegram import stt
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

    def get_file_path(self, file_id):
        return f"voice/{file_id}.ogg"

    def download(self, file_path, timeout=120):
        return b"VOICE-BYTES"


class _PollingClient(_FakeClient):
    def __init__(self, stop_event):
        super().__init__()
        self.stop_event = stop_event
        self.calls = []

    def _call(self, method, params, timeout=None):
        self.calls.append((method, dict(params), timeout))
        self.stop_event.set()
        return []


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


def _voice(*, update_id=1, message_id=10):
    return {
        "update_id": update_id,
        "message": {
            "chat": {"id": 7},
            "from": {"id": 7},
            "message_id": message_id,
            "voice": {"file_id": "v1"},
        },
    }


def _bridge(state_dir=None):
    b = object.__new__(AttachBridge)
    b.cfg = Config(agent="generic", token="1:2", allowed_user_ids=[7], tmux_session="a2t")
    b.tg = _FakeClient()
    b._allowed = {7}
    b._owner_chat = 7
    b._turn_end = None
    b._session = _FakeSession()
    b._sent_keys = set()
    b._queue_path = None
    b._pending_send = []
    b._turn_active = threading.Event()
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
    b._stop = threading.Event()
    if state_dir is not None:
        state = Path(state_dir)
        b._offset_file = state / "offset"
        b._processed_updates_file = state / "processed_updates"
        b._processed_update_ids, b._processed_update_order = b._read_processed_updates()
    return b


class AttachInboundTests(unittest.TestCase):
    def test_message_during_active_turn_injects_immediately(self):
        b = _bridge()
        b._turn_active.set()

        b._handle(_message("second turn"))

        self.assertEqual(b._session.injected, ["second turn"])
        self.assertTrue(b._turn_active.is_set())

    def test_voice_transcription_failure_notifies_owner(self):
        b = _bridge()
        b.cfg.elevenlabs_api_key = "fake-key"
        b._turn_text_sent = False
        orig = stt.transcribe

        def fail_transcribe(*_a, **_kw):
            raise stt.STTError("ElevenLabs request failed after 3 attempts: timed out")

        stt.transcribe = fail_transcribe
        try:
            b._handle(_voice())
        finally:
            stt.transcribe = orig

        self.assertEqual(b._session.injected, [])
        self.assertEqual(len(b.tg.sent), 1)
        self.assertEqual(b.tg.sent[0][0], 7)
        self.assertIn("Přepis hlasovky se nepovedl", b.tg.sent[0][1])
        self.assertIn("timed out", b.tg.sent[0][1])
        self.assertFalse(b._turn_text_sent)


class AttachOffsetPersistenceTests(unittest.TestCase):
    def test_inbound_loop_loads_persisted_offset(self):
        with tempfile.TemporaryDirectory() as d:
            b = _bridge(d)
            b._save_offset(42)
            b.tg = _PollingClient(b._stop)

            b._inbound_loop()

            self.assertEqual(b.tg.calls[0][0], "getUpdates")
            self.assertEqual(b.tg.calls[0][1]["offset"], 42)

    def test_processed_update_is_not_reinjected_after_restart(self):
        with tempfile.TemporaryDirectory() as d:
            b = _bridge(d)

            offset = b._handle_update_once(_message("first", update_id=10), b._load_offset())

            self.assertEqual(offset, 11)
            self.assertEqual(b._session.injected, ["first"])
            self.assertEqual(b._load_offset(), 11)
            b._offset_file.unlink()

            restarted = _bridge(d)
            offset = restarted._handle_update_once(_message("first", update_id=10), restarted._load_offset())

            self.assertEqual(offset, 11)
            self.assertEqual(restarted._session.injected, [])


if __name__ == "__main__":
    unittest.main()

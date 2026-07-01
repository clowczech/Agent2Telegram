"""Behavioural tests for the bridge — no network, no real agent."""
import tempfile
import unittest
from pathlib import Path

from agent2telegram import stt
from agent2telegram.bridge import Bridge, Task
from agent2telegram.config import Config


class _FakeClient:
    def __init__(self):
        self.sent = []
        self.actions = []
        self.files = {}            # file_id -> bytes

    def get_me(self):
        return {"username": "fakebot"}

    def send_chat_action(self, chat_id, action="typing"):
        self.actions.append((chat_id, action))

    def send_message(self, chat_id, text, parse_mode=None):
        self.sent.append((chat_id, text))

    # attachment support
    def get_file_path(self, file_id):
        return f"path/{file_id}.bin"

    def download(self, file_path, timeout=120):
        return b"FILE-BYTES"


class _PollingClient(_FakeClient):
    def __init__(self, batches):
        super().__init__()
        self.batches = list(batches)
        self.offsets = []
        self.stop_event = None

    def get_updates(self, offset, *, timeout=50):
        self.offsets.append(offset)
        if self.batches:
            return self.batches.pop(0)
        if self.stop_event is not None:
            self.stop_event.set()
        return []


class _FakeAdapter:
    def __init__(self):
        self.calls = []

    def run(self, prompt, *, chat_dir, is_continuation):
        chat_dir.mkdir(parents=True, exist_ok=True)
        self.calls.append({"prompt": prompt, "is_continuation": is_continuation})
        return f"echo: {prompt}"


class BridgeTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        cfg = Config(agent="claude-code", token="1:2", allowed_user_ids=[7], workdir=self.tmp.name)
        self.bridge = Bridge(cfg, client=_FakeClient())
        self.adapter = _FakeAdapter()
        self.bridge.adapter = self.adapter

    def tearDown(self):
        self.tmp.cleanup()

    def sent_texts(self):
        return [t for _, t in self.bridge.tg.sent]


class ContinuityTests(BridgeTestBase):
    def test_first_turn_is_not_continuation_second_is(self):
        self.bridge.process(100, Task(text="hello"))
        self.bridge.process(100, Task(text="again"))
        self.assertFalse(self.adapter.calls[0]["is_continuation"])
        self.assertTrue(self.adapter.calls[1]["is_continuation"])

    def test_reply_is_sent(self):
        self.bridge.process(100, Task(text="hello"))
        self.assertEqual(self.bridge.tg.sent[-1], (100, "echo: hello"))

    def test_reset_makes_next_turn_fresh(self):
        self.bridge.process(100, Task(text="hello"))
        self.bridge._reset_chat(100)
        self.bridge.process(100, Task(text="after reset"))
        self.assertFalse(self.adapter.calls[-1]["is_continuation"])

    def test_separate_chats_are_independent(self):
        self.bridge.process(1, Task(text="a"))
        self.bridge.process(2, Task(text="b"))
        self.assertFalse(self.adapter.calls[0]["is_continuation"])
        self.assertFalse(self.adapter.calls[1]["is_continuation"])


class AuthTests(BridgeTestBase):
    def test_unauthorized_user_is_refused(self):
        self.bridge._dispatch({"update_id": 1, "message": {
            "chat": {"id": 100}, "from": {"id": 999}, "text": "do something"}})
        self.assertEqual(self.adapter.calls, [])
        self.assertTrue(any("not authorized" in t.lower() for t in self.sent_texts()))

    def test_unauthorized_reset_is_refused_and_does_not_clear_chat(self):
        marker = self.bridge._marker(self.bridge.chat_dir(100))
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("started", "utf-8")

        self.bridge._dispatch({"update_id": 1, "message": {
            "chat": {"id": 100}, "from": {"id": 999}, "text": "/reset"}})

        self.assertTrue(marker.exists())
        self.assertTrue(any("not authorized" in t.lower() for t in self.sent_texts()))


class CommandTests(BridgeTestBase):
    def test_authorized_reset_clears_chat(self):
        marker = self.bridge._marker(self.bridge.chat_dir(100))
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("started", "utf-8")

        self.bridge._dispatch({"update_id": 1, "message": {
            "chat": {"id": 100}, "from": {"id": 7}, "text": "/reset"}})

        self.assertFalse(marker.exists())
        self.assertTrue(any("Fresh conversation" in t for t in self.sent_texts()))

    def test_status_command_with_bot_suffix_reports_auth_state(self):
        self.bridge._dispatch({"update_id": 1, "message": {
            "chat": {"id": 100}, "from": {"id": 7}, "text": "/status@agent2telegram_bot"}})

        self.assertEqual(self.adapter.calls, [])
        self.assertTrue(any("agent: claude-code" in t for t in self.sent_texts()))


class AttachmentTests(BridgeTestBase):
    def test_image_is_downloaded_and_attached(self):
        task = self.bridge._build_task(100, {"photo": [{"file_id": "small"}, {"file_id": "big"}]}, "look")
        self.assertIsNotNone(task.attachment)
        self.assertTrue(task.attachment.endswith("image.jpg"))
        with open(task.attachment, "rb") as f:
            self.assertEqual(f.read(), b"FILE-BYTES")
        self.assertEqual(task.text, "look")

    def test_document_is_downloaded(self):
        task = self.bridge._build_task(
            100, {"document": {"file_id": "d1", "file_name": "report.pdf"}}, "")
        self.assertTrue(task.attachment.endswith("report.pdf"))

    def test_attachment_path_is_added_to_prompt(self):
        self.bridge.process(100, Task(text="describe", attachment="/tmp/pic.jpg"))
        self.assertIn("/tmp/pic.jpg", self.adapter.calls[-1]["prompt"])
        self.assertIn("describe", self.adapter.calls[-1]["prompt"])

    def test_unsafe_filename_is_sanitized(self):
        task = self.bridge._build_task(
            100, {"document": {"file_id": "d", "file_name": "../../etc/passwd"}}, "")
        self.assertNotIn("/", task.attachment.split("attachments/")[-1])


class VoiceTests(BridgeTestBase):
    def test_voice_without_key_is_disabled(self):
        task = self.bridge._build_task(100, {"voice": {"file_id": "v1"}}, "")
        self.assertIsNone(task)
        self.assertTrue(any("aren't enabled" in t for t in self.sent_texts()))

    def test_voice_with_key_is_transcribed(self):
        self.bridge._stt_key = "fake-key"
        orig = stt.transcribe
        stt.transcribe = lambda audio, **kw: "hello from voice"
        try:
            task = self.bridge._build_task(100, {"voice": {"file_id": "v1"}}, "")
        finally:
            stt.transcribe = orig
        self.assertIsNotNone(task)
        self.assertEqual(task.text, "hello from voice")


class AdversarialDispatchTests(BridgeTestBase):
    def test_control_chars_and_shell_syntax_are_one_prompt_not_command(self):
        text = "hello\x00\n$(touch /tmp/pwned); rm -rf /\x85"

        self.bridge.process(100, Task(text=text))

        self.assertEqual(self.adapter.calls[-1]["prompt"], text)
        self.assertEqual(self.bridge.tg.sent[-1], (100, f"echo: {text}"))

    def test_run_skips_malformed_update_without_crashing(self):
        with tempfile.TemporaryDirectory() as d:
            client = _PollingClient([
                [{"message": {"chat": {"id": 100}, "from": {"id": 7}, "text": "missing id"}}],
            ])
            cfg = Config(agent="claude-code", token="1:2", allowed_user_ids=[7], workdir=d)
            bridge = Bridge(cfg, client=client)
            bridge.adapter = _FakeAdapter()
            bridge._offset_file = Path(d) / "offset"
            client.stop_event = bridge._stop

            bridge.run()

            self.assertEqual(client.offsets, [0, 0])
            self.assertEqual(bridge.adapter.calls, [])
            self.assertEqual(client.sent, [])

    def test_transient_network_outage_warns_errors_once_then_recovers(self):
        """Síťový/DNS výpadek getUpdates (i zabalený v TelegramError, jak ho reálně vyhazuje
        telegram.py po vyčerpání retmů) se loguje jako WARNING; ERROR jen JEDNOU při delším
        výpadku (≥10 chyb v řadě), aby monitoring nealertoval na sebe-zotavující se VPN-DNS
        blip; po obnově se loguje INFO a čítač se resetuje."""
        import logging as _logging
        import urllib.error as _uerr
        from unittest.mock import patch as _patch
        from agent2telegram.telegram import TelegramError as _TgErr

        class _FlakyClient(_FakeClient):
            def __init__(self, fail_times):
                super().__init__()
                self.fail_times = fail_times
                self.calls = 0
                self.stop_event = None

            def get_updates(self, offset, *, timeout=50):
                self.calls += 1
                if self.calls <= self.fail_times:
                    # přesně jak to vyhazuje telegram.py: TelegramError s __cause__ = síťová chyba
                    raise _TgErr("getUpdates: <urlopen error [Errno 8] nodename nor servname>") \
                        from _uerr.URLError("[Errno 8] nodename nor servname provided, or not known")
                if self.stop_event is not None:
                    self.stop_event.set()
                return []

        with tempfile.TemporaryDirectory() as d:
            client = _FlakyClient(fail_times=13)   # delší výpadek než threshold 10, pak obnova
            cfg = Config(agent="claude-code", token="1:2", allowed_user_ids=[7], workdir=d)
            bridge = Bridge(cfg, client=client)
            bridge.adapter = _FakeAdapter()
            bridge._offset_file = Path(d) / "offset"
            client.stop_event = bridge._stop
            with _patch.object(bridge._stop, "wait", lambda *a, **k: False), \
                 self.assertLogs("agent2telegram.bridge", level="INFO") as cm:
                bridge.run()
            gu = [r for r in cm.records if "getUpdates" in r.getMessage()]
            warns = [r for r in gu if r.levelno == _logging.WARNING]
            errors = [r for r in gu if r.levelno == _logging.ERROR]
            infos = [r for r in gu if r.levelno == _logging.INFO]
            self.assertGreaterEqual(len(warns), 9)          # chyby 1–9 = WARNING
            self.assertEqual(len(errors), 1)                # ERROR přesně jednou za výpadek
            self.assertGreaterEqual(len(infos), 1)          # obnova = INFO
            # síťová chyba se NIKDY neloguje starým "getUpdates failed" (= ne-síťová větev)
            self.assertFalse(any("getUpdates failed" in r.getMessage() for r in gu))


if __name__ == "__main__":
    unittest.main()

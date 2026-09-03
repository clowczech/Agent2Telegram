"""Reply routing: odpoved na zpravu jine session se doruci te session, ne aktualnimu cili."""
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from agent2telegram import origins


class OriginsStoreTests(unittest.TestCase):
    def test_record_and_lookup(self):
        with tempfile.TemporaryDirectory() as td:
            origins.record(Path(td), [11, 12], sid="abc", cwd="/x", label="Hlídač")
            self.assertEqual(origins.lookup(Path(td), 11)["sid"], "abc")
            self.assertEqual(origins.lookup(Path(td), 12)["label"], "Hlídač")
            self.assertIsNone(origins.lookup(Path(td), 99))

    def test_empty_sid_is_not_recorded(self):
        with tempfile.TemporaryDirectory() as td:
            origins.record(Path(td), [11], sid="", cwd="", label="")
            self.assertIsNone(origins.lookup(Path(td), 11))

    def test_old_record_expires(self):
        with tempfile.TemporaryDirectory() as td:
            origins.record(Path(td), [11], sid="abc", cwd="")
            with mock.patch.object(time, "time", return_value=time.time() + 31 * 86400):
                self.assertIsNone(origins.lookup(Path(td), 11))

    def test_prune_removes_old_files(self):
        with tempfile.TemporaryDirectory() as td:
            origins.record(Path(td), [11], sid="abc", cwd="")
            self.assertEqual(origins.prune(Path(td), max_age_days=0), 1)
            self.assertIsNone(origins.lookup(Path(td), 11))

    def test_lookup_survives_missing_dir(self):
        self.assertIsNone(origins.lookup(Path("/nonexistent-xyz"), 1))


class RoutingDecisionTests(unittest.TestCase):
    """_maybe_route_reply na urovni bridge — pres fake z test_attach_queue."""

    def _bridge(self):
        from tests.test_attach_queue import _bridge
        return _bridge()

    def _reply_msg(self, mid, text="udelej to"):
        return {"chat": {"id": 7}, "from": {"id": 7}, "message_id": 50,
                "text": text, "reply_to_message": {"message_id": mid, "text": "puvodni hlaska"}}

    def test_reply_to_foreign_origin_is_routed(self):
        b = self._bridge()
        from agent2telegram.config import _state_dir
        origins.record(_state_dir(b.cfg), [77], sid="cizi-sid", cwd="/tmp", label="Hlídač X")
        with mock.patch.object(b, "_current_target_sid", return_value="muj-sid"), \
             mock.patch.object(b, "_routed_reply_worker") as worker:
            routed = b._maybe_route_reply(self._reply_msg(77), "udelej to", 7)
        self.assertTrue(routed)
        # Volá se SYNCHRONNĚ (3. 9. 2026): durable záznam se smí smazat až po doručení.
        worker.assert_called_once()
        self.assertEqual(worker.call_args.args[0]["sid"], "cizi-sid")

    def test_reply_to_own_message_stays_local(self):
        b = self._bridge()
        from agent2telegram.config import _state_dir
        origins.record(_state_dir(b.cfg), [78], sid="muj-sid", cwd="/tmp", label="ja")
        with mock.patch.object(b, "_current_target_sid", return_value="muj-sid"):
            self.assertFalse(b._maybe_route_reply(self._reply_msg(78), "ok", 7))

    def test_reply_without_origin_stays_local(self):
        b = self._bridge()
        with mock.patch.object(b, "_current_target_sid", return_value="muj-sid"):
            self.assertFalse(b._maybe_route_reply(self._reply_msg(999), "ok", 7))

    def test_plain_message_is_never_routed(self):
        b = self._bridge()
        msg = {"chat": {"id": 7}, "from": {"id": 7}, "message_id": 51, "text": "ahoj"}
        self.assertFalse(b._maybe_route_reply(msg, "ahoj", 7))


class RoutedWorkerTests(unittest.TestCase):
    def test_worker_sends_ack_reply_and_records_follow_origin(self):
        from tests.test_attach_queue import _bridge
        b = _bridge()
        fake_rt = mock.Mock()
        fake_rt.sid = "novy-sid"
        fake_rt.cwd = "/tmp"
        fake_rt.send.return_value = "hotovo, smazano"
        with mock.patch("agent2telegram.switcher.ResumeTarget", return_value=fake_rt), \
             mock.patch.object(b, "_record_origin") as rec:
            b._routed_reply_worker({"sid": "cizi", "cwd": "/tmp", "label": "Hlídač X"},
                                   {"reply_to_message": {"message_id": 77, "text": "disk se plni"}},
                                   "promaz to", 7)
        texts = [t for _, t in b.tg.sent]
        self.assertTrue(any("Předávám" in t and "Hlídač X" in t for t in texts))
        self.assertTrue(any("hotovo, smazano" in t for t in texts))
        # prompt nese kontext, na co Jan odpovidal
        self.assertIn("disk se plni", fake_rt.send.call_args[0][0])
        rec.assert_called_once()
        self.assertEqual(rec.call_args.kwargs["sid"], "novy-sid")

    def test_worker_failure_tells_the_user(self):
        from tests.test_attach_queue import _bridge
        b = _bridge()
        fake_rt = mock.Mock()
        fake_rt.send.side_effect = RuntimeError("session gone")
        with mock.patch("agent2telegram.switcher.ResumeTarget", return_value=fake_rt):
            b._routed_reply_worker({"sid": "cizi", "cwd": "/tmp", "label": "Hlídač X"},
                                   {"reply_to_message": {"message_id": 77, "text": "x"}},
                                   "promaz to", 7)
        self.assertTrue(any("nepodařilo" in t for _, t in b.tg.sent))


if __name__ == "__main__":
    unittest.main()


class ChatScopedOriginsTests(unittest.TestCase):
    """message_id je unikatni jen v ramci chatu — klic proto musi mit i chat (Codex 3. 9.)."""

    def test_same_message_id_in_two_chats_does_not_collide(self):
        with tempfile.TemporaryDirectory() as td:
            origins.record(Path(td), [5], sid="a", cwd="/x", chat_id=111)
            origins.record(Path(td), [5], sid="b", cwd="/y", chat_id=222)
            self.assertEqual(origins.lookup(Path(td), 5, chat_id=111)["sid"], "a")
            self.assertEqual(origins.lookup(Path(td), 5, chat_id=222)["sid"], "b")

    def test_old_records_without_chat_still_resolve(self):
        with tempfile.TemporaryDirectory() as td:
            origins.record(Path(td), [7], sid="stary", cwd="/x")
            self.assertEqual(origins.lookup(Path(td), 7, chat_id=999)["sid"], "stary")

    def test_record_from_other_chat_is_not_used(self):
        with tempfile.TemporaryDirectory() as td:
            origins.record(Path(td), [9], sid="a", cwd="/x", chat_id=111)
            self.assertIsNone(origins.lookup(Path(td), 9, chat_id=222))

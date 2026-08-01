"""Voice-reply mode: the /voice toggle (persistent) and the core marker that tells the AGENT
to write speakable replies. Uses the same hand-built bridge as test_v2_durability."""
import tempfile
import unittest
from pathlib import Path

from agent2telegram.attach import VOICE_MODE_HINT
from tests.test_v2_durability import _bridge, _msg


def _voice_bridge(td, *, key="el-key", on=False, session=None):
    b = _bridge(td, session=session)
    b.cfg.elevenlabs_api_key = key
    b._voice_state_path = Path(td) / "voice_mode"
    b._voice_on = on
    return b


class ToggleTests(unittest.TestCase):
    def test_toggle_on_off_persists(self):
        with tempfile.TemporaryDirectory() as td:
            b = _voice_bridge(td, on=False)
            b._toggle_voice(7)
            self.assertTrue(b._voice_reply_on(), "first /voice should turn it on")
            self.assertEqual((Path(td) / "voice_mode").read_text().strip(), "on")

            # A fresh bridge over the same state dir must load it as ON (survives restart).
            b2 = _voice_bridge(td)
            self.assertTrue(b2._load_voice_state())

            b._toggle_voice(7)
            self.assertFalse(b._voice_reply_on(), "second /voice should turn it off")
            self.assertEqual((Path(td) / "voice_mode").read_text().strip(), "off")

    def test_toggle_refused_without_key(self):
        with tempfile.TemporaryDirectory() as td:
            b = _voice_bridge(td, key="", on=False)
            b._toggle_voice(7)
            self.assertFalse(b._voice_reply_on())
            self.assertTrue(any("key" in s.lower() for s in b.tg.sent),
                            "should tell the user a key is needed")

    def test_reply_on_requires_both_switch_and_key(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertFalse(_voice_bridge(td, key="el", on=False)._voice_reply_on())
            self.assertFalse(_voice_bridge(td, key="", on=True)._voice_reply_on())
            self.assertTrue(_voice_bridge(td, key="el", on=True)._voice_reply_on())


class MarkerInjectionTests(unittest.TestCase):
    def test_voice_on_injects_hint_ahead_of_message(self):
        with tempfile.TemporaryDirectory() as td:
            b = _voice_bridge(td, on=True)
            b._handle(_msg(1, "co je nového?"))
            self.assertTrue(b._session.injected, "message should reach the session")
            injected = b._session.injected[0]
            self.assertIn(VOICE_MODE_HINT, injected)
            self.assertIn("co je nového?", injected)

    def test_voice_off_does_not_inject_hint(self):
        with tempfile.TemporaryDirectory() as td:
            b = _voice_bridge(td, on=False)
            b._handle(_msg(1, "co je nového?"))
            self.assertTrue(b._session.injected)
            self.assertNotIn(VOICE_MODE_HINT, b._session.injected[0])


if __name__ == "__main__":
    unittest.main()

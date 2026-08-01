"""Unfulfilled-promise watchdog: detect a promised follow-up, remind the AGENT (not the user)
once if it never comes, and stay quiet when the promise is kept or a new turn starts."""
import tempfile
import time
import unittest
from pathlib import Path

from agent2telegram.attach import PROMISE_TIMEOUT
from tests.test_v2_durability import _bridge


def _pb(td):
    b = _bridge(td)
    b._pending_promise = None
    b._sent_path = Path(td) / "attach_sent.txt"
    b._use_durable_outbox = False          # observe immediate sends / go the simple path
    return b


class DetectTests(unittest.TestCase):
    def test_detects_czech_and_english_promises(self):
        with tempfile.TemporaryDirectory() as td:
            b = _pb(td)
            self.assertIsNotNone(b._detect_promise("Hotovo, posílám hned zbytek."))
            self.assertIsNotNone(b._detect_promise("Pokračování v další zprávě."))
            self.assertIsNotNone(b._detect_promise("Great — more in the next message."))

    def test_plain_reply_is_not_a_promise(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertIsNone(_pb(td)._detect_promise("Tady je celá odpověď, hotovo."))


class WatchdogTests(unittest.TestCase):
    def test_promise_then_silence_reminds_agent_once(self):
        with tempfile.TemporaryDirectory() as td:
            b = _pb(td)
            b._send_final("Mrknu na to a posílám hned.", key="p1")
            self.assertIsNotNone(b._pending_promise, "promise should be armed")

            # Nothing followed; force the deadline into the past and tick the watchdog.
            b._pending_promise["deadline"] = time.monotonic() - 1
            b._check_promise()
            reminders = [t for t in b._session.injected if "unfulfilled promise" in t]
            self.assertEqual(len(reminders), 1, "agent should be reminded exactly once")
            self.assertIsNone(b._pending_promise, "promise cleared after firing")

            # A second tick must NOT nag again.
            b._check_promise()
            self.assertEqual(sum("unfulfilled promise" in t for t in b._session.injected), 1)

    def test_kept_promise_does_not_remind(self):
        with tempfile.TemporaryDirectory() as td:
            b = _pb(td)
            b._send_final("Část jedna, posílám hned zbytek.", key="a")
            self.assertIsNotNone(b._pending_promise)
            b._send_final("Část dvě, tady je zbytek.", key="b")   # the follow-up arrived
            self.assertIsNone(b._pending_promise, "a follow-up reply fulfils the promise")
            b._check_promise()
            self.assertFalse(any("unfulfilled promise" in t for t in b._session.injected))

    def test_new_turn_cancels_the_promise(self):
        with tempfile.TemporaryDirectory() as td:
            b = _pb(td)
            b._send_final("Posílám hned.", key="c")
            self.assertIsNotNone(b._pending_promise)
            b._begin_turn()                     # user re-engaged
            self.assertIsNone(b._pending_promise)

    def test_before_deadline_no_reminder(self):
        with tempfile.TemporaryDirectory() as td:
            b = _pb(td)
            b._send_final("Ozvu se za chvíli.", key="d")
            b._check_promise()                  # deadline is ~120 s away
            self.assertFalse(any("unfulfilled promise" in t for t in b._session.injected))
            self.assertIsNotNone(b._pending_promise)


if __name__ == "__main__":
    unittest.main()

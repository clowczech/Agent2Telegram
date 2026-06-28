"""Tests for tmux session input handling."""
import unittest
from unittest.mock import patch

from agent2telegram import session
from agent2telegram.session import MAX_TMUX_INJECTION_CHARS, TmuxSession, sanitize_for_tmux


class SanitizeForTmuxTests(unittest.TestCase):
    def test_regular_text_is_unchanged(self):
        text = "Hello, world 123. Regular punctuation: []{};:,./?\n\tDone."
        self.assertEqual(sanitize_for_tmux(text), text)

    def test_control_characters_are_stripped_except_newline_and_tab(self):
        control_chars = "".join(chr(i) for i in [*range(0x20), 0x7f, *range(0x80, 0xa0)])
        self.assertEqual(sanitize_for_tmux(f"a{control_chars}b"), "a\t\nb")

    def test_text_is_truncated(self):
        text = "x" * (MAX_TMUX_INJECTION_CHARS + 1)
        self.assertEqual(sanitize_for_tmux(text), "x" * MAX_TMUX_INJECTION_CHARS)


class TmuxSessionSendKeysTests(unittest.TestCase):
    def test_send_keys_passes_sanitized_literal_argument_to_tmux(self):
        with patch.object(session, "_tmux") as tmux, patch.object(session.time, "sleep"):
            s = object.__new__(TmuxSession)
            s.name = "a2t-test"
            s._origin = "from tg: "
            s._send_keys("hello\x0bthere\x03\x1b[31m\x7fworld\x85!")

        calls = [call.args for call in tmux.call_args_list]
        literal_calls = [c for c in calls if c[:5] == ("send-keys", "-t", "a2t-test", "-l", "--")]
        self.assertEqual(len(literal_calls), 1)
        self.assertEqual(literal_calls[0][-1], "from tg: hellothere[31mworld!")


if __name__ == "__main__":
    unittest.main()

"""Tests for tmux session input handling."""
import subprocess
import unittest
from unittest.mock import patch

from agent2telegram import session
from agent2telegram.session import MAX_TMUX_INJECTION_CHARS, SessionError, TmuxSession, sanitize_for_tmux


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
    def _bare(self, expected=("codex",)):
        s = object.__new__(TmuxSession)
        s.name = "a2t-test"
        s._origin = ""
        s._expected_agent_commands = tuple(expected)
        return s

    @staticmethod
    def _cp(args, *, stdout="", returncode=0):
        return subprocess.CompletedProcess(args, returncode, stdout=stdout, stderr="")

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

    def test_allowed_agent_pane_allows_injection(self):
        calls = []

        def tmux(*args, **kwargs):
            calls.append(args)
            if args[:4] == ("display-message", "-p", "-t", "a2t-test"):
                return self._cp(args, stdout="codex\n")
            return self._cp(args)

        with patch.object(session, "_tmux", side_effect=tmux), \
             patch.object(session.time, "sleep"), \
             patch.object(TmuxSession, "_exists", return_value=True):
            self._bare(("codex",)).inject("hello")

        self.assertIn(("send-keys", "-t", "a2t-test", "Enter"), calls)

    def test_node_wrapper_for_expected_agent_allows_injection(self):
        calls = []

        def tmux(*args, **kwargs):
            calls.append(args)
            if args[:4] == ("display-message", "-p", "-t", "a2t-test"):
                if args[-1] == "#{pane_current_command}":
                    return self._cp(args, stdout="node\n")
                if args[-1] == "#{pane_pid}":
                    return self._cp(args, stdout="100\n")
            return self._cp(args)

        ps_out = "\n".join([
            "100 1 /bin/zsh zsh",
            "101 100 /usr/local/bin/node node /opt/tools/codex/bin/codex.js",
        ])
        with patch.object(session, "_tmux", side_effect=tmux), \
             patch.object(session.subprocess, "run", return_value=self._cp(["ps"], stdout=ps_out)), \
             patch.object(session.time, "sleep"):
            self._bare(("codex",))._send_keys("hello")

        self.assertIn(("send-keys", "-t", "a2t-test", "Enter"), calls)

    def test_shell_leader_with_full_path_claude_child_allows_injection(self):
        calls = []
        processes = [
            (100, 1, "-sh", "-sh"),
            (
                101,
                100,
                "/Users/asistent/.local/bin/claude",
                "/Users/asistent/.local/bin/claude --resume abc123 --dangerously-skip-permissions",
            ),
        ]

        def tmux(*args, **kwargs):
            calls.append(args)
            return self._cp(args)

        with patch.object(session, "_pane_value", return_value="2.1.185"), \
             patch.object(session, "_pane_processes", return_value=processes), \
             patch.object(session, "_tmux", side_effect=tmux), \
             patch.object(session.time, "sleep"):
            self._bare(("claude",))._send_keys("hello")

        self.assertIn(("send-keys", "-t", "a2t-test", "Enter"), calls)

    def test_shell_leader_without_agent_child_blocks_injection(self):
        calls = []
        processes = [(100, 1, "-sh", "-sh")]

        def tmux(*args, **kwargs):
            calls.append(args)
            return self._cp(args)

        with patch.object(session, "_pane_value", return_value="2.1.185"), \
             patch.object(session, "_pane_processes", return_value=processes), \
             patch.object(session, "_tmux", side_effect=tmux), \
             patch.object(session.time, "sleep"):
            with self.assertRaises(SessionError):
                self._bare(("claude",))._send_keys("echo pwned")

        send_calls = [c for c in calls if c and c[0] == "send-keys"]
        self.assertEqual(send_calls, [])

    def test_shell_prompt_blocks_injection(self):
        calls = []

        def tmux(*args, **kwargs):
            calls.append(args)
            if args[:4] == ("display-message", "-p", "-t", "a2t-test"):
                return self._cp(args, stdout="zsh\n")
            return self._cp(args)

        with patch.object(session, "_tmux", side_effect=tmux), \
             patch.object(session.time, "sleep"), \
             patch.object(TmuxSession, "_exists", return_value=True):
            with self.assertRaises(SessionError):
                self._bare(("codex",)).inject("echo pwned")

        send_calls = [c for c in calls if c and c[0] == "send-keys"]
        self.assertEqual(send_calls, [])


if __name__ == "__main__":
    unittest.main()

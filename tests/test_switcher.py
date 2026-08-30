"""Tests for the fork's session-switching module."""
import json
import unittest
from unittest import mock

from agent2telegram import switcher


def _proc(stdout="", returncode=0, stderr=""):
    m = mock.Mock()
    m.stdout, m.returncode, m.stderr = stdout, returncode, stderr
    return m


class ResumeTargetTests(unittest.TestCase):
    def test_send_parses_result_and_follows_fork(self):
        rt = switcher.ResumeTarget("old-sid", "/tmp")
        out = json.dumps({"result": "hotovo", "session_id": "new-sid", "is_error": False})
        with mock.patch("subprocess.run", return_value=_proc(out)) as run:
            self.assertEqual(rt.send("ahoj"), "hotovo")
        self.assertEqual(rt.sid, "new-sid")            # follow-up resumes the fork
        argv = run.call_args[0][0]
        self.assertIn("--resume", argv)
        self.assertIn("old-sid", argv)                 # first message resumed the ORIGINAL sid
        self.assertEqual(run.call_args[1]["cwd"], "/tmp")

    def test_send_error_flag_raises(self):
        rt = switcher.ResumeTarget("s", "/tmp")
        out = json.dumps({"result": "boom", "is_error": True, "session_id": "x"})
        with mock.patch("subprocess.run", return_value=_proc(out)):
            with self.assertRaises(RuntimeError):
                rt.send("ahoj")

    def test_send_plain_output_passthrough(self):
        rt = switcher.ResumeTarget("s", "/tmp")
        with mock.patch("subprocess.run", return_value=_proc("plain text reply")):
            self.assertEqual(rt.send("ahoj"), "plain text reply")
        self.assertEqual(rt.sid, "s")                  # no json → sid unchanged

    def test_send_failure_without_output_raises(self):
        rt = switcher.ResumeTarget("s", "/tmp")
        with mock.patch("subprocess.run", return_value=_proc("", 1, "kaput")):
            with self.assertRaises(RuntimeError):
                rt.send("ahoj")


class TmuxForPidTests(unittest.TestCase):
    def test_finds_session_through_process_tree(self):
        def fake_run(argv, timeout=15):
            if argv[0] == "tmux":
                return "ai 100\nskialpuj 200\n"
            return "100 1\n150 100\n155 150\n200 1\n250 200\n"
        with mock.patch.object(switcher, "_run", side_effect=fake_run):
            self.assertEqual(switcher.tmux_session_for_pid(155), "ai")
            self.assertEqual(switcher.tmux_session_for_pid(250), "skialpuj")
            self.assertIsNone(switcher.tmux_session_for_pid(999))   # headless
        self.assertIsNone(switcher.tmux_session_for_pid(None))


class RunningSessionsTests(unittest.TestCase):
    def test_lists_and_survives_broken_cli(self):
        agents = json.dumps([
            {"sessionId": "abc", "cwd": "/nonexistent-xyz", "pid": 5, "name": "auto-7"},
            {"cwd": "/x"},                               # no sessionId → skipped
        ])
        with mock.patch.object(switcher, "_run", return_value=agents):
            rows = switcher.running_sessions()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["sid"], "abc")
        self.assertEqual(rows[0]["topic"], "auto-7")     # no transcript → fallback name
        with mock.patch.object(switcher, "_run", side_effect=OSError("no claude")):
            self.assertEqual(switcher.running_sessions(), [])


if __name__ == "__main__":
    unittest.main()

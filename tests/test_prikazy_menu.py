"""Prikazy v nabidce bota — musi sedet s tim, co umi terminal (`bezi`, `obnov`, `pojmenuj`, …)."""
import unittest
from unittest import mock

from agent2telegram import attach


class BotCommandMenuTests(unittest.TestCase):
    def test_terminal_commands_are_offered(self):
        names = {c["command"] for c in attach.BOT_COMMANDS}
        for c in ("bezi", "obnov", "zavri", "pojmenuj", "hledej", "ceka", "recap"):
            self.assertIn(c, names)

    def test_telegram_limits(self):
        for c in attach.BOT_COMMANDS:
            self.assertRegex(c["command"], r"^[a-z0-9_]{1,32}$")
            self.assertLessEqual(len(c["description"]), 256)

    def test_recap_falls_through_to_the_agent(self):
        # /recap obsluhuje skill v session, ne most — jinak by ho most spolkl.
        self.assertNotIn("recap", attach.BRIDGE_COMMANDS)

    def test_trailing_command_recognises_new_ones(self):
        self.assertEqual(attach._bridge_command("mrkni na to /pojmenuj"), "/pojmenuj")
        self.assertEqual(attach._bridge_command("/hledej Fringe letenky"), "/hledej Fringe letenky")


class NewCommandDispatchTests(unittest.TestCase):
    def _bridge(self):
        from tests.test_attach_queue import _bridge
        b = _bridge()
        b.cfg.agent = "claude-code"          # prepinani/pojmenovani umi jen Claude Code
        return b

    def test_obnov_is_an_alias_of_hist(self):
        b = self._bridge()
        with mock.patch.object(b, "_cmd_hist", return_value=True) as hist:
            self.assertTrue(b._handle_command("/obnov", 7))
        hist.assert_called_once()

    def test_pojmenuj_without_args_prints_usage(self):
        b = self._bridge()
        with mock.patch.object(attach.switcher, "running_sessions", return_value=[]):
            self.assertTrue(b._handle_command("/pojmenuj", 7))
        self.assertIn("Použití", b.tg.sent[-1][1])

    def test_pojmenuj_writes_through_sessions_py(self):
        b = self._bridge()
        rows = [{"sid": "s-1", "cwd": "/tmp", "pid": 1, "topic": "Fringe", "age": "1h"}]
        run = mock.Mock(return_value=mock.Mock(stdout="pojmenováno: Letenky"))
        with mock.patch.object(attach.switcher, "running_sessions", return_value=rows), \
             mock.patch.object(attach.subprocess, "run", run):
            self.assertTrue(b._handle_command("/pojmenuj 1 Letenky", 7))
        argv = run.call_args.args[0]
        self.assertIn("sid:s-1", argv)
        self.assertEqual(argv[-1], "Letenky")

    def test_pojmenuj_rejects_number_out_of_range(self):
        b = self._bridge()
        rows = [{"sid": "s-1", "cwd": "/tmp", "pid": 1, "topic": "Fringe", "age": "1h"}]
        with mock.patch.object(attach.switcher, "running_sessions", return_value=rows), \
             mock.patch.object(attach.subprocess, "run") as run:
            self.assertTrue(b._handle_command("/pojmenuj 9 Letenky", 7))
        run.assert_not_called()

    def test_hledej_without_query_asks_for_one(self):
        b = self._bridge()
        with mock.patch("threading.Thread") as thr:
            self.assertTrue(b._handle_command("/hledej", 7))
        thr.assert_not_called()

    def test_ceka_runs_the_same_script_as_the_terminal(self):
        b = self._bridge()
        with mock.patch("threading.Thread") as thr:
            self.assertTrue(b._handle_command("/ceka", 7))
        thr.assert_called_once()
        self.assertTrue(b._nastroj_argv("ceka", "")[1].endswith("scripts/ceka.py"))

    def test_tool_worker_reports_failure_instead_of_raising(self):
        b = self._bridge()
        with mock.patch.object(attach.subprocess, "run", side_effect=OSError("bum")):
            b._nastroj_worker("ceka", "", 7)
        self.assertIn("nepovedlo", b.tg.sent[-1][1])


if __name__ == "__main__":
    unittest.main()


class CommandLanguageScopeTests(unittest.TestCase):
    """Nabidka se musi registrovat i pro jazyk klienta — Jan ma Telegram v anglictine."""

    def test_commands_are_registered_for_every_language(self):
        from agent2telegram.telegram import TelegramClient
        c = TelegramClient.__new__(TelegramClient)
        with mock.patch.object(TelegramClient, "_call") as call:
            c.set_my_commands([{"command": "bezi", "description": "x"}])
        langs = [call.args[1].get("language_code", "") for call in call.call_args_list]
        self.assertEqual(langs, list(TelegramClient.COMMAND_LANGUAGES))

    def test_one_failing_language_does_not_stop_the_rest(self):
        from agent2telegram.telegram import TelegramClient, TelegramError
        c = TelegramClient.__new__(TelegramClient)
        with mock.patch.object(TelegramClient, "_call",
                               side_effect=[TelegramError("bum"), None, None, None]) as call:
            c.set_my_commands([{"command": "bezi", "description": "x"}])
        self.assertEqual(call.call_count, 4)

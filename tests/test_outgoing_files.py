"""Sending files FROM the agent (`[tg-file] <path>`).

The path comes from the agent's own reply text, so the allowlist is a security
boundary, not a convenience: without it anything able to influence the agent's
output could make the bridge upload `~/.ssh/id_rsa`. These tests pin that down
together with the plumbing (marker extraction, method per file type).
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent2telegram.config import Config
from agent2telegram.telegram import TelegramClient


def cfg_with_outbox(outbox: Path, extra: list[str] | None = None) -> Config:
    c = Config(token="1:x", agent="claude-code")
    c.outbox_dirs = [str(outbox)] + (extra or [])
    return c


class FakeBridge:
    """Only the bits `_safe_outbox_path` / `_extract_files` / `_send_files` touch."""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.tg = mock.Mock()
        self._owner_chat = 42
        self.sent_text: list[str] = []
        self._pending_files: list[str] = []

    def _send_final(self, text, key=None, *, turn_text=True):
        self.sent_text.append(text)


def bind(bridge: FakeBridge):
    from agent2telegram.attach import AttachBridge
    for name in ("_safe_outbox_path", "_extract_files", "_send_files", "_flush_files"):
        setattr(bridge, name, getattr(AttachBridge, name).__get__(bridge, FakeBridge))
    return bridge


class OutgoingFilesTest(unittest.TestCase):
    def test_marker_se_vyjme_z_textu(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            b = bind(FakeBridge(cfg_with_outbox(Path(tmp))))
            text, files = b._extract_files(
                "Tady je náhled.\n[tg-file] /tmp/a.mp4\nA ještě zvuk.\n[tg-file] '/tmp/b.mp3'")
            self.assertEqual(text, "Tady je náhled.\nA ještě zvuk.")
            self.assertEqual(files, ["/tmp/a.mp4", "/tmp/b.mp3"])

    def test_soubor_v_outboxu_projde(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "outbox"
            out.mkdir()
            f = out / "clip.mp4"
            f.write_bytes(b"x" * 100)
            b = bind(FakeBridge(cfg_with_outbox(out)))
            resolved, reason = b._safe_outbox_path(str(f))
            self.assertEqual(reason, "ok")
            self.assertEqual(resolved, f.resolve())

    def test_soubor_mimo_outbox_se_odmitne(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "outbox"
            out.mkdir()
            secret = Path(tmp) / "id_rsa"
            secret.write_bytes(b"PRIVATE KEY")
            b = bind(FakeBridge(cfg_with_outbox(out)))
            resolved, reason = b._safe_outbox_path(str(secret))
            self.assertIsNone(resolved)
            self.assertIn("outside", reason)

    def test_symlink_z_outboxu_ven_se_odmitne(self) -> None:
        """Nejzrádnější případ: odkaz uvnitř povolené složky mířící na tajný soubor.
        Proto se cesta rozbaluje (resolve) PŘED kontrolou, ne po ní."""
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "outbox"
            out.mkdir()
            secret = Path(tmp) / "credentials.json"
            secret.write_bytes(b'{"token":"secret"}')
            link = out / "innocent.json"
            os.symlink(secret, link)
            b = bind(FakeBridge(cfg_with_outbox(out)))
            resolved, reason = b._safe_outbox_path(str(link))
            self.assertIsNone(resolved, "symlink ven z outboxu musí být odmítnutý")
            self.assertIn("outside", reason)

    def test_odmitnuti_se_ohlasi_do_chatu(self) -> None:
        """Tiše zahozená příloha by byla horší než viditelná chyba."""
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "outbox"
            out.mkdir()
            b = bind(FakeBridge(cfg_with_outbox(out)))
            b._send_files([str(Path(tmp) / "nekde-jinde.mp4")])
            self.assertTrue(b.sent_text, "odmítnutí se musí objevit v chatu")
            self.assertIn("Couldn't send", b.sent_text[0])
            b.tg.send_file.assert_not_called()

    def test_prazdny_soubor_se_odmitne(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "outbox"
            out.mkdir()
            f = out / "prazdny.mp4"
            f.touch()
            b = bind(FakeBridge(cfg_with_outbox(out)))
            resolved, reason = b._safe_outbox_path(str(f))
            self.assertIsNone(resolved)
            self.assertIn("empty", reason)

    def test_metoda_podle_typu_souboru(self) -> None:
        self.assertEqual(TelegramClient.kind_for("a.mp4"), ("sendVideo", "video"))
        self.assertEqual(TelegramClient.kind_for("a.MOV"), ("sendVideo", "video"))
        self.assertEqual(TelegramClient.kind_for("a.opus"), ("sendAudio", "audio"))
        self.assertEqual(TelegramClient.kind_for("a.png"), ("sendPhoto", "photo"))
        self.assertEqual(TelegramClient.kind_for("a.zip"), ("sendDocument", "document"))

    def test_prilis_velky_soubor_selze_hned(self) -> None:
        """Radši jasná chyba než pět minut uploadu a pak 413."""
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "velky.mp4"
            f.write_bytes(b"x" * 16)
            c = TelegramClient("1:x", opener=mock.Mock())
            with mock.patch.object(os.path, "getsize", return_value=TelegramClient.MAX_UPLOAD + 1), \
                 self.assertRaises(Exception) as cm:
                c.send_file(1, str(f))
            self.assertIn("at most", str(cm.exception))

    def test_vychozi_outbox_je_vzdy_povoleny(self) -> None:
        c = Config(token="1:x", agent="claude-code")
        self.assertIn(c.path_outbox(), c.allowed_outbox_dirs())


class NotifyFilesTest(unittest.TestCase):
    """`notify --file` je cesta pro zprávy z pozadí a cronu. Musí mít STEJNOU
    hranici jako marker v chatu – běh na pozadí není důvěryhodnější než agent."""

    def test_notify_odmitne_soubor_mimo_outbox(self) -> None:
        from agent2telegram import __main__ as m
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "outbox"
            out.mkdir()
            secret = Path(tmp) / "credentials.json"
            secret.write_text("{}", encoding="utf-8")
            cfg = cfg_with_outbox(out)
            cfg.allowed_user_ids = [1]
            client = mock.Mock()
            args = mock.Mock(message="ahoj", config=None, file=[str(secret)])
            with mock.patch("agent2telegram.config.load", return_value=cfg), \
                 mock.patch("agent2telegram.telegram.TelegramClient", return_value=client):
                rc = m._cmd_notify(args)
            self.assertEqual(rc, 1, "odmítnutí musí vrátit nenulový kód")
            client.send_file.assert_not_called()
            client.send_message.assert_called_once()      # text projde, soubor ne

    def test_notify_posle_soubor_z_outboxu(self) -> None:
        from agent2telegram import __main__ as m
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "outbox"
            out.mkdir()
            f = out / "vlna.png"
            f.write_bytes(b"x" * 50)
            cfg = cfg_with_outbox(out)
            cfg.allowed_user_ids = [1]
            client = mock.Mock()
            args = mock.Mock(message=None, config=None, file=[str(f)])
            with mock.patch("agent2telegram.config.load", return_value=cfg), \
                 mock.patch("agent2telegram.telegram.TelegramClient", return_value=client):
                rc = m._cmd_notify(args)
            self.assertEqual(rc, 0)
            client.send_file.assert_called_once()
            client.send_message.assert_not_called()       # bez textu se nic neposílá


if __name__ == "__main__":
    unittest.main()

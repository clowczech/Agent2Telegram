"""Testy nikdy nesmí doručit zprávu do reálného chatu.

2026-08-23: spuštění staré verze test_attach_backstop.py proti novému kódu zapsalo
fixture stringy do SDÍLENÉHO durable outboxu ve state adresáři. Běžící bridge – jiný
proces nad stejnou frontou – je vzal a poslal Petrovi. Jedenáct zpráv typu
„the real answer" a „thanks!". Test se nesmí spoléhat na to, že si každý autor
vzpomene vypnout outbox; tuhle záruku musí držet kód.
"""
import os
import unittest
from pathlib import Path

from agent2telegram import attach


class _FakeBridge:
    """Minimum, co `_ensure_outbox` potřebuje – přesně jako to dělá zapomnětlivý test."""
    _use_durable_outbox = True
    cfg = None

    _ensure_outbox = attach.AttachBridge._ensure_outbox


class OutboxIsolationTests(unittest.TestCase):
    def test_test_run_without_isolated_queue_gets_no_outbox(self):
        self.assertIn("PYTEST_CURRENT_TEST", os.environ, "tenhle test dává smysl jen pod pytestem")
        self.assertIsNone(_FakeBridge()._ensure_outbox(),
                          "test bez izolované fronty se dostal k reálnému outboxu")

    def test_isolated_queue_path_still_works(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            b = _FakeBridge()
            b._queue_path = str(Path(d) / "queue" / "offset")
            self.assertIsNotNone(b._ensure_outbox(),
                                 "izolovaná fronta se nesmí zakázat – jinak by testy nešly psát")


if __name__ == "__main__":
    unittest.main()

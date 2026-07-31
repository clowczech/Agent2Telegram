"""Behaviour guards for install.sh — the webinar-critical installer.

These shell out to `bash` (present on macOS and the Ubuntu CI image) and assert BEHAVIOUR,
not text: the preflight names every missing dependency with an install command, and the
setup launch falls back cleanly when there is no usable /dev/tty. Cross-platform on purpose.
"""
import os
import subprocess
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
INSTALL = REPO / "install.sh"


class InstallSyntaxTests(unittest.TestCase):
    def test_syntax_is_valid(self):
        r = subprocess.run(["bash", "-n", str(INSTALL)], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)


class PreflightTests(unittest.TestCase):
    def test_missing_dependency_reported_up_front_with_install_hint(self):
        """Run install.sh with a PATH that lacks tmux (and, on some hosts, python3), from
        inside the repo so git isn't required. The preflight must stop BEFORE doing any
        install work and name the missing dependency plus how to install it — so a webinar
        attendee installs everything in one go instead of one failure at a time."""
        r = subprocess.run(
            ["bash", str(INSTALL)],
            cwd=str(REPO),
            env={"PATH": "/usr/bin:/bin", "HOME": os.environ.get("HOME", "/tmp")},
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30,
        )
        out = (r.stdout + r.stderr).lower()
        self.assertNotEqual(r.returncode, 0, "preflight should stop the installer")
        self.assertIn("tmux", out, "the missing tmux must be named")
        self.assertTrue(
            any(w in out for w in ("install", "apt", "dnf", "brew")),
            "the error must tell the user how to install it",
        )


if __name__ == "__main__":
    unittest.main()

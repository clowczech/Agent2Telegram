"""Session switching for Claude Code bridges (fork addition).

Upstream binds one bridge to one tmux session forever. This module lets a single bridge
list every *running* Claude Code session on the machine (`claude agents --json`) and
retarget on the fly:

  * a session that lives in a tmux pane      -> live attach (send-keys, transcript tail)
  * a headless one (mobile app / claude.ai)  -> "resume mode": each message runs
    ``claude -p --resume <sid>`` in that session's cwd. The reply comes from stdout, so
    the transcript reader stays out of it. Each run returns a NEW session id (a fork of
    the conversation) which is tracked, so follow-ups keep full context.

Everything here is Claude Code specific — the entry points no-op for other agents.
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import time
from pathlib import Path

log = logging.getLogger("agent2telegram.switcher")

#: Longest topic label that still fits a Telegram button next to the age suffix.
TOPIC_CHARS = 28


def _run(argv: list[str], timeout: float = 15) -> str:
    res = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    return res.stdout


def running_sessions() -> list[dict]:
    """Every running Claude Code session: sessionId, cwd, pid, topic, age.

    ``claude agents --json`` is the only safe form — the bare command is an interactive
    picker that would swallow the terminal.
    """
    try:
        agents = json.loads(_run(["claude", "agents", "--json"]) or "[]")
    except (subprocess.SubprocessError, OSError, json.JSONDecodeError) as e:
        log.warning("claude agents --json failed: %s", e)
        return []
    out = []
    for a in agents:
        sid, cwd = a.get("sessionId", ""), a.get("cwd", "")
        if not sid:
            continue
        t = _transcript_path(cwd, sid)
        out.append({
            "sid": sid, "cwd": cwd, "pid": a.get("pid"),
            "topic": _topic(t) or a.get("name", "?"),
            "age": _age(t),
        })
    return out


def _transcript_path(cwd: str, sid: str) -> Path | None:
    d = re.sub(r"[^A-Za-z0-9]", "-", cwd)
    p = Path.home() / ".claude" / "projects" / d / f"{sid}.jsonl"
    return p if p.exists() else None


def _topic(path: Path | None, limit: int = TOPIC_CHARS) -> str:
    """First real user message of the conversation — auto-generated names say nothing."""
    if not path:
        return ""
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                if d.get("type") != "user" or d.get("toolUseResult"):
                    continue
                c = d.get("message", {}).get("content")
                if not isinstance(c, str):
                    continue
                c = " ".join(c.split())
                if not c or c.startswith(("<", "/")):
                    continue
                return c[:limit] + ("…" if len(c) > limit else "")
    except OSError:
        pass
    return ""


def _age(path: Path | None) -> str:
    if not path:
        return ""
    try:
        m = time.time() - path.stat().st_mtime
    except OSError:
        return ""
    if m < 90:
        return "teď"
    if m < 3600:
        return f"{int(m // 60)} min"
    if m < 86400:
        return f"{int(m // 3600)} h"
    return f"{int(m // 86400)} d"


# ---------------------------------------------------------------- pid -> tmux session
def tmux_session_for_pid(pid: int | None) -> str | None:
    """Name of the tmux session whose pane subtree contains *pid* — i.e. the session the
    agent actually runs in. None when the agent is headless (mobile app, claude.ai)."""
    if not pid:
        return None
    try:
        panes = _run(["tmux", "list-panes", "-a", "-F", "#{session_name} #{pane_pid}"], 5)
        ps = _run(["ps", "-axo", "pid=,ppid="], 5)
    except (subprocess.SubprocessError, OSError):
        return None
    children: dict[int, list[int]] = {}
    for line in ps.splitlines():
        try:
            p, pp = (int(x) for x in line.split())
        except ValueError:
            continue
        children.setdefault(pp, []).append(p)
    for line in panes.splitlines():
        try:
            name, pane_pid_s = line.rsplit(" ", 1)
            stack = [int(pane_pid_s)]
        except ValueError:
            continue
        while stack:
            cur = stack.pop()
            if cur == pid:
                return name
            stack.extend(children.get(cur, ()))
    return None


# ---------------------------------------------------------------- resume mode
class ResumeTarget:
    """A headless Claude Code session driven via ``claude -p --resume``.

    Each run forks the conversation and yields a fresh session id; that id is carried
    forward so consecutive Telegram messages stay one continuous thread.
    """

    def __init__(self, sid: str, cwd: str, topic: str = "", timeout: int = 600) -> None:
        self.sid = sid
        self.cwd = cwd or str(Path.home())
        self.topic = topic
        self.timeout = timeout

    def send(self, prompt: str) -> str:
        argv = ["claude", "-p", "--resume", self.sid, prompt,
                "--output-format", "json"]
        proc = subprocess.run(argv, cwd=self.cwd, capture_output=True, text=True,
                              timeout=self.timeout, stdin=subprocess.DEVNULL)
        out = (proc.stdout or "").strip()
        try:
            data = json.loads(out)
        except ValueError:
            # Old CLI or plain output — pass it through rather than lose the reply.
            if proc.returncode != 0 and not out:
                raise RuntimeError((proc.stderr or "").strip()[:500]
                                   or f"claude exited {proc.returncode}")
            return out
        if data.get("session_id"):
            self.sid = data["session_id"]      # follow the fork
        if data.get("is_error"):
            raise RuntimeError(str(data.get("result", ""))[:500] or "claude reported an error")
        return str(data.get("result", "")).strip() or "(empty response)"

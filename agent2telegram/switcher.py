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


#: Vlastní evidence názvů (příkaz `pojmenuj`, klíč = sessionId) — má nejvyšší prioritu.
_NAMES_STORE = Path.home() / ".claude" / "scripts" / "session-names.json"


def _custom_names() -> dict:
    try:
        return json.loads(_NAMES_STORE.read_text("utf-8"))
    except (OSError, ValueError):
        return {}


def _renamed_via_rename(pid) -> str:
    """Název z /rename (~/.claude/sessions/<pid>.json). U přejmenované session klíč
    `nameSource` úplně CHYBÍ (automatická má "derived") — testovat na != derived,
    ne na pravdivost. Automatické tvary `<složka>-<2 hex>` ignorovat, nic neříkají."""
    if not pid:
        return ""
    try:
        d = json.loads((Path.home() / ".claude" / "sessions" / f"{pid}.json").read_text("utf-8"))
    except (OSError, ValueError):
        return ""
    n = d.get("name") or ""
    if not n or d.get("nameSource") == "derived" or re.search(r"-[0-9a-f]{2}$", n):
        return ""
    return n


def _display_name(sid: str, pid, fallback: str) -> str:
    """Jméno pro /bezi a /hist: pojmenuj > /rename > první zpráva konverzace.
    Jan si sessions přejmenovává a seznam bez těch názvů nedával smysl (30. 8.)."""
    return (_custom_names().get(sid)
            or _renamed_via_rename(pid)
            or fallback)


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
    # `claude agents --json` hlásí i DÁVNO UKONČENÉ sessions a skončené subagenty (PID
    # se mezitím recykloval na cizí proces, takže os.kill(pid,0) je nepozná). Filtrujeme
    # na PIDy, které opravdu patří živému `claude.exe` — jinak /bezi ukázal 43 řádků,
    # z toho 37 mrtvých (30. 8. 2026). Duplicitní sid navíc sjednotíme na nejnovější.
    live = _live_claude_pids()
    out, seen = [], set()
    for a in agents:
        sid, cwd, pid = a.get("sessionId", ""), a.get("cwd", ""), a.get("pid")
        if not sid or sid in seen:
            continue
        if live and pid not in live:      # když se PIDy nepodaří zjistit, nefiltruj
            continue
        seen.add(sid)
        t = _transcript_path(cwd, sid)
        out.append({
            "sid": sid, "cwd": cwd, "pid": pid,
            "topic": _display_name(sid, pid, _topic(t) or a.get("name", "?")),
            "age": _age(t),
        })
    return out


def _live_claude_pids() -> set:
    """PIDy skutečně běžících Claude procesů. Prázdná množina = nepodařilo se
    zjistit → volající pak filtr přeskočí (fail-open).

    Pozor na tvary: terminál/rc = `claude.exe` či `claude`, ale sessions z APPKY
    běží jako `~/.claude/remote/ccd-cli/<verze>` — filtr jen na "claude.exe" je
    všechny vyhodil a /bezi spadl ze 43 na 4 místo na správných 16 (30. 8.).
    Substring "claude" pokryje všechny tvary; PIDy stejně pochází z `claude
    agents`, tady jen ověřujeme, že PID nebyl recyklován cizím procesem."""
    try:
        out = _run(["ps", "-axo", "pid=,command="], 5)
    except (subprocess.SubprocessError, OSError):
        return set()
    pids = set()
    for line in out.splitlines():
        line = line.strip()
        if not line or "claude" not in line.lower():
            continue
        head = line.split(None, 1)[0]
        try:
            pids.add(int(head))
        except ValueError:
            pass
    return pids


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
def session_for_ancestor(pid: int | None) -> dict | None:
    """The running Claude session this process tree hangs under, or None.

    Walks the ppid chain from *pid* upward and returns the first ancestor that is a live
    Claude session (per ``claude agents --json``). This is how ``notify`` finds out WHICH
    session sent a notification without the caller passing anything: the notify CLI runs
    as a child of the session's Bash tool, so its ancestry leads straight to the session.
    """
    if not pid:
        return None
    rows = {r.get("pid"): r for r in running_sessions() if r.get("pid")}
    cur = pid
    for _ in range(40):                       # bounded — a process tree is never this deep
        if cur in rows:
            return rows[cur]
        try:
            out = _run(["ps", "-o", "ppid=", "-p", str(cur)], timeout=5).strip()
            cur = int(out)
        except (subprocess.SubprocessError, OSError, ValueError):
            return None
        if cur <= 1:
            return None
    return None


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
        # --permission-mode auto: bez něj se telegramová větev chovala jako „návštěva" —
        # zápis do repa i vaultu čekal na schválení, které v headless běhu nemá kdo dát.
        # Janovo výslovné rozhodnutí 30. 8. 2026: větev má mít stejná práva jako rc wrapper
        # a tmux session (obě `--permission-mode auto` už mají).
        argv = ["claude", "-p", "--resume", self.sid, prompt,
                "--output-format", "json", "--permission-mode", "auto"]
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


# Cache: agent_for_tmux běží z resolučního cyklu mostu, tak ať nespouští
# `claude agents --json` několikrát za sekundu.
_afm_cache: dict = {}
_AFM_TTL = 10.0


def agent_for_tmux(name: str) -> dict | None:
    """The running Claude session INSIDE tmux session *name* (via the process tree).

    This pins the bridge to the pane's own transcript. Resolving "newest .jsonl under
    the pane's cwd" breaks the moment several sessions share a cwd (typically ~): a
    busier sibling always wins the mtime race, the reader tails a foreign transcript,
    never sees the origin-prefixed user record, and replies are silently withheld.
    Seen live on the very first bridged turn (2026-08-30)."""
    now = time.time()
    hit = _afm_cache.get(name)
    if hit and now - hit[0] < _AFM_TTL:
        return hit[1]
    row = _agent_for_tmux_uncached(name)
    _afm_cache[name] = (now, row)
    return row


def _agent_for_tmux_uncached(name: str) -> dict | None:
    try:
        panes = _run(["tmux", "list-panes", "-t", name, "-F", "#{pane_pid}"], 5)
        ps = _run(["ps", "-axo", "pid=,ppid="], 5)
    except (subprocess.SubprocessError, OSError):
        return None
    children: dict[int, list[int]] = {}
    for line in ps.splitlines():
        try:
            pid, ppid = (int(x) for x in line.split())
        except ValueError:
            continue
        children.setdefault(ppid, []).append(pid)
    subtree: set[int] = set()
    for pane_pid in panes.split():
        try:
            stack = [int(pane_pid)]
        except ValueError:
            continue
        while stack:
            cur = stack.pop()
            subtree.add(cur)
            stack.extend(children.get(cur, ()))
    if not subtree:
        return None
    for r in running_sessions():
        if r.get("pid") in subtree:
            return r
    return None


def transcript_for_tmux(name: str) -> Path | None:
    """Exact transcript path of the Claude session running in tmux session *name*."""
    r = agent_for_tmux(name)
    if not r:
        return None
    return _transcript_path(r["cwd"], r["sid"])


def recent_ended_sessions(n: int = 8, base: Path | None = None) -> list[dict]:
    """Nedávno UKONČENÉ konverzace (transcript na disku, ale neběží) — pro /hist.
    `claude -p --resume` je umí oživit i po ukončení."""
    base = base or (Path.home() / ".claude" / "projects")
    running = {r["sid"] for r in running_sessions()}
    files = []
    for d in base.glob("*"):
        if "shim" in d.name or "Caches" in d.name:
            continue
        for f in d.glob("*.jsonl"):
            try:
                files.append((f.stat().st_mtime, f))
            except OSError:
                pass
    files.sort(reverse=True)
    out = []
    for _, f in files:
        sid = f.stem
        if sid in running:
            continue
        cwd = _first_cwd(f)
        out.append({"sid": sid, "cwd": cwd or str(Path.home()),
                    "topic": _display_name(sid, None, _topic(f) or "(bez tématu)"),
                    "age": _age(f), "pid": None})
        if len(out) >= n:
            break
    return out


def session_from_disk(sid: str, base: Path | None = None) -> dict | None:
    """Najde session podle sid v transcriptech na disku (i ukončenou)."""
    base = base or (Path.home() / ".claude" / "projects")
    for f in base.glob(f"*/{sid}.jsonl"):
        cwd = _first_cwd(f)
        return {"sid": sid, "cwd": cwd or str(Path.home()),
                "topic": _display_name(sid, None, _topic(f) or "(bez tématu)"),
                "age": _age(f), "pid": None}
    return None


def _first_cwd(path: Path) -> str:
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    c = json.loads(line).get("cwd", "")
                except ValueError:
                    continue
                if c:
                    return c
    except OSError:
        pass
    return ""


# ---------------------------------------------------------------- zavírání sessions
def _own_process_tree() -> set:
    """PIDy, které NIKDY nezabíjet: most sám a celý jeho rodičovský řetěz.

    Bez toho by `/zavri` na aktuální cíl sestřelil i proces, který ten příkaz právě
    obsluhuje. Most sice `/zavri` řeší sám (žádný `claude -p --resume` u toho neběží),
    ale jistota stojí za deset řádků — stačí jedno budoucí volání z jiného místa.
    """
    safe, pid = set(), os.getpid()
    try:
        ppids = {}
        for line in _run(["ps", "-axo", "pid=,ppid="], 5).splitlines():
            try:
                p, pp = (int(x) for x in line.split())
            except ValueError:
                continue
            ppids[p] = pp
        while pid and pid not in safe:
            safe.add(pid)
            pid = ppids.get(pid, 0)
    except (subprocess.SubprocessError, OSError):
        safe.add(os.getpid())
    return safe


def pids_for_session(sid: str) -> list[int]:
    """Živé procesy, které drží konverzaci *sid* — tedy to, co ji drží v `/bezi`.

    Hledá se podle sid v příkazové řádce: appka i Remote Control pouštějí
    `~/.claude/remote/ccd-cli/<verze>` s `--resume <sid>`. Pozor, appka nechává běžet
    až čtyři procesy TÉŽE konverzace (staré při každém otevření nezavře), takže tohle
    běžně vrací víc než jeden PID — a dokud žije jediný z nich, session v `/bezi` visí.
    """
    if not sid:
        return []
    try:
        out = _run(["ps", "-axo", "pid=,command="], 5)
    except (subprocess.SubprocessError, OSError):
        return []
    safe, pids = _own_process_tree(), []
    for line in out.splitlines():
        line = line.strip()
        if sid not in line:
            continue
        try:
            p = int(line.split(None, 1)[0])
        except (ValueError, IndexError):
            continue
        if p in safe:
            continue
        pids.append(p)
    return pids


def close_session(sid: str, grace: float = 1.5) -> dict:
    """Ukončit konverzaci *sid* → zmizí z `/bezi`. Transcript na disku ZŮSTÁVÁ.

    Vrací ``{"killed": [pid…], "left": [pid…], "tmux": name|None}``. Prázdné `killed`
    i `left` znamená, že už nic neběželo — což je taky úspěch, ne chyba.

    U session v tmuxu procesy zabít jde, ale zůstane po ní prázdný pane; volající to
    má uživateli říct a nabídnout `tmux kill-session`.
    """
    pids = pids_for_session(sid)
    tmux = None
    for p in pids:
        tmux = tmux or tmux_session_for_pid(p)
    killed = []
    for p in pids:
        try:
            os.kill(p, 15)          # SIGTERM: ať si Claude stihne zavřít transcript
            killed.append(p)
        except (ProcessLookupError, PermissionError) as e:
            log.warning("kill %s failed: %s", p, e)
    if killed:
        time.sleep(grace)
    left = [p for p in killed if _pid_alive(p)]
    for p in left:
        try:
            os.kill(p, 9)           # tvrdohlavý zbytek
        except (ProcessLookupError, PermissionError):
            pass
    if left:
        time.sleep(0.5)
        left = [p for p in left if _pid_alive(p)]
    log.info("CLOSE sid=%s killed=%s left=%s tmux=%s", sid[:8], killed, left, tmux)
    return {"killed": killed, "left": left, "tmux": tmux}


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True

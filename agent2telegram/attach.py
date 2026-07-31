"""Attach mode — drive an existing live agent session, the way a hand-rolled bridge does.

Async model:
  * **inbound** (main thread): poll Telegram → inject each message into the live tmux session
    via send-keys. No blocking wait.
  * **outbound** (background thread): tail the agent transcript and, via an agent-specific
    :mod:`reader`, forward every assistant message of Telegram-originated turns, drive a live
    one-line tool-call status bubble, and detect end-of-turn (Codex: ``task_complete`` in the
    log; Claude Code: a marker its Stop hook writes; plus an idle fallback).
  * **typing** (background thread): assert "typing…" while a turn is in flight, independent of
    the send path so a flood-control sleep can't starve it.

This keeps the agent's full session (context, persona, tools) and adds Telegram I/O around it.
"""
from __future__ import annotations

from collections import deque
import glob
import html
import json
import logging
import queue
import subprocess
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from . import adapters
from . import readers
from .compat import AlreadyRunning, single_instance_lock
from .config import Config, _state_dir
from .durable import DurableInbox, DurableOutbox
from .session import SessionError, TmuxSession
from .telegram import TelegramClient, TelegramError, is_network_error, split_message

log = logging.getLogger("agent2telegram.attach")

#: Fallback only: how long the transcript may be quiet before we force-end a turn, in case
#: the Stop-hook turn-end marker never arrives. The marker is the primary, precise signal —
#: this just stops "typing…" from hanging forever if the hook is missing/misconfigured.
IDLE_DONE = 90.0
# Zápis do tmuxu selhává i přechodně (zaneprázdněné okno, plný buffer), tak se opakuje.
# Krátká pauza záměrně: zpráva od uživatele nesmí čekat déle, než kolik trvá jeho trpělivost.
INJECT_ATTEMPTS = 3
INJECT_RETRY_WAIT = 1.5
# Kolikrát se zkusí doručit uložená příchozí zpráva, než skončí v dead-letter.
# Radši ji tam odložit s důvodem než ji smazat – aspoň je dohledatelná.
INBOUND_MAX_ATTEMPTS = 5
# Kolikrát se opakuje odchozí příloha, než se vzdá. Bez stropu by jedna vadná příloha
# držela hlavu FIFO fronty navždy a zablokovala všechny další odpovědi.
OUTBOX_MAX_ATTEMPTS = 3
# Jak často se za BĚHU zkusí znovu doručit zpráva, která zůstala v trvalém úložišti.
# Bez toho se čekalo na restart – u služby běžící týdny prakticky navždy (revize Sol #1).
INBOUND_RETRY_INTERVAL = 30.0
#: How often we re-assert the "typing…" chat action (Telegram shows it for ~5s). Kept well
#: under that window so a turn never shows a gap, even right after a sent message clears it.
TYPING_INTERVAL = 1.5

# Turn-end backstop: transcript writes can lag the turn-end signal by a fraction of a second
# (especially after a reset/reconnect), so give the final assistant text a short chance to land.
BACKSTOP_RETRY_ATTEMPTS = 5
BACKSTOP_RETRY_DELAY = 0.35

#: Codex only: hold the first scraped tool bubble until the turn's intro text has been forwarded
#: (agents usually say what they'll do, THEN call the tool). The scraper is live but the Codex
#: transcript text lags, so without this the bubble jumps ahead of the intro line. After this
#: grace we show bubbles anyway, since some turns call a tool with no intro text.
TUI_BUBBLE_GRACE = 3.0

# Short disk ledger for Telegram update_ids already accepted by attach mode. Offset persistence is
# the primary protection; this catches stale replays if a crash lands in the small ACK window.
PROCESSED_UPDATE_LEDGER_LIMIT = 500
# Keep slow media downloads/STT from becoming a resource sink. Telegram voice notes are normally
# tiny; 25 MB still leaves room for long clips while bounding download + STT work.
MAX_AUDIO_BYTES = 25 * 1024 * 1024
# Final prompt guard after text, captions, downloaded-file notes, or STT results are combined.
MAX_INBOUND_PROMPT_CHARS = 12_000

#: Registered with Telegram (setMyCommands) so typing "/" shows the command autocomplete menu.
BOT_COMMANDS = [
    {"command": "start", "description": "Intro and what you can send"},
    {"command": "help", "description": "Intro and what you can send"},
    {"command": "status", "description": "Connection and voice status"},
    {"command": "setkey", "description": "Enable voice (your ElevenLabs API key)"},
    {"command": "id", "description": "Show your Telegram id"},
]

import re as _re  # noqa: E402
from .readers import _short  # noqa: E402

#: Codex renders tool activity live in its TUI but only writes it to the rollout at completion.
#: For Codex (attach), we scrape the tmux pane for these lines so tool bubbles appear LIVE —
#: matching Claude Code (which logs tool_use to its transcript immediately). Claude needs no scrape.
_TUI_VERBS = {"Read": "📄", "List": "📂", "Search": "🔎", "Ran": "🛠️",
              "Edit": "✏️", "Wrote": "✏️", "Added": "✏️", "Updated": "✏️",
              "Deleted": "🗑️", "Removed": "🗑️"}


def _extract_tui_tools(pane: str) -> list:
    """Pull live tool/web-search lines out of a Codex TUI capture, as bubble summaries."""
    out = []
    for raw in pane.splitlines():
        s = raw.strip()
        m = _re.search(r"Searched the web for\s+(.+)", s)
        if m:
            out.append("🔎 Web search: " + _short(m.group(1)))
            continue
        if "Searching the web" in s:
            out.append("🔎 Searching the web")
            continue
        # Codex prints the call on a bullet line ("● Ran df -h /", "● Read foo.py") and its
        # output nested under "└ …". Strip any leading bullet/branch markers so we catch the
        # verb on either line; the verb whitelist keeps plain agent text (other words) out.
        body = s.lstrip("└├│•●▪▸·*- \t")
        if not body:
            continue
        verb = body.split(" ", 1)[0]
        if verb in _TUI_VERBS:
            rest = body[len(verb):].strip()
            out.append(f"{_TUI_VERBS[verb]} {verb} {_short(rest)}".rstrip())
    return out


def _expected_agent_commands(cfg: Config) -> list[str]:
    """Commands that are allowed to own the tmux pane before we send keys into it."""
    commands: list[str] = []
    cls = adapters.REGISTRY.get(cfg.agent)
    if cls is not None:
        commands.extend(cls.tui_launch()[:1])
        if cls.binary:
            commands.append(cls.binary)
    if cfg.command:
        commands.append(cfg.command[0])
    if cfg.continue_command:
        commands.append(cfg.continue_command[0])

    seen = set()
    out = []
    for command in commands:
        name = Path(str(command)).name
        if name and name not in seen:
            seen.add(name)
            out.append(name)
    if not out:
        raise ValueError(
            "attach mode cannot verify the tmux pane: configure an agent command to allow"
        )
    return out


class AttachBridge:
    _turn_end_backstop_enabled = True
    #: Durable outbox s potvrzením po částech. Podtřídy s vlastní frontou (StreamBridge)
    #: ho vypínají – sdílejí tenhle kód, ale ne jeho životní cyklus.
    _use_durable_outbox = True

    def __init__(self, cfg: Config, *, client: TelegramClient | None = None) -> None:
        if not cfg.tmux_session:
            raise ValueError("attach mode requires 'tmux_session' in config")
        self.cfg = cfg
        self.tg = client or TelegramClient(cfg.token)
        self._allowed = set(cfg.allowed_user_ids)
        self._marker = cfg.progress_marker
        self._origin = cfg.origin_prefix
        # The reader knows the agent's transcript format and turns it into a common event stream.
        self._reader = readers.for_agent(cfg.agent)
        self._pending_turn_end = False       # set when the reader signals end-of-turn (Codex)
        # Accept the configured prefix plus the legacy "Telegram:" one, so a prefix change
        # mid-conversation doesn't drop the turn in flight.
        self._origins = tuple({p for p in (cfg.origin_prefix.strip(), "Telegram:", "[TG]") if p})
        self._owner_chat = cfg.allowed_user_ids[0] if cfg.allowed_user_ids else None
        self._signal = Path(cfg.signal_file) if cfg.signal_file else None
        # Claude Code only: end-of-turn marker its Stop hook writes (keeps "typing…" lit through
        # long thinking and off exactly at turn end). Codex needs none — its rollout records
        # task_complete, so the reader signals turn end directly.
        self._turn_end = (self._signal.parent / "turn_end") if self._signal else None
        # Outbound-loop heartbeat: touched at the end of every forward cycle (see _outbound_loop).
        # The process and the inbound poller can stay alive while forwarding is wedged — a blocking
        # send or a persistent exception freezes replies silently. A watchdog notices this file go
        # stale and restarts the bridge. Per-bridge (keyed on the tmux session) so several bridges
        # from one install don't share a heartbeat.
        _slug = "".join(c if (c.isalnum() or c in "._-") else "_" for c in (cfg.tmux_session or "bridge"))
        self._heartbeat = (self._signal.parent / f"outbound_heartbeat_{_slug}") if self._signal else None
        # Codex writes a fresh rollout-*.jsonl per session under ~/.codex/sessions; auto-detect
        # the newest one (and re-detect if the session restarts). Claude Code uses a fixed path.
        self._transcript = self._resolve_transcript()
        self._last_resolve = 0.0
        expected_agent_commands = _expected_agent_commands(cfg)
        self._session = TmuxSession([], name=cfg.tmux_session, cwd=Path.home(),
                                    origin_prefix=cfg.origin_prefix, boot_wait=0,
                                    expected_agent_commands=expected_agent_commands)
        self._stop = threading.Event()
        self._init_inbound_worker_state()
        self._init_update_state()
        # Persisted ledger of already-forwarded message uuids — survives restarts/crashes/reboots
        # so resuming an interrupted turn never re-sends what was already delivered.
        self._sent_path = Path.home() / ".config" / "agent2telegram" / "attach_sent.txt"
        try:
            self._sent_keys: set = set(self._sent_path.read_text("utf-8").split())
        except OSError:
            self._sent_keys = set()
        # Durable outbound queue: a reply whose send HARD-fails (network reset, etc.) is persisted
        # here and re-sent by the outbound loop until Telegram confirms — so a turn's answer is
        # NEVER silently lost. Survives restarts (re-loaded below). Per-bridge (tmux slug).
        self._queue_path = (self._signal.parent / f"outbound_queue_{_slug}.jsonl") if self._signal else None
        self._pending_send: list = self._load_queue()
        self._tpos = 0
        self._turn_active = threading.Event()
        self._turn_from_tg = False           # is the current transcript turn Telegram-originated?
        self._last_activity = 0.0            # monotonic ts of last transcript activity (for typing)
        self._status = {"mid": None, "shown": ""}   # live one-line tool-call status bubble
        self._last_typing = 0.0                      # monotonic ts of last "typing…" chat action
        self._typing_count = 0                       # diagnostics: typing actions in current turn
        self._turn_started = 0.0                     # diagnostics: monotonic ts of turn start
        self._max_gap = 0.0                          # diagnostics: largest gap between typing actions
        self._last_pane_warning = 0.0                # throttle unsafe-pane owner alerts
        # Persist the bubble's message_id so a restart/crash mid-turn can delete the orphan it
        # would otherwise leave behind in the chat.
        self._status_path = (self._signal.parent / "status_bubble") if self._signal else None
        self._seen_tools: set = set()
        self._tui_seen: set = set()          # Codex TUI scrape: tool lines already shown this turn
        self._turn_text_sent = False         # has any text been forwarded this turn (bubble gate)

    # ---- transcript resolution --------------------------------------------
    def _codex_sessions_dir(self) -> Path:
        tp = (self.cfg.transcript_path or "").strip()
        if tp and tp.lower() != "auto":
            p = Path(tp).expanduser()
            if p.is_dir():
                return p
        return Path.home() / ".codex" / "sessions"

    @staticmethod
    def _newest_under(base: Path, *patterns: str) -> Path | None:
        files: list[str] = []
        for pat in (patterns or ("*.jsonl",)):
            files = glob.glob(str(base / "**" / pat), recursive=True)
            if files:
                break
        if not files:
            return None
        try:
            return Path(max(files, key=lambda f: Path(f).stat().st_mtime))
        except OSError:
            return None

    def _session_cwd(self) -> str | None:
        """Working directory of the driven tmux session — used to pick the matching Codex
        rollout even when other `codex` processes (e.g. cron jobs) write newer rollouts."""
        try:
            out = subprocess.run(
                ["tmux", "display-message", "-p", "-t", self.cfg.tmux_session, "#{pane_current_path}"],
                capture_output=True, text=True, timeout=5)
            return out.stdout.strip() or None
        except (subprocess.SubprocessError, OSError):
            return None

    @staticmethod
    def _rollout_cwd(path: Path) -> str | None:
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                rec = json.loads(f.readline() or "{}")
            return (rec.get("payload") or {}).get("cwd")
        except (OSError, json.JSONDecodeError, ValueError):
            return None

    @staticmethod
    def _norm(p: str | None) -> str:
        """Normalize a path for comparison — resolves symlinks like /tmp → /private/tmp (macOS)."""
        if not p:
            return ""
        try:
            return str(Path(p).resolve())
        except (OSError, RuntimeError):
            return p

    def _cwd_matches(self, rollout: Path) -> bool:
        sc = self._session_cwd()
        return bool(sc) and self._norm(self._rollout_cwd(rollout)) == self._norm(sc)

    def _newest_rollout(self) -> Path | None:
        base = self._codex_sessions_dir()
        files = (glob.glob(str(base / "**" / "rollout-*.jsonl"), recursive=True)
                 or glob.glob(str(base / "**" / "*.jsonl"), recursive=True))
        if not files:
            return None
        try:
            files.sort(key=lambda f: Path(f).stat().st_mtime, reverse=True)
        except OSError:
            return None
        cwd = self._norm(self._session_cwd())
        if cwd:
            for f in files:                       # newest first → our session's own rollout
                if self._norm(self._rollout_cwd(Path(f))) == cwd:
                    return Path(f)
            return None                           # cwd known but no rollout yet → wait, don't grab
        return Path(files[0])                     # cwd unknown → best-effort newest overall

    def _resolve_transcript(self) -> Path | None:
        """Resolve the transcript to tail. An explicit path is used as-is; ``""``/``"auto"``
        auto-detects the newest transcript for the agent (Codex rollout / Claude Code session)."""
        tp = (self.cfg.transcript_path or "").strip()
        if tp and tp.lower() != "auto":
            p = Path(tp).expanduser()
            return self._newest_under(p) if p.is_dir() else p
        if self.cfg.agent == "codex":
            return self._newest_rollout()
        if self.cfg.agent == "claude-code":
            return self._newest_claude()
        return None

    def _newest_claude(self) -> Path | None:
        """Newest Claude Code transcript for the driven session, scoped by cwd so it never picks
        up another concurrent Claude session (Claude stores transcripts under a per-cwd project
        dir: ``~/.claude/projects/<cwd-with-slashes-as-dashes>/``)."""
        base = Path.home() / ".claude" / "projects"
        cwd = self._session_cwd()
        dirs: list[Path] = []
        if cwd:
            for c in {cwd, self._norm(cwd)}:
                d = base / c.replace("/", "-")
                if d.is_dir():
                    dirs.append(d)
        if not dirs:
            return self._newest_under(base) if not cwd else None
        best, best_m = None, -1.0
        for d in dirs:
            p = self._newest_under(d, "*.jsonl")
            try:
                if p and p.stat().st_mtime > best_m:
                    best, best_m = p, p.stat().st_mtime
            except OSError:
                pass
        return best

    def _maybe_reresolve(self) -> None:
        """Keep the tailed transcript pointed at our tmux session's own log (auto mode only).

        Agents write the transcript on the first message (not at launch), and a session restart
        starts a new one — so we re-check periodically. We switch when a better match appears, but
        never abandon a transcript we're already on for an in-flight turn. A no-op when the config
        gives an explicit transcript path (the path resolves to itself)."""
        if (self.cfg.transcript_path or "").strip().lower() not in ("", "auto"):
            return                                # explicit path → nothing to re-resolve
        now = time.monotonic()
        if now - self._last_resolve < 3.0:
            return
        self._last_resolve = now
        newest = self._resolve_transcript()
        if not newest or newest == self._transcript:
            return
        # cwd-scoped resolution returns OUR session's own log, so follow it even mid-turn: a new
        # rollout for the same session means the current turn is being written THERE. Blocking the
        # switch while a turn was active caused a ~90s lag (it only switched after the idle timeout)
        # — the first message looked like it took ~2 minutes. Only the cwd-unknown best-effort
        # fallback still avoids jumping away during a live turn.
        if self._session_cwd() is None and self._transcript is not None and self._turn_active.is_set():
            return
        log.info("transcript → %s", newest.name)
        self._transcript = newest
        self._tpos = 0
        self._resume_position()

    # ---- lifecycle ---------------------------------------------------------
    def run(self) -> None:
        # Zámek na state dir. Dvě instance nad jedním botem se perou o getUpdates (409),
        # obě mohou injektovat tentýž update a obě drainovat stejnou frontu – zprávy pak
        # mizí i duplikují. Zámek byl v compat.py napsaný a otestovaný, ale nikdy se
        # nevolal (křížová recenze: Sol #3, Fable F3), takže ta ochrana byla jen na papíře.
        with self._instance_lock():
            self._run_locked()

    @contextmanager
    def _instance_lock(self):
        cesta = getattr(self, "_offset_file", None)
        if cesta is None:
            yield
            return
        try:
            with single_instance_lock(Path(cesta).parent / "bridge.lock"):
                yield
        except AlreadyRunning:
            raise RuntimeError(
                f"Nad stavem {Path(cesta).parent} už běží jiná instance bridge. "
                "Dvě naráz si přetahují zprávy – ukonči tu druhou, nebo dej každé "
                "vlastní AGENT2TELEGRAM_STATE."
            ) from None

    def _run_locked(self) -> None:
        me = self.tg.get_me()
        log.info("Attach bridge live as @%s → tmux '%s', owner=%s",
                 me.get("username"), self.cfg.tmux_session, self._owner_chat)
        self.tg.set_my_commands(BOT_COMMANDS)    # enable the "/" command menu in Telegram
        if not self._session.alive:
            raise RuntimeError(f"tmux session '{self.cfg.tmux_session}' not found")
        # Start tailing at EOF. If we've run before (the ledger has entries), rewind to the start
        # of the current turn so a reply written while we were restarting still gets forwarded —
        # the ledger dedups, so nothing already delivered is re-sent. On the very first run we do
        # NOT rewind, so attaching to an already-busy session never re-posts its prior turn.
        if self._transcript and self._transcript.exists():
            self._tpos = self._transcript.stat().st_size
            if self._sent_keys:
                self._resume_position()
        self._cleanup_orphan_status()       # remove a bubble orphaned by a prior crash/restart
        # Typing runs in its own thread so a flood-control sleep in the send path never starves it.
        threading.Thread(target=self._outbound_loop, daemon=True).start()
        threading.Thread(target=self._typing_loop, daemon=True).start()
        if self.cfg.agent == "codex":
            # Codex logs tools to the rollout only at completion → scrape the TUI for LIVE bubbles.
            threading.Thread(target=self._tui_scrape_loop, daemon=True).start()
        self._inbound_loop()

    def _resume_position(self) -> None:
        """Find the most recent non-empty user message and rewind ``_tpos`` to just after it,
        so the current turn's assistant messages are re-read on startup. Combined with the
        persisted ledger this re-delivers a reply that was written while we were down, without
        re-sending anything already delivered. Also recovers the turn's Telegram origin."""
        size = self._tpos
        start = max(0, size - 5_000_000)        # large window: tool outputs can be big
        try:
            with open(self._transcript, "rb") as f:
                f.seek(start)
                tail = f.read()
        except OSError:
            return
        pos = start
        last_user_end = None
        from_tg = self._turn_from_tg
        for raw in tail.split(b"\n"):
            line_end = pos + len(raw) + 1       # +1 for the newline separator
            pos = line_end
            try:
                rec = json.loads(raw.decode("utf-8", "ignore"))
            except (json.JSONDecodeError, ValueError):
                continue
            utext = self._reader.user_text(rec)
            if utext and utext.strip():
                from_tg = utext.lstrip().startswith(self._origins)
                last_user_end = min(line_end, size)
        if last_user_end is not None:
            self._tpos = last_user_end
            self._turn_from_tg = from_tg

    def _mark_sent(self, uuid: str) -> None:
        """Record a forwarded message uuid in memory and on disk (append-only ledger)."""
        if not uuid or uuid in self._sent_keys:
            return
        self._sent_keys.add(uuid)
        try:
            self._sent_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self._sent_path, "a", encoding="utf-8") as f:
                f.write(uuid + "\n")
        except OSError:
            pass

    # ---- inbound update persistence -----------------------------------------
    def _init_update_state(self) -> None:
        self._offset_file = _state_dir(self.cfg) / "offset"
        self._processed_updates_file = _state_dir(self.cfg) / "processed_updates"
        self._processed_update_ids, self._processed_update_order = self._read_processed_updates()

    def _ensure_update_state(self) -> None:
        # StreamBridge reuses this inbound loop but builds its state without calling our __init__.
        if not hasattr(self, "_offset_file"):
            self._offset_file = _state_dir(self.cfg) / "offset"
        if not hasattr(self, "_processed_updates_file"):
            self._processed_updates_file = _state_dir(self.cfg) / "processed_updates"
        if not hasattr(self, "_processed_update_ids") or not hasattr(self, "_processed_update_order"):
            self._processed_update_ids, self._processed_update_order = self._read_processed_updates()
        self._ensure_inbox()

    def _ensure_inbox(self) -> DurableInbox | None:
        """Durable inbox nad TÍMŽ adresářem, kde leží offset – ať se v testech i v provozu
        stav drží pohromadě a nezáleží na tom, kdo objekt složil."""
        inbox = getattr(self, "_inbox", None)
        if inbox is None:
            try:
                inbox = DurableInbox(self._offset_file.parent)
            except Exception as e:                     # nesmí shodit příjem zpráv
                log.error("durable inbox se nepodařilo otevřít: %s", e)
                inbox = None
            self._inbox = inbox
        return inbox

    def _load_offset(self) -> int:
        self._ensure_update_state()
        try:
            return int(json.loads(self._offset_file.read_text("utf-8"))["offset"])
        except Exception:
            return 0

    def _save_offset(self, offset: int) -> None:
        self._ensure_update_state()
        try:
            self._offset_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._offset_file.parent / (self._offset_file.name + ".tmp")
            tmp.write_text(json.dumps({"offset": int(offset)}), encoding="utf-8")
            tmp.replace(self._offset_file)
        except OSError as e:
            log.warning("could not persist attach offset: %s", e)

    def _read_processed_updates(self) -> tuple[set[int], deque[int]]:
        ids: set[int] = set()
        order: deque[int] = deque()
        try:
            raw = json.loads(self._processed_updates_file.read_text("utf-8")).get("update_ids", [])
        except Exception:
            raw = []
        for value in raw[-PROCESSED_UPDATE_LEDGER_LIMIT:]:
            try:
                update_id = int(value)
            except (TypeError, ValueError):
                continue
            if update_id not in ids:
                ids.add(update_id)
                order.append(update_id)
        return ids, order

    def _persist_processed_updates(self) -> None:
        self._ensure_update_state()
        try:
            self._processed_updates_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._processed_updates_file.parent / (self._processed_updates_file.name + ".tmp")
            data = {"update_ids": list(self._processed_update_order)}
            tmp.write_text(json.dumps(data), encoding="utf-8")
            tmp.replace(self._processed_updates_file)
        except OSError as e:
            log.warning("could not persist processed updates: %s", e)

    def _mark_update_processed(self, update_id: int | None) -> None:
        if update_id is None:
            return
        self._ensure_update_state()
        try:
            update_id = int(update_id)
        except (TypeError, ValueError):
            return
        if update_id in self._processed_update_ids:
            return
        self._processed_update_ids.add(update_id)
        self._processed_update_order.append(update_id)
        while len(self._processed_update_order) > PROCESSED_UPDATE_LEDGER_LIMIT:
            old = self._processed_update_order.popleft()
            self._processed_update_ids.discard(old)
        self._persist_processed_updates()

    def _update_was_processed(self, update_id: int | None) -> bool:
        if update_id is None:
            return False
        self._ensure_update_state()
        try:
            return int(update_id) in self._processed_update_ids
        except (TypeError, ValueError):
            return False

    def _handle_update_once(self, upd: dict, offset: int) -> int:
        raw_id = upd.get("update_id")
        try:
            update_id = int(raw_id)
        except (TypeError, ValueError):
            log.warning("skipping malformed Telegram update without a valid update_id: %r", upd)
            return offset
        next_offset = max(offset, update_id + 1) if update_id is not None else offset
        if update_id is not None and update_id < offset:
            return offset
        if self._update_was_processed(update_id):
            self._save_offset(next_offset)
            return next_offset

        # POŘADÍ JE KRITICKÉ. Telegram bere update za vyřízený, jakmile si řekneme o vyšší
        # offset – znovu ho nepošle nikdy. Dřív se offset posunul dřív, než zpráva doputovala
        # kamkoli trvalého (sedla si jen do fronty v paměti), takže pád v tom okně ji smazal
        # ze světa. Monitor bridge 30. 7. zabíjel 4× denně, takže to nebyla teorie.
        #
        # Nově: nejdřív zápis na disk, teprve pak ACK. Když zápis selže, offset se NEPOSUNE
        # a Telegram nám zprávu pošle znovu – radši dvakrát než nikdy.
        inbox = self._ensure_inbox()
        if inbox is None:
            # Bez trvalého úložiště se offset posunout NESMÍ. Dřív se v tomhle případě
            # pokračovalo po staré ztrátové cestě – tedy přesně tehdy, když je disk plný
            # nebo poškozený a durabilita je nejpotřebnější (recenze Sol #2).
            log.error("update %s: trvalé úložiště není k dispozici, offset neposouvám", update_id)
            return offset
        try:
            record_id = inbox.reserve(upd)
        except Exception as e:
            log.error("update %s se nepodařilo uložit, offset neposouvám: %s", update_id, e)
            return offset
        self._save_offset(next_offset)
        self._submit_inbound_update(upd, record_id)
        self._mark_update_processed(update_id)
        return next_offset

    # ---- durable outbound delivery (never drop a reply) --------------------
    def _load_queue(self) -> list:
        if self._queue_path is None or not self._queue_path.exists():
            return []
        out = []
        try:
            for line in self._queue_path.read_text("utf-8").splitlines():
                line = line.strip()
                if line:
                    out.append(json.loads(line))
        except (OSError, ValueError):
            return []
        return out

    def _persist_queue(self) -> None:
        if self._queue_path is None:
            return
        try:
            self._queue_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._queue_path.parent / (self._queue_path.name + ".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                for item in self._pending_send:
                    f.write(json.dumps(item, ensure_ascii=False) + "\n")
            tmp.replace(self._queue_path)          # atomic, no os import needed
        except OSError:
            pass

    def _enqueue(self, text: str, key: str | None, *, turn_text: bool = True) -> None:
        item = {"text": text, "key": key}
        if not turn_text:
            item["turn_text"] = False
        self._pending_send.append(item)
        self._persist_queue()

    def _send_final(self, text: str, key: str | None = None, *, turn_text: bool = True) -> None:
        """Forward one reply RELIABLY. Marks the dedup ledger only AFTER a confirmed send; on any
        send failure the reply is queued to disk and the outbound loop keeps retrying until Telegram
        confirms. Order is preserved: if anything is already queued, this appends behind it."""
        if self._owner_chat is None or (not text and not getattr(self, "_pending_files", None)):
            return
        if key and key in self._sent_keys:
            return
        # `[tg-file] <path>` lines are attachments, not content — pull them out, send the
        # text first, then upload. Without this the agent had to bypass the bridge with a
        # raw curl, which is exactly what we do not want (markdown broke, [tg] leaked).
        if self.cfg.file_marker and self.cfg.file_marker.lower() in text.lower():
            text, files = self._extract_files(text)
            if files:
                self._pending_files = getattr(self, "_pending_files", []) + files
            if not text and self._ensure_outbox() is None:
                # Jen bez durable fronty se jde starou cestou. Dřív tudy propadla KAŽDÁ
                # odpověď složená jen z přílohy: _flush_files smaže cestu z paměti ještě
                # před uploadem, takže selhání sítě přílohu ztratilo (recenze Sol #6).
                self._flush_files()
                return
        # Durable outbox eviduje KAŽDOU ČÁST zvlášť. Dřív se při selhání vracel do fronty
        # celý text, takže už doručené části dorazily podruhé – Petr to vídal jako zdvojené
        # úvodní odstavce dlouhých odpovědí (nález D; Sol i Fable ho našli nezávisle).
        # Přílohy do fronty nešly vůbec a po restartu mizely (nález F).
        outbox = self._ensure_outbox()
        if outbox is not None:
            chunks = split_message(text) if text else []
            soubory = list(getattr(self, "_pending_files", []) or [])
            try:
                rid = outbox.enqueue(chunks, soubory, key)
            except Exception as e:
                log.error("odchozí zprávu se nepodařilo uložit, jedu starou cestou: %s", e)
            else:
                self._pending_files = []
                if turn_text:
                    self._outbox_turn_text.add(rid)
                return

        if self._pending_send:                       # something already waiting → keep FIFO order
            self._enqueue(text, key, turn_text=turn_text)
            return
        try:
            self.tg.send_message(self._owner_chat, text)
        except Exception as e:                        # network reset, 5xx after retries, etc.
            log.warning("forward failed → queued for re-delivery: %s", e)
            self._enqueue(text, key, turn_text=turn_text)
            return
        if key:
            self._mark_sent(key)
        if turn_text:
            self._turn_text_sent = True
        log.info("FWD (send) %r", text[:30])
        self._flush_files()

    def _ensure_outbox(self) -> "DurableOutbox | None":
        # Jen attach mód. StreamBridge tenhle kód sdílí, ale má vlastní frontu vedle configů
        # a jiný životní cyklus – přepínat ho beze změny zadání by byla nevyžádaná změna
        # chování (a jeho test to správně odhalil).
        if not getattr(self, "_use_durable_outbox", False):
            return None
        outbox = getattr(self, "_outbox", None)
        if outbox is None and getattr(self, "_queue_path", None) is not None:
            try:
                outbox = DurableOutbox(Path(self._queue_path).parent)
            except Exception as e:                     # doručování se nesmí zastavit
                log.error("durable outbox se nepodařilo otevřít: %s", e)
                outbox = None
            self._outbox = outbox
            if not hasattr(self, "_outbox_turn_text"):
                self._outbox_turn_text = set()
        return outbox

    def _flush_pending(self) -> None:
        """Doručí frontu FIFO a zastaví se na první chybě, aby se zachovalo pořadí.
        Volá se každý odchozí cyklus, takže se výpadek sítě sám zahojí do ~0,4 s."""
        outbox = self._ensure_outbox()
        while outbox is not None and self._owner_chat is not None:
            rec = outbox.head()
            if rec is None:
                break
            # Části už potvrzené Telegramem se NEPOSÍLAJÍ znovu – jen ty zbývající.
            for index, chunk in rec.pending_chunks:
                try:
                    self.tg.send_message(self._owner_chat, chunk)
                except Exception as e:
                    log.warning("doručení části %d stále selhává: %s", index, e)
                    return
                outbox.mark_chunk_sent(rec.record_id, index)
            vzdano = False
            for cesta in rec.pending_files:
                try:
                    self._send_one_file(cesta)
                except Exception as e:
                    # Omezené opakování: bez něj by jedna vadná příloha (soubor nad limit,
                    # smazaný soubor, HTTP 400) držela hlavu FIFO fronty navždy a Petrovi
                    # by nedorazila ŽÁDNÁ další odpověď (Fable F2).
                    pokusu = outbox.fail(rec.record_id, f"{cesta}: {e}")
                    log.warning("doručení přílohy %s selhalo (%d/%d): %s",
                                cesta, pokusu, OUTBOX_MAX_ATTEMPTS, e)
                    if pokusu >= OUTBOX_MAX_ATTEMPTS:
                        # NESMÍ se použít mark_file_sent: to by do vlastní evidence zapsalo,
                        # že příloha dorazila, ačkoli nedorazila – tedy tichá ztráta, přesně
                        # ta, kterou tenhle projekt odstraňuje. Původní podoba téhle opravy
                        # to dělala; našel to Sol ve třetím kole revize.
                        # Záznam jde celý do dead-letter: z fronty zmizí (neucpe ji), ale
                        # zůstane dohledatelný i s tím, které části se doručit stihly.
                        self._ohlas_trvale_odmitnuti(Path(cesta).name, str(e))
                        outbox.give_up(rec.record_id, f"příloha {cesta}: {e}")
                        log.error("příloha %s se vzdala po %d pokusech → dead-letter",
                                  cesta, pokusu)
                        vzdano = True
                        break
                    return
                outbox.mark_file_sent(rec.record_id, cesta)
            if vzdano:
                continue          # záznam je v dead-letter, fronta jede dál
            if rec.key:
                self._mark_sent(rec.key)
            outbox.done(rec.record_id)
            if rec.record_id in getattr(self, "_outbox_turn_text", set()):
                self._turn_text_sent = True
                self._outbox_turn_text.discard(rec.record_id)
            else:
                self._turn_text_sent = True
            log.info("FWD (doručeno) %d částí, %d příloh",
                     len(rec.chunks), len(rec.files))

        while self._pending_send and self._owner_chat is not None:
            item = self._pending_send[0]
            try:
                self.tg.send_message(self._owner_chat, item.get("text", ""))
            except Exception as e:
                log.warning("re-delivery still failing (%d queued): %s", len(self._pending_send), e)
                return
            self._pending_send.pop(0)
            self._persist_queue()
            if item.get("key"):
                self._mark_sent(item["key"])
            if item.get("turn_text", True):
                self._turn_text_sent = True
            log.info("FWD (re-delivered) %r", str(item.get("text", ""))[:30])

    # ---- inbound (Telegram → session) -------------------------------------
    def _init_inbound_worker_state(self) -> None:
        self._inbound_queue = queue.Queue()
        self._inbound_worker_started = False
        self._inbound_worker_lock = threading.Lock()

    def _ensure_inbound_worker_state(self) -> None:
        # StreamBridge and focused tests may construct the object without AttachBridge.__init__.
        if not hasattr(self, "_inbound_queue"):
            self._inbound_queue = queue.Queue()
        if not hasattr(self, "_inbound_worker_started"):
            self._inbound_worker_started = False
        if not hasattr(self, "_inbound_worker_lock"):
            self._inbound_worker_lock = threading.Lock()

    def _start_inbound_worker(self) -> None:
        self._ensure_inbound_worker_state()
        with self._inbound_worker_lock:
            if self._inbound_worker_started:
                return
            threading.Thread(target=self._inbound_worker_loop, daemon=True).start()
            self._inbound_worker_started = True

    def _submit_inbound_update(self, upd: dict, record_id: str | None = None) -> None:
        self._start_inbound_worker()
        if record_id:
            self._inbound_inflight().add(record_id)
        self._inbound_queue.put((upd, record_id))

    def _inbound_inflight(self) -> set:
        """Záznamy právě rozpracované ve frontě v paměti. Bez téhle evidence by
        opakovaný pokus zařadil tutéž zprávu podruhé a Petr by ji dostal dvakrát."""
        if not hasattr(self, "_inflight_ids"):
            self._inflight_ids = set()
        return self._inflight_ids

    def _replay_pending_inbound(self) -> int:
        """Po startu doručí zprávy, které zbyly v trvalém úložišti.

        Bez tohohle byla durabilita jen poloviční: zpráva se uložila na disk, ale nikdo
        ji odtamtud nikdy nevzal – Telegram ji už znovu nepošle, takže by tam ležela až
        do vypršení retence. Našli Sol i Fable nezávisle při křížové recenzi.
        """
        inbox = self._ensure_inbox()
        if inbox is None:
            return 0
        try:
            cekajici = inbox.pending()
        except Exception as e:
            log.error("nepodařilo se přečíst zbylé příchozí zprávy: %s", e)
            return 0
        rozpracovane = self._inbound_inflight()
        cekajici = [r for r in cekajici if r.record_id not in rozpracovane]
        for rec in cekajici:
            self._submit_inbound_update(rec.update, rec.record_id)
        if cekajici:
            log.info("po startu doručuji %d zprávu/y, které zbyly z minula", len(cekajici))
        return len(cekajici)

    def _inbound_worker_loop(self) -> None:
        """Process accepted Telegram updates FIFO outside the long-poll thread.

        Media download and STT retries can take minutes; keeping them here prevents one slow voice
        note from blocking getUpdates while preserving message order for the owner.
        """
        while not self._stop.is_set() or not self._inbound_queue.empty():
            try:
                polozka = self._inbound_queue.get(timeout=0.2)
            except queue.Empty:
                continue
            upd, record_id = polozka if isinstance(polozka, tuple) else (polozka, None)
            try:
                doruceno = self._handle(upd)
            except Exception as e:
                log.exception("inbound worker error: %s", e)
                self._inbound_failed(record_id, str(e))
            else:
                # `_handle` vrací False, když se zpráva nedostala do session. Dřív se v obou
                # případech jen zavolalo task_done a zpráva zmizela – to je nález B z auditu.
                if doruceno is False:
                    self._inbound_failed(record_id, "nedoručeno do session")
                else:
                    self._inbound_done(record_id)
            finally:
                self._inbound_queue.task_done()

    def _inbound_done(self, record_id: str | None) -> None:
        self._inbound_inflight().discard(record_id)
        inbox = getattr(self, "_inbox", None)
        if record_id and inbox is not None:
            try:
                inbox.done(record_id)
            except Exception as e:
                log.warning("úklid doručeného záznamu %s selhal: %s", record_id, e)

    def _inbound_failed(self, record_id: str | None, duvod: str) -> None:
        """Nedoručeno → záznam zůstává a zkusí se znovu. Po vyčerpání pokusů jde do
        dead-letter, ať zpráva nezmizí bez stopy ani v beznadějném případě."""
        self._inbound_inflight().discard(record_id)
        inbox = getattr(self, "_inbox", None)
        if not record_id or inbox is None:
            return
        try:
            pokusu = inbox.fail(record_id, duvod)
            if pokusu >= INBOUND_MAX_ATTEMPTS:
                inbox.give_up(record_id, f"{duvod} (po {pokusu} pokusech)")
                log.error("update %s se nepodařilo doručit ani na %d. pokus → dead-letter",
                          record_id, pokusu)
        except Exception as e:
            log.warning("označení nedoručeného záznamu %s selhalo: %s", record_id, e)

    def _inbound_loop(self) -> None:
        self._start_inbound_worker()
        self._replay_pending_inbound()   # nejdřív dluh z minula, pak nové zprávy
        offset = self._load_offset()
        allowed_updates = json.dumps(["message", "edited_message", "message_reaction"])
        transient_fails = 0
        outage_alerted = False
        while not self._stop.is_set():
            try:
                updates = self.tg._call(
                    "getUpdates",
                    {"offset": offset, "timeout": self.cfg.poll_timeout,
                     "allowed_updates": allowed_updates},
                    timeout=self.cfg.poll_timeout + 15,
                )
            except (TelegramError, OSError) as e:
                if is_network_error(e):
                    # Přechodný/výpadkový síťový/DNS problém (Errno 8 "nodename nor servname",
                    # connection reset, timeout) – bridge se sám zotaví. WARNING + backoff;
                    # ERROR jen JEDNOU při delším výpadku, ať monitoring nealertuje na
                    # sebe-zotavující se VPN-DNS blip.
                    transient_fails += 1
                    if transient_fails >= 10 and not outage_alerted:
                        log.error("getUpdates network výpadek (%d× v řadě) trvá déle: %s",
                                  transient_fails, e)
                        outage_alerted = True
                    else:
                        log.warning("getUpdates přechodná síťová chyba (%d): %s", transient_fails, e)
                    self._stop.wait(min(3 * transient_fails, 30))
                    continue
                log.error("getUpdates failed: %s", e)   # skutečná (ne-síťová) chyba
                self._stop.wait(3)
                continue
            if outage_alerted:
                log.info("getUpdates síť obnovena po %d chybách", transient_fails)
            transient_fails = 0
            outage_alerted = False
            for upd in updates:
                try:
                    offset = self._handle_update_once(upd, offset)
                except Exception as e:
                    log.exception("inbound error: %s", e)

    def _handle(self, upd: dict) -> bool:
        """Zpracuje jeden update. Vrací False JEN když se zpráva nedoručila do session.

        Návratová hodnota řídí durable inbox: True = vyřízeno, záznam smí zmizet;
        False = nedoručeno, záznam zůstává a zkusí se znovu. Proto všechny větve typu
        „není co dělat" vracejí True – opakovat je nemá smysl. False chodí výhradně
        ze selhaného zápisu do tmuxu, tedy přesně z toho, co opakování léčí.
        """
        # Reactions (e.g. ❤️) → quick-feedback line.
        mr = upd.get("message_reaction")
        if mr:
            if mr.get("user", {}).get("id") not in self._allowed:
                return True
            emojis = "".join(r.get("emoji", "") for r in mr.get("new_reaction", [])
                             if r.get("type") == "emoji")
            if emojis:
                self._begin_turn()
                return self._inject(
                    f"{emojis} reacted {emojis} to your message #{mr.get('message_id')} "
                    f"— quick feedback; no need to reply unless relevant.")
            return True

        msg = upd.get("message") or upd.get("edited_message")
        if not msg:
            return True
        user_id = msg.get("from", {}).get("id")
        chat_id = msg["chat"]["id"]
        if user_id not in self._allowed:
            self.tg.send_message(chat_id, "⛔ Not authorized.")
            return True

        # Bridge-level slash commands (e.g. /start, /help) are answered here instead of being
        # forwarded to the agent — so the first contact is a friendly intro, not the agent
        # puzzling over "/start". Only plain-text commands, never media captions.
        text0 = (msg.get("text") or "").strip()
        if text0.startswith("/") and not (msg.get("voice") or msg.get("audio")
                                           or msg.get("photo") or msg.get("document")):
            if self._handle_command(text0, chat_id, msg.get("message_id")):
                return True

        text = (msg.get("text") or msg.get("caption") or "").strip()
        if msg.get("voice") or msg.get("audio"):
            # Přepis i stahování si řeší vlastní opakování a uživateli samy hlásí chybu,
            # takže se sem nevracíme – opakování zvenčí by jen znovu platilo za přepis.
            text = self._transcribe(msg.get("voice") or msg.get("audio"), chat_id) or text
            if not text:
                return True
        elif msg.get("photo") or msg.get("document"):
            note = self._download_note(msg, chat_id)
            if not note:
                return True
            text = f"{text}\n{note}".strip()
        if text:
            text = self._limit_inbound_prompt(text, chat_id)
            if not text:
                return True
            self._begin_turn()
            return self._inject(text)
        return True

    def _begin_turn(self) -> None:
        # Light "typing…" from the actual injection point.
        self._consume_turn_end()                 # drop any stale end-marker from a prior turn
        now = time.monotonic()
        self._turn_active.set()
        self._turn_from_tg = True
        self._last_activity = now
        self._turn_started = now
        self._typing_count = 1
        self._max_gap = 0.0
        self._last_typing = now
        self._turn_text_sent = False             # gate TUI bubbles until intro text lands
        # Seed the TUI dedup with tool lines ALREADY on screen from previous turns, so the
        # scraper only emits calls that appear DURING this turn — otherwise stale lines still
        # visible in the pane get re-sent as bubbles under the new turn.
        if self.cfg.agent == "codex" and hasattr(self, "_session"):
            try:
                self._tui_seen = set(_extract_tui_tools(self._session._capture()))
            except Exception:
                self._tui_seen = set()
        else:
            self._tui_seen = set()
        self.tg.send_chat_action(self._owner_chat, "typing")   # instant, don't wait for the loop
        log.info("TURN START t=%.2f", time.time())

    def _inject(self, text: str) -> bool:
        """Doručí zprávu do session. Selhání NIKDY nesmí být tiché.

        Dřív se selhaný zápis do tmuxu jen zalogoval a zpráva zmizela – uživatel se
        nedozvěděl nic. Doloženo z provozu 31. 7.:
            19:04:41 ERROR inject failed: '[TG] Jsi tam tedy?' timed out after 10 seconds
        Petr tu zprávu poslal a odpovědi se nedočkal; nikde po ní nezůstala stopa.

        Zápis do tmuxu selhává i přechodně (zaneprázdněné okno, plný buffer), proto se
        opakuje. Když ani opakování nepomůže, uživatel se to musí dozvědět.
        """
        self._turn_active.set()
        self._last_activity = time.monotonic()   # keep typing lit from the very start
        posledni_chyba: Exception | None = None
        for pokus in range(1, INJECT_ATTEMPTS + 1):
            try:
                self._session.inject(text)
                if pokus > 1:
                    log.info("inject prošel až na %d. pokus", pokus)
                return True
            except SessionError as e:
                posledni_chyba = e
                if "refusing to inject" in str(e):
                    # Pane neběží očekávaný agent – opakování nepomůže, jen by zdrželo.
                    log.error("inject failed: %s", e)
                    self._turn_active.clear()
                    self._notify_unsafe_pane(str(e))
                    return False
            except Exception as e:
                posledni_chyba = e
            if pokus < INJECT_ATTEMPTS:
                self._stop.wait(INJECT_RETRY_WAIT)
        log.error("inject failed po %d pokusech: %s", INJECT_ATTEMPTS, posledni_chyba)
        self._turn_active.clear()
        self._notify_inject_failed(text, str(posledni_chyba))
        return False

    def _notify_inject_failed(self, text: str, reason: str) -> None:
        """Řekne majiteli, že jeho zpráva nedošla – radši dvakrát než nikdy."""
        if not self._owner_chat:
            return
        nahled = text.strip().replace("\n", " ")
        if len(nahled) > 80:
            nahled = nahled[:80] + "…"
        try:
            self.tg.send_message(
                self._owner_chat,
                "⚠️ Couldn't deliver your message to the agent session — the write to tmux "
                f"failed after {INJECT_ATTEMPTS} attempts.\n\n"
                f"Message: {nahled}\n{reason}\n\n"
                "The agent may be frozen. Check the tmux pane, then send it again.",
            )
        except Exception as e:
            log.warning("inject-failure notification failed: %s", e)

    def _notify_unsafe_pane(self, reason: str) -> None:
        if not self._owner_chat:
            return
        now = time.monotonic()
        if now - self._last_pane_warning < 60:
            return
        self._last_pane_warning = now
        try:
            self.tg.send_message(
                self._owner_chat,
                "⚠️ Agent2Telegram blocked a Telegram message because the configured tmux "
                f"session no longer appears to be running the expected agent.\n\n{reason}\n\n"
                "Restart the agent in that tmux pane, then resend the message.",
            )
        except Exception as e:
            log.warning("unsafe-pane owner notification failed: %s", e)

    def _handle_command(self, text: str, chat_id: int, message_id: int | None = None) -> bool:
        """Answer a bridge-level slash command. Returns True if handled (don't forward to agent)."""
        parts = text.split(maxsplit=1)
        cmd = parts[0].lstrip("/").split("@")[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""
        labels = {"codex": "Codex", "claude-code": "Claude Code"}
        agent = labels.get(self.cfg.agent, self.cfg.agent)
        if cmd in ("start", "help"):
            voice = "on" if self.cfg.elevenlabs_api_key else "off — enable with /setkey"
            self.tg.send_message(chat_id,
                f"👋 You're connected to a live *{agent}* session via Agent2Telegram.\n\n"
                "Just send a message — it goes straight to the agent and you'll see typing, live "
                "progress, what tools it runs, and the reply. You can also send *photos* and "
                "*files*, and react with ❤️ as quick feedback.\n\n"
                f"🎤 Voice transcription: {voice}.\n\n"
                "Commands: /help · /status · /id · /setkey")
            return True
        if cmd == "id":
            self.tg.send_message(chat_id, f"Your Telegram id: `{chat_id}`")
            return True
        if cmd == "status":
            voice = "✓" if self.cfg.elevenlabs_api_key else "✗"
            self.tg.send_message(chat_id,
                f"✅ Connected — *{agent}* in tmux session `{self.cfg.tmux_session}`.\n"
                f"🎤 Voice (ElevenLabs): {voice}")
            return True
        if cmd == "setkey":
            return self._set_voice_key(arg, chat_id, message_id)
        return False    # unknown command → let the agent handle it

    def _set_voice_key(self, key: str, chat_id: int, message_id: int | None) -> bool:
        """Save an ElevenLabs key to enable voice, then delete the message so the secret isn't
        left in the chat history."""
        if not key:
            self.tg.send_message(chat_id,
                "Usage: `/setkey <your ElevenLabs API key>` — enables voice-message transcription.\n"
                "I'll delete your message right after so the key isn't left in the chat.")
            return True
        self.cfg.elevenlabs_api_key = key
        try:
            from .config import mark_secret_from_file, save
            mark_secret_from_file(self.cfg, "elevenlabs_api_key")
            save(self.cfg)                       # persisted 0600 to the active config path
        except Exception as e:
            log.error("setkey: could not persist config: %s", e)
        if message_id is not None:
            self.tg.delete_message(chat_id, message_id)   # don't leave the secret in history
        self.tg.send_message(chat_id,
            "✅ Voice transcription enabled — key saved. I deleted your message so the key "
            "isn't left in the chat history. Send a voice note to try it.")
        return True

    def _typing_loop(self) -> None:
        """Dedicated thread: assert "typing…" every TYPING_INTERVAL during Telegram turns.

        It runs independently of the outbound/send loop, so a flood-control sleep in the send path
        (which happens during a burst of messages) can never starve the indicator — that was the
        cause of mid-turn typing gaps. It stops the instant the turn ends (turn_active cleared), so
        no action fires after the final message and typing stops with it (bar Telegram's ~5s decay)."""
        while not self._stop.is_set():
            if self._turn_active.is_set() and self._turn_from_tg and self._owner_chat is not None:
                now = time.monotonic()
                gap = now - self._last_typing
                if gap > self._max_gap:
                    self._max_gap = gap
                self.tg.send_chat_action(self._owner_chat, "typing")
                self._last_typing = now
                self._typing_count += 1
            self._stop.wait(TYPING_INTERVAL)

    def _tui_scrape_loop(self) -> None:
        """Codex only: scrape the tmux pane for live tool/web-search lines → status bubbles, so
        Codex (whose rollout logs tools only at completion) shows them live like Claude Code."""
        while not self._stop.is_set():
            if self._turn_active.is_set() and self._turn_from_tg and self._owner_chat is not None:
                # Hold bubbles until the intro text is forwarded (so the bubble doesn't jump ahead
                # of "I'll search the web…"), then release; after a short grace show them anyway.
                ready = self._turn_text_sent or \
                    (time.monotonic() - self._turn_started) >= TUI_BUBBLE_GRACE
                if ready:
                    try:
                        for summary in _extract_tui_tools(self._session._capture()):
                            if summary not in self._tui_seen:
                                self._tui_seen.add(summary)
                                self._status_push(summary)
                    except Exception as e:
                        log.debug("tui scrape: %s", e)
            self._stop.wait(1.0)

    def _consume_turn_end(self) -> None:
        if self._turn_end is not None:
            try:
                self._turn_end.unlink()
            except OSError:
                pass

    def _last_assistant_text(self) -> str | None:
        """The most recent assistant text in the transcript (the turn's final answer). Read-only
        tail scan — used purely by the turn-end backstop, doesn't touch the live _tpos cursor."""
        if not self._transcript:
            return None
        try:
            size = self._transcript.stat().st_size
            with open(self._transcript, "rb") as f:
                f.seek(max(0, size - 2_000_000))
                tail = f.read()
        except OSError:
            return None
        last = None
        for raw in tail.split(b"\n"):
            line = raw.strip()
            if not line:
                continue
            try:
                rec = json.loads(line.decode("utf-8", "ignore"))
            except (ValueError, json.JSONDecodeError):
                continue
            try:
                for ev in self._reader.parse(rec):
                    if ev.kind == "text" and ev.text and ev.text.strip():
                        last = ev.text
            except Exception:
                continue
        return last

    def _wait_backstop_retry(self) -> None:
        stop = getattr(self, "_stop", None)
        if stop is not None:
            stop.wait(BACKSTOP_RETRY_DELAY)
        else:
            time.sleep(BACKSTOP_RETRY_DELAY)

    def _retry_last_assistant_text(self) -> str | None:
        last = self._last_assistant_text()
        if last and last.strip():
            return last
        for attempt in range(BACKSTOP_RETRY_ATTEMPTS):
            if self._turn_text_sent:
                return None
            try:
                self._drain_transcript()
            except Exception as e:
                log.debug("turn-end backstop transcript drain failed: %s", e)
            if self._turn_text_sent:
                return None
            last = self._last_assistant_text()
            if last and last.strip():
                return last
            if attempt != BACKSTOP_RETRY_ATTEMPTS - 1:
                self._wait_backstop_retry()
        return None

    def _consume_signal_text(self) -> str | None:
        signal = getattr(self, "_signal", None)
        if not signal or not signal.exists():
            return None
        try:
            answer = signal.read_text("utf-8").strip()
            signal.unlink()
        except OSError:
            return None
        return answer or None

    def _has_turn_end_backstop_source(self) -> bool:
        return (getattr(self, "_transcript", None) is not None
                or getattr(self, "_signal", None) is not None)

    def _finish_turn(self) -> None:
        """Drop the technical bubble and stop the typing indicator at the real end of a turn."""
        self._status_clear()
        was_active = self._turn_active.is_set()
        # Backstop: a Telegram-originated turn must NEVER go unanswered. If nothing was forwarded
        # this turn (the [tg] marker was forgotten, the turn was a long heads-down working stretch,
        # or interim forwarding missed it), deliver the final assistant message now. The
        # `_turn_text_sent` guard means this only fires when truly nothing was sent (no double-send),
        # and _send_final sets it True so a second _finish_turn won't re-fire.
        if (was_active and self._turn_from_tg and not self._turn_text_sent
                and self._owner_chat is not None
                and getattr(self, "_turn_end_backstop_enabled", True)):
            source = "transcript"
            out = ""
            if self._has_turn_end_backstop_source():
                last = self._retry_last_assistant_text()
                out = self._strip_marker(last) if last else ""
                if not out and not self._turn_text_sent:
                    source = "signal"
                    answer = self._consume_signal_text()
                    out = self._strip_marker(answer) if answer else ""
            if out and not self._turn_text_sent:
                self._send_final(out)
                log.info("TURN END backstop → forwarded final answer from %s %r",
                         source, out[:30])
            elif not self._turn_text_sent:
                dur = time.monotonic() - self._turn_started
                log.error("TURN END backstop: Telegram turn ended without an answer; "
                          "dur=%.1fs typing_count=%d transcript_configured=%s "
                          "signal_configured=%s",
                          dur, self._typing_count, getattr(self, "_transcript", None) is not None,
                          getattr(self, "_signal", None) is not None)
        self._turn_active.clear()
        self._pending_turn_end = False
        self._consume_turn_end()
        if was_active:
            log.info("TURN END t=%.2f dur=%.1fs typing_fired=%d max_gap=%.2fs",
                     time.time(), time.monotonic() - self._turn_started,
                     self._typing_count, self._max_gap)

    def _end_turn(self) -> None:
        # Claude Stop-hook path: catch anything written just before the hook fired, then finish.
        self._drain_transcript()
        self._finish_turn()

    # ---- outbound (session → Telegram) ------------------------------------
    def _outbound_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._maybe_reresolve()
                self._flush_pending()         # re-deliver any reply a prior send failed to push
                self._drain_transcript()      # may set _pending_turn_end (Codex task_complete)
                self._drain_signal()
                # End-of-turn detection, in priority order:
                #   * Codex: the reader saw task_complete → end now (no hook needed).
                #   * Claude Code: the Stop hook wrote the end-of-turn marker. Authoritative even
                #     if turn_active is unset (e.g. a restart mid-turn that would orphan a bubble).
                #   * Fallback: force-end if the transcript went quiet too long (hook missing).
                if self._pending_turn_end:
                    self._finish_turn()
                elif self._turn_end is not None and self._turn_end.exists():
                    self._end_turn()
                elif self._turn_active.is_set() and time.monotonic() - self._last_activity > IDLE_DONE:
                    # Dřív tahle větev turn jen zavřela: backstop se nespustil, takže odpověď
                    # se neodeslala a nezůstal po ní ani řádek v logu. Byla to jedna ze tří
                    # cest, kterými zprávy mizely beze stopy (audit 2026-07-31, nález C).
                    # Konec turnu teď prochází jednou společnou cestou bez ohledu na to, čím
                    # byl vyvolán; warning odlišuje "skončilo tichem" od "ohlásil to hook".
                    log.warning("konec turnu podle ticha (%.0f s bez aktivity), hook se neozval",
                                IDLE_DONE)
                    self._end_turn()
                # Živý retry: zprávy, které se nepodařilo doručit, se zkoušejí i za běhu.
                # Replay jen při startu znamenal u dlouho běžící služby čekání donekonečna.
                if time.monotonic() - getattr(self, "_last_inbound_retry", 0.0) > INBOUND_RETRY_INTERVAL:
                    self._last_inbound_retry = time.monotonic()
                    self._replay_pending_inbound()
                self._beat()                  # reached only on a full, non-blocking forward cycle
            except Exception as e:
                log.error("outbound error: %s", e)
            self._stop.wait(0.4)

    def _beat(self) -> None:
        """Touch the outbound heartbeat — proof the forward loop completed a cycle without blocking.
        A wedged send or a persistent exception never reaches here, so the file goes stale and a
        watchdog can restart the bridge."""
        if self._heartbeat is None:
            return
        try:
            self._heartbeat.write_text(str(int(time.time())), encoding="utf-8")
        except OSError:
            pass

    # ---- live tool-call status bubble (shown during the turn, deleted at the end) ------
    def _status_push(self, line: str) -> None:
        # Single line, emoji at the start, rendered in italics. One bubble is edited in place
        # across a run of consecutive tool calls; it's deleted when the next progress message
        # arrives (then re-created below it) and at turn end — so it always trails at the bottom.
        if self._owner_chat is None or not line or line == self._status["shown"]:
            return
        body = f"<i>{html.escape(line)}</i>"
        if self._status["mid"] is None:
            mid = self.tg.send_plain_id(self._owner_chat, body, parse_mode="HTML")
            if mid:
                self._status["mid"] = mid
                self._status["shown"] = line
                self._persist_status(mid)
        else:
            self.tg.edit_plain(self._owner_chat, self._status["mid"], body, parse_mode="HTML")
            self._status["shown"] = line

    def _status_clear(self) -> None:
        if self._status["mid"] is not None and self._owner_chat is not None:
            try:
                self.tg.delete_message(self._owner_chat, self._status["mid"])
            except Exception as e:
                log.warning("status bubble cleanup failed: %s", e)
        self._status = {"mid": None, "shown": ""}
        self._seen_tools.clear()
        self._persist_status(None)

    def _persist_status(self, mid: int | None) -> None:
        if self._status_path is None:
            return
        try:
            if mid is None:
                self._status_path.unlink()
            else:
                self._status_path.parent.mkdir(parents=True, exist_ok=True)
                self._status_path.write_text(str(mid), "utf-8")
        except OSError:
            pass

    def _cleanup_orphan_status(self) -> None:
        """Delete a status bubble left over from a previous run that died mid-turn."""
        if self._status_path is None or self._owner_chat is None:
            return
        try:
            mid = int(self._status_path.read_text("utf-8").strip())
        except (OSError, ValueError):
            return
        self.tg.delete_message(self._owner_chat, mid)
        try:
            self._status_path.unlink()
        except OSError:
            pass

    def _drain_signal(self) -> None:
        answer = self._consume_signal_text()
        if not answer:
            return
        if answer and self._owner_chat is not None:
            self._status_clear()                         # final message → drop the technical bubble
            self._send_final(answer)                     # reliable: queue + retry on send failure
            self._turn_active.clear()

    def _drain_transcript(self) -> None:
        if not self._transcript or not self._transcript.exists():
            return
        size = self._transcript.stat().st_size
        if size < self._tpos:          # file rotated/truncated
            self._tpos = 0
        if size == self._tpos:
            return
        with open(self._transcript, "rb") as f:
            f.seek(self._tpos)
            chunk = f.read()
        # Only consume up to the last complete line; keep a partial trailing line for next time.
        nl = chunk.rfind(b"\n")
        if nl == -1:
            return
        self._tpos += nl + 1
        for raw in chunk[:nl].split(b"\n"):
            line = raw.decode("utf-8", "ignore").strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            for ev in self._reader.parse(rec):
                self._handle_event(ev)
        # Any new transcript content during a Telegram turn = the agent is still working; refresh
        # activity so the idle fallback doesn't fire prematurely.
        if self._turn_from_tg:
            self._last_activity = time.monotonic()

    # ---- outgoing files ------------------------------------------------------
    def _safe_outbox_path(self, raw: str):
        """Validate a path the AGENT asked to send. Returns the resolved path or a reason.

        The path comes from the agent's own reply text, so this is the security boundary:
        without it, anything able to influence the agent's output could make the bridge
        upload `~/.ssh/id_rsa`. Rules: must resolve inside an allowed directory (symlinks
        are followed BEFORE the check), must be a regular file, must be non-empty.
        """
        try:
            path = Path(raw).expanduser().resolve()
        except OSError as e:
            return None, f"cannot resolve ({e.__class__.__name__})"
        allowed = self.cfg.allowed_outbox_dirs()
        if not any(path == d or d in path.parents for d in allowed):
            return None, ("outside the allowed folders — put it in "
                          f"{self.cfg.path_outbox()} or add the folder to 'outbox_dirs'")
        if not path.is_file():
            return None, "not a regular file"
        if path.stat().st_size == 0:
            return None, "file is empty"
        return path, "ok"

    def _extract_files(self, text: str):
        """Pull `[tg-file] <path>` lines out of a reply. Returns (text without them, paths)."""
        marker = self.cfg.file_marker.lower()
        keep, wanted = [], []
        for ln in text.splitlines():
            s = ln.strip()
            if s.lower().startswith(marker):
                arg = s[len(self.cfg.file_marker):].strip().strip('"').strip("'")
                if arg:
                    wanted.append(arg)
                continue
            keep.append(ln)
        return "\n".join(keep).strip(), wanted

    def _send_files(self, paths: list[str]) -> None:
        """Upload the requested files. A rejection is always reported back to the chat —
        silently dropping an attachment would be worse than a visible error."""
        for raw in paths:
            resolved, reason = self._safe_outbox_path(raw)
            if resolved is None:
                log.warning("refusing to send %r: %s", raw, reason)
                self._send_final(f"⚠️ Couldn't send {Path(raw).name}: {reason}", turn_text=False)
                continue
            try:
                self.tg.send_file(self._owner_chat, resolved)
            except Exception as e:
                log.warning("file send failed (%s): %s", resolved.name, e)
                self._send_final(f"⚠️ Couldn't send {resolved.name}: {e}", turn_text=False)

    def _send_one_file(self, raw: str) -> None:
        """Pošle jednu přílohu z durable outboxu.

        Rozlišuje dva druhy selhání, protože se s nimi nakládá opačně:
        cesta mimo povolené složky je TRVALÉ odmítnutí – ohlásí se a bere se za vyřízené,
        jinak by ucpalo frontu navěky. Chyba při odeslání je PŘECHODNÁ a propustí se ven,
        aby ji outbox zkusil znovu.
        """
        resolved, reason = self._safe_outbox_path(raw)
        if resolved is None:
            self._ohlas_trvale_odmitnuti(Path(raw).name, reason)
            return
        try:
            self.tg.send_file(self._owner_chat, resolved)
        except OSError as e:
            # Soubor zmizel nebo se nedá přečíst – opakováním se to nespraví.
            self._ohlas_trvale_odmitnuti(resolved.name, str(e))
        # Ostatní chyby (včetně TelegramError) propouštíme ven jako přechodné. Rozlišovat je
        # binárně se ukázalo jako past: nejasnou chybu bych buď zahodila (ztráta zprávy), nebo
        # opakovala donekonečna (ucpaná FIFO fronta blokující všechny další odpovědi – Fable F2).
        # Místo hádání se opakuje OMEZENĚ a pak se to vzdá; počitadlo drží outbox.

    def _ohlas_trvale_odmitnuti(self, jmeno: str, duvod: str) -> None:
        """Přílohu, kterou nemá smysl zkoušet znovu, ohlásí a bere za vyřízenou.
        Tiché zahození by bylo horší než viditelná chyba – a ucpaná fronta ještě horší."""
        log.warning("přílohu %s neposílám: %s", jmeno, duvod)
        try:
            self.tg.send_message(self._owner_chat, f"⚠️ Couldn't send {jmeno}: {duvod}")
        except Exception as e:
            log.warning("nešlo ohlásit odmítnutou přílohu: %s", e)

    def _flush_files(self) -> None:
        """Upload whatever the last reply asked for. Kept separate from the text path so a
        failed upload can never block or duplicate the message itself."""
        files = getattr(self, "_pending_files", [])
        if not files:
            return
        self._pending_files = []
        self._send_files(files)

    def _strip_marker(self, text: str) -> str:
        """Remove the progress marker (e.g. ``[TG]``) from the start of *any* line. It's a routing
        token, never content — so a stray one mid-message (narration before the marked reply) must
        not leak into the chat. Case-insensitive so ``[tg]`` and ``[TG]`` are both caught."""
        marker = self._marker.lower()
        lines = text.splitlines()
        for i, ln in enumerate(lines):
            s = ln.lstrip()
            if s.lower().startswith(marker):
                lines[i] = s[len(self._marker):].lstrip()
        return "\n".join(lines).strip()

    def _handle_event(self, ev) -> None:
        """Apply one normalized reader event to the Telegram side."""
        if ev.kind == "user":
            # Remember whether this turn came from Telegram (origin prefix) — only those are
            # forwarded; terminal-originated turns stay local.
            self._turn_from_tg = ev.text.lstrip().startswith(self._origins)
            return
        if ev.kind == "turn_start":
            if not self._turn_active.is_set():
                self._turn_from_tg = False
            return                              # local TUI turns do not make Telegram inbound busy
        if ev.kind == "turn_end":
            self._pending_turn_end = True       # outbound loop finishes the turn after this drain
            return
        if not self._turn_from_tg or self._owner_chat is None:
            return
        if ev.kind == "text":
            out = self._strip_marker(ev.text)
            if out and ev.key not in self._sent_keys:
                # A new progress message → delete the current technical bubble so the next tool
                # calls re-create it BELOW this message (the bubble always trails at the bottom).
                self._status_clear()
                # Reliable forward: the dedup ledger is marked only AFTER a confirmed send, and a
                # failed send is queued + retried — so a reply is never silently dropped.
                self._send_final(out, key=ev.key)
        elif ev.kind == "tool":
            if self.cfg.agent == "codex":
                return                            # Codex tools come live from the TUI scraper
            if ev.key and ev.key not in self._seen_tools:
                self._seen_tools.add(ev.key)
                self._status_push(ev.text)

    # ---- media helpers (reuse the same download/STT as one-shot mode) ------
    def _reject_audio_too_large(self, chat_id: int, size: int | None) -> None:
        mb = (int(size or 0) + 1024 * 1024 - 1) // (1024 * 1024)
        self.tg.send_message(
            chat_id,
            f"🎤 That audio is ~{mb} MB. Voice/audio limit is 25 MB — "
            "please send a shorter clip or write it as text.",
        )

    def _limit_inbound_prompt(self, text: str, chat_id: int) -> str | None:
        if len(text) <= MAX_INBOUND_PROMPT_CHARS:
            return text
        self.tg.send_message(
            chat_id,
            f"⚠️ That message is too long after processing ({len(text)} characters). "
            f"Please shorten it to {MAX_INBOUND_PROMPT_CHARS} characters or less.",
        )
        log.warning("inbound prompt too long: %d characters", len(text))
        return None

    def _transcribe(self, media: dict, chat_id: int) -> str | None:
        from . import stt
        if not self.cfg.elevenlabs_api_key:
            self.tg.send_message(chat_id,
                "🎤 Voice transcription isn't enabled yet. Add your ElevenLabs key with "
                "`/setkey <your-key>` (I'll delete the message right after) — then resend the voice note.")
            return None
        try:
            size = int(media.get("file_size") or 0)
        except (TypeError, ValueError):
            size = 0
        if size > MAX_AUDIO_BYTES:
            self._reject_audio_too_large(chat_id, size)
            log.warning("voice/audio too large for STT: %s bytes", size)
            return None
        try:
            fp = self.tg.get_file_path(media["file_id"])
            audio = self.tg.download(fp)
            if len(audio) > MAX_AUDIO_BYTES:
                self._reject_audio_too_large(chat_id, len(audio))
                log.warning("downloaded voice/audio too large for STT: %s bytes", len(audio))
                return None
            return stt.transcribe(audio, api_key=self.cfg.elevenlabs_api_key,
                                  filename=Path(fp).name or "voice.ogg")
        except Exception as e:
            log.error("transcription failed: %s", e)
            # This is an inbound voice-note failure, not agent output for the current turn.
            self._send_final(
                f"🎤 Přepis hlasovky se nepovedl ({e}). "
                "Pošli prosím znovu nebo napiš textem.",
                turn_text=False,
            )
            return None

    def _download_note(self, msg: dict, chat_id: int) -> str:
        import re
        if msg.get("photo"):
            file_id, default = msg["photo"][-1]["file_id"], "image.jpg"
            size = msg["photo"][-1].get("file_size")
        else:
            doc = msg["document"]
            file_id, default = doc["file_id"], doc.get("file_name") or "file"
            size = doc.get("file_size")
        # Telegram bots can only fetch files up to 20 MB via getFile — fail fast with a clear,
        # actionable message instead of a silent "couldn't download".
        if size and size > 20 * 1024 * 1024:
            self.tg.send_message(chat_id,
                f"⚠️ That file is ~{size // 1024 // 1024} MB. Telegram bots can only receive files up "
                "to 20 MB — please share a link (Drive/Dropbox) or a smaller/zipped version.")
            log.warning("attachment '%s' too big for the Bot API: %s bytes", default, size)
            return ""
        try:
            fp = self.tg.get_file_path(file_id)
            data = self.tg.download(fp)
        except Exception as e:
            log.warning("attachment '%s' download failed: %s", default, e)
            self.tg.send_message(chat_id,
                "⚠️ Couldn't download the attachment (too large, or a transient error — try again).")
            return ""
        name = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(default).name) or "file"
        if "." not in name and (ext := Path(fp).suffix):
            name += ext
        d = Path.home() / ".local/state/agent2telegram/attachments"
        d.mkdir(parents=True, exist_ok=True)
        dest = d / name
        if dest.exists():
            i = 1
            while (cand := d / f"{dest.stem}-{i}{dest.suffix}").exists():
                i += 1
            dest = cand
        dest.write_bytes(data)
        log.info("saved attachment -> %s (%d bytes)", dest, len(data))
        return f"[The user attached a file saved at: {dest} — open and use it as appropriate.]"

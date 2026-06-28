"""Stream mode — drive Codex via ``codex exec --json`` and forward its **live** event stream.

Attach mode tails an agent's transcript file, which Codex only writes at step *completion* (so
tool calls / web searches show up batched, after the fact). Stream mode instead spawns
``codex exec --json`` per message and reads its JSONL event stream from stdout in real time:

  thread.started        → the conversation id (used to resume, so context persists)
  turn.started          → a turn began
  item.started/<tool>   → a tool/web-search/command started  → live status bubble
  item.completed/agent_message → assistant text              → forwarded (kept) message
  turn.completed        → the turn finished                  → bubble cleared, typing off

This gives the same live UX as Claude Code (progress, tool bubbles, typing) for Codex, and
removes the "terminal shows it instantly, Telegram lags" effect. The whole Telegram side
(sending, the italic status bubble, the typing thread, slash commands, voice, the dedup ledger)
is reused from :class:`~agent2telegram.attach.AttachBridge`.
"""
from __future__ import annotations

import json
import logging
import shutil
import subprocess
import threading
import time
from pathlib import Path

from .attach import AttachBridge
from .config import Config
from .telegram import TelegramClient

log = logging.getLogger("agent2telegram.stream")


def _stream_tool_summary(item: dict) -> str:
    """One-line bubble for a Codex ``codex exec --json`` item (tool / web search / command)."""
    from .readers import _short
    it = item.get("type")
    if it == "web_search":
        action = item.get("action") if isinstance(item.get("action"), dict) else {}
        q = item.get("query") or action.get("query") or (action.get("queries") or [""])[0] or ""
        return "🔎 Web search: " + _short(q) if q else "🔎 Searching the web"
    if it == "command_execution":
        return "🛠️ " + _short(item.get("command", "command"))
    if it == "file_change":
        return "✏️ " + _short("editing files")
    if it == "mcp_tool_call":
        return "🔌 " + _short(item.get("tool") or item.get("server") or "mcp tool")
    if it == "todo_list":
        return "🗒️ " + _short("plan updated")
    return "🛠️ " + _short(str(it or "tool"))


class StreamBridge(AttachBridge):
    """Codex via ``codex exec --json``. Reuses AttachBridge's Telegram side; replaces the
    transcript-tailing source with a live subprocess event stream."""

    @staticmethod
    def _codex_bin() -> str:
        """Resolve the codex binary: PATH, then the common ~/.local/bin install (which is on PATH
        only in login shells), else bare 'codex'. Used as an absolute path when spawning, so a
        minimal (non-login) launcher environment still finds it."""
        found = shutil.which("codex")
        if found:
            return found
        local = Path.home() / ".local" / "bin" / "codex"
        return str(local) if local.exists() else "codex"

    def __init__(self, cfg: Config, *, client: TelegramClient | None = None) -> None:
        self._codex = self._codex_bin()
        if self._codex == "codex" and not shutil.which("codex"):
            log.warning("codex binary not found on PATH or ~/.local/bin — will try 'codex' anyway")
        # ---- Telegram-side state (mirrors AttachBridge.__init__, minus tmux/transcript) ----
        self.cfg = cfg
        self.tg = client or TelegramClient(cfg.token)
        self._allowed = set(cfg.allowed_user_ids)
        self._marker = cfg.progress_marker
        self._origin = cfg.origin_prefix
        self._origins = tuple({p for p in (cfg.origin_prefix.strip(), "Telegram:", "[TG]") if p})
        self._owner_chat = cfg.allowed_user_ids[0] if cfg.allowed_user_ids else None
        self._signal = Path(cfg.signal_file) if cfg.signal_file else None
        self._turn_end = None                      # no Stop-hook marker in stream mode
        self._stop = threading.Event()
        self._sent_path = Path.home() / ".config" / "agent2telegram" / "stream_sent.txt"
        try:
            self._sent_keys: set = set(self._sent_path.read_text("utf-8").split())
        except OSError:
            self._sent_keys = set()
        # Stream mode has no attach signal directory, but it still needs durable retry state:
        # a Telegram send failure must leave the final text queued until a later confirmed send.
        self._queue_path = Path.home() / ".config" / "agent2telegram" / "stream_outbound_queue.jsonl"
        self._pending_send: list = self._load_queue()
        self._turn_active = threading.Event()
        self._init_inbound_queue()
        self._turn_from_tg = True                  # every turn here is Telegram-originated
        self._transcript = None                    # _finish_turn backstop is transcript-only
        self._last_activity = 0.0
        self._status = {"mid": None, "shown": ""}
        self._last_typing = 0.0
        self._typing_count = 0
        self._turn_started = 0.0
        self._max_gap = 0.0
        self._status_path = (self._signal.parent / "stream_status_bubble") if self._signal else None
        self._seen_tools: set = set()
        self._tui_seen: set = set()
        self._turn_text_sent = False
        self._pending_turn_end = False
        # ---- Codex stream specifics ----
        self._thread_id: str | None = None         # conversation id → resume keeps context
        self._proc_lock = threading.Lock()         # one turn at a time

    # ---- lifecycle ---------------------------------------------------------
    def run(self) -> None:
        me = self.tg.get_me()
        log.info("Stream bridge live as @%s → codex exec --json, owner=%s",
                 me.get("username"), self._owner_chat)
        from .attach import BOT_COMMANDS
        self.tg.set_my_commands(BOT_COMMANDS)
        self._cleanup_orphan_status()
        threading.Thread(target=self._delivery_loop, daemon=True).start()
        threading.Thread(target=self._typing_loop, daemon=True).start()
        self._inbound_loop()

    def _delivery_loop(self) -> None:
        """Retry stream-mode sends that failed after Codex already emitted the final text."""
        while not self._stop.is_set():
            try:
                self._flush_pending()
            except Exception as e:
                log.error("delivery retry error: %s", e)
            self._stop.wait(0.4)

    # ---- inbound (override): spawn codex exec --json instead of tmux send-keys ----
    def _inject(self, text: str) -> None:
        """Run one Codex turn as a streamed subprocess; forward its events live."""
        threading.Thread(target=self._run_turn, args=(text,), daemon=True).start()

    def _codex_argv(self, prompt: str) -> list:
        base = ["codex", "exec", "--json"]
        if self.cfg.command:                        # user override (without {prompt})
            base = [a for a in self.cfg.command if a != "{prompt}"]
        if base and base[0] == "codex":             # spawn by absolute path (PATH-independent)
            base[0] = self._codex
        if self._thread_id:                          # ['codex','exec', ...] → insert resume <id>
            base = base[:2] + ["resume", self._thread_id] + base[2:]
        return base + [prompt]

    def _run_turn(self, text: str) -> None:
        with self._proc_lock:                       # serialize turns (one Codex at a time)
            now = time.monotonic()
            self._turn_active.set()
            self._last_activity = self._turn_started = self._last_typing = now
            self._typing_count = 1
            self._max_gap = 0.0
            if self._owner_chat is not None:
                self.tg.send_chat_action(self._owner_chat, "typing")
            log.info("TURN START t=%.2f", time.time())
            argv = self._codex_argv(text)
            try:
                proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                        text=True, cwd=str(Path.home()))
            except OSError as e:
                log.error("codex spawn failed: %s", e)
                if self._owner_chat is not None:
                    self.tg.send_message(self._owner_chat, f"⚠️ Couldn't start Codex: {e}")
                self._finish_turn()
                return
            try:
                for line in proc.stdout:            # blocking read, line by line (live)
                    line = line.strip()
                    if not line:
                        continue
                    self._last_activity = time.monotonic()
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    self._handle_stream_event(obj)
                proc.wait(timeout=5)
            except Exception as e:
                log.error("stream read error: %s", e)
            finally:
                self._finish_turn()

    # ---- stream event → Telegram actions ----------------------------------
    def _handle_stream_event(self, obj: dict) -> None:
        t = obj.get("type")
        if t == "thread.started":
            self._thread_id = obj.get("thread_id") or self._thread_id
            return
        item = obj.get("item") if isinstance(obj.get("item"), dict) else {}
        itype = item.get("type")
        if t == "item.completed" and itype == "agent_message":
            out = self._strip_marker(item.get("text", ""))
            key = item.get("id") or out[:40]
            if out and self._owner_chat is not None and key not in self._sent_keys:
                self._status_clear()               # progress/final text → drop the tool bubble
                # Reuse AttachBridge's durable path: mark the ledger only after Telegram confirms;
                # on failure the message is persisted and retried instead of being lost.
                self._send_final(out, key=key)
        elif t == "item.started" and itype and itype != "agent_message":
            iid = item.get("id")
            if iid and iid not in self._seen_tools:
                self._seen_tools.add(iid)
                self._status_push(_stream_tool_summary(item))
        elif t == "item.completed" and itype == "web_search":
            # the start had an empty query; refine the bubble once we know it
            self._status_push(_stream_tool_summary(item))
        # turn.completed handled by process exit → _finish_turn

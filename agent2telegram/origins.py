"""Who sent which Telegram message — so a reply can be routed back to its author.

Every outbound message (a forwarded turn, a resume-mode reply, a ``notify`` from a
background job) may record its origin: the Claude session id and cwd it came from.
When the user REPLIES to such a message, the bridge looks the origin up and delivers
the reply to that session instead of the currently attached one.

One JSON file per Telegram message id, in ``<state>/origins/``. A directory of tiny
files instead of one JSON map on purpose: the bridge and the ``notify`` CLI are
separate processes writing concurrently, and per-file writes need no locking.
"""
from __future__ import annotations

import json
import logging
import os
import random
import time
from pathlib import Path

log = logging.getLogger(__name__)

#: Older records are pruned — a reply to a month-old notice routing into a long-gone
#: session would be more surprising than helpful.
MAX_AGE_DAYS = 30


def _dir(state_dir: Path) -> Path:
    return Path(state_dir) / "origins"


def record(state_dir: Path, message_ids, *, sid: str, cwd: str, label: str = "") -> None:
    """Remember that Telegram messages *message_ids* came from session *sid*.

    Best-effort: a failed write must never break the send that already happened.
    """
    if not sid:
        return
    d = _dir(state_dir)
    try:
        d.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        log.warning("origins dir failed: %s", e)
        return
    payload = json.dumps({"sid": sid, "cwd": cwd or "", "label": label or "", "ts": time.time()},
                         ensure_ascii=False)
    for mid in message_ids or ():
        if not mid:
            continue
        try:
            tmp = d / f".{mid}.tmp"
            tmp.write_text(payload, encoding="utf-8")
            tmp.replace(d / f"{mid}.json")      # atomic — a reader never sees a half-write
        except OSError as e:
            log.warning("origin record for message %s failed: %s", mid, e)
    # Amortized cleanup: roughly one send in fifty pays for the pruning walk.
    if random.random() < 0.02:
        prune(state_dir)


def lookup(state_dir: Path, message_id) -> dict | None:
    """Origin of Telegram message *message_id*, or None when unknown/expired."""
    if not message_id:
        return None
    try:
        d = json.loads((_dir(state_dir) / f"{message_id}.json").read_text("utf-8"))
    except (OSError, ValueError):
        return None
    if time.time() - d.get("ts", 0) > MAX_AGE_DAYS * 86400:
        return None
    return d if d.get("sid") else None


def prune(state_dir: Path, max_age_days: float = MAX_AGE_DAYS) -> int:
    """Drop records older than *max_age_days*. Returns how many were removed."""
    cutoff = time.time() - max_age_days * 86400
    n = 0
    try:
        entries = list(_dir(state_dir).iterdir())
    except OSError:
        return 0
    for p in entries:
        try:
            if p.stat().st_mtime < cutoff:
                os.unlink(p)
                n += 1
        except OSError:
            pass
    return n

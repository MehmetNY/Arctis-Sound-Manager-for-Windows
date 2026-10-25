# Copyright (C) 2026 loteran — Windows portability layer
# SPDX-License-Identifier: GPL-3.0-or-later

"""A non-blocking front for the engine's control channel, for the GUI thread and the daemon's event loop.

:class:`~.control.EngineClient` calls block (up to their timeout) when the engine is slow or gone. A
volume slider fires dozens of changes a second and a Qt timer polls ten times a second, so none of that
may wait on a socket. :class:`EngineBridge` therefore

* queues commands and sends them from one background thread, keeping only the **latest** command of each
  kind (a slider dragged through 40 positions sends the last one, not 40 round trips);
* keeps a cached copy of the engine's ``status`` that a background poller refreshes while somebody is
  looking at it, so reads are instant.

Both never raise: with no engine running, commands are dropped and ``status()`` is ``None``.
"""
from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Callable

from arctis_sound_manager.win.audio.control import EngineClient, EngineUnavailable

_log = logging.getLogger(__name__)


class EngineBridge:
    def __init__(self, config_dir: Path | None = None, client: EngineClient | None = None,
                 poll_interval: float = 0.5, idle_after: float = 5.0, clock: Callable[[], float] = time.monotonic):
        self._client = client or EngineClient(config_dir, timeout=1.0)
        self._poll_interval = poll_interval
        self._idle_after = idle_after
        self._clock = clock
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._pending: dict[str, tuple[str, dict]] = {}            # key -> (command, fields); insertion-ordered
        self._status: dict | None = None
        self._status_at = -1e9
        self._wanted_at = -1e9                                       # when status() was last asked for
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._busy = False                                           # a command is being delivered right now
        self.sent = 0                                                # commands delivered (for tests/diagnostics)
        self.dropped = 0                                             # commands that found no engine

    # ── commands ────────────────────────────────────────────────────────────

    def send(self, key: str, cmd: str, **fields) -> None:
        """Queue ``cmd`` for the engine; a newer command with the same *key* replaces one still waiting."""
        with self._lock:
            self._pending.pop(key, None)                             # re-insert: keep the order of arrival
            self._pending[key] = (cmd, fields)
        self._ensure_thread()
        self._wake.set()

    def volume(self, channel: str, pct: float) -> None:
        self.send(f"volume:{channel}", "volume", channel=channel, pct=float(pct))

    def mute(self, channel: str, muted: bool) -> None:
        self.send(f"mute:{channel}", "mute", channel=channel, muted=bool(muted))

    def chatmix(self, channels_pct: float, chat_pct: float, channels: list[str] | None = None) -> None:
        fields: dict = {"channels_pct": float(channels_pct), "chat_pct": float(chat_pct)}
        if channels is not None:
            fields["channels"] = list(channels)
        self.send("chatmix", "chatmix", **fields)

    def reload(self) -> None:
        self.send("reload", "reload")

    def route(self, exe: str, channel: str | None) -> None:
        """Send a program to a channel (``"bypass"``: straight to the headset); ``None`` forgets the choice."""
        if channel is None:
            self.send(f"route:{exe}", "unroute", exe=exe)
        else:
            self.send(f"route:{exe}", "route", exe=exe, channel=channel)

    def flush(self, timeout: float = 5.0) -> bool:
        """Wait until everything queued has been handed to the engine (or dropped). For tests and shutdown."""
        deadline = self._clock() + timeout
        while self._clock() < deadline:
            with self._lock:
                if not self._pending and not self._busy:
                    return True
            time.sleep(0.005)
        return False

    # ── status ──────────────────────────────────────────────────────────────

    def status(self, max_age: float | None = None) -> dict | None:
        """The engine's last known status (``None`` when it is not running). Never blocks.

        Asking keeps the background poller going for a few seconds, so a page that calls this every
        500 ms always sees fresh data and one that stopped asking costs nothing.
        """
        self._wanted_at = self._clock()
        self._ensure_thread()
        self._wake.set()
        if max_age is not None and self._clock() - self._status_at > max_age:
            return None
        return self._status

    def running(self) -> bool:
        status = self.status()
        return bool(status and status.get("running"))

    # ── plumbing ────────────────────────────────────────────────────────────

    def _ensure_thread(self) -> None:
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                self._stop.clear()
                self._thread = threading.Thread(target=self._run, name="asm-engine-bridge", daemon=True)
                self._thread.start()

    def close(self) -> None:
        self._stop.set()
        self._wake.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.0)

    def _take(self) -> tuple[str, dict] | None:
        with self._lock:
            if not self._pending:
                return None
            key = next(iter(self._pending))
            self._busy = True
            return self._pending.pop(key)

    def _deliver(self, cmd: str, fields: dict) -> None:
        try:
            reply = self._client.request(cmd, **fields)
            self.sent += 1
            if not reply.get("ok", True):
                _log.debug("engine refused %s: %s", cmd, reply.get("error"))
        except EngineUnavailable:
            self.dropped += 1
            self._status = None
        except Exception:                                            # noqa: BLE001 — a bridge must never raise
            _log.debug("engine command %s failed", cmd, exc_info=True)

    def _refresh_status(self) -> None:
        try:
            reply = self._client.status()
            self._status = reply.get("status") if reply.get("ok", True) else None
        except EngineUnavailable:
            self._status = None
        except Exception:                                            # noqa: BLE001
            _log.debug("engine status failed", exc_info=True)
            self._status = None
        self._status_at = self._clock()

    def _run(self) -> None:
        while not self._stop.is_set():
            self._wake.clear()                       # clear *before* looking: a send() after this sets it again
            work = self._take()
            if work is not None:
                try:
                    self._deliver(*work)
                finally:
                    self._busy = False
                continue
            now = self._clock()
            watching = now - self._wanted_at < self._idle_after
            if watching and now - self._status_at >= self._poll_interval:
                self._refresh_status()
                continue
            self._wake.wait(max(0.01, self._poll_interval - (now - self._status_at)) if watching else None)


_shared: EngineBridge | None = None
_shared_lock = threading.Lock()


def shared_bridge() -> EngineBridge:
    """The process-wide bridge (one per daemon / GUI process)."""
    global _shared
    with _shared_lock:
        if _shared is None:
            _shared = EngineBridge()
        return _shared

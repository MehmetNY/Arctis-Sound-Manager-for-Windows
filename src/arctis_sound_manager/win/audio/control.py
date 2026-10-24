# Copyright (C) 2026 loteran — Windows portability layer
# SPDX-License-Identifier: GPL-3.0-or-later

"""How the daemon and the GUI talk to the running audio engine.

The engine is a separate process (so a sound-card hiccup can never freeze the GUI, and the GUI's
Python never competes with the audio threads). It listens on a loopback TCP port and speaks one JSON
object per line::

    -> {"token": "...", "cmd": "volume", "channel": "game", "pct": 80}
    <- {"ok": true}

The port and a random token are written to ``win_audio_engine.port`` in ASM's config folder when the
engine starts and removed when it stops. The token keeps a web page that posts to ``localhost`` (or
another user's program) from steering the engine; only a process that can read the user's own files
knows it.
"""
from __future__ import annotations

import json
import logging
import os
import secrets
import socket
import socketserver
import threading
from pathlib import Path
from typing import Callable

_log = logging.getLogger(__name__)

PORT_FILE = "win_audio_engine.port"
MAX_LINE = 64 * 1024
IDLE_TIMEOUT = 10.0


class EngineUnavailable(RuntimeError):
    """No engine is running (or it did not answer)."""


def port_file(config_dir: Path | None = None) -> Path:
    from arctis_sound_manager.win.audio.params import default_config_dir
    return (config_dir or default_config_dir()) / PORT_FILE


class _Handler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        server: ControlServer = self.server.owner                      # type: ignore[attr-defined]
        self.request.settimeout(IDLE_TIMEOUT)
        while True:
            try:
                line = self.rfile.readline(MAX_LINE + 1)
            except (OSError, socket.timeout):
                return
            if not line:
                return
            if len(line) > MAX_LINE:
                self._send({"ok": False, "error": "request too long"})
                return
            self._send(server.dispatch(line))
            if server.closing:
                return

    def _send(self, reply: dict) -> None:
        try:
            self.wfile.write((json.dumps(reply) + "\n").encode("utf-8"))
            self.wfile.flush()
        except OSError:
            pass


class _TcpServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


class ControlServer:
    """Serves ``handler(request: dict) -> dict`` on 127.0.0.1; ``handler`` may raise (that becomes an error reply)."""

    def __init__(self, handler: Callable[[dict], dict], config_dir: Path | None = None, host: str = "127.0.0.1"):
        self.handler = handler
        self.config_dir = config_dir
        self.token = secrets.token_hex(16)
        self.closing = False
        self._server = _TcpServer((host, 0), _Handler)
        self._server.owner = self                                       # type: ignore[attr-defined]
        self.port = self._server.server_address[1]
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._server.serve_forever, kwargs={"poll_interval": 0.2},
                                        name="asm-engine-control", daemon=True)
        self._thread.start()
        path = port_file(self.config_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps({"port": self.port, "token": self.token, "pid": os.getpid()}), encoding="utf-8")
        os.replace(tmp, path)

    def stop(self) -> None:
        self.closing = True
        try:
            path = port_file(self.config_dir)
            if json.loads(path.read_text(encoding="utf-8")).get("pid") == os.getpid():   # never delete a newer engine's file
                path.unlink()
        except (OSError, ValueError):
            pass
        self._server.shutdown()
        self._server.server_close()

    def dispatch(self, line: bytes) -> dict:
        try:
            request = json.loads(line.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {"ok": False, "error": "not JSON"}
        if not isinstance(request, dict):
            return {"ok": False, "error": "not an object"}
        if not secrets.compare_digest(str(request.get("token", "")), self.token):
            return {"ok": False, "error": "bad token"}
        try:
            reply = self.handler(request)
        except Exception as exc:                                        # noqa: BLE001 — a bad command must not kill the engine
            _log.exception("control command %r failed", request.get("cmd"))
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        reply = dict(reply or {})
        reply.setdefault("ok", True)
        return reply


class EngineClient:
    """Talks to a running engine; every call raises :class:`EngineUnavailable` if there is none."""

    def __init__(self, config_dir: Path | None = None, timeout: float = 1.5):
        self.config_dir = config_dir
        self.timeout = timeout

    def _endpoint(self) -> tuple[int, str]:
        try:
            info = json.loads(port_file(self.config_dir).read_text(encoding="utf-8"))
            return int(info["port"]), str(info["token"])
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise EngineUnavailable("the audio engine is not running") from exc

    def request(self, cmd: str, **fields) -> dict:
        port, token = self._endpoint()
        payload = (json.dumps({"token": token, "cmd": cmd, **fields}) + "\n").encode("utf-8")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=self.timeout) as sock:
                sock.settimeout(self.timeout)
                sock.sendall(payload)
                data = b""
                while not data.endswith(b"\n"):
                    chunk = sock.recv(65536)
                    if not chunk:
                        break
                    data += chunk
        except OSError as exc:
            raise EngineUnavailable(f"the audio engine did not answer: {exc}") from exc
        try:
            return json.loads(data.decode("utf-8"))
        except ValueError as exc:
            raise EngineUnavailable("the audio engine sent an unreadable answer") from exc

    def available(self) -> bool:
        try:
            return bool(self.request("ping").get("ok"))
        except EngineUnavailable:
            return False

    # conveniences used by the daemon / GUI
    def status(self) -> dict:
        return self.request("status")

    def set_volume(self, channel: str, pct: float) -> dict:
        return self.request("volume", channel=channel, pct=pct)

    def set_mute(self, channel: str, muted: bool) -> dict:
        return self.request("mute", channel=channel, muted=muted)

    def set_chatmix(self, channels_pct: float, chat_pct: float, channels: list[str] | None = None) -> dict:
        extra = {"channels": channels} if channels is not None else {}
        return self.request("chatmix", channels_pct=channels_pct, chat_pct=chat_pct, **extra)

    def reload(self) -> dict:
        return self.request("reload")

    def reconfigure(self) -> dict:
        return self.request("reconfigure")

    def shutdown(self) -> dict:
        return self.request("shutdown")

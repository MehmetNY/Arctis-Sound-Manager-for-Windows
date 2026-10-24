# Copyright (C) 2026 loteran — Windows portability layer
# SPDX-License-Identifier: GPL-3.0-or-later

"""Local IPC that stands in for the D-Bus session bus on Windows.

Transport: newline-delimited JSON over a loopback TCP socket.

* The daemon listens on ``127.0.0.1`` on an ephemeral port and writes
  ``{port, token, pid}`` to ``<runtime dir>/ipc.json`` (a per-user folder).
* A client reads that file, connects, and must send the token first. The token
  is what keeps other local users (and web pages poking at localhost ports)
  from driving the daemon: only a process that can read the owner's file has it.

Request  ``{"id": 1, "interface": "...", "member": "...", "body": [...]}``
Response ``{"id": 1, "ok": true, "body": [...]}`` or
         ``{"id": 1, "ok": false, "error": {"name": "...", "text": "..."}}``

Two more operations, both ``{"id": n, "op": "..."}``:

* ``"introspect"`` answers ``{interface: {"methods": [...], "signals": [...]}}``.
* ``"subscribe"`` (with ``interface`` and ``member``) turns the connection into a signal
  listener: from then on the daemon pushes ``{"signal": true, "interface": "...",
  "member": "...", "body": [...]}`` frames whenever that signal is emitted.
"""
from __future__ import annotations

import asyncio
import atexit
import enum
import hmac
import inspect
import json
import logging
import os
import secrets
import time
from pathlib import Path
from typing import Any

from arctis_sound_manager.file_lock import runtime_dir
from arctis_sound_manager.win.dbus_shim.constants import (ACCESS_DENIED, FAILED, INVALID_ARGS,
                                                          UNKNOWN_INTERFACE, UNKNOWN_METHOD)
from arctis_sound_manager.win.dbus_shim.errors import DBusError
from arctis_sound_manager.win.dbus_shim.service import ServiceInterface, describe, pack_result

log = logging.getLogger(__name__)

PROTOCOL_VERSION = 1
LINE_LIMIT = 32 * 1024 * 1024   # a full settings/EQ payload is far larger than asyncio's 64 KiB default
AUTH_TIMEOUT_S = 5.0
PROBE_TIMEOUT_S = 1.0
MAX_PENDING_BYTES = 4 * 1024 * 1024   # a subscriber that stops reading is dropped, not buffered forever


def state_path() -> Path:
    return runtime_dir() / "ipc.json"


def read_state() -> dict | None:
    try:
        state = json.loads(state_path().read_text(encoding="utf-8"))
        if isinstance(state, dict) and isinstance(state.get("port"), int) and isinstance(state.get("token"), str):
            return state
    except (OSError, ValueError):
        pass
    return None


def _write_state(state: dict) -> None:
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state), encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)  # POSIX only matters here; Windows keeps the profile folder's ACL
    except OSError:
        pass
    # Windows: Python opens files without FILE_SHARE_DELETE, so while a client happens to be
    # reading ipc.json the rename fails with PermissionError. The read lasts microseconds; wait it out.
    for attempt in range(40):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == 39:
                raise
            time.sleep(0.05)


def _jsonable(value: Any) -> Any:
    if isinstance(value, enum.Enum):
        return value.value
    if isinstance(value, (bytes, bytearray)):
        return list(value)
    if isinstance(value, (set, frozenset, tuple)):
        return list(value)
    return str(value)


async def close_writer(writer: asyncio.StreamWriter) -> None:
    """Close a stream and wait for it to be closed.

    Without the wait, a stream closed just before its event loop ends is torn down by the
    garbage collector afterwards and Windows' Proactor loop reports "Event loop is closed".
    The GUI tray opens a fresh loop for every poll, so that would be a steady stream of noise.
    """
    try:
        writer.close()
        await asyncio.wait_for(writer.wait_closed(), 1.0)
    except Exception:  # noqa: BLE001 - peer already gone / reset: nothing left to clean up
        pass


def encode(obj: dict) -> bytes:
    return json.dumps(obj, default=_jsonable, separators=(",", ":")).encode("utf-8") + b"\n"


async def read_json(reader: asyncio.StreamReader, timeout: float | None) -> dict | None:
    """One JSON object per line; None on EOF."""
    line = await asyncio.wait_for(reader.readline(), timeout)
    if not line:
        return None
    obj = json.loads(line)
    if not isinstance(obj, dict):
        raise ValueError("IPC frames must be JSON objects")
    return obj


# ── server ───────────────────────────────────────────────────────────────────

class IpcServer:
    def __init__(self, interfaces: dict[str, ServiceInterface]):
        self._interfaces = interfaces      # shared, live: MessageBus.export() adds to it
        self._server: asyncio.AbstractServer | None = None
        self._token = ""
        self.port = 0
        self._loop: asyncio.AbstractEventLoop | None = None
        self._subscribers: dict[asyncio.StreamWriter, set[tuple[str, str]]] = {}

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._token = secrets.token_hex(16)
        self._server = await asyncio.start_server(self._on_client, "127.0.0.1", 0, limit=LINE_LIMIT)
        self.port = self._server.sockets[0].getsockname()[1]
        _write_state({"v": PROTOCOL_VERSION, "port": self.port, "token": self._token, "pid": os.getpid()})
        # The daemon exits without closing its bus (DbusManager.stop() only flips a flag),
        # so tidy up at interpreter exit as well. A hard kill still leaves the file; clients
        # and probe_existing_daemon() treat a dead port as "no daemon", so that is harmless.
        atexit.register(self._remove_state)
        log.info("IPC listening on 127.0.0.1:%d", self.port)

    def _remove_state(self) -> None:
        state = read_state()
        if state is not None and state.get("token") == self._token:  # never delete a successor's file
            try:
                state_path().unlink()
            except OSError:
                pass

    async def close(self) -> None:
        server, self._server = self._server, None
        if server is None:
            return
        server.close()
        self._remove_state()
        # Listeners see EOF and know the daemon is gone; ordinary calls are one-shot anyway.
        for writer in list(self._subscribers):
            writer.close()
        self._subscribers.clear()

    # -- signals ------------------------------------------------------------

    def emit(self, interface: str, member: str, body: list) -> None:
        """Push a signal to its subscribers. Safe to call from any thread."""
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        try:
            loop.call_soon_threadsafe(self._push, interface, member, body)
        except RuntimeError:   # loop closed between the check and the call
            pass

    def _push(self, interface: str, member: str, body: list) -> None:
        if not self._subscribers:
            return
        frame = encode({"signal": True, "interface": interface, "member": member, "body": body})
        for writer, wanted in list(self._subscribers.items()):
            if (interface, member) not in wanted:
                continue
            transport = writer.transport
            if writer.is_closing() or transport.get_write_buffer_size() > MAX_PENDING_BYTES:
                log.warning("dropping a signal listener that is not keeping up")
                self._subscribers.pop(writer, None)
                writer.close()
                continue
            writer.write(frame)

    async def _on_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            auth = await read_json(reader, AUTH_TIMEOUT_S)
            presented = str((auth or {}).get("auth", ""))
            if not hmac.compare_digest(presented, self._token):
                writer.write(encode({"ok": False, "error": {"name": ACCESS_DENIED, "text": "bad token"}}))
                await writer.drain()
                return
            writer.write(encode({"ok": True, "v": PROTOCOL_VERSION}))
            await writer.drain()
            while True:
                request = await read_json(reader, None)
                if request is None:
                    break
                if request.get("op") == "subscribe":
                    writer.write(encode(self._subscribe(writer, request)))
                else:
                    writer.write(encode(await self._handle(request)))
                await writer.drain()
        except (asyncio.TimeoutError, ConnectionError, ValueError, asyncio.IncompleteReadError):
            pass
        except Exception:  # noqa: BLE001 - one bad client must never take the daemon down
            log.exception("IPC client handler failed")
        finally:
            self._subscribers.pop(writer, None)
            await close_writer(writer)

    def _subscribe(self, writer: asyncio.StreamWriter, request: dict) -> dict:
        rid = request.get("id")
        interface, member = str(request.get("interface") or ""), str(request.get("member") or "")
        exported = self._interfaces.get(interface)
        if exported is None:
            return self._error(rid, UNKNOWN_INTERFACE, f"no such interface: {interface!r}")
        if member not in describe(exported)["signals"]:
            return self._error(rid, UNKNOWN_METHOD, f"no such signal: {member!r}")
        self._subscribers.setdefault(writer, set()).add((interface, member))
        return {"id": rid, "ok": True, "body": []}

    @staticmethod
    def _error(request_id: Any, name: str, text: str) -> dict:
        return {"id": request_id, "ok": False, "error": {"name": name, "text": text}}

    async def _handle(self, request: dict) -> dict:
        rid = request.get("id")
        if request.get("op") == "ping":
            return {"id": rid, "ok": True, "body": ["pong"]}
        if request.get("op") == "introspect":
            return {"id": rid, "ok": True,
                    "body": [{name: describe(iface) for name, iface in self._interfaces.items()}]}

        interface = self._interfaces.get(request.get("interface") or "")
        if interface is None:
            return self._error(rid, UNKNOWN_INTERFACE, f"no such interface: {request.get('interface')!r}")
        fn = interface._asm_find_method(str(request.get("member") or ""))
        if fn is None:
            return self._error(rid, UNKNOWN_METHOD, f"no such method: {request.get('member')!r}")

        body = request.get("body") or []
        try:
            inspect.signature(fn).bind(*body)
        except TypeError as exc:
            return self._error(rid, INVALID_ARGS, str(exc))

        try:
            result = fn(*body)
            if inspect.isawaitable(result):
                result = await result
            return {"id": rid, "ok": True, "body": pack_result(fn, result)}
        except DBusError as exc:
            return self._error(rid, exc.type, exc.text)
        except Exception as exc:  # noqa: BLE001 - reported to the caller, logged here
            log.exception("IPC method %s.%s failed", request.get("interface"), request.get("member"))
            return self._error(rid, FAILED, f"{type(exc).__name__}: {exc}")


# ── client ───────────────────────────────────────────────────────────────────

async def open_authenticated(state: dict, timeout: float = AUTH_TIMEOUT_S):
    """Connect and authenticate; returns (reader, writer). Raises OSError/PermissionError."""
    reader, writer = await asyncio.wait_for(
        asyncio.open_connection("127.0.0.1", state["port"], limit=LINE_LIMIT), timeout)
    try:
        writer.write(encode({"auth": state["token"]}))
        await writer.drain()
        reply = await read_json(reader, timeout)
    except BaseException:
        await close_writer(writer)
        raise
    if not reply or not reply.get("ok"):
        await close_writer(writer)
        raise PermissionError("IPC authentication was refused")
    return reader, writer


async def probe_existing_daemon() -> bool:
    """True when a live daemon already answers on the advertised port."""
    state = read_state()
    if state is None:
        return False
    try:
        reader, writer = await open_authenticated(state, PROBE_TIMEOUT_S)
    except (OSError, asyncio.TimeoutError, ValueError):
        return False
    try:
        writer.write(encode({"id": 0, "op": "ping"}))
        await writer.drain()
        reply = await read_json(reader, PROBE_TIMEOUT_S)
        return bool(reply and reply.get("ok"))
    except (OSError, asyncio.TimeoutError, ValueError):
        return False
    finally:
        await close_writer(writer)

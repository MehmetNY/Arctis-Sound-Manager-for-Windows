# Copyright (C) 2026 loteran — Windows portability layer
# SPDX-License-Identifier: GPL-3.0-or-later

"""``dbus_next.aio.message_bus.MessageBus`` look-alike over the local IPC (see ``ipc``).

One class, two roles, decided by use exactly as in D-Bus: a process that calls
``export()`` + ``request_name()`` becomes the server; one that only ``call()``s
is a client. ``connect()`` never touches the network, so — like a real session
bus — it succeeds whether or not the daemon is up; the *call* then fails with
``ServiceUnknown`` when nobody is listening.
"""
from __future__ import annotations

import asyncio
import itertools
import logging
import re
from typing import Any, Callable

from arctis_sound_manager.win.dbus_shim import ipc
from arctis_sound_manager.win.dbus_shim.constants import (FAILED, SERVICE_UNKNOWN, BusType,
                                                          MessageType, RequestNameReply)
from arctis_sound_manager.win.dbus_shim.errors import DBusError, InterfaceNotFoundError
from arctis_sound_manager.win.dbus_shim.message import Message
from arctis_sound_manager.win.dbus_shim.service import ServiceInterface

log = logging.getLogger(__name__)

CALL_TIMEOUT_S = 120.0  # the slowest daemon-side operations rebuild audio routing
_serials = itertools.count(1)


def _snake(name: str) -> str:
    """``StatusChanged`` -> ``status_changed`` (the same rule dbus_next uses for proxy attributes)."""
    return re.sub("([a-z0-9])([A-Z])", r"\1_\2", re.sub("(.)([A-Z][a-z]+)", r"\1_\2", name)).lower()


def _error_reply(call: Message, name: str, text: str) -> Message:
    return Message(message_type=MessageType.ERROR, error_name=name, body=[text],
                   reply_serial=call.serial, signature="s")


class MessageBus:
    def __init__(self, bus_address: str | None = None, bus_type: BusType = BusType.SESSION,
                 auth=None, negotiate_unix_fd: bool = False):
        self._bus_type = bus_type
        self._interfaces: dict[str, ServiceInterface] = {}
        self._server: ipc.IpcServer | None = None
        self._closed = asyncio.Event()  # no loop needed at construction on Python >= 3.10
        self.connected = False
        # client side signals: one connection, subscribed to every (interface, member) a handler asked for
        self._signal_handlers: dict[tuple[str, str], list[Callable[..., Any]]] = {}
        self._signal_task: asyncio.Task | None = None
        self._signal_writer: asyncio.StreamWriter | None = None

    # -- lifecycle ----------------------------------------------------------

    async def connect(self) -> "MessageBus":
        self.connected = True
        return self

    def disconnect(self) -> None:
        self.connected = False
        task, self._signal_task = self._signal_task, None
        if task is not None and not task.done():
            task.cancel()   # its finally block closes the connection
        server, self._server = self._server, None
        if server is not None:
            try:
                asyncio.get_running_loop().create_task(server.close())
            except RuntimeError:  # no loop: nothing is listening in this process anyway
                pass
        self._closed.set()

    async def wait_for_disconnect(self) -> None:
        await self._closed.wait()

    # -- server role --------------------------------------------------------

    def export(self, path: str, interface: ServiceInterface) -> None:
        self._interfaces[interface.name] = interface
        interface._asm_bus = self

    def unexport(self, path: str, interface: ServiceInterface | str | None = None) -> None:
        name = interface.name if isinstance(interface, ServiceInterface) else interface
        if name is not None:
            gone = self._interfaces.pop(name, None)
            if gone is not None and getattr(gone, "_asm_bus", None) is self:
                gone._asm_bus = None

    def _emit_signal(self, interface: str, member: str, body: list) -> None:
        server = self._server
        if server is not None:
            server.emit(interface, member, body)

    async def request_name(self, name: str, flags: int = 0) -> RequestNameReply:
        if self._server is not None:
            return RequestNameReply.ALREADY_OWNER
        if self._bus_type is BusType.SYSTEM:
            return RequestNameReply.EXISTS
        if await ipc.probe_existing_daemon():
            return RequestNameReply.EXISTS
        self._server = ipc.IpcServer(self._interfaces)
        await self._server.start()
        return RequestNameReply.PRIMARY_OWNER

    async def release_name(self, name: str) -> int:
        server, self._server = self._server, None
        if server is not None:
            await server.close()
        return 1

    # -- client role --------------------------------------------------------

    async def call(self, msg: Message) -> Message:
        msg.serial = next(_serials)
        if self._bus_type is BusType.SYSTEM:
            return _error_reply(msg, SERVICE_UNKNOWN, "there is no system bus on this platform")

        state = ipc.read_state()
        if state is None:
            return _error_reply(msg, SERVICE_UNKNOWN, "asm-daemon is not running (no IPC state file)")
        try:
            reader, writer = await ipc.open_authenticated(state)
        except (OSError, asyncio.TimeoutError, ValueError) as exc:
            return _error_reply(msg, SERVICE_UNKNOWN, f"asm-daemon is not reachable: {exc!r}")

        try:
            writer.write(ipc.encode({"id": msg.serial, "interface": msg.interface,
                                     "member": msg.member, "body": msg.body}))
            await writer.drain()
            reply = await ipc.read_json(reader, CALL_TIMEOUT_S)
        except (OSError, asyncio.TimeoutError, ValueError) as exc:
            return _error_reply(msg, FAILED, f"IPC call failed: {exc!r}")
        finally:
            await ipc.close_writer(writer)

        if reply is None:
            return _error_reply(msg, FAILED, "asm-daemon closed the connection")
        if reply.get("ok"):
            return Message(message_type=MessageType.METHOD_RETURN, body=reply.get("body") or [],
                           reply_serial=msg.serial)
        err = reply.get("error") or {}
        return _error_reply(msg, err.get("name", FAILED), err.get("text", ""))

    # -- client role: introspection, proxies, signals -------------------------

    async def _round_trip(self, frame: dict) -> dict:
        """One request on a fresh connection; raises DBusError the way a missing service does."""
        if self._bus_type is BusType.SYSTEM:
            raise DBusError(SERVICE_UNKNOWN, "there is no system bus on this platform")
        state = ipc.read_state()
        if state is None:
            raise DBusError(SERVICE_UNKNOWN, "asm-daemon is not running (no IPC state file)")
        try:
            reader, writer = await ipc.open_authenticated(state)
        except (OSError, asyncio.TimeoutError, ValueError) as exc:
            raise DBusError(SERVICE_UNKNOWN, f"asm-daemon is not reachable: {exc!r}") from exc
        try:
            writer.write(ipc.encode(frame))
            await writer.drain()
            reply = await ipc.read_json(reader, CALL_TIMEOUT_S)
        except (OSError, asyncio.TimeoutError, ValueError) as exc:
            raise DBusError(FAILED, f"IPC call failed: {exc!r}") from exc
        finally:
            await ipc.close_writer(writer)
        if reply is None:
            raise DBusError(FAILED, "asm-daemon closed the connection")
        if not reply.get("ok"):
            err = reply.get("error") or {}
            raise DBusError(err.get("name", FAILED), err.get("text", ""))
        return reply

    async def introspect(self, bus_name: str, path: str, timeout: float = 30.0) -> "Introspection":
        reply = await self._round_trip({"id": next(_serials), "op": "introspect"})
        table = (reply.get("body") or [{}])[0]
        return Introspection(bus_name, path, table if isinstance(table, dict) else {})

    def get_proxy_object(self, bus_name: str, path: str, introspection: "Introspection") -> "ProxyObject":
        return ProxyObject(self, bus_name, path, introspection)

    def _add_signal_handler(self, interface: str, member: str, handler: Callable[..., Any]) -> None:
        self._signal_handlers.setdefault((interface, member), []).append(handler)
        if self._signal_task is None or self._signal_task.done():
            self._signal_task = asyncio.get_running_loop().create_task(self._signal_loop())
        elif self._signal_writer is not None and not self._signal_writer.is_closing():
            self._send_subscribe(self._signal_writer, interface, member)

    def _remove_signal_handler(self, interface: str, member: str, handler: Callable[..., Any]) -> None:
        handlers = self._signal_handlers.get((interface, member), [])
        if handler in handlers:
            handlers.remove(handler)

    @staticmethod
    def _send_subscribe(writer: asyncio.StreamWriter, interface: str, member: str) -> None:
        writer.write(ipc.encode({"id": next(_serials), "op": "subscribe",
                                 "interface": interface, "member": member}))

    async def _signal_loop(self) -> None:
        """Hold one authenticated connection and hand every pushed signal to its handlers.

        When the connection ends (daemon stopped, or ``disconnect()``), ``connected`` turns False
        and ``wait_for_disconnect()`` returns, as with a real bus that went away. The caller's own
        retry loop then builds a fresh MessageBus once the daemon is back.
        """
        writer: asyncio.StreamWriter | None = None
        try:
            state = ipc.read_state()
            if state is None:
                return
            reader, writer = await ipc.open_authenticated(state)
            self._signal_writer = writer
            for interface, member in list(self._signal_handlers):
                self._send_subscribe(writer, interface, member)
            await writer.drain()
            while True:
                frame = await ipc.read_json(reader, None)
                if frame is None:
                    return
                if frame.get("signal"):
                    self._dispatch(frame)
                elif frame.get("ok") is False:
                    log.warning("signal subscription refused: %s", frame.get("error"))
        except asyncio.CancelledError:
            raise
        except (OSError, asyncio.TimeoutError, ValueError, asyncio.IncompleteReadError) as exc:
            log.debug("signal connection ended: %r", exc)
        finally:
            self._signal_writer = None
            if writer is not None:
                await ipc.close_writer(writer)
            self.connected = False
            self._closed.set()

    def _dispatch(self, frame: dict) -> None:
        body = frame.get("body") or []
        for handler in list(self._signal_handlers.get((frame.get("interface"), frame.get("member")), [])):
            try:
                handler(*body)
            except Exception:  # noqa: BLE001 - one broken listener must not silence the others
                log.exception("signal handler for %s.%s failed", frame.get("interface"), frame.get("member"))


class Introspection:
    """What ``introspect()`` returns: the daemon's interfaces with their methods and signals."""

    def __init__(self, bus_name: str, path: str, table: dict):
        self.bus_name = bus_name
        self.path = path
        self.interfaces = table


class ProxyObject:
    def __init__(self, bus: MessageBus, bus_name: str, path: str, introspection: Introspection):
        self._bus = bus
        self.bus_name = bus_name
        self.path = path
        self._introspection = introspection

    def get_interface(self, name: str) -> "ProxyInterface":
        members = self._introspection.interfaces.get(name)
        if members is None:
            raise InterfaceNotFoundError(f"interface not found on {self.path}: {name}")
        return ProxyInterface(self._bus, self.bus_name, self.path, name, members)


class ProxyInterface:
    """``on_<signal>``, ``off_<signal>`` and ``call_<method>``, named like dbus_next's proxies."""

    def __init__(self, bus: MessageBus, bus_name: str, path: str, name: str, members: dict):
        self._bus = bus
        self._bus_name = bus_name
        self._path = path
        self.name = name
        self._signals = {_snake(member): member for member in members.get("signals", [])}
        self._methods = {_snake(member): member for member in members.get("methods", [])}

    def __getattr__(self, attr: str):
        if attr.startswith("_"):
            raise AttributeError(attr)
        if attr.startswith("on_") and attr[3:] in self._signals:
            member = self._signals[attr[3:]]
            return lambda handler: self._bus._add_signal_handler(self.name, member, handler)
        if attr.startswith("off_") and attr[4:] in self._signals:
            member = self._signals[attr[4:]]
            return lambda handler: self._bus._remove_signal_handler(self.name, member, handler)
        if attr.startswith("call_") and attr[5:] in self._methods:
            return self._caller(self._methods[attr[5:]])
        raise AttributeError(f"{self.name} has no member {attr!r}")

    def _caller(self, member: str):
        async def call(*body):
            reply = await self._bus.call(Message(destination=self._bus_name, path=self._path,
                                                 interface=self.name, member=member, body=list(body)))
            if reply.message_type == MessageType.ERROR:
                raise DBusError(reply.error_name or FAILED, str(reply.body[0]) if reply.body else "")
            if not reply.body:
                return None
            return reply.body[0] if len(reply.body) == 1 else list(reply.body)
        return call

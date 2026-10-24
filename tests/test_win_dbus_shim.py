"""The Windows D-Bus stand-in: signature helpers, local IPC, and the real dbus_service on top.

All of it runs on Linux (loopback sockets), so the protocol, auth, error mapping
and the *real* ASM service classes are exercised in CI; only the Windows socket
stack itself is left to the hardware run.
"""
from __future__ import annotations

import asyncio
import importlib
import json
import sys
import threading
import types
from types import SimpleNamespace

import pytest

from arctis_sound_manager.win import install_dbus_shim
from arctis_sound_manager.win.dbus_shim import ipc
from arctis_sound_manager.win.dbus_shim.aio.message_bus import MessageBus
from arctis_sound_manager.win.dbus_shim.constants import (ACCESS_DENIED, FAILED, INVALID_ARGS, SERVICE_UNKNOWN,
                                                          UNKNOWN_INTERFACE, UNKNOWN_METHOD, BusType,
                                                          MessageType, RequestNameReply)
from arctis_sound_manager.win.dbus_shim.errors import DBusError, InterfaceNotFoundError
from arctis_sound_manager.win.dbus_shim.message import Message
from arctis_sound_manager.win.dbus_shim.service import (ServiceInterface, count_complete_types, method,
                                                        pack_result, signal)

IFACE = "test.Interface"


@pytest.fixture(autouse=True)
def isolated_runtime_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    return tmp_path


_TOUCHED = ("dbus_next", "dbus_next.aio", "dbus_next.aio.message_bus", "dbus_next.constants",
            "dbus_next.errors", "dbus_next.message", "dbus_next.service",
            "arctis_sound_manager.dbus_service")


@pytest.fixture(autouse=True)
def sys_modules_sandbox():
    """Put back exactly what was (or was not) in sys.modules, so the shim can never leak
    into other test files, whichever order they run in."""
    before = {name: sys.modules.get(name) for name in _TOUCHED}
    pkg = sys.modules.get("arctis_sound_manager")
    pkg_attr = getattr(pkg, "dbus_service", None)
    yield
    for name, module in before.items():
        if module is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = module
    if pkg is not None:
        if pkg_attr is not None:
            pkg.dbus_service = pkg_attr
        elif hasattr(pkg, "dbus_service"):
            del pkg.dbus_service


class Fake(ServiceInterface):
    def __init__(self):
        super().__init__(IFACE)
        self.seen: list = []

    @method("Echo")
    def echo(self, text: "s") -> "s":  # type: ignore # noqa: F821
        return text

    @method("Add")
    async def add(self, a: "s", b: "s") -> "s":  # type: ignore # noqa: F821
        await asyncio.sleep(0)
        return str(int(a) + int(b))

    @method("Pair")
    def pair(self) -> "bs":  # type: ignore # noqa: F821
        return (True, "yes")

    @method("Nothing")
    def nothing(self):
        self.seen.append("nothing")

    @method("Boom")
    def boom(self) -> "s":  # type: ignore # noqa: F821
        raise RuntimeError("kaput")

    @method("Denied")
    def denied(self) -> "s":  # type: ignore # noqa: F821
        raise DBusError("org.example.Denied", "nope")

    @signal("Changed")
    def changed(self, payload: str) -> "s":  # type: ignore # noqa: F821
        return payload


def call(bus, member, *body, interface=IFACE):
    return bus.call(Message(destination="x", path="/x", interface=interface, member=member,
                            signature="s" * len(body), body=list(body)))


async def served(fake=None):
    server = MessageBus()
    await server.connect()
    server.export("/x", fake or Fake())
    assert await server.request_name("x") is RequestNameReply.PRIMARY_OWNER
    return server


# ── signature helpers ────────────────────────────────────────────────────────

@pytest.mark.parametrize("sig,n", [("", 0), ("s", 1), ("b", 1), ("bs", 2), ("a{sv}", 1), ("as", 1),
                                   ("(ii)s", 2), ("aai", 1), ("a(si)i", 2), ("a{s(ii)}b", 2)])
def test_count_complete_types(sig, n):
    assert count_complete_types(sig) == n


def test_pack_result_follows_the_return_annotation():
    fake = Fake()
    assert pack_result(fake.echo, "hi") == ["hi"]
    assert pack_result(fake.pair, (True, "yes")) == [True, "yes"]
    assert pack_result(fake.nothing, None) == []


def test_decorated_methods_remain_directly_callable_and_return_their_value():
    # dbus_next's wrapper drops the value; ASM works around that (status_json()). The shim must not regress it.
    assert Fake().echo("direct") == "direct"
    assert Fake().changed("p") == "p"


# ── round trips ──────────────────────────────────────────────────────────────

def test_sync_async_multi_and_void_methods():
    async def go():
        server = await served()
        client = MessageBus()
        await client.connect()
        r = await call(client, "Echo", "héllo ✓")
        assert (r.message_type, r.body) == (MessageType.METHOD_RETURN, ["héllo ✓"])
        assert (await call(client, "Add", "2", "40")).body == ["42"]          # async handler
        assert (await call(client, "Pair")).body == [True, "yes"]            # two out-values
        assert (await call(client, "Nothing")).body == []
        await server.release_name("x")
    asyncio.run(go())


def test_error_mapping():
    async def go():
        server = await served()
        client = MessageBus()
        await client.connect()
        r = await call(client, "Nope")
        assert (r.message_type, r.error_name) == (MessageType.ERROR, UNKNOWN_METHOD)
        assert (await call(client, "Echo", interface="other.Iface")).error_name == UNKNOWN_INTERFACE
        assert (await call(client, "Echo")).error_name == INVALID_ARGS        # wrong arity
        assert (await call(client, "Boom")).error_name == FAILED
        assert "kaput" in (await call(client, "Boom")).body[0]
        denied = await call(client, "Denied")
        assert (denied.error_name, denied.body) == ("org.example.Denied", ["nope"])
        await server.release_name("x")
    asyncio.run(go())


def test_a_megabyte_payload_survives_asyncios_default_line_limit():
    async def go():
        server = await served()
        client = MessageBus()
        await client.connect()
        blob = "x" * 1_000_000
        assert (await call(client, "Echo", blob)).body == [blob]
        await server.release_name("x")
    asyncio.run(go())


def test_one_connection_serves_many_calls_in_order():
    async def go():
        server = await served()
        state = ipc.read_state()
        reader, writer = await ipc.open_authenticated(state)
        for i in range(5):
            writer.write(ipc.encode({"id": i, "interface": IFACE, "member": "Echo", "body": [str(i)]}))
            await writer.drain()
            assert (await ipc.read_json(reader, 2))["body"] == [str(i)]
        writer.close()
        await server.release_name("x")
    asyncio.run(go())


# ── no daemon / stale state / system bus ─────────────────────────────────────

def test_no_daemon_is_serviceunknown_not_an_exception():
    async def go():
        client = MessageBus()
        assert await client.connect() is client          # like a real bus: connecting never fails
        r = await call(client, "Echo", "x")
        assert (r.message_type, r.error_name) == (MessageType.ERROR, SERVICE_UNKNOWN)
    asyncio.run(go())


def test_stale_state_file_is_serviceunknown():
    async def go():
        ipc._write_state({"v": 1, "port": 1, "token": "dead", "pid": 1})   # nothing listens on port 1
        client = MessageBus()
        await client.connect()
        assert (await call(client, "Echo", "x")).error_name == SERVICE_UNKNOWN
    asyncio.run(go())


def test_system_bus_has_no_endpoint():
    async def go():
        server = await served()
        bus = MessageBus(bus_type=BusType.SYSTEM)
        await bus.connect()
        assert (await call(bus, "Echo", "x")).error_name == SERVICE_UNKNOWN   # logind does not exist here
        await server.release_name("x")
    asyncio.run(go())


# ── auth and ownership ───────────────────────────────────────────────────────

def test_a_client_without_the_token_is_refused_and_gets_nothing():
    async def go():
        server = await served()
        state = ipc.read_state()
        reader, writer = await asyncio.open_connection("127.0.0.1", state["port"])
        writer.write(ipc.encode({"auth": "wrong"}))
        await writer.drain()
        assert (await ipc.read_json(reader, 2))["error"]["name"] == ACCESS_DENIED
        # and the connection is dead: a request on it is never answered
        writer.write(ipc.encode({"id": 1, "interface": IFACE, "member": "Echo", "body": ["secret"]}))
        await writer.drain()
        assert await ipc.read_json(reader, 2) is None
        writer.close()
        await server.release_name("x")
    asyncio.run(go())


def test_state_file_holds_the_token_and_is_owner_only_on_posix():
    async def go():
        server = await served()
        path = ipc.state_path()
        assert json.loads(path.read_text())["token"]
        if sys.platform != "win32":
            assert path.stat().st_mode & 0o077 == 0
        await server.release_name("x")
    asyncio.run(go())


def test_second_daemon_gets_exists_and_a_successor_can_take_over():
    async def go():
        first = await served()
        second = MessageBus()
        await second.connect()
        second.export("/x", Fake())
        assert await second.request_name("x") is RequestNameReply.EXISTS     # DbusManager reports "already running"
        assert await first.request_name("x") is RequestNameReply.ALREADY_OWNER
        await first.release_name("x")
        assert not ipc.state_path().exists()                                  # cleaned up
        assert await second.request_name("x") is RequestNameReply.PRIMARY_OWNER
        await second.release_name("x")
    asyncio.run(go())


def test_a_dead_daemons_stale_file_does_not_block_a_new_one():
    async def go():
        ipc._write_state({"v": 1, "port": 1, "token": "old", "pid": 1})
        fresh = await served()                                                # not EXISTS: nobody answers port 1
        client = MessageBus()
        await client.connect()
        assert (await call(client, "Echo", "ok")).body == ["ok"]
        await fresh.release_name("x")
    asyncio.run(go())


def test_closing_never_deletes_a_successors_state_file():
    async def go():
        old = ipc.IpcServer({})
        await old.start()
        successor_state = {"v": 1, "port": 4242, "token": "successor", "pid": 2}
        ipc._write_state(successor_state)
        await old.close()
        assert ipc.read_state() == successor_state
    asyncio.run(go())


# ── registration under the real module names ─────────────────────────────────

SHIM_NAMES = ("dbus_next", "dbus_next.aio", "dbus_next.aio.message_bus", "dbus_next.constants",
              "dbus_next.errors", "dbus_next.message", "dbus_next.service")


def test_every_import_form_asm_uses_resolves():
    for name in SHIM_NAMES:
        sys.modules.pop(name, None)
    assert install_dbus_shim() is True
    # the exact statements found in the code base:
    from dbus_next import BusType as BT, Message as M, MessageType as MT        # desktop_session
    from dbus_next.aio import MessageBus as MB1                                 # desktop_session
    from dbus_next.aio.message_bus import MessageBus as MB2                     # gui, cli, status
    from dbus_next.constants import BusType, MessageType, RequestNameReply      # noqa: F401
    from dbus_next.errors import DBusError, InvalidBusNameError, InvalidObjectPathError  # noqa: F401
    from dbus_next.message import Message                                       # noqa: F401
    from dbus_next.service import ServiceInterface, method, signal              # noqa: F401
    assert MB1 is MB2 is MessageBus and BT is BusType and M is Message and MT is MessageType


def test_a_real_dbus_next_that_is_already_loaded_is_left_alone(monkeypatch):
    real = types.ModuleType("dbus_next")
    monkeypatch.setitem(sys.modules, "dbus_next", real)
    assert install_dbus_shim() is False and sys.modules["dbus_next"] is real


# ── the real ASM service classes on top of the shim ──────────────────────────

def _import_dbus_service_under_the_shim():
    """A fresh copy of the real ``arctis_sound_manager.dbus_service`` bound to the shim
    (the autouse sandbox fixture restores the original afterwards)."""
    for name in SHIM_NAMES:
        sys.modules.pop(name, None)
    install_dbus_shim()
    sys.modules.pop("arctis_sound_manager.dbus_service", None)
    return importlib.import_module("arctis_sound_manager.dbus_service")


def test_the_real_status_service_answers_over_the_shim():
    fresh = _import_dbus_service_under_the_shim()
    assert fresh.ServiceInterface is ServiceInterface           # really bound to the shim
    core = SimpleNamespace(device_status=None, device_config=None)

    async def go():
        manager = fresh.DbusManager()
        await manager.start(core)
        client = MessageBus()
        await client.connect()
        reply = await client.call(Message(
            destination=fresh.DBUS_BUS_NAME, path=fresh.DBUS_STATUS_OBJECT_PATH,
            interface=fresh.DBUS_STATUS_INTERFACE_NAME, member="GetStatus"))
        assert reply.message_type == MessageType.METHOD_RETURN
        assert json.loads(reply.body[0]) == {}                  # no device attached: the real code's answer
        unknown = await client.call(Message(
            destination=fresh.DBUS_BUS_NAME, path=fresh.DBUS_STATUS_OBJECT_PATH,
            interface=fresh.DBUS_STATUS_INTERFACE_NAME, member="NoSuchThing"))
        assert unknown.error_name == UNKNOWN_METHOD
        manager.stop()
        await asyncio.sleep(0)
        for task in (manager._status_watch,):
            task.cancel()
        # a second daemon is told the name is taken, which is what DbusManager turns into its error
        other = MessageBus()
        await other.connect()
        other.export("/x", Fake())
        assert await other.request_name("x") is RequestNameReply.EXISTS
    asyncio.run(go())


# ── Windows-specific hardening ───────────────────────────────────────────────

def test_state_file_write_waits_out_a_reader_holding_the_file(monkeypatch):
    """Windows: a client reading ipc.json blocks the rename (PermissionError); it must be retried."""
    real_replace, calls = ipc.os.replace, []

    def flaky(src, dst):
        calls.append(1)
        if len(calls) < 3:
            raise PermissionError(13, "sharing violation")
        return real_replace(src, dst)

    monkeypatch.setattr(ipc.os, "replace", flaky)
    monkeypatch.setattr(ipc.time, "sleep", lambda _s: None)
    ipc._write_state({"v": 1, "port": 7, "token": "t", "pid": 1})
    assert len(calls) == 3 and ipc.read_state()["port"] == 7


def test_state_file_write_gives_up_with_the_original_error(monkeypatch):
    monkeypatch.setattr(ipc.os, "replace", lambda *_: (_ for _ in ()).throw(PermissionError(13, "locked")))
    monkeypatch.setattr(ipc.time, "sleep", lambda _s: None)
    with pytest.raises(PermissionError):
        ipc._write_state({"v": 1, "port": 7, "token": "t", "pid": 1})


def test_every_call_closes_its_connection_before_its_loop_ends():
    """The tray runs asyncio.run() per poll; a connection left for the GC trips Windows' Proactor loop."""
    async def go():
        server = await served()
        closed = []
        original = ipc.close_writer

        async def spy(writer):
            await original(writer)
            closed.append(writer.is_closing())
        ipc_close = ipc.close_writer
        try:
            ipc.close_writer = spy
            client = MessageBus()
            await client.connect()
            assert (await call(client, "Echo", "x")).body == ["x"]
        finally:
            ipc.close_writer = ipc_close
        assert closed and all(closed)
        await server.release_name("x")
    asyncio.run(go())


# ── signals, introspection and proxies (the DAC-wheel / ChatMix OSD path) ────

async def _wait_for(predicate, timeout=5.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        assert asyncio.get_running_loop().time() < deadline, "timed out waiting for the condition"
        await asyncio.sleep(0.01)


async def _listening(received, interface=IFACE, member_attr="on_changed"):
    """A client wired up exactly like gui/chatmix_osd.py does it."""
    client = MessageBus()
    await client.connect()
    intro = await client.introspect("x", "/x")
    iface = client.get_proxy_object("x", "/x", intro).get_interface(interface)
    getattr(iface, member_attr)(lambda *args: received.append(args))
    return client, iface


def test_introspection_lists_methods_and_signals():
    async def go():
        server = await served()
        client = MessageBus()
        await client.connect()
        intro = await client.introspect("x", "/x")
        members = intro.interfaces[IFACE]
        assert "Echo" in members["methods"] and "Changed" in members["signals"]
        assert "Changed" not in members["methods"]
        await server.release_name("x")
    asyncio.run(go())


def test_introspect_without_a_daemon_raises_serviceunknown():
    """chatmix_osd retries on any exception here, so a missing daemon must be an exception."""
    async def go():
        client = MessageBus()
        await client.connect()
        with pytest.raises(DBusError) as caught:
            await client.introspect("x", "/x")
        assert caught.value.type == SERVICE_UNKNOWN
        with pytest.raises(DBusError):
            await MessageBus(bus_type=BusType.SYSTEM).introspect("org.freedesktop.login1", "/")
    asyncio.run(go())


def test_unknown_interface_on_a_proxy_is_an_error_not_a_silent_object():
    async def go():
        server = await served()
        client = MessageBus()
        await client.connect()
        proxy = client.get_proxy_object("x", "/x", await client.introspect("x", "/x"))
        with pytest.raises(InterfaceNotFoundError):
            proxy.get_interface("no.such.Interface")
        iface = proxy.get_interface(IFACE)
        with pytest.raises(AttributeError):
            iface.on_no_such_signal(lambda *a: None)
        await server.release_name("x")
    asyncio.run(go())


def test_a_signal_reaches_a_subscribed_client():
    async def go():
        fake = Fake()
        server = await served(fake)
        received: list = []
        client, _ = await _listening(received)
        await _wait_for(lambda: server._server._subscribers)       # subscription is registered
        assert fake.changed("hello") == "hello"                    # emitting still returns the payload
        await _wait_for(lambda: received)
        assert received == [("hello",)]
        client.disconnect()
        await server.release_name("x")
    asyncio.run(go())


def test_signals_arrive_in_order_and_only_for_the_subscribed_member():
    class Two(Fake):
        @signal("Other")
        def other(self, payload: str) -> "s":  # type: ignore # noqa: F821
            return payload

    async def go():
        fake = Two()
        server = await served(fake)
        received: list = []
        client, _ = await _listening(received)                     # listens to Changed only
        await _wait_for(lambda: server._server._subscribers)
        for i in range(5):
            fake.changed(str(i))
            fake.other("not for us")
        await _wait_for(lambda: len(received) == 5)
        await asyncio.sleep(0.05)
        assert received == [(str(i),) for i in range(5)]
        client.disconnect()
        await server.release_name("x")
    asyncio.run(go())


def test_a_handler_added_after_the_connection_is_up_is_subscribed_too():
    class Two(Fake):
        @signal("Other")
        def other(self, payload: str) -> "s":  # type: ignore # noqa: F821
            return payload

    async def go():
        fake = Two()
        server = await served(fake)
        first: list = []
        client, iface = await _listening(first)
        await _wait_for(lambda: server._server._subscribers)
        second: list = []
        iface.on_other(lambda *args: second.append(args))
        await _wait_for(lambda: any(len(w) == 2 for w in server._server._subscribers.values()))
        fake.other("late")
        await _wait_for(lambda: second)
        assert second == [("late",)] and first == []
        client.disconnect()
        await server.release_name("x")
    asyncio.run(go())


def test_off_stops_delivery_and_a_broken_handler_does_not_silence_the_others():
    async def go():
        fake = Fake()
        server = await served(fake)
        got: list = []
        client = MessageBus()
        await client.connect()
        iface = client.get_proxy_object("x", "/x", await client.introspect("x", "/x")).get_interface(IFACE)

        def boom(*_args):
            raise RuntimeError("listener bug")

        def keep(*args):
            got.append(args)

        iface.on_changed(boom)
        iface.on_changed(keep)
        await _wait_for(lambda: server._server._subscribers)
        fake.changed("a")
        await _wait_for(lambda: got)
        iface.off_changed(keep)
        fake.changed("b")
        await asyncio.sleep(0.2)
        assert got == [("a",)]
        client.disconnect()
        await server.release_name("x")
    asyncio.run(go())


def test_a_signal_emitted_from_another_thread_is_delivered():
    """core.py runs its status refreshes on worker threads."""
    async def go():
        fake = Fake()
        server = await served(fake)
        received: list = []
        client, _ = await _listening(received)
        await _wait_for(lambda: server._server._subscribers)
        # Writing to an asyncio transport from a foreign thread is only safe on the loop thread
        # (it is outright broken on Windows' Proactor loop), so the push itself must hop over.
        loop_thread = threading.get_ident()
        pushed_on: list[int] = []
        original_push = server._server._push
        server._server._push = lambda *a: (pushed_on.append(threading.get_ident()), original_push(*a))[1]
        worker = threading.Thread(target=lambda: fake.changed("from a thread"))
        worker.start()
        await _wait_for(lambda: received)
        worker.join()
        assert received == [("from a thread",)]
        assert pushed_on == [loop_thread]
        client.disconnect()
        await server.release_name("x")
    asyncio.run(go())


def test_emitting_with_nobody_listening_or_no_bus_is_harmless():
    fake = Fake()
    assert fake.changed("no bus yet") == "no bus yet"              # never exported

    async def go():
        server = await served(fake)
        assert fake.changed("no subscribers") == "no subscribers"
        await server.release_name("x")
        assert fake.changed("server gone") == "server gone"
    asyncio.run(go())


def test_the_listener_learns_that_the_daemon_went_away():
    """chatmix_osd loops on ``bus.connected`` and reconnects with a fresh bus when it drops."""
    async def go():
        server = await served()
        received: list = []
        client, _ = await _listening(received)
        await _wait_for(lambda: server._server._subscribers)
        assert client.connected
        await server.release_name("x")
        await _wait_for(lambda: not client.connected)
        await asyncio.wait_for(client.wait_for_disconnect(), 5)
    asyncio.run(go())


def test_disconnect_closes_the_listener_connection():
    async def go():
        server = await served()
        client, _ = await _listening([])
        await _wait_for(lambda: server._server._subscribers)
        client.disconnect()
        await _wait_for(lambda: not server._server._subscribers)
        assert not client.connected
        await server.release_name("x")
    asyncio.run(go())


def test_a_listener_that_stops_reading_is_dropped_instead_of_buffered_forever(monkeypatch):
    async def go():
        monkeypatch.setattr(ipc, "MAX_PENDING_BYTES", 1)           # any pending byte is "too much"
        fake = Fake()
        server = await served(fake)
        reader, writer = await ipc.open_authenticated(ipc.read_state())
        writer.write(ipc.encode({"id": 1, "op": "subscribe", "interface": IFACE, "member": "Changed"}))
        await writer.drain()
        assert (await ipc.read_json(reader, 5))["ok"] is True
        await _wait_for(lambda: server._server._subscribers)
        # Make the daemon-side buffer non-empty without the client ever reading it.
        sub = next(iter(server._server._subscribers))
        sub.transport.pause_reading()
        monkeypatch.setattr(sub.transport.__class__, "get_write_buffer_size", lambda self: 10, raising=False)
        fake.changed("x" * 10)
        await _wait_for(lambda: not server._server._subscribers)
        await ipc.close_writer(writer)
        await server.release_name("x")
    asyncio.run(go())


def test_subscribing_to_something_that_is_not_a_signal_is_refused():
    async def go():
        server = await served()
        reader, writer = await ipc.open_authenticated(ipc.read_state())
        for frame in ({"interface": "no.such", "member": "Changed"},
                      {"interface": IFACE, "member": "Echo"}):       # a method, not a signal
            writer.write(ipc.encode({"id": 7, "op": "subscribe", **frame}))
            await writer.drain()
            reply = await ipc.read_json(reader, 5)
            assert reply["ok"] is False
        assert not server._server._subscribers
        await ipc.close_writer(writer)
        await server.release_name("x")
    asyncio.run(go())


def test_proxy_call_methods_follow_dbus_nexts_conventions():
    async def go():
        server = await served()
        client = MessageBus()
        await client.connect()
        iface = client.get_proxy_object("x", "/x", await client.introspect("x", "/x")).get_interface(IFACE)
        assert await iface.call_echo("hi") == "hi"            # one value comes back bare
        assert await iface.call_pair() == [True, "yes"]       # several come back as a list
        assert await iface.call_nothing() is None
        with pytest.raises(DBusError) as caught:
            await iface.call_denied()
        assert caught.value.type == "org.example.Denied"
        await server.release_name("x")
    asyncio.run(go())


def test_the_real_status_watcher_feeds_the_dac_wheel_listener():
    """Real DbusManager._watch_status -> real StatusChanged signal -> shim -> the code path chatmix_osd uses."""
    fresh = _import_dbus_service_under_the_shim()
    payloads = ['{"headset_volume": 1}']
    fresh.ArctisManagerDbusStatusService.status_json = lambda self: payloads[-1]
    fresh.DbusManager.STATUS_WATCH_INTERVAL = 0.02
    core = SimpleNamespace(device_status=None, device_config=None)

    async def go():
        manager = fresh.DbusManager()
        await manager.start(core)
        received: list = []
        client = MessageBus()
        await client.connect()
        intro = await client.introspect(fresh.DBUS_BUS_NAME, fresh.DBUS_STATUS_OBJECT_PATH)
        iface = client.get_proxy_object(fresh.DBUS_BUS_NAME, fresh.DBUS_STATUS_OBJECT_PATH,
                                        intro).get_interface(fresh.DBUS_STATUS_INTERFACE_NAME)
        iface.on_status_changed(lambda payload: received.append(json.loads(payload)))
        await asyncio.sleep(0.2)                                  # baseline is taken, nothing to report yet
        assert received == []
        payloads.append('{"headset_volume": 2}')
        await _wait_for(lambda: received)
        assert received == [{"headset_volume": 2}]
        client.disconnect()
        manager.stop()
        manager._status_watch.cancel()
        await asyncio.sleep(0)
    asyncio.run(go())


def test_the_daemon_only_pushes_the_signals_a_connection_asked_for():
    """Filtering must happen on the daemon, not rely on the client ignoring strangers."""
    class Two(Fake):
        @signal("Other")
        def other(self, payload: str) -> "s":  # type: ignore # noqa: F821
            return payload

    async def go():
        fake = Two()
        server = await served(fake)
        reader, writer = await ipc.open_authenticated(ipc.read_state())
        writer.write(ipc.encode({"id": 1, "op": "subscribe", "interface": IFACE, "member": "Changed"}))
        await writer.drain()
        assert (await ipc.read_json(reader, 5))["ok"] is True
        fake.other("not requested")
        fake.changed("requested")
        frame = await ipc.read_json(reader, 5)
        assert frame["member"] == "Changed" and frame["body"] == ["requested"]
        await ipc.close_writer(writer)
        await server.release_name("x")
    asyncio.run(go())

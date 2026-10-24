"""The engine's control channel: loopback TCP, JSON lines, a token from the port file."""
from __future__ import annotations

import json
import os
import socket
import threading

import pytest

from arctis_sound_manager.win.audio.control import (MAX_LINE, ControlServer, EngineClient, EngineUnavailable,
                                                    port_file)


@pytest.fixture
def served(tmp_path):
    seen = []

    def handler(req):
        seen.append(req)
        if req["cmd"] == "boom":
            raise ValueError("kaboom")
        if req["cmd"] == "echo":
            return {"echo": req.get("value")}
        return {}

    server = ControlServer(handler, tmp_path)
    server.start()
    yield server, EngineClient(tmp_path, timeout=2.0), seen, tmp_path
    server.stop()


def raw(port, data: bytes) -> bytes:
    with socket.create_connection(("127.0.0.1", port), timeout=2) as s:
        s.sendall(data)
        out = b""
        while not out.endswith(b"\n"):
            chunk = s.recv(4096)
            if not chunk:
                break
            out += chunk
        return out


def test_the_port_file_holds_port_token_and_pid(served):
    server, client, seen, tmp = served
    info = json.loads(port_file(tmp).read_text())
    assert info == {"port": server.port, "token": server.token, "pid": os.getpid()}
    assert server._server.server_address[0] == "127.0.0.1"                 # loopback only


def test_requests_and_replies_round_trip(served):
    server, client, seen, tmp = served
    assert client.request("echo", value=[1, "ü", {"a": None}]) == {"ok": True, "echo": [1, "ü", {"a": None}]}
    assert client.available()
    assert [r["cmd"] for r in seen] == ["echo", "ping"]
    assert "token" in seen[0]


def test_the_convenience_calls_send_what_the_engine_expects(served):
    server, client, seen, tmp = served
    client.set_volume("game", 80)
    client.set_mute("chat", True)
    client.set_chatmix(60, 40)
    client.set_chatmix(60, 40, ["game", "media"])
    client.status(); client.reload(); client.reconfigure(); client.shutdown()
    cmds = [{k: v for k, v in r.items() if k != "token"} for r in seen]
    assert cmds == [{"cmd": "volume", "channel": "game", "pct": 80},
                    {"cmd": "mute", "channel": "chat", "muted": True},
                    {"cmd": "chatmix", "channels_pct": 60, "chat_pct": 40},
                    {"cmd": "chatmix", "channels_pct": 60, "chat_pct": 40, "channels": ["game", "media"]},
                    {"cmd": "status"}, {"cmd": "reload"}, {"cmd": "reconfigure"}, {"cmd": "shutdown"}]


def test_a_wrong_token_is_refused_and_the_handler_never_runs(served):
    server, client, seen, tmp = served
    reply = json.loads(raw(server.port, json.dumps({"token": "nope", "cmd": "volume"}).encode() + b"\n"))
    assert reply == {"ok": False, "error": "bad token"} and seen == []
    reply = json.loads(raw(server.port, json.dumps({"cmd": "volume"}).encode() + b"\n"))
    assert reply["ok"] is False and seen == []


@pytest.mark.parametrize("junk", [b"hello\n", b"[1, 2]\n", b"\xff\xfe\n", b"42\n"])
def test_junk_is_answered_not_fatal(served, junk):
    server, client, seen, tmp = served
    assert json.loads(raw(server.port, junk))["ok"] is False
    assert client.available()                                              # the server is still fine


def test_a_failing_handler_becomes_an_error_reply(served):
    server, client, seen, tmp = served
    reply = client.request("boom")
    assert reply["ok"] is False and "ValueError: kaboom" in reply["error"]
    assert client.available()


def test_oversized_requests_are_cut_off(served):
    server, client, seen, tmp = served
    reply = json.loads(raw(server.port, b"x" * (MAX_LINE + 10) + b"\n"))
    assert reply == {"ok": False, "error": "request too long"} and seen == []


def test_several_requests_on_one_connection_and_several_clients(served):
    server, client, seen, tmp = served
    token = server.token
    with socket.create_connection(("127.0.0.1", server.port), timeout=2) as s:
        f = s.makefile("rwb")
        for i in range(3):
            f.write((json.dumps({"token": token, "cmd": "echo", "value": i}) + "\n").encode())
            f.flush()
            assert json.loads(f.readline())["echo"] == i
    results = []
    threads = [threading.Thread(target=lambda i=i: results.append(client.request("echo", value=i)["echo"])) for i in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sorted(results) == list(range(8))


def test_without_a_port_file_there_is_no_engine(tmp_path):
    client = EngineClient(tmp_path)
    assert not client.available()
    with pytest.raises(EngineUnavailable):
        client.status()


def test_a_stale_port_file_means_no_engine(tmp_path):
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()                                                              # nothing listens there now
    port_file(tmp_path).write_text(json.dumps({"port": port, "token": "t", "pid": 1}))
    with pytest.raises(EngineUnavailable):
        EngineClient(tmp_path, timeout=0.5).request("ping")


def test_a_garbled_port_file_is_no_engine(tmp_path):
    port_file(tmp_path).write_text("{oops")
    assert not EngineClient(tmp_path).available()


def test_stop_removes_the_port_file_but_not_a_newer_engines(tmp_path):
    a = ControlServer(lambda r: {}, tmp_path)
    a.start()
    b = ControlServer(lambda r: {}, tmp_path)                              # a newer engine took over the file
    info = json.loads(port_file(tmp_path).read_text())
    port_file(tmp_path).write_text(json.dumps({**info, "pid": os.getpid() + 1}))
    a.stop()
    assert port_file(tmp_path).exists()                                    # not ours any more: left alone
    b.start()
    b.stop()
    assert not port_file(tmp_path).exists()


def test_after_stop_the_client_gets_unavailable(tmp_path):
    server = ControlServer(lambda r: {}, tmp_path)
    server.start()
    client = EngineClient(tmp_path, timeout=0.5)
    assert client.available()
    server.stop()
    assert not client.available()

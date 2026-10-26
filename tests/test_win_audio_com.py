"""COM on the thread that touches a PortAudio stream (Windows).

On a real Windows runner a stream started from any thread but the main one failed with "Error starting stream ...
-9999" until COM was initialised on that thread; the engine rebuilds from other threads (control channel, supervisor).
"""
from __future__ import annotations

import threading

import pytest

from arctis_sound_manager.win.audio import backend as backend_mod
from arctis_sound_manager.win.audio.backend import DeviceInfo, SoundDeviceBackend, ensure_com


class FakeOle32:
    """What ``ctypes.windll.ole32`` offers: CoInitializeEx, remembering which threads called it."""

    def __init__(self, result=0):
        self.result = result
        self.calls: list[tuple[str, int]] = []
        self.ready_threads: set[int] = set()
        self._lock = threading.Lock()

    def CoInitializeEx(self, reserved, flags):                      # noqa: N802 — the Windows name
        with self._lock:
            self.calls.append((threading.current_thread().name, flags))
            if self.result >= 0:
                self.ready_threads.add(threading.get_ident())
        return self.result


@pytest.fixture
def windows(monkeypatch):
    """Pretend to be Windows with a fake ole32, and forget which threads were initialised."""
    ole = FakeOle32()
    monkeypatch.setattr(backend_mod, "_IS_WINDOWS", True)
    monkeypatch.setattr(backend_mod, "_load_ole32", lambda: ole)
    backend_mod._com_state = threading.local()
    yield ole
    backend_mod._com_state = threading.local()


def on_thread(fn, name="worker"):
    box = {}

    def run():
        try:
            box["value"] = fn()
        except BaseException as exc:                                # noqa: BLE001
            box["error"] = exc

    t = threading.Thread(target=run, name=name)
    t.start()
    t.join(10)
    if "error" in box:
        raise box["error"]
    return box.get("value")


# ── ensure_com ───────────────────────────────────────────────────────────────

def test_off_windows_nothing_happens(monkeypatch):
    ole = FakeOle32()
    monkeypatch.setattr(backend_mod, "_IS_WINDOWS", False)
    monkeypatch.setattr(backend_mod, "_load_ole32", lambda: ole)
    backend_mod._com_state = threading.local()
    ensure_com()
    assert ole.calls == []


def test_com_is_initialised_once_per_thread_and_multi_threaded(windows):
    ensure_com()
    ensure_com()
    assert [flags for _, flags in windows.calls] == [0]             # COINIT_MULTITHREADED, a single time on this thread
    on_thread(ensure_com, "one")
    on_thread(ensure_com, "two")
    assert [name for name, _ in windows.calls][1:] == ["one", "two"]


def test_a_thread_that_already_has_com_as_an_apartment_counts_as_ready(windows):
    windows.result = backend_mod._RPC_E_CHANGED_MODE
    ensure_com()
    ensure_com()
    assert len(windows.calls) == 1


def test_a_failed_initialisation_is_tried_again_and_never_raises(windows):
    windows.result = -2147024882                                     # E_OUTOFMEMORY
    ensure_com()
    ensure_com()
    assert len(windows.calls) == 2
    windows.result = 0
    ensure_com()
    ensure_com()
    assert len(windows.calls) == 3


def test_a_machine_without_ole32_is_not_an_error(monkeypatch):
    monkeypatch.setattr(backend_mod, "_IS_WINDOWS", True)

    def boom():
        raise OSError("no ole32")
    monkeypatch.setattr(backend_mod, "_load_ole32", boom)
    backend_mod._com_state = threading.local()
    ensure_com()                                                     # must not raise


# ── the streams ──────────────────────────────────────────────────────────────

DEVICES = [{"name": "CABLE Input (VB-Audio Virtual Cable)", "index": 11, "hostapi": 0, "max_input_channels": 0,
            "max_output_channels": 2, "default_samplerate": 48000.0},
           {"name": "CABLE Output (VB-Audio Virtual Cable)", "index": 12, "hostapi": 0, "max_input_channels": 2,
            "max_output_channels": 0, "default_samplerate": 48000.0}]


class PickyPortAudio:
    """A sounddevice stand-in that behaves like the real one on Windows: a stream can be opened anywhere, but starting,
    stopping or closing it needs COM on the calling thread."""

    def __init__(self, ole):
        self.ole = ole
        self.events: list[str] = []
        outer = self

        class WasapiSettings:
            def __init__(self, exclusive=False, auto_convert=None):
                pass

        class Stream:
            def __init__(self, **kwargs):
                self.active = False

            def _need_com(self, what):
                if threading.get_ident() not in outer.ole.ready_threads:
                    outer.events.append(f"{what} refused")
                    raise RuntimeError("Error starting stream: Unanticipated host error [PaErrorCode -9999]")
                outer.events.append(what)

            def start(self):
                self._need_com("start")
                self.active = True

            def stop(self):
                self._need_com("stop")
                self.active = False

            def close(self):
                self._need_com("close")

        self.WasapiSettings = WasapiSettings
        self.InputStream = type("InputStream", (Stream,), {})
        self.OutputStream = type("OutputStream", (Stream,), {})

    def query_hostapis(self):
        return [{"name": "Windows WASAPI", "default_input_device": 12, "default_output_device": 11}]

    def query_devices(self):
        return DEVICES


def a_cable(backend):
    return next(d for d in backend.devices() if d.name.startswith("CABLE Input"))


def test_a_stream_can_be_started_from_a_thread_that_never_set_up_com(windows):
    backend = SoundDeviceBackend(PickyPortAudio(windows))

    def rebuild():
        stream = backend.open_output(a_cable(backend), 2, lambda out: None)
        stream.start()
        stream.stop()
        stream.close()
        return True

    assert on_thread(rebuild, "control-channel")
    assert backend._sd.events == ["start", "stop", "close"]


def test_without_the_fix_the_same_thing_fails_like_on_the_real_machine(windows, monkeypatch):
    monkeypatch.setattr(backend_mod, "ensure_com", lambda: None)    # what the code did before
    backend = SoundDeviceBackend(PickyPortAudio(windows))
    stream = backend.open_output(a_cable(backend), 2, lambda out: None)
    with pytest.raises(RuntimeError, match="Error starting stream"):
        on_thread(stream.start, "control-channel")


def test_every_entry_point_sets_com_up_before_it_touches_portaudio(windows):
    order: list[str] = []
    real = backend_mod.ensure_com

    def spy():
        order.append("com")
        real()

    sd = PickyPortAudio(windows)
    backend_mod_ensure = backend_mod.ensure_com
    backend_mod.ensure_com = spy
    try:
        backend = SoundDeviceBackend(sd)
        backend.devices()
        stream = backend.open_input(next(d for d in backend.devices() if d.max_inputs), 1, lambda block: None)
        stream.start()
        stream.stop()
        stream.close()
    finally:
        backend_mod.ensure_com = backend_mod_ensure
    assert order.count("com") >= 6                                   # devices x2, open, start, stop, close
    assert sd.events == ["start", "stop", "close"]

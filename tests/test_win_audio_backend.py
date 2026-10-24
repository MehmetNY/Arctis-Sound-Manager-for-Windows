"""Device lookup and stream opening, against a stand-in for the ``sounddevice`` module."""
from __future__ import annotations

import sys

import numpy as np
import pytest

from arctis_sound_manager.win.audio.backend import (BackendError, DeviceInfo, FakeBackend, SoundDeviceBackend,
                                                    find_device)


def dev(index, name, host="Windows WASAPI", ins=0, outs=0, default_in=False, default_out=False):
    return DeviceInfo(index, name, host, ins, outs, 48000.0, default_in, default_out)


DEVICES = [
    dev(0, "CABLE Output (VB-Audio Virtual Cable)", "MME", ins=2),
    dev(1, "CABLE Input (VB-Audio Virtual Cable)", "MME", outs=2),
    dev(2, "Speakers (Realtek(R) Audio)", "MME", outs=2, default_out=True),
    dev(10, "CABLE Output (VB-Audio Virtual Cable)", ins=8),
    dev(11, "CABLE Input (VB-Audio Virtual Cable)", outs=8),
    dev(12, "CABLE-A Output (VB-Audio Cable A)", ins=2),
    dev(13, "Speakers (Realtek(R) Audio)", outs=2, default_out=True),
    dev(14, "Headset Earphone (Arctis Nova Pro Wireless)", outs=2),
    dev(15, "Headset Microphone (Arctis Nova Pro Wireless)", ins=1, default_in=True),
    dev(20, "CABLE Output (VB-Audio Virtual Cable) 2", ins=2),
]


# ── lookup ───────────────────────────────────────────────────────────────────

def test_find_by_name_prefers_wasapi_then_exact_then_shortest():
    assert find_device(DEVICES, "cable output", "input").index == 10             # the WASAPI copy, not the MME one
    assert find_device(DEVICES, "CABLE Output (VB-Audio Virtual Cable)", "input").index == 10
    assert find_device(DEVICES, "arctis", "output").name.startswith("Headset Earphone")
    assert find_device(DEVICES, "arctis", "input").name.startswith("Headset Microphone")


def test_find_respects_the_direction():
    assert find_device(DEVICES, "cable input", "input") is None                   # it only renders
    assert find_device(DEVICES, "cable-a", "output") is None
    assert find_device(DEVICES, "cable-a", "input").index == 12


def test_find_falls_back_to_other_host_apis_when_there_is_no_wasapi_copy():
    only_mme = [d for d in DEVICES if d.hostapi == "MME"]
    assert find_device(only_mme, "cable output", "input").index == 0


def test_find_by_index_default_and_empty():
    assert find_device(DEVICES, "index:11", "output").index == 11
    assert find_device(DEVICES, "index:11", "input") is None                       # wrong direction
    assert find_device(DEVICES, "index:abc", "output") is None
    assert find_device(DEVICES, "default", "output").index == 13                  # the WASAPI default
    assert find_device(DEVICES, "default", "input").index == 15
    assert find_device(DEVICES, "", "output") is None and find_device(DEVICES, None, "input") is None
    assert find_device(DEVICES, "no such device", "output") is None


def test_exact_name_beats_a_longer_one_that_contains_it():
    assert find_device(DEVICES, "cable output (vb-audio virtual cable)", "input").index == 10   # not "... 2"


# ── a stand-in for sounddevice ───────────────────────────────────────────────

class FakePortAudioError(Exception):
    pass


class FakeSD:
    """Just enough of the sounddevice module: device queries, WasapiSettings and the two stream classes."""

    def __init__(self, refuse=None, old_wasapi=False):
        self.refuse = refuse or (lambda **kw: False)          # kwargs -> True when PortAudio would refuse
        self.old_wasapi = old_wasapi
        self.created = []
        outer = self

        class WasapiSettings:
            def __init__(self, exclusive=False, auto_convert=None):
                if auto_convert is not None and outer.old_wasapi:
                    raise TypeError("unexpected keyword 'auto_convert'")
                self.exclusive, self.auto_convert = exclusive, bool(auto_convert)

        class Stream:
            kind = ""

            def __init__(self, **kwargs):
                if outer.refuse(kind=self.kind, **kwargs):
                    raise FakePortAudioError("Invalid number of channels [PaErrorCode -9998]")
                self.kwargs = kwargs
                self.started = self.closed = False
                self.active = False
                outer.created.append(self)

            def start(self):
                self.active = self.started = True

            def stop(self):
                self.active = False

            def close(self):
                self.closed = True

        class InputStream(Stream):
            kind = "input"

        class OutputStream(Stream):
            kind = "output"

        self.WasapiSettings, self.InputStream, self.OutputStream = WasapiSettings, InputStream, OutputStream

    def query_hostapis(self):
        return [{"name": "MME", "default_input_device": 0, "default_output_device": 2},
                {"name": "Windows WASAPI", "default_input_device": 15, "default_output_device": 13}]

    def query_devices(self):
        return [{"name": d.name, "index": d.index, "hostapi": 0 if d.hostapi == "MME" else 1,
                 "max_input_channels": d.max_inputs, "max_output_channels": d.max_outputs,
                 "default_samplerate": 48000.0} for d in sorted(DEVICES, key=lambda d: d.index)]


def test_devices_are_listed_with_their_host_api_and_defaults():
    backend = SoundDeviceBackend(FakeSD())
    listed = backend.devices()
    assert len(listed) == len(DEVICES)
    names = {d.index: d for d in listed}
    assert names[10].wasapi and not names[0].wasapi and names[0].hostapi == "MME"
    assert names[10].max_inputs == 8 and names[11].max_outputs == 8


def test_every_host_api_default_is_flagged_and_default_lookup_prefers_wasapi():
    listed = SoundDeviceBackend(FakeSD()).devices()
    flagged_out = {d.index for d in listed if d.default_output}
    flagged_in = {d.index for d in listed if d.default_input}
    assert flagged_out == {2, 13} and flagged_in == {0, 15}
    assert find_device(listed, "default", "output").index == 13
    assert find_device(listed, "default", "input").index == 15


# ── opening ──────────────────────────────────────────────────────────────────

def test_opening_a_capture_stream_asks_for_48k_float_shared_mode_with_auto_convert():
    sd = FakeSD()
    backend = SoundDeviceBackend(sd, latency="low")
    got = []
    opened = backend.open_input(find_device(DEVICES, "cable output", "input"), 8, got.append)
    s = sd.created[-1]
    assert opened.channels == 8
    k = s.kwargs
    assert k["samplerate"] == 48000 and k["dtype"] == "float32" and k["channels"] == 8 and k["device"] == 10
    assert k["latency"] == "low" and k["blocksize"] == 0
    assert k["extra_settings"].exclusive is False and k["extra_settings"].auto_convert is True
    opened.start()
    assert opened.active
    k["callback"](np.ones((480, 8), dtype=np.float32), 480, None, None)
    assert got[0].shape == (480, 8)
    opened.stop()
    opened.close()
    assert not opened.active and s.closed


def test_a_stereo_cable_that_refuses_eight_channels_is_opened_with_two():
    sd = FakeSD(refuse=lambda kind, channels, **kw: channels > 2)
    opened = SoundDeviceBackend(sd).open_input(find_device(DEVICES, "cable output", "input"), 8, lambda x: None)
    assert opened.channels == 2                                          # the caller learns what it really got


def test_auto_convert_is_dropped_when_the_device_refuses_it():
    sd = FakeSD(refuse=lambda kind, extra_settings=None, **kw: extra_settings is not None and extra_settings.auto_convert)
    opened = SoundDeviceBackend(sd).open_output(find_device(DEVICES, "arctis", "output"), 2, lambda x: None)
    assert opened.channels == 2 and sd.created[-1].kwargs["extra_settings"].auto_convert is False


def test_an_old_sounddevice_without_auto_convert_still_opens():
    sd = FakeSD(old_wasapi=True)
    opened = SoundDeviceBackend(sd).open_output(find_device(DEVICES, "arctis", "output"), 2, lambda x: None)
    assert opened.channels == 2 and sd.created[-1].kwargs["extra_settings"].exclusive is False


def test_non_wasapi_devices_get_no_extra_settings():
    sd = FakeSD()
    SoundDeviceBackend(sd).open_output(find_device([d for d in DEVICES if d.hostapi == "MME"], "speakers", "output"), 2,
                                       lambda x: None)
    assert "extra_settings" not in sd.created[-1].kwargs


def test_channel_options_never_exceed_what_the_device_has():
    f = SoundDeviceBackend._channel_options
    assert f(8, 8) == [8, 2, 1] and f(8, 2) == [2, 1] and f(2, 8) == [2, 8, 1] and f(1, 1) == [1] and f(8, 0) == []


def test_every_attempt_failing_gives_a_helpful_error():
    sd = FakeSD(refuse=lambda **kw: True)
    with pytest.raises(BackendError, match="48000 Hz"):
        SoundDeviceBackend(sd).open_output(find_device(DEVICES, "arctis", "output"), 2, lambda x: None)


def test_callbacks_never_raise_into_portaudio_and_outputs_fall_silent():
    sd = FakeSD()
    backend = SoundDeviceBackend(sd)
    flags = []

    def boom(data):
        raise RuntimeError("bug")
    backend.open_input(find_device(DEVICES, "cable output", "input"), 2, boom, status=flags.append)
    sd.created[-1].kwargs["callback"](np.zeros((10, 2), dtype=np.float32), 10, None, "input overflow")
    assert flags == ["input overflow"]                                   # the status still reached us
    backend.open_output(find_device(DEVICES, "arctis", "output"), 2, boom)
    out = np.ones((10, 2), dtype=np.float32)
    sd.created[-1].kwargs["callback"](out, 10, None, None)
    assert not np.any(out)


def test_a_missing_sounddevice_package_is_reported_clearly(monkeypatch):
    monkeypatch.setitem(sys.modules, "sounddevice", None)
    with pytest.raises(BackendError, match="pip install sounddevice"):
        SoundDeviceBackend()


# ── the in-memory backend ────────────────────────────────────────────────────

def test_fake_backend_hands_out_streams_that_can_be_driven_by_hand():
    fake = FakeBackend(DEVICES)
    got = []
    cin = fake.open_input(find_device(DEVICES, "cable-a", "input"), 8, got.append)
    assert cin.channels == 2                                             # capped by the device
    cin.start()
    cin.deliver(np.ones((480, 2)))
    assert got[0].dtype == np.float32 and cin.frames == 480 and cin.active
    cout = fake.open_output(find_device(DEVICES, "arctis", "output"), 2, lambda out: out.__setitem__(slice(None), 0.5))
    assert np.all(cout.collect(100) == 0.5)
    assert fake.stream("arctis", "output") is cout
    with pytest.raises(KeyError):
        fake.stream("nope", "input")


def test_fake_backend_can_refuse_a_device():
    fake = FakeBackend(DEVICES, max_channels={"Speakers (Realtek(R) Audio)": 0})
    with pytest.raises(BackendError):
        fake.open_output(find_device(DEVICES, "speakers", "output"), 2, lambda out: None)

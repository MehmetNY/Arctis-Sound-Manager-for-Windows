"""Streamer mode in the Windows engine: a second, independent mix (Game + Chat + Media + Aux + Mic) for OBS."""
from __future__ import annotations

import json

import numpy as np
import pytest

from arctis_sound_manager.win.audio.backend import DeviceInfo, FakeBackend
from arctis_sound_manager.win.audio.dynamics import db2lin
from arctis_sound_manager.win.audio.engine_config import EngineConfig
from arctis_sound_manager.win.audio.graph import (BLOCK, EngineGraph, StreamMix, to_stereo, volume_gain)
from arctis_sound_manager.win.audio.params import StreamSpec, load_stream_spec
from arctis_sound_manager.win.audio.runtime import Engine

RATE = 48000
CHUNK = 480


def spec(enabled=True, **levels):
    """StreamSpec with ``name=(volume, muted)`` overrides; everything else at 100 %."""
    out = []
    for ch in ("game", "chat", "media", "aux", "mic"):
        volume, muted = levels.get(ch, (100.0, False))
        out.append((ch, float(volume), bool(muted)))
    return StreamSpec(enabled=enabled, levels=tuple(out))


# ── the state files ──────────────────────────────────────────────────────────

def write_state(tmp_path, streamer=None, mix=None, raw_yaml=None):
    if raw_yaml is not None:
        (tmp_path / "general_settings.yaml").write_text(raw_yaml)
    elif streamer is not None:
        (tmp_path / "general_settings.yaml").write_text(f"streamer_mode: {'true' if streamer else 'false'}\nother: 1\n")
    if mix is not None:
        (tmp_path / "stream_mix.json").write_text(mix if isinstance(mix, str) else json.dumps(mix))


def test_without_any_file_streamer_mode_is_off_and_everything_is_full(tmp_path):
    s = load_stream_spec(tmp_path)
    assert s.enabled is False and s.levels == tuple((ch, 100.0, False) for ch in ("game", "chat", "media", "aux", "mic"))


def test_the_switch_and_the_levels_are_read_from_asms_files(tmp_path):
    write_state(tmp_path, True, {"game": {"volume": 40, "muted": False}, "mic": {"volume": 80, "muted": True}})
    s = load_stream_spec(tmp_path)
    levels = {ch: (v, m) for ch, v, m in s.levels}
    assert s.enabled and levels["game"] == (40.0, False) and levels["mic"] == (80.0, True) and levels["chat"] == (100.0, False)


@pytest.mark.parametrize("text", ["streamer_mode: false", "streamer_mode: 1", "streamer_mode: yes please", "streamer_mode: [true]",
                                  "- a list", ":::: not yaml ::::\n\t-", "", "streamer_mode: 'true'"])
def test_only_a_real_true_switches_it_on(tmp_path, text):
    write_state(tmp_path, raw_yaml=text)
    assert load_stream_spec(tmp_path).enabled is False


@pytest.mark.parametrize("mix", ["not json", "[1, 2]", "null", '{"game": 7}', '{"game": {"volume": "loud", "muted": "yes"}}',
                                 '{"game": {"volume": true}}', '{"game": {"volume": NaN}}'])
def test_a_damaged_stream_file_reads_as_full_volume(tmp_path, mix):
    write_state(tmp_path, True, mix)
    assert {ch: (v, m) for ch, v, m in load_stream_spec(tmp_path).levels}["game"] == (100.0, False)


def test_volumes_are_clamped_and_one_bad_entry_spares_the_others(tmp_path):
    write_state(tmp_path, True, {"game": {"volume": 900}, "chat": {"volume": -5}, "media": {"volume": "x"}, "aux": {"volume": 30}})
    levels = {ch: v for ch, v, _ in load_stream_spec(tmp_path).levels}
    assert levels == {"game": 100.0, "chat": 0.0, "media": 100.0, "aux": 30.0, "mic": 100.0}


# ── the mixer ────────────────────────────────────────────────────────────────

def block(level, n=BLOCK):
    return np.full((2, n), float(level))


def test_the_sources_add_up_with_cubic_levels():
    m = StreamMix()
    m.configure(spec(game=(50, False), chat=(100, False)))
    out = m.mix({"game": block(0.4), "chat": block(0.1)}, BLOCK)
    assert out[0, -1] == pytest.approx(0.4 * volume_gain(50) + 0.1) and out.shape == (2, BLOCK)


def test_a_muted_source_is_silent_and_missing_sources_are_fine():
    m = StreamMix()
    m.configure(spec(game=(100, True)))
    m.mix({"game": block(0.5)}, BLOCK)                                               # the first block fades out
    assert np.all(m.mix({"game": block(0.5), "chat": None}, BLOCK) == 0.0)
    assert np.all(m.mix({}, BLOCK) == 0.0)


def test_level_changes_ramp_across_the_block():
    m = StreamMix()
    m.configure(spec())
    m.mix({"game": block(0.5)}, BLOCK)
    m.configure(spec(game=(100, True)))
    y = m.mix({"game": block(0.5)}, BLOCK)[0]
    assert y[0] == pytest.approx(0.5 * (1 - 1 / BLOCK)) and y[-1] == 0.0 and np.all(np.diff(y) < 0)
    assert np.all(m.mix({"game": block(0.5)}, BLOCK) == 0.0)


def test_unmuting_ramps_up_again():
    m = StreamMix()
    m.configure(spec(game=(100, True)))
    m.mix({"game": block(0.5)}, BLOCK)
    m.configure(spec())
    y = m.mix({"game": block(0.5)}, BLOCK)[0]
    assert y[0] == pytest.approx(0.5 / BLOCK) and y[-1] == pytest.approx(0.5) and np.all(np.diff(y) > 0)


# ── folding a device's channels to stereo ────────────────────────────────────

def test_to_stereo_handles_every_channel_count():
    n = 4
    mono = np.full((1, n), 0.3)
    assert np.array_equal(to_stereo(mono), np.vstack([mono, mono]))
    stereo = np.arange(8, dtype=float).reshape(2, n)
    assert to_stereo(stereo) is stereo
    x6 = np.zeros((6, n)); x6[2] = 1.0                                              # a 5.1 cable, centre only
    assert to_stereo(x6)[0, 0] == pytest.approx(0.7071) and to_stereo(x6)[1, 0] == pytest.approx(0.7071)
    x8 = np.zeros((8, n)); x8[3] = 1.0                                              # LFE is left out
    assert np.all(to_stereo(x8) == 0.0)
    assert to_stereo(np.ones((10, n))).shape == (2, n)                              # extra channels are ignored


# ── in the graph ─────────────────────────────────────────────────────────────

def play(graph, blocks, sources, mic=None):
    """Run blocks of constant-level sources; return the last block's buses."""
    out = None
    for _ in range(blocks):
        out = graph.process({k: v for k, v in sources.items()}, mic=mic)
    return out


def stereo_in(level, width=2):
    x = np.zeros((width, BLOCK))
    x[:2] = level
    return x


def test_streamer_mode_off_makes_no_stream_bus():
    g = EngineGraph(master_limiter=False)
    assert "stream" not in play(g, 5, {"game": stereo_in(0.2)})


def test_the_stream_mix_is_the_raw_channels_not_the_personal_mix():
    g = EngineGraph(master_limiter=False)
    g.stream.configure(spec())
    g.strips["game"].set_controls(volume_pct=10, muted=False, chatmix_pct=30)
    g.set_master(volume_pct=20)
    out = play(g, 10, {"game": stereo_in(0.3)})
    assert out["stream"][0, -1] == pytest.approx(0.3)                              # untouched by volume, ChatMix, master
    assert out["headset"][0, -1] == pytest.approx(0.3 * volume_gain(10) * (0.3 / (2 - 0.3)) * volume_gain(20), rel=1e-6)


def test_the_stream_mix_is_taken_before_the_channels_eq():
    g = EngineGraph(master_limiter=False)
    from arctis_sound_manager.eq_types import EqBand
    g.strips["game"].configure_eq({"bands": [EqBand(freq=1000.0, gain=-12.0, q=1.0, type="peakingEQ", enabled=True)],
                                   "basses_db": 0, "voix_db": 0, "aigus_db": 0, "boost_db": 0, "smart_volume": None})
    g.stream.configure(spec())
    t = np.arange(BLOCK * 40) / RATE
    tone = np.sin(2 * np.pi * 1000 * t) * 0.1
    heads, streams = [], []
    for i in range(40):
        x = np.vstack([tone[i * BLOCK:(i + 1) * BLOCK]] * 2)
        out = g.process({"game": x})
        heads.append(out["headset"]); streams.append(out["stream"])
    h, s = np.concatenate(heads, axis=1)[:, -9600:], np.concatenate(streams, axis=1)[:, -9600:]
    assert np.sqrt(np.mean(s ** 2)) == pytest.approx(0.1 / np.sqrt(2), rel=0.01)    # flat, as played
    assert np.sqrt(np.mean(h ** 2)) == pytest.approx(0.25 * np.sqrt(np.mean(s ** 2)), rel=0.05)   # the -12 dB cut only reaches the headset


def test_surround_channels_are_folded_with_the_itu_matrix():
    g = EngineGraph(master_limiter=False)
    g.stream.configure(spec())
    x = np.zeros((8, BLOCK)); x[2] = 0.5                                             # centre only
    out = play(g, 3, {"media": x})
    assert out["stream"][0, -1] == pytest.approx(0.5 * 0.7071) and out["stream"][1, -1] == pytest.approx(0.5 * 0.7071)


def test_the_microphone_joins_both_sides_after_its_own_level():
    g = EngineGraph(master_limiter=False)
    g.stream.configure(spec(mic=(50, False)))
    mic = np.full(BLOCK, 0.4)
    out = play(g, 5, {}, mic=mic)
    assert out["stream"][0, -1] == pytest.approx(0.4 * volume_gain(50)) == pytest.approx(out["stream"][1, -1])
    g.stream.configure(spec(mic=(50, True)))
    assert np.all(play(g, 5, {}, mic=mic)["stream"] == 0.0)


def test_each_source_has_its_own_level():
    g = EngineGraph(master_limiter=False)
    g.stream.configure(spec(game=(100, True), chat=(60, False), media=(100, False)))
    out = play(g, 10, {"game": stereo_in(0.2), "chat": stereo_in(0.1), "media": stereo_in(0.05, 8)})
    assert out["stream"][0, -1] == pytest.approx(0.1 * volume_gain(60) + 0.05)


def test_the_stream_mix_is_protected_by_a_limiter_like_the_headset():
    g = EngineGraph()
    g.stream.configure(spec())
    out = None
    peak = 0.0
    for _ in range(30):
        out = g.process({"game": stereo_in(0.8), "chat": stereo_in(0.8)})
        peak = max(peak, float(np.max(np.abs(out["stream"]))))
    assert peak == pytest.approx(db2lin(-0.5), abs=0.03)                             # 1.6 in, clamped
    assert EngineGraph(master_limiter=False)._stream_limiter is None


def test_the_stream_mix_does_not_touch_the_headset_mix():
    a, b = EngineGraph(), EngineGraph()
    b.stream.configure(spec())
    x = {"game": stereo_in(0.2), "media": stereo_in(0.1, 8)}
    for _ in range(10):
        oa, ob = a.process(x), b.process(x)
        assert np.array_equal(oa["headset"], ob["headset"])


def test_channels_routed_to_the_stream_bus_and_the_mix_share_one_output():
    g = EngineGraph(master_limiter=False, routing={"media": "stream"})
    g.stream.configure(spec())
    out = play(g, 10, {"game": stereo_in(0.2), "media": stereo_in(0.1)})
    assert list(out) == ["headset", "stream"]
    assert out["stream"][0, -1] == pytest.approx(0.1 + 0.2 + 0.1)                    # routed media + (game + media) mix
    assert out["headset"][0, -1] == pytest.approx(0.2)


def test_the_flag_can_force_the_mix_off_or_on():
    g = EngineGraph(master_limiter=False)
    g.stream.configure(spec(enabled=True))
    assert "stream" not in g.process({"game": stereo_in(0.2)}, stream=False)
    g.stream.configure(spec(enabled=False))
    assert "stream" in g.process({"game": stereo_in(0.2)}, stream=True)


def test_the_stream_mix_is_part_of_the_state_the_graph_applies(tmp_path):
    from arctis_sound_manager.win.audio.graph import read_state
    write_state(tmp_path, True, {"chat": {"volume": 30, "muted": False}})
    g = EngineGraph(master_limiter=False)
    g.apply_playback(read_state(tmp_path))
    assert g.stream.enabled and {c: v for c, v, _ in g.stream.spec.levels}["chat"] == 30.0
    write_state(tmp_path, False)
    g.apply_playback(read_state(tmp_path))
    assert not g.stream.enabled


# ── the whole engine, against in-memory devices ──────────────────────────────

def dev(index, name, ins=0, outs=0):
    return DeviceInfo(index, name, "Windows WASAPI", ins, outs, 48000.0)


DEVICES = [
    dev(1, "CABLE-A Output (VB-Audio Cable A)", ins=2), dev(2, "CABLE-A Input (VB-Audio Cable A)", outs=2),
    dev(3, "CABLE-B Output (VB-Audio Cable B)", ins=2), dev(4, "CABLE-B Input (VB-Audio Cable B)", outs=2),
    dev(5, "CABLE-C Output (VB-Audio Cable C)", ins=2), dev(6, "CABLE-C Input (VB-Audio Cable C)", outs=2),
    dev(7, "CABLE-D Output (VB-Audio Cable D)", ins=2), dev(8, "CABLE-D Input (VB-Audio Cable D)", outs=2),
    dev(9, "Headset Earphone (Arctis Nova Pro Wireless)", outs=2),
    dev(10, "Headset Microphone (Arctis Nova Pro Wireless)", ins=1),
]


def config(stream=True, mic=True):
    return EngineConfig(inputs={"game": "CABLE-A Output", "chat": "CABLE-B Output", "media": "", "aux": ""},
                        outputs={"headset": "Arctis Nova Pro", **({"stream": "CABLE-D Input"} if stream else {})},
                        mic_input="Microphone (Arctis" if mic else "", mic_output="CABLE-C Input" if mic else "",
                        latency="low")


def tone(freq, amp):
    def make(idx, channels):
        x = (np.sin(2 * np.pi * freq * idx / RATE) * amp).astype(np.float32)
        return np.repeat(x[:, None], channels, axis=1)
    return make


def level(x):
    return float(np.sqrt(np.mean(np.asarray(x, dtype=np.float64) ** 2)))


class Rig:
    def __init__(self, tmp_path, cfg=None, phase=0.0):
        self.tmp = tmp_path
        self.backend = FakeBackend(DEVICES)
        self.t = 0
        self.phase = phase                       # how late in a device chunk the engine runs (0 = right away)
        self.now = 0.0
        # the ports measure the devices' pace with a clock: here the rig's own, so a loaded test machine cannot skew it
        self.engine = Engine(cfg or config(), self.backend, config_dir=tmp_path, port_clock=lambda: self.now)
        self.heard = {"headset": [], "stream": [], "mic": []}
        self.sources = {"game": None, "chat": None, "mic": None}

    def start(self):
        self.engine.start(threaded=False)

    def tick(self, ticks=1):
        for _ in range(ticks):
            idx = np.arange(self.t, self.t + CHUNK)
            self.now = self.t / RATE
            for name, key in (("CABLE-A Output", "game"), ("CABLE-B Output", "chat"), ("Microphone", "mic")):
                try:
                    s = self.backend.stream(name, "input")
                except KeyError:
                    continue
                src = self.sources[key]
                if s.running:
                    s.deliver(src(idx, s.channels) if src else np.zeros((CHUNK, s.channels)))
            for name, key in (("Arctis", "headset"), ("CABLE-C Input", "mic"), ("CABLE-D Input", "stream")):
                try:
                    s = self.backend.stream(name, "output")
                except KeyError:
                    continue
                if s.running:
                    self.heard[key].append(s.collect(CHUNK))
            self.now = (self.t + self.phase * CHUNK) / RATE
            self.engine.playback.step()
            self.engine.mic.step()
            self.t += CHUNK

    def last(self, key, seconds=0.4):
        data = np.concatenate(self.heard[key]) if self.heard[key] else np.zeros((0, 2))
        return data[-int(seconds * RATE):]


def test_with_streamer_mode_off_the_stream_cable_gets_silence(tmp_path):
    write_state(tmp_path, False)
    rig = Rig(tmp_path)
    rig.sources["game"] = tone(1000, 0.2)
    rig.start()
    rig.tick(150)
    assert level(rig.last("headset")) > 0.1 and level(rig.last("stream")) == 0.0
    assert rig.engine.status()["stream"] == {"enabled": False, "output": True}


@pytest.mark.parametrize("phase", [0.0, 0.5, 1.0])
def test_the_stream_cable_carries_game_and_microphone_while_the_headset_hears_only_the_game(tmp_path, phase):
    """``phase``: whether the devices' callbacks fall right after the engine's block or right before the next one."""
    write_state(tmp_path, True)
    rig = Rig(tmp_path, phase=phase)
    rig.sources["game"] = tone(1000, 0.2)
    rig.sources["mic"] = tone(3000, 0.1)
    rig.start()
    rig.tick(300)
    stream, head = rig.last("stream", 0.5), rig.last("headset", 0.5)
    spectrum = np.abs(np.fft.rfft(stream[:, 0] * np.hanning(stream.shape[0])))
    freqs = np.fft.rfftfreq(stream.shape[0], 1 / RATE)
    peak = lambda f: spectrum[np.argmin(np.abs(freqs - f))]
    assert peak(1000) > 20 * np.median(spectrum) and peak(3000) > 20 * np.median(spectrum)
    assert peak(3000) / peak(1000) == pytest.approx(0.5, rel=0.1)                    # 0.1 mic vs 0.2 game
    for f in (1000, 3000):                                                          # pitch untouched: no drift correction
        near = np.abs(freqs - f) < 10
        assert freqs[near][np.argmax(spectrum[near])] == pytest.approx(f, abs=0.5)
    head_spec = np.abs(np.fft.rfft(head[:, 0] * np.hanning(head.shape[0])))
    assert head_spec[np.argmin(np.abs(freqs - 3000))] < 0.01 * head_spec[np.argmin(np.abs(freqs - 1000))]   # no mic in the headset
    ep = rig.engine.status()["endpoints"]
    assert ep["out:stream"]["state"] == "running" and ep["out:stream"]["underruns"] == 0
    assert ep["out:headset"]["underruns"] == 0


def test_the_stream_levels_change_the_stream_only(tmp_path):
    write_state(tmp_path, True)
    rig = Rig(tmp_path)
    rig.sources["game"] = tone(1000, 0.2)
    rig.start()
    rig.tick(200)
    head0, stream0 = level(rig.last("headset")), level(rig.last("stream"))
    write_state(tmp_path, True, {"game": {"volume": 50, "muted": False}})
    rig.engine.reload()
    rig.tick(100)
    assert level(rig.last("stream")) == pytest.approx(stream0 * volume_gain(50), rel=0.03)
    assert level(rig.last("headset")) == pytest.approx(head0, rel=0.01)
    write_state(tmp_path, True, {"game": {"volume": 100, "muted": True}})
    rig.engine.reload()
    rig.tick(100)
    assert level(rig.last("stream")) < 1e-6 and level(rig.last("headset")) == pytest.approx(head0, rel=0.01)


def test_the_headset_controls_do_not_move_the_stream(tmp_path):
    write_state(tmp_path, True)
    rig = Rig(tmp_path)
    rig.sources["game"] = tone(1000, 0.2)
    rig.start()
    rig.tick(200)
    stream0 = level(rig.last("stream"))
    rig.engine.set_volume("game", 20)
    rig.engine.set_mute("master", True)
    rig.engine.set_chatmix(10, 100)
    rig.tick(100)
    assert level(rig.last("headset")) < 1e-6
    assert level(rig.last("stream")) == pytest.approx(stream0, rel=0.01)


def test_switching_streamer_mode_on_and_off_from_the_settings_file(tmp_path):
    write_state(tmp_path, False)
    rig = Rig(tmp_path)
    rig.sources["game"] = tone(1000, 0.2)
    rig.sources["mic"] = tone(3000, 0.1)
    rig.start()
    rig.tick(100)
    assert level(rig.last("stream")) == 0.0
    write_state(tmp_path, True)
    rig.engine.reload()
    rig.tick(200)
    assert level(rig.last("stream")) > 0.1
    assert rig.engine.status()["stream"]["enabled"]
    write_state(tmp_path, False)
    rig.engine.reload()
    rig.tick(100)
    assert level(rig.last("stream")) == 0.0


def test_the_microphone_tap_stays_in_step_with_the_headset_clock(tmp_path):
    """Two domains on two clocks: the microphone must reach the stream mix without gaps or growing delay."""
    write_state(tmp_path, True)
    rig = Rig(tmp_path)
    rig.sources["mic"] = tone(3000, 0.1)
    rig.start()
    rig.tick(600)
    tap = rig.engine.mic_tap
    assert tap is not None and tap.callbacks > 100
    assert tap.underruns == 0 and tap.ring.overruns == 0 and tap.resyncs == 0
    assert abs(tap.level - tap.target) < 2 * rig.engine.block + 480


def test_streamer_mode_without_a_stream_cable_says_so_and_costs_nothing(tmp_path):
    write_state(tmp_path, True)
    rig = Rig(tmp_path, config(stream=False))
    rig.sources["game"] = tone(1000, 0.2)
    rig.start()
    rig.tick(100)
    status = rig.engine.status()
    assert status["stream"] == {"enabled": True, "output": False}
    assert any("Streamer mode is on" in n for n in status["notes"])
    assert level(rig.last("headset")) > 0.1
    assert rig.engine.mic_tap.callbacks >= 0


def test_the_stream_mix_works_without_a_microphone(tmp_path):
    write_state(tmp_path, True)
    rig = Rig(tmp_path, config(mic=False))
    rig.sources["game"] = tone(1000, 0.2)
    rig.start()
    assert rig.engine.mic_tap is None
    rig.tick(150)
    assert level(rig.last("stream")) == pytest.approx(0.2 / np.sqrt(2), rel=0.03)


def test_the_stream_cable_is_a_slave_output_with_its_own_clock(tmp_path):
    write_state(tmp_path, True)
    rig = Rig(tmp_path)
    rig.start()
    ports = rig.engine.playback.outputs
    assert ports["headset"].master and not ports["stream"].master
    assert rig.engine.playback.master() is ports["headset"]


@pytest.mark.parametrize("latency", ["low", "balanced", "safe"])
def test_the_tap_holds_a_microphone_block_plus_a_playback_block_in_every_profile(tmp_path, latency):
    """Sizing rule: the microphone hands over a whole block at once and the playback side takes a block at a time."""
    write_state(tmp_path, True)
    cfg = config()
    cfg.latency = latency
    rig = Rig(tmp_path, cfg)
    rig.start()
    tap, engine = rig.engine.mic_tap, rig.engine
    assert tap.target >= engine.mic_block + engine.block


def test_a_stream_cable_that_is_not_there_never_drains_the_microphone_tap(tmp_path):
    write_state(tmp_path, True)
    rig = Rig(tmp_path, config(stream=False))
    rig.sources["mic"] = tone(3000, 0.1)
    rig.start()
    rig.tick(100)
    assert rig.engine.mic_tap.callbacks > 50 and not rig.engine.mic_tap.primed          # nobody pulled from it


def test_a_stream_cable_that_turns_up_late_starts_from_the_microphone_as_it_is_now(tmp_path):
    """While the cable was missing the tap filled up (up to a second of old microphone). It must not be played."""
    write_state(tmp_path, True)
    rig = Rig(tmp_path)
    rig.backend.fail_open.add("CABLE-D Input (VB-Audio Cable D)")
    rig.sources["mic"] = tone(3000, 0.1)
    rig.start()
    rig.tick(150)
    tap = rig.engine.mic_tap
    assert tap.ring.available > 20000                                                    # a lot of stale audio is waiting
    rig.backend.fail_open.clear()
    rig.engine.supervise(now=1e9)
    assert rig.engine.status()["endpoints"]["out:stream"]["state"] == "running"
    rig.tick(100)
    assert tap.resyncs == 0 and tap.underruns == 0
    assert tap.ring.available < 3 * rig.engine.block + 2 * rig.engine.mic_block


def tone_energy(x, freq):
    t = np.arange(x.shape[0]) / RATE
    return float(np.hypot(np.dot(x, np.sin(2 * np.pi * freq * t)), np.dot(x, np.cos(2 * np.pi * freq * t))) / x.shape[0])


def test_switching_streamer_mode_back_on_never_replays_the_microphone_of_before(tmp_path):
    write_state(tmp_path, True)
    rig = Rig(tmp_path)
    rig.sources["mic"] = tone(3000, 0.2)
    rig.start()
    rig.tick(200)
    write_state(tmp_path, False)
    rig.engine.reload()
    rig.tick(60)
    rig.sources["mic"] = tone(5000, 0.2)                                         # the microphone is different now
    rig.tick(20)
    write_state(tmp_path, True)
    rig.engine.reload()
    mark = len(rig.heard["stream"])
    rig.tick(60)
    after = np.concatenate(rig.heard["stream"][mark:])[:, 0]
    old = [tone_energy(after[i:i + CHUNK], 3000) for i in range(0, after.shape[0] - CHUNK, CHUNK)]
    new = [tone_energy(after[i:i + CHUNK], 5000) for i in range(0, after.shape[0] - CHUNK, CHUNK)]
    assert max(old) < 0.01 and max(new) > 0.05


def test_switching_the_mix_off_flushes_the_limiters_memory():
    """The limiter delays by 5 ms; those frames of the old mix must not come out when the mix starts again."""
    g = EngineGraph()
    g.stream.configure(spec())
    for _ in range(10):
        g.process({"game": stereo_in(0.5)})
    g.stream.configure(spec(enabled=False))
    assert "stream" not in g.process({"game": stereo_in(0.5)})
    g.stream.configure(spec())
    first = g.process({}, mic=None)["stream"]
    assert np.all(first == 0.0)

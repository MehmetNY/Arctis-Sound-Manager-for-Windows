"""Rings, drift control and the input/output ports, driven by simulated device clocks (no threads, no sleeping)."""
from __future__ import annotations

import numpy as np
import pytest

from arctis_sound_manager.win.audio.ports import DriftController, FrameRing, InputPort, OutputPort

RATE = 48000


# ── the ring ─────────────────────────────────────────────────────────────────

def test_ring_round_trip_and_wraparound():
    ring = FrameRing(2, capacity=10)
    data = np.arange(40, dtype=np.float32).reshape(20, 2)
    out = []
    for i in range(0, 20, 4):
        ring.write(data[i:i + 4])
        out.append(ring.read(4))
    assert np.array_equal(np.concatenate(out), data)
    assert ring.available == 0 and ring.overruns == 0 and ring.underruns == 0


def test_ring_drops_the_oldest_audio_when_the_reader_is_too_slow():
    ring = FrameRing(1, capacity=8)
    ring.write(np.arange(6, dtype=np.float32)[:, None])
    ring.write(np.arange(6, 12, dtype=np.float32)[:, None])
    assert ring.overruns == 4 and ring.available == 8
    assert ring.read(8)[:, 0].tolist() == [4, 5, 6, 7, 8, 9, 10, 11]
    ring.write(np.arange(30, dtype=np.float32)[:, None])               # bigger than the ring itself
    assert ring.read(8)[:, 0].tolist() == list(range(22, 30))


def test_ring_underrun_is_counted_and_nothing_is_consumed():
    ring = FrameRing(1, capacity=8)
    ring.write(np.ones((3, 1), dtype=np.float32))
    assert ring.read(5) is None and ring.underruns == 1 and ring.available == 3
    assert ring.read_available(2).shape == (2, 1) and ring.available == 1
    assert ring.read_available().shape == (1, 1)
    ring.clear()
    assert ring.available == 0


# ── the controller ───────────────────────────────────────────────────────────

def test_controller_speeds_up_when_the_buffer_is_too_full_and_slows_down_when_empty():
    c = DriftController(target=1000, block=480)
    for _ in range(200):
        c.update(1480)
    assert c.ratio > 1.0 and c.ppm > 0
    c = DriftController(target=1000, block=480)
    for _ in range(200):
        c.update(500)
    assert c.ratio < 1.0
    c = DriftController(target=1000, block=480)
    for _ in range(200):
        c.update(1000)
    assert c.ratio == 1.0


def test_controller_has_a_dead_zone_and_is_proportional_outside_it():
    c = DriftController(target=1000, block=480, gain=4e-3, deadband=64)
    for level in (1000 - 64, 1000 + 64, 1010, 990):
        c.reset()
        assert c.update(level) == 1.0                                       # inside: exactly 1.0, a bit-exact copy
    c.reset()
    assert c.update(1064 + 120) == pytest.approx(1 + 4e-3 * 120 / 480)      # outside: proportional to the excess
    c.reset()
    assert c.update(1000 - 64 - 240) == pytest.approx(1 - 4e-3 * 240 / 480)
    c.reset(target=5000)                                                    # reset can move the target
    assert c.target == 5000 and c.update(5000) == 1.0


def test_controller_smooths_the_level_it_is_given():
    c = DriftController(target=1000, block=480, smoothing=0.1, deadband=0)
    c.update(1000)
    one = c.update(1480)                                                    # a single outlier barely moves it
    assert one == pytest.approx(1 + 4e-3 * 48 / 480)


def test_controller_is_clamped_and_resettable():
    c = DriftController(target=1000, block=480, max_ppm=500)
    for _ in range(500):
        c.update(100000)
    assert c.ppm == pytest.approx(500.0)
    c.reset()
    assert c.ratio == 1.0


# ── input port against a simulated device clock ──────────────────────────────

def simulate_input(ppm, seconds=70.0, block=960, chunk=480, freq=200.0, stall=None, jitter_ms=0.0, seed=1,
                   target_blocks=2.0):
    """A device whose clock is off by *ppm* delivering ``chunk``-frame callbacks (up to ``jitter_ms`` late)
    to an engine that takes a block every ``block`` frames. Time is simulated: no threads, no sleeping."""
    now = [0.0]
    port = InputPort("game", 1, block, target_blocks=target_blocks, clock=lambda: now[0])
    rng = np.random.default_rng(seed)
    dev_rate = RATE * (1 + ppm * 1e-6)
    k = 0                       # device frames produced so far
    pending = []                # (delivery time, data)
    ready, levels, outs, ratios = [], [], [], []
    t = 0.0
    for tick in range(int(seconds * RATE / block)):
        t += block / RATE
        while (k + chunk) / dev_rate <= t:
            arrives = (k + chunk) / dev_rate
            if stall is None or not (stall[0] <= arrives < stall[1]):
                idx = np.arange(k, k + chunk)
                pending.append((arrives + rng.uniform(0, jitter_ms * 1e-3),
                                np.sin(2 * np.pi * freq * idx / RATE).astype(np.float32)[:, None]))
            k += chunk
        pending.sort(key=lambda item: item[0])
        while pending and pending[0][0] <= t:
            now[0], data = pending.pop(0)
            port.on_audio(data)
        now[0] = t
        ready.append(port.level)                      # what the engine finds when it comes for a block
        out = port.pull()
        outs.append(out[0])
        levels.append(port.level)
        ratios.append(port.controller.ppm)
    return port, np.array(levels), np.concatenate(outs), np.array(ratios), np.array(ready)


@pytest.mark.parametrize("jitter_ms, ppm", [(0.0, 0), (0.0, 60), (0.0, -300), (0.0, 1000), (2.0, 0), (2.0, -60),
                                           (2.0, 300), (2.0, -1000)])
def test_input_port_follows_a_drifting_clock_without_underruns(ppm, jitter_ms):
    port, levels, out, ratios, ready = simulate_input(ppm, jitter_ms=jitter_ms)
    quarter = len(levels) // 4
    assert port.underruns == 0 and port.resyncs == 0 and port.ring.overruns == 0
    # the engine always finds a block plus the interpolator's look-ahead, with room to spare
    assert np.min(ready[quarter:]) > 960 + 16 + 400
    assert np.max(ready[quarter:]) < port.target + 960 + 400
    # it found the drift and holds it (the sawtooth of a 10 ms chunk would have shown up here as ±1000 ppm)
    assert np.mean(ratios[-500:]) == pytest.approx(ppm, abs=15)
    assert np.ptp(ratios[-500:]) < (5.0 if jitter_ms == 0 else 150.0)
    # the tone comes out without a single glitch: second difference stays that of a 200 Hz sine
    tail = out[RATE:]
    second = np.abs(np.diff(tail, 2))
    ideal = (2 * np.pi * 200 / RATE) ** 2
    assert np.max(second) < 1.2 * ideal


def test_input_port_leaves_matching_clocks_alone():
    port, levels, out, ratios, ready = simulate_input(0, seconds=30.0, jitter_ms=1.0)
    assert np.all(ratios == 0.0)                                            # never even touched the resampler step


def test_input_port_works_with_other_chunk_sizes_and_deeper_buffers():
    for chunk, block, target in ((480, 480, 2.0), (960, 960, 3.0), (256, 960, 2.0)):
        port, levels, out, ratios, ready = simulate_input(120, seconds=60.0, block=block, chunk=chunk,
                                                          target_blocks=target, jitter_ms=1.0)
        assert port.underruns == 0 and port.resyncs == 0, (chunk, block, target)
        assert np.mean(ratios[-300:]) == pytest.approx(120, abs=25), (chunk, block, target)


def test_input_port_never_credits_more_than_one_chunk_for_a_silent_device():
    now = [0.0]
    port = InputPort("x", 1, 960, clock=lambda: now[0])
    assert port._partial() == 0.0                                           # no callback yet
    port.on_audio(np.zeros((480, 1), dtype=np.float32))
    now[0] = 0.005
    assert port._partial() == pytest.approx(240.0)                          # 5 ms at 48 kHz
    now[0] = 3.0                                                            # the device went quiet
    assert port._partial() == 480.0
    now[0] = -1.0                                                           # a clock that stepped back
    assert port._partial() == 0.0


def test_input_port_primes_before_playing_and_outputs_silence_meanwhile():
    port = InputPort("x", 1, 960)
    assert not np.any(port.pull())                                          # nothing yet
    port.on_audio(np.ones((960, 1), dtype=np.float32))
    assert not np.any(port.pull()) and not port.primed                       # still below the target
    port.on_audio(np.ones((2000, 1), dtype=np.float32))
    assert np.any(port.pull()) and port.primed
    assert port.underruns == 0


def test_input_port_refills_to_its_target_after_an_underrun():
    port = InputPort("x", 1, 960)
    port.on_audio(np.ones((2500, 1), dtype=np.float32))
    assert np.any(port.pull()) and port.primed
    for _ in range(5):                                                      # run it dry
        port.pull()
    assert port.underruns == 1 and not port.primed
    port.on_audio(np.ones((1200, 1), dtype=np.float32))                     # enough for a block, not for the target
    assert not np.any(port.pull()) and port.underruns == 1 and not port.primed
    port.on_audio(np.ones((1200, 1), dtype=np.float32))
    assert np.any(port.pull()) and port.primed


def test_input_port_recovers_from_a_stalled_device_and_counts_it():
    port, levels, out, ratios, ready = simulate_input(0, seconds=12.0, stall=(4.0, 5.0))
    assert port.underruns >= 1
    assert abs(levels[-1] - (port.target - 960)) < 600                       # back on target afterwards
    assert np.max(np.abs(out[-RATE:])) > 0.9                                # and sound again


def test_input_port_jumps_to_the_target_after_a_big_backlog():
    port = InputPort("x", 1, 960)
    port.on_audio(np.zeros((960 * 3, 1), dtype=np.float32))
    port.pull()
    port.on_audio(np.zeros((960 * 20, 1), dtype=np.float32))                 # the device delivered a burst
    port.pull()
    assert port.resyncs == 1 and port.level < port.target + 2 * 960


def test_multichannel_input_keeps_channels_apart():
    port = InputPort("game", 8, 960)
    data = np.zeros((4000, 8), dtype=np.float32)
    data[:, 2] = 0.5
    port.on_audio(data)
    out = port.pull()
    assert out.shape == (8, 960) and np.allclose(out[2, 32:], 0.5, atol=1e-6) and not np.any(out[[0, 1, 3, 4, 5, 6, 7]])


# ── output port ──────────────────────────────────────────────────────────────

def test_master_output_hands_the_engines_blocks_to_the_device_in_order():
    port = OutputPort("headset", 2, 960, master=True)
    block = np.vstack([np.arange(960), -np.arange(960)]).astype(np.float64)
    port.push(block)
    assert port.fill == 960
    dev = np.zeros((480, 2), dtype=np.float32)
    port.fill_device(dev)
    assert np.array_equal(dev[:, 0], np.arange(480)) and np.array_equal(dev[:, 1], -np.arange(480))
    port.fill_device(dev)
    assert np.array_equal(dev[:, 0], np.arange(480, 960)) and port.underruns == 0 and port.started


def test_master_output_underrun_plays_what_it_has_then_silence():
    port = OutputPort("headset", 1, 960)
    port.push(np.ones((1, 100)))
    dev = np.full((480, 1), 9.0, dtype=np.float32)
    port.fill_device(dev)
    assert np.all(dev[:100] == 1.0) and np.all(dev[100:] == 0.0) and port.underruns == 1


def simulate_slave(ppm, seconds=70.0, block=960, chunk=480, target_blocks=2.0, jitter_ms=0.0, seed=2, phase=0.0):
    """An engine pushing a block every ``block`` frames to a second output whose clock is off by *ppm*.

    ``phase`` (0..1) is where, within a chunk, the device's callbacks fall relative to the engine's pushes."""
    now = [0.0]
    port = OutputPort("speakers", 1, block, master=False, target_blocks=target_blocks, clock=lambda: now[0])
    rng = np.random.default_rng(seed)
    dev_rate = RATE * (1 + ppm * 1e-6)
    k = produced = 0
    t = 0.0
    lag = phase * chunk / RATE
    out, at_callback, ratios = [], [], []
    for tick in range(int(seconds * RATE / block)):
        t += block / RATE
        while (k + chunk) / dev_rate + lag <= t:            # device callbacks that fall before this push
            now[0] = (k + chunk) / dev_rate + lag
            if port.primed:
                at_callback.append(port.fill)
            dev = np.zeros((chunk, 1), dtype=np.float32)
            port.fill_device(dev)
            out.append(dev[:, 0])
            k += chunk
        idx = np.arange(produced, produced + block)
        now[0] = t + rng.uniform(0, jitter_ms * 1e-3)
        port.push(np.sin(2 * np.pi * 200 * idx / RATE)[None, :])
        produced += block
        ratios.append(port.controller.ppm)
    return port, np.concatenate(out), np.array(at_callback), np.array(ratios)


@pytest.mark.parametrize("jitter_ms, ppm", [(0.0, 0), (0.0, 100), (0.0, -300), (0.0, 1000), (2.0, 0), (2.0, -100),
                                           (2.0, 300), (2.0, -1000)])
def test_slave_output_follows_its_own_clock_through_the_resampler(ppm, jitter_ms):
    port, y, at_callback, ratios = simulate_slave(ppm, jitter_ms=jitter_ms)
    quarter = len(at_callback) // 4
    assert port.underruns == 0                                              # priming is not an underrun
    assert np.min(at_callback[quarter:]) > 480 + 16 + 400                    # a chunk always has audio waiting
    assert np.max(at_callback[quarter:]) < port.target + 960 + 400
    assert np.mean(ratios[-500:]) == pytest.approx(-ppm, abs=15)            # a faster device is fed slower input
    assert np.ptp(ratios[-500:]) < (5.0 if jitter_ms == 0 else 150.0)
    tail = y[RATE * 5:]
    assert np.max(np.abs(np.diff(tail, 2))) < 1.2 * (2 * np.pi * 200 / RATE) ** 2


@pytest.mark.parametrize("phase", [0.0, 0.2, 0.45, 0.7, 0.95])
def test_slave_output_does_not_chase_the_phase_it_happens_to_start_in(phase):
    """Whether the device's callbacks fall just after the engine's pushes or just before the next ones is luck.
    With matching clocks the controller must not move at all, wherever the stream lands."""
    port, y, at_callback, ratios = simulate_slave(0, seconds=12.0, phase=phase)
    assert port.underruns == 0
    assert np.max(np.abs(ratios)) < 5.0                                     # not a single ppm of "correction"
    assert np.max(np.abs(np.diff(y[RATE * 2:], 2))) < 1.2 * (2 * np.pi * 200 / RATE) ** 2


def test_slave_output_is_silent_without_counting_underruns_until_it_holds_its_target():
    port = OutputPort("speakers", 1, 960, master=False)
    dev = np.ones((480, 1), dtype=np.float32)
    port.fill_device(dev)
    assert not np.any(dev) and port.underruns == 0 and not port.primed
    port.push(np.ones((1, 960)))
    port.fill_device(dev)
    assert not np.any(dev) and not port.primed                              # 960 is below two blocks
    port.push(np.ones((1, 1920)))
    port.fill_device(dev)
    assert port.primed and np.any(dev)


def test_slave_output_reset_starts_over():
    port = OutputPort("speakers", 1, 960, master=False)
    port.push(np.ones((1, 4000)))
    port.fill_device(np.zeros((480, 1), dtype=np.float32))
    assert port.primed
    port.reset()
    assert not port.primed and port.fill == 0 and not port.started


def test_master_reset_clears_the_ring():
    port = OutputPort("headset", 1, 960)
    port.push(np.ones((1, 960)))
    port.fill_device(np.zeros((100, 1), dtype=np.float32))
    port.reset()
    assert port.fill == 0 and not port.started and port.primed


def test_slave_output_that_runs_dry_goes_silent_and_primes_again():
    port = OutputPort("speakers", 1, 960, master=False)
    port.push(np.ones((1, 4000)))
    dev = np.zeros((480, 1), dtype=np.float32)
    for _ in range(12):                                                     # drain everything it has
        port.fill_device(dev)
    assert port.underruns == 1 and not port.primed
    port.fill_device(dev)                                                   # now it waits quietly instead of counting
    assert port.underruns == 1 and not np.any(dev)
    port.push(np.ones((1, 4000)))
    port.fill_device(dev)
    assert port.primed and np.any(dev)


def test_ports_count_device_callbacks_and_the_master_wakes_the_engine():
    inp = InputPort("x", 1, 960)
    for _ in range(3):
        inp.on_audio(np.zeros((480, 1), dtype=np.float32))
    assert inp.callbacks == 3
    master = OutputPort("m", 1, 960)
    assert not master.wake.is_set()
    master.fill_device(np.zeros((480, 1), dtype=np.float32))
    assert master.callbacks == 1 and master.wake.is_set()
    slave = OutputPort("s", 1, 960, master=False)
    slave.fill_device(np.zeros((480, 1), dtype=np.float32))
    assert slave.callbacks == 1 and not slave.wake.is_set()

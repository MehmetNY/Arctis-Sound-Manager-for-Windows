"""The engine's side of the memory shared with the microphone effect, against the same bytes in an ordinary buffer."""
import struct

import numpy as np
import pytest

from arctis_sound_manager.win.fx import shared
from tests.fx_effect import Effect


@pytest.fixture
def clock():
    class Clock:
        now = 1000

        def __call__(self):
            return self.now
    return Clock()


def test_layout_matches_the_header_file():
    # native/micfx/fxshared.h is the other copy of these numbers
    text = (shared.__file__.rsplit("src", 1)[0] + "native/micfx/fxshared.h")
    header = open(text, encoding="utf-8").read()
    assert f"#define ASMFX_RING_FRAMES  {shared.RING_FRAMES}u" in header
    assert f"#define ASMFX_VERSION      {shared.VERSION}u" in header
    assert f"#define ASMFX_OFF_IN_WRITE {shared.OFF_IN_WRITE}u" in header
    assert f"#define ASMFX_OFF_OUT_READ {shared.OFF_OUT_READ}u" in header
    import re
    declared = {name: int(offset) for name, offset in re.findall(r"\b(\w+);\s*//\s*(\d+)\b", header)}
    for name, (offset, _fmt) in shared._FIELDS.items():
        assert declared.get(name) == offset, name


def test_a_fresh_memory_is_valid_and_unclaimed(clock):
    link = shared.FxLink(shared.LocalMemory(), clock=clock, pid=11)
    assert link.valid() and not link.owns() and not link.apo_alive()


def test_claim_and_the_other_engine_that_is_alive(clock):
    memory = shared.LocalMemory()
    first = shared.FxLink(memory, clock=clock, pid=11)
    second = shared.FxLink(memory, clock=clock, pid=22)
    assert first.claim() and first.owns()
    assert not second.claim()                                # a living engine keeps it
    clock.now += shared.ENGINE_STALE_MS + 1
    assert second.claim() and second.owns() and not first.owns()    # a silent one loses it


def test_heartbeat_and_release_tell_the_effect_what_to_do(clock):
    memory = shared.LocalMemory()
    link = shared.FxLink(memory, clock=clock, pid=11)
    link.claim(wants=True, lead_frames=960)
    assert link.get("engine_wants") == 1 and link.get("lead_frames") == 960
    link.heartbeat(False)
    assert link.get("engine_wants") == 0
    link.release()
    assert link.get("engine_pid") == 0


def test_raw_microphone_comes_in_and_processed_goes_back(clock):
    memory = shared.LocalMemory()
    effect = Effect(memory, channels=1)
    link = shared.FxLink(memory, clock=clock, pid=11)
    link.claim()
    assert link.sync()                                       # the effect started a stream: align
    block = np.linspace(-0.5, 0.5, 480, dtype=np.float32).reshape(-1, 1)
    effect.push(block)
    assert link.available() == 480
    got = link.read(480, 1)
    assert np.array_equal(got, block) and link.available() == 0
    assert link.write(got * 0.5) == 480
    assert link.pending() == 480
    assert np.allclose(effect.take(480), block * 0.5)


def test_stereo_and_the_ring_wrapping_around(clock):
    memory = shared.LocalMemory()
    effect = Effect(memory, channels=2)
    link = shared.FxLink(memory, clock=clock, pid=11)
    link.claim()
    link.sync()
    rng = np.random.default_rng(1)
    for _ in range(40):                                      # 40 * 1000 frames goes round the 16384-frame ring twice
        block = rng.standard_normal((1000, 2)).astype(np.float32)
        effect.push(block)
        got = link.read(1000, 2)
        assert np.array_equal(got, block)
        link.write(got)
        assert np.array_equal(effect.take(1000), block)


def test_a_new_stream_makes_both_sides_drop_what_was_pending(clock):
    memory = shared.LocalMemory()
    effect = Effect(memory, channels=1)
    link = shared.FxLink(memory, clock=clock, pid=11)
    link.claim()
    link.sync()
    effect.push(np.ones((300, 1), dtype=np.float32))
    link.write(np.ones((200, 1), dtype=np.float32))
    effect.start(channels=2)                                 # the effect begins another stream
    assert link.sync()
    assert link.available() == 0 and link.pending() == 0
    assert link.format() == (48000, 2)
    assert not link.sync()                                   # nothing new since


def test_the_effect_resetting_its_counter_is_noticed(clock):
    memory = shared.LocalMemory()
    effect = Effect(memory, channels=1)
    link = shared.FxLink(memory, clock=clock, pid=11)
    link.claim()
    link.sync()
    effect.push(np.ones((100, 1), dtype=np.float32))
    link.read(100, 1)
    effect.set_counter(shared.OFF_IN_WRITE, 0)               # in_write now behind in_read
    assert link.available() == 0


def test_output_ring_never_takes_more_than_it_holds(clock):
    link = shared.FxLink(shared.LocalMemory(), clock=clock, pid=11)
    link.claim()
    assert link.write(np.zeros((shared.RING_FRAMES + 100, 1), dtype=np.float32)) == shared.RING_FRAMES
    assert link.room() == 0
    assert link.write(np.zeros((10, 1), dtype=np.float32)) == 0


def test_apo_alive_follows_its_clock(clock):
    memory = shared.LocalMemory()
    effect = Effect(memory)
    link = shared.FxLink(memory, clock=clock, pid=11)
    assert link.apo_alive()
    clock.now += shared.APO_STALE_MS + 1
    assert not link.apo_alive()


def test_info_names_the_state(clock):
    memory = shared.LocalMemory()
    effect = Effect(memory)
    effect.put("apo_state", shared.STATE_PROCESSING)
    link = shared.FxLink(memory, clock=clock, pid=11)
    assert link.info()["state"] == "processing"


def test_nothing_to_open_off_windows():
    assert shared.open_link() is None

"""A stand-in for ``native/micfx`` (the microphone effect inside Windows' audio engine) for tests: it does to the shared
memory what the DLL does, as far as the audio engine can tell."""
import struct

import numpy as np

from arctis_sound_manager.win.fx import shared


class Effect:
    """What native/micfx does to the shared memory, as far as the engine can tell."""

    def __init__(self, memory, channels=1, rate=48000):
        self.mem = memory
        self.channels = channels
        self.in_write = 0
        self.out_read = 0
        self.pid = 4242
        self.start(channels, rate)

    def put(self, name, value):
        off, fmt = shared._FIELDS[name]
        struct.pack_into(fmt, self.mem.buf, off, value)

    def get(self, name):
        off, fmt = shared._FIELDS[name]
        return struct.unpack_from(fmt, self.mem.buf, off)[0]

    def counter(self, off):
        return int(np.frombuffer(self.mem.buf, dtype=np.uint64, count=1, offset=off)[0])

    def set_counter(self, off, value):
        np.frombuffer(self.mem.buf, dtype=np.uint64, count=1, offset=off)[0] = value

    def start(self, channels, rate=48000, now=1000):
        self.channels = channels
        self.put("apo_pid", self.pid)
        self.put("rate", rate)
        self.put("channels", channels)
        self.put("apo_tick_ms", now)
        self.set_counter(shared.OFF_IN_WRITE, self.counter(shared.OFF_IN_READ))
        self.set_counter(shared.OFF_OUT_READ, self.counter(shared.OFF_OUT_WRITE))
        self.put("generation", self.get("generation") + 1)

    def push(self, block):                              # the raw microphone
        write = self.counter(shared.OFF_IN_WRITE)
        ring = np.frombuffer(self.mem.buf, dtype=np.float32, count=shared.RING_FRAMES * 2, offset=shared.OFF_IN_DATA)
        for k, frame in enumerate(block.reshape(-1, self.channels)):
            i = (write + k) % shared.RING_FRAMES
            ring[i * self.channels:(i + 1) * self.channels] = frame
        self.set_counter(shared.OFF_IN_WRITE, write + block.shape[0])

    def take(self, frames):                             # the processed one
        read = self.counter(shared.OFF_OUT_READ)
        ring = np.frombuffer(self.mem.buf, dtype=np.float32, count=shared.RING_FRAMES * 2, offset=shared.OFF_OUT_DATA)
        out = np.empty((frames, self.channels), dtype=np.float32)
        for k in range(frames):
            i = (read + k) % shared.RING_FRAMES
            out[k] = ring[i * self.channels:(i + 1) * self.channels]
        self.set_counter(shared.OFF_OUT_READ, read + frames)
        return out

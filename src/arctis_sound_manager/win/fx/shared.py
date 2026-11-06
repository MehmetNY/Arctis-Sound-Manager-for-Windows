# Copyright (C) 2026 loteran — Windows portability layer
# SPDX-License-Identifier: GPL-3.0-or-later
"""The engine's side of the memory it shares with the microphone effect (``native/micfx``).

The layout is the one in ``native/micfx/fxshared.h``: a 256-byte header, four 64-bit frame counters (each written by
exactly one side) and two rings of float32 frames. The effect creates the named objects inside Windows' audio engine
(only a service may create ``Global\\`` objects); the engine opens them here. Nothing in this module needs Windows
except :class:`Win32Memory`; tests drive :class:`LocalMemory`, which is the same bytes in an ordinary buffer.
"""
from __future__ import annotations

import ctypes
import os
import struct
import sys
import threading
import time
from typing import Callable, Protocol

import numpy as np

MAGIC = 0x58464D41
VERSION = 1
MAP_NAME = "Global\\ASM_MicFx_v1"
EVENT_NAME = "Global\\ASM_MicFx_Wake_v1"
RING_FRAMES = 16384
MAX_CHANNELS = 2
RATE = 48000

OFF_IN_WRITE, OFF_IN_READ, OFF_OUT_WRITE, OFF_OUT_READ = 256, 320, 384, 448
OFF_IN_DATA = 512
OFF_OUT_DATA = OFF_IN_DATA + RING_FRAMES * MAX_CHANNELS * 4
MAP_BYTES = OFF_OUT_DATA + RING_FRAMES * MAX_CHANNELS * 4

# header fields: name -> (offset, struct format)
_FIELDS = {
    "magic": (0, "<I"), "version": (4, "<I"), "instance": (8, "<I"), "apo_pid": (12, "<I"), "rate": (16, "<I"),
    "channels": (20, "<I"), "period_frames": (24, "<I"), "generation": (28, "<I"), "engine_pid": (32, "<I"),
    "engine_wants": (36, "<I"), "lead_frames": (40, "<I"), "apo_state": (44, "<I"), "apo_tick_ms": (48, "<Q"),
    "engine_tick_ms": (56, "<Q"), "in_frames": (64, "<Q"), "out_frames": (72, "<Q"), "underruns": (80, "<I"),
    "overruns": (84, "<I"), "calls": (88, "<I"), "fault": (92, "<I"), "dll_build": (96, "<I"),
}

STATE_IDLE, STATE_PRIMING, STATE_PROCESSING, STATE_NO_ENGINE, STATE_BYPASS, STATE_FORMAT, STATE_FAULT, STATE_NOT_OWNER = range(8)
STATE_NAMES = {
    STATE_IDLE: "idle", STATE_PRIMING: "starting", STATE_PROCESSING: "processing", STATE_NO_ENGINE: "no engine",
    STATE_BYPASS: "bypass", STATE_FORMAT: "unsupported format", STATE_FAULT: "fault", STATE_NOT_OWNER: "not owner",
}

ENGINE_STALE_MS = 3000          # an engine that has not shown a sign of life for this long no longer owns the rings
APO_STALE_MS = 2000             # the effect is "running" if it was called within this long


class Memory(Protocol):
    buf: memoryview

    def wait(self, timeout_s: float) -> bool: ...
    def close(self) -> None: ...


class LocalMemory:
    """The same bytes in an ordinary buffer (tests, and the stand-in for the effect in them)."""

    def __init__(self):
        self._raw = bytearray(MAP_BYTES)
        self.buf = memoryview(self._raw)
        self._event = threading.Event()
        struct.pack_into("<II", self._raw, 0, MAGIC, VERSION)

    def wait(self, timeout_s: float) -> bool:
        hit = self._event.wait(timeout_s)
        self._event.clear()
        return hit

    def signal(self) -> None:
        self._event.set()

    def close(self) -> None:
        pass


class Win32Memory:
    """The real thing: the file mapping and the event the effect created in session 0."""

    def __init__(self, view: ctypes.Array, handles: tuple, kernel32):
        self._view = view
        self.buf = memoryview(view)
        self._handles = handles
        self._k = kernel32
        self._event = handles[1]

    @classmethod
    def open(cls) -> "Win32Memory | None":
        if sys.platform != "win32":
            return None
        from ctypes import wintypes
        k = ctypes.WinDLL("kernel32", use_last_error=True)       # type: ignore[attr-defined]
        k.OpenFileMappingW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
        k.OpenFileMappingW.restype = wintypes.HANDLE
        k.MapViewOfFile.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, ctypes.c_size_t]
        k.MapViewOfFile.restype = ctypes.c_void_p
        k.OpenEventW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
        k.OpenEventW.restype = wintypes.HANDLE
        k.CloseHandle.argtypes = [wintypes.HANDLE]
        k.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        k.WaitForSingleObject.restype = wintypes.DWORD
        k.UnmapViewOfFile.argtypes = [ctypes.c_void_p]
        FILE_MAP_ALL_ACCESS = 0xF001F
        SYNCHRONIZE, EVENT_MODIFY_STATE = 0x00100000, 0x0002
        mapping = k.OpenFileMappingW(FILE_MAP_ALL_ACCESS, False, MAP_NAME)
        if not mapping:
            return None
        address = k.MapViewOfFile(mapping, FILE_MAP_ALL_ACCESS, 0, 0, MAP_BYTES)
        if not address:
            k.CloseHandle(mapping)
            return None
        event = k.OpenEventW(SYNCHRONIZE | EVENT_MODIFY_STATE, False, EVENT_NAME)
        if not event:
            k.UnmapViewOfFile(address)
            k.CloseHandle(mapping)
            return None
        view = (ctypes.c_char * MAP_BYTES).from_address(address)
        mem = cls(view, (mapping, event, address), k)
        return mem

    def wait(self, timeout_s: float) -> bool:
        return self._k.WaitForSingleObject(self._event, int(max(0.0, timeout_s) * 1000)) == 0

    def close(self) -> None:
        handles, self._handles = self._handles, ()
        if handles:
            try:
                self.buf.release()
            except Exception:                                         # noqa: BLE001
                pass
            self._k.UnmapViewOfFile(handles[2])
            self._k.CloseHandle(handles[1])
            self._k.CloseHandle(handles[0])


def tick_ms() -> int:
    """Windows' GetTickCount64 (the clock the effect stamps with); elsewhere the monotonic clock in ms."""
    if sys.platform == "win32":
        return int(ctypes.windll.kernel32.GetTickCount64())          # type: ignore[attr-defined]
    return int(time.monotonic() * 1000)


class FxLink:
    """What the audio engine does with the shared memory: claim it, show it is alive, take the raw microphone, give the
    processed one back."""

    def __init__(self, memory: Memory, clock: Callable[[], int] = tick_ms, pid: int | None = None):
        self.memory = memory
        self._clock = clock
        self.pid = pid if pid is not None else os.getpid()
        buf = memory.buf
        self._counter = {name: np.frombuffer(buf, dtype=np.uint64, count=1, offset=off)
                         for name, off in (("in_write", OFF_IN_WRITE), ("in_read", OFF_IN_READ),
                                           ("out_write", OFF_OUT_WRITE), ("out_read", OFF_OUT_READ))}
        self._in = np.frombuffer(buf, dtype=np.float32, count=RING_FRAMES * MAX_CHANNELS, offset=OFF_IN_DATA)
        self._out = np.frombuffer(buf, dtype=np.float32, count=RING_FRAMES * MAX_CHANNELS, offset=OFF_OUT_DATA)
        self._generation = -1

    # ── header ──────────────────────────────────────────────────────────────

    def get(self, name: str) -> int:
        off, fmt = _FIELDS[name]
        return struct.unpack_from(fmt, self.memory.buf, off)[0]

    def put(self, name: str, value: int) -> None:
        off, fmt = _FIELDS[name]
        struct.pack_into(fmt, self.memory.buf, off, value)

    def valid(self) -> bool:
        return self.get("magic") == MAGIC and self.get("version") == VERSION

    # ── life cycle ──────────────────────────────────────────────────────────

    def claim(self, wants: bool = True, lead_frames: int = 1024) -> bool:
        """Become *the* engine of the rings. ``False`` when another living engine already is."""
        owner = self.get("engine_pid")
        now = self._clock()
        if owner not in (0, self.pid) and now - self.get("engine_tick_ms") < ENGINE_STALE_MS:
            return False
        self.put("lead_frames", int(lead_frames))
        self.put("engine_wants", 1 if wants else 0)
        self.put("engine_tick_ms", now)
        self.put("engine_pid", self.pid)
        self._generation = -1                                          # align on the first sync
        return True

    def owns(self) -> bool:
        return self.get("engine_pid") == self.pid

    def heartbeat(self, wants: bool, lead_frames: int | None = None) -> None:
        self.put("engine_wants", 1 if wants else 0)
        if lead_frames is not None and self.get("lead_frames") != lead_frames:
            self.put("lead_frames", int(lead_frames))
        self.put("engine_tick_ms", self._clock())

    def release(self) -> None:
        """The engine is going away: the effect passes the microphone through from now on."""
        try:
            if self.owns():
                self.put("engine_wants", 0)
                self.put("engine_pid", 0)
        except Exception:                                              # noqa: BLE001 — the view may already be gone
            pass

    def close(self) -> None:
        self.release()
        self.memory.close()

    # ── the stream ──────────────────────────────────────────────────────────

    def format(self) -> tuple[int, int]:
        return self.get("rate"), self.get("channels")

    def apo_alive(self) -> bool:
        return self.get("apo_pid") != 0 and self._clock() - self.get("apo_tick_ms") < APO_STALE_MS

    def sync(self) -> bool:
        """``True`` when the effect started a new stream since the last call: both sides drop what was pending."""
        generation = self.get("generation")
        if generation == self._generation:
            return False
        self._generation = generation
        self._counter["in_read"][0] = self._counter["in_write"][0]
        self._counter["out_write"][0] = self._counter["out_read"][0]
        return True

    def discard(self) -> int:
        """Drop whatever raw microphone is waiting (the chain starts from the microphone as it is now). Returns the frames."""
        write, read = int(self._counter["in_write"][0]), int(self._counter["in_read"][0])
        self._counter["in_read"][0] = write
        return max(0, write - read)

    def available(self) -> int:
        write, read = int(self._counter["in_write"][0]), int(self._counter["in_read"][0])
        if write < read:                                               # the effect reset its side
            self._counter["in_read"][0] = write
            return 0
        return min(write - read, RING_FRAMES)

    def read(self, frames: int, channels: int) -> np.ndarray:
        """``frames`` frames of the raw microphone as a ``(frames, channels)`` float32 copy (caller checks ``available``)."""
        read = int(self._counter["in_read"][0])
        i = read % RING_FRAMES
        first = min(frames, RING_FRAMES - i)
        out = np.empty((frames, channels), dtype=np.float32)
        out[:first] = self._in[i * channels:(i + first) * channels].reshape(first, channels)
        if frames > first:
            out[first:] = self._in[:(frames - first) * channels].reshape(frames - first, channels)
        self._counter["in_read"][0] = read + frames
        return out

    def room(self) -> int:
        write, read = int(self._counter["out_write"][0]), int(self._counter["out_read"][0])
        used = write - read if write >= read else 0
        return RING_FRAMES - min(used, RING_FRAMES)

    def pending(self) -> int:
        """Frames of the processed microphone the effect has not taken yet."""
        write, read = int(self._counter["out_write"][0]), int(self._counter["out_read"][0])
        return write - read if write >= read else 0

    def write(self, block: np.ndarray) -> int:
        """Put a ``(frames, channels)`` block into the output ring; returns the frames that fitted."""
        frames = min(block.shape[0], self.room())
        if frames <= 0:
            return 0
        channels = block.shape[1]
        write = int(self._counter["out_write"][0])
        i = write % RING_FRAMES
        first = min(frames, RING_FRAMES - i)
        self._out[i * channels:(i + first) * channels] = block[:first].reshape(-1)
        if frames > first:
            self._out[:(frames - first) * channels] = block[first:frames].reshape(-1)
        self._counter["out_write"][0] = write + frames
        return frames

    def wait(self, timeout_s: float) -> bool:
        return self.memory.wait(timeout_s)

    # ── reporting ───────────────────────────────────────────────────────────

    def info(self) -> dict:
        state = self.get("apo_state")
        return {"state": STATE_NAMES.get(state, str(state)), "apo_alive": self.apo_alive(), "apo_pid": self.get("apo_pid"),
                "rate": self.get("rate"), "channels": self.get("channels"), "period_frames": self.get("period_frames"),
                "calls": self.get("calls"), "underruns": self.get("underruns"), "overruns": self.get("overruns"),
                "in_frames": self.get("in_frames"), "out_frames": self.get("out_frames"), "fault": self.get("fault"),
                "dll_build": self.get("dll_build"), "engine_owns": self.owns()}


def open_link() -> FxLink | None:
    """The link to the effect inside Windows' audio engine, or ``None`` when it is not loaded (no program is recording
    yet, the effect is not installed, or this is not Windows)."""
    memory = Win32Memory.open()
    if memory is None:
        return None
    link = FxLink(memory)
    if not link.valid():
        memory.close()
        return None
    return link

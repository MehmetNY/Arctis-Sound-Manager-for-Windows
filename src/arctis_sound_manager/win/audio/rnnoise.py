# Copyright (C) 2026 loteran — Windows portability layer
# SPDX-License-Identifier: GPL-3.0-or-later

"""RNNoise noise suppression — what ASM's "Noise cancelling" switch drives on Linux.

On Linux the microphone chain runs ``noise_suppressor_mono`` from *noise-suppression-for-voice*
(a LADSPA wrapper around Xiph's RNNoise) with one control, ``VAD Threshold (%)``, set from the
slider. This module is the same thing without LADSPA: it calls the RNNoise C library directly.

The library is found next to the ``pyrnnoise`` wheel (``rnnoise.dll`` on Windows, ``librnnoise.so`` on
Linux, ``librnnoise.dylib`` on macOS), which is the easiest way to get a prebuilt one::

    py -m pip install --no-deps pyrnnoise

``pyrnnoise``'s own Python wrapper is not used: it squeezes every frame through int16, and it does not
have the voice-activity gating. The ctypes calls below keep the audio in float.

Gating follows the plugin: a 10 ms frame whose speech probability is under the threshold is silenced,
once a *grace period* (default 200 ms) has run out since the last speech frame, and an optional
*retroactive grace* re-opens the frames just before speech is detected (it costs that much latency).
Unlike the plugin, the gate ramps over the frame instead of cutting, so closing and re-opening it
does not click.
"""
from __future__ import annotations

import ctypes
import ctypes.util
import importlib.util
import os
import sys
from collections import deque
from pathlib import Path

import numpy as np

FRAME = 480                    # RNNoise works on 10 ms frames at 48 kHz
SCALE = 32768.0                # and expects float samples in the int16 range

_NAMES = ("rnnoise.dll", "librnnoise.so", "librnnoise.dylib", "librnnoise_ladspa.so")


class RNNoiseUnavailable(RuntimeError):
    """The RNNoise library could not be found or loaded."""


def find_library() -> Path | None:
    """Locate the RNNoise shared library, or ``None``."""
    env = os.environ.get("ASM_RNNOISE_LIB")
    if env and Path(env).is_file():
        return Path(env)
    candidates: list[Path] = []
    try:
        spec = importlib.util.find_spec("pyrnnoise")      # looks, does not import
    except (ImportError, ValueError):
        spec = None
    if spec and spec.submodule_search_locations:
        candidates += [Path(p) for p in spec.submodule_search_locations]
    if getattr(sys, "frozen", False):
        # the installed program: build.ps1 puts rnnoise.dll next to the exe, and PyInstaller collects the copy from
        # the pyrnnoise wheel into its own folder
        candidates.append(Path(sys.executable).parent)
        bundle = getattr(sys, "_MEIPASS", None)
        if bundle:
            candidates += [Path(bundle), Path(bundle) / "pyrnnoise"]
    candidates.append(Path.home() / ".config" / "arctis_manager" / "native")
    for folder in candidates:
        for name in _NAMES[:3]:
            if (folder / name).is_file():
                return folder / name
    found = ctypes.util.find_library("rnnoise")
    return Path(found) if found else None


class NativeRNNoise:
    """The four C functions we need, with their ctypes signatures."""

    def __init__(self, path: str | Path):
        try:
            if sys.platform == "win32" and hasattr(os, "add_dll_directory"):
                os.add_dll_directory(str(Path(path).parent))
            lib = ctypes.CDLL(str(path))
            lib.rnnoise_create.argtypes = [ctypes.c_void_p]
            lib.rnnoise_create.restype = ctypes.c_void_p
            lib.rnnoise_destroy.argtypes = [ctypes.c_void_p]
            lib.rnnoise_get_frame_size.restype = ctypes.c_int
            lib.rnnoise_process_frame.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_float),
                                                  ctypes.POINTER(ctypes.c_float)]
            lib.rnnoise_process_frame.restype = ctypes.c_float
            if lib.rnnoise_get_frame_size() != FRAME:
                raise OSError(f"unexpected RNNoise frame size {lib.rnnoise_get_frame_size()}")
        except (OSError, AttributeError) as exc:
            raise RNNoiseUnavailable(f"cannot load RNNoise from {path}: {exc}") from exc
        self._lib = lib
        self.path = Path(path)

    def create(self):
        state = self._lib.rnnoise_create(None)
        if not state:
            raise RNNoiseUnavailable("rnnoise_create returned NULL")
        return state

    def destroy(self, state) -> None:
        self._lib.rnnoise_destroy(state)

    def process(self, state, frame: np.ndarray) -> float:
        """Denoise one float32 frame (int16 range) in place; returns the speech probability."""
        ptr = frame.ctypes.data_as(ctypes.POINTER(ctypes.c_float))
        return float(self._lib.rnnoise_process_frame(state, ptr, ptr))


_native: NativeRNNoise | None = None


def load_native() -> NativeRNNoise:
    global _native
    if _native is None:
        path = find_library()
        if path is None:
            raise RNNoiseUnavailable("RNNoise library not found (py -m pip install --no-deps pyrnnoise)")
        _native = NativeRNNoise(path)
    return _native


def available() -> bool:
    try:
        load_native()
        return True
    except RNNoiseUnavailable:
        return False


class RNNoise:
    """Mono noise suppressor with the plugin's voice-activity gate. Blocks are multiples of 480."""

    def __init__(self, vad_threshold: float = 0.5, grace_ms: float = 200.0, retro_ms: float = 0.0,
                 native=None):
        self._native = native or load_native()
        self._state = None
        self.set_params(vad_threshold, grace_ms, retro_ms)
        self.reset()

    def __del__(self):
        self.close()

    def close(self) -> None:
        state, self._state = getattr(self, "_state", None), None
        if state:
            self._native.destroy(state)

    def set_params(self, vad_threshold: float, grace_ms: float | None = None, retro_ms: float | None = None) -> None:
        """``vad_threshold`` is 0..1 (the plugin's "VAD Threshold (%)" / 100)."""
        self.vad_threshold = max(0.0, min(1.0, float(vad_threshold)))
        if grace_ms is not None:
            self.grace_frames = max(0, int(round(grace_ms / 10.0)))
        if retro_ms is not None:
            retro = max(0, int(round(retro_ms / 10.0)))
            if retro != getattr(self, "retro_frames", None):
                self.retro_frames = retro
                self._pending = deque()
            else:
                self.retro_frames = retro

    @property
    def latency_frames(self) -> int:
        return self.retro_frames * FRAME

    def reset(self) -> None:
        if self._state:
            self._native.destroy(self._state)
        self._state = self._native.create()
        self._grace_left = 0
        self._gain = 1.0                       # gate gain at the end of the last emitted frame
        self._pending: deque = deque()         # [audio, open] waiting for the retroactive window
        self.last_vad = 0.0

    def _emit(self, audio: np.ndarray, open_: bool) -> np.ndarray:
        target = 1.0 if open_ else 0.0
        if target == self._gain == 1.0:
            return audio
        ramp = self._gain + (target - self._gain) * (np.arange(1, FRAME + 1) / FRAME)
        self._gain = target
        return audio * ramp

    def process(self, x: np.ndarray) -> np.ndarray:
        """``x``: 1-D float, nominally -1..1; returns the same length."""
        x = np.asarray(x, dtype=np.float64)
        n = x.shape[-1]
        if n % FRAME:
            raise ValueError(f"block length must be a multiple of {FRAME}, got {n}")
        out = np.empty(n)
        for lo in range(0, n, FRAME):
            frame = np.ascontiguousarray(x[lo:lo + FRAME] * SCALE, dtype=np.float32)
            vad = self._native.process(self._state, frame)
            self.last_vad = vad
            clean = frame.astype(np.float64) / SCALE

            speech = vad >= self.vad_threshold
            if speech:
                self._grace_left = self.grace_frames
            elif self._grace_left > 0:
                self._grace_left -= 1
                speech = True
            if self.retro_frames:
                if speech:                                    # re-open what is still waiting
                    for item in self._pending:
                        item[1] = True
                self._pending.append([clean, speech])
                if len(self._pending) > self.retro_frames:
                    audio, open_ = self._pending.popleft()
                    out[lo:lo + FRAME] = self._emit(audio, open_)
                else:
                    out[lo:lo + FRAME] = 0.0                  # still filling the look-behind window
            else:
                out[lo:lo + FRAME] = self._emit(clean, speech)
        return out

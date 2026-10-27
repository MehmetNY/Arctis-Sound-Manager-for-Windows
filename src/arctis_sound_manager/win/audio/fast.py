# Copyright (C) 2026 loteran — Windows portability layer
# SPDX-License-Identifier: GPL-3.0-or-later

"""Optional compiled kernels (numba) for the few loops numpy cannot vectorise.

The plate reverb, the limiter and the dynamics processors are recursive: every sample depends on the
last. numpy handles that by cutting blocks into small vectorised passes, which costs a lot of
interpreter time. With `numba <https://numba.pydata.org>`_ installed (``py -m pip install numba``) the
same loops are compiled once to machine code and run 20-100x faster, in a thread that does *not* hold
the GIL, so the audio callbacks are never blocked by them.

numba is optional. Without it everything runs on the numpy code paths, to the same results (the tests
check the two against each other). ``ASM_NO_NUMBA=1`` forces the numpy paths.
"""
from __future__ import annotations

import logging
import os
import threading
import time

_log = logging.getLogger(__name__)

HAVE_NUMBA = False
_numba = None
if not os.environ.get("ASM_NO_NUMBA"):
    try:
        import numba as _numba                              # noqa: PLC0415
        HAVE_NUMBA = True
    except Exception as exc:                                # noqa: BLE001 — a broken install must not break the audio
        _log.debug("numba not available: %r", exc)


def compile(fn):                                            # noqa: A001 — a decorator-like helper
    """The numba-compiled *fn* (cached on disk, GIL released), or ``None`` when numba is unavailable."""
    if not HAVE_NUMBA:
        return None
    return _numba.njit(cache=True, nogil=True)(fn)


def disable(reason: str) -> None:
    """Called when a kernel fails at run time: log once; the caller falls back to its numpy path."""
    _log.warning("compiled kernel disabled, using the numpy path: %s", reason)


# ── warm-up ─────────────────────────────────────────────────────────────────

_ready = threading.Event()
_ready.set()                                    # nothing to wait for until a warm-up has been started


def warmup() -> float:
    """Compile (or load from numba's on-disk cache) every kernel now, by running each on a tiny block along the
    same code path the engine uses, so the audio threads never meet a first-call compile. Returns the seconds
    it took (0.0 without numba). The first start on a machine compiles for several seconds; later ones load
    the cache in a fraction of one."""
    if not HAVE_NUMBA:
        return 0.0
    import numpy as np                                          # noqa: PLC0415
    from arctis_sound_manager.win.audio import dynamics, eq, resample, reverb  # noqa: PLC0415 — they import this module
    started = time.perf_counter()
    try:
        reverb.PlateReverb(instances=2).process(np.zeros((2, 64)))
        dynamics.Limiter(48000.0, 2).process(np.zeros((2, 480)))
        sos = np.array([[1.0, 0.1, 0.0, 1.0, -0.2, 0.0]])
        eq.BiquadCascade(2, sos).process(np.zeros((2, 480)))
        reader = resample.StreamResampler(2)                    # not silence: silence never reaches the kernel
        reader.push(np.full((2, 256), 0.1))
        reader.pull(128, 1.00003)
    except Exception as exc:                                    # noqa: BLE001 — the kernels disable themselves
        _log.warning("kernel warm-up failed: %r", exc)
    took = time.perf_counter() - started
    _log.info("compiled kernels ready in %.1f s", took)
    return took


def warmup_async() -> threading.Thread | None:
    """Start :func:`warmup` in a background thread; :func:`wait_ready` waits for it."""
    if not HAVE_NUMBA:
        return None
    _ready.clear()

    def run() -> None:
        try:
            warmup()
        except Exception:                                       # noqa: BLE001 — never leave the engine waiting
            _log.exception("kernel warm-up failed")
        finally:
            _ready.set()

    thread = threading.Thread(target=run, name="asm-warmup", daemon=True)
    thread.start()
    return thread


def wait_ready(timeout: float = 180.0) -> bool:
    """Block until a warm-up started by :func:`warmup_async` has finished (immediately when none was)."""
    return _ready.wait(timeout)


def is_warming() -> bool:
    return not _ready.is_set()

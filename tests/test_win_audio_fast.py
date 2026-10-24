"""The optional compiled kernels: warm-up and fallbacks."""
from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

from arctis_sound_manager.win.audio import fast


def test_waiting_for_a_warmup_that_was_never_started_returns_at_once():
    t0 = time.time()
    assert fast.wait_ready(timeout=5.0)
    assert time.time() - t0 < 0.5 and not fast.is_warming()


def test_wait_ready_blocks_until_the_background_warmup_is_done(monkeypatch):
    if not fast.HAVE_NUMBA:
        pytest.skip("numba is not installed")
    gate = threading.Event()
    monkeypatch.setattr(fast, "warmup", lambda: gate.wait(5) and 0.0)
    thread = fast.warmup_async()
    try:
        assert fast.is_warming()
        assert not fast.wait_ready(timeout=0.2)                                # still compiling
        gate.set()
        assert fast.wait_ready(timeout=5.0) and not fast.is_warming()
    finally:
        gate.set()
        thread.join(timeout=5)
        fast._ready.set()


def test_a_failing_warmup_still_releases_the_waiters(monkeypatch):
    if not fast.HAVE_NUMBA:
        pytest.skip("numba is not installed")

    def boom():
        raise RuntimeError("no compiler")
    monkeypatch.setattr(fast, "warmup", boom)
    thread = fast.warmup_async()
    thread.join(timeout=5)
    assert fast.wait_ready(timeout=1.0)


def test_without_numba_there_is_nothing_to_warm_up(monkeypatch):
    monkeypatch.setattr(fast, "HAVE_NUMBA", False)
    assert fast.warmup() == 0.0 and fast.warmup_async() is None and fast.wait_ready(0.1)


def test_the_warmup_compiles_every_signature_the_engine_will_use():
    """In a fresh process: after warmup() the real processing paths add no new compiled signature."""
    if not fast.HAVE_NUMBA:
        pytest.skip("numba is not installed")
    script = textwrap.dedent("""
        import numpy as np
        from arctis_sound_manager.win.audio import fast, reverb, dynamics
        assert fast.warmup() > 0
        count = lambda: (len(reverb._KERNEL.signatures), len(dynamics._LIMITER_KERNEL.signatures))
        before = count()
        rng = np.random.default_rng(1)
        r = reverb.PlateReverb(2.5, 0.5, 0.5, instances=8)
        lim = dynamics.Limiter(48000.0, 8, limit_db=-3.0)
        lim2 = dynamics.Limiter(48000.0, 2)
        for n in (960, 480, 1920, 7):
            r.process(rng.standard_normal((8, n)) * 0.3)
            lim.process(rng.standard_normal((8, n)) * 2.0)
            lim2.process(rng.standard_normal((2, n)) * 2.0)
        assert count() == before, (before, count())
        print("ok")
    """)
    src = str(Path(fast.__file__).resolve().parents[3])                      # .../src
    env = {**os.environ, "PYTHONPATH": src}
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=300, env=env)
    assert result.returncode == 0 and "ok" in result.stdout, result.stderr[-2000:]

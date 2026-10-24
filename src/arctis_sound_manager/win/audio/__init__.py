# Copyright (C) 2026 loteran — Windows portability layer
# SPDX-License-Identifier: GPL-3.0-or-later

"""ASM's Windows audio engine.

On Linux the sound path is a PipeWire graph (virtual sinks -> EQ filter-chain -> HeSuVi virtual
surround -> headset, plus a microphone filter-chain). Windows has no such graph, so this package
re-implements the same processing in Python (numpy/scipy) and runs it as its own process:

    biquad / eq        PipeWire-exact RBJ biquads, the 16-slot EQ rack, macros, boost, headroom
    dynamics           the swh-plugins compressor (SC4 mono), gate and fast-lookahead limiter
    reverb             the swh plate reverb ("Distance")
    convolver          uniform-partition FFT convolution
    spatial            HeSuVi 7.1 virtual surround
    mic / denoise      the microphone chain (RNNoise, gate, compressor, EQ)

Every module here is pure DSP on numpy arrays: no audio I/O, no Windows API. That is what makes it
testable on any machine. The runtime that feeds it real devices lives in ``engine``.
"""
from __future__ import annotations

# The engine processes audio in fixed blocks of 10 ms at 48 kHz. 480 frames is also exactly one
# RNNoise frame, so the microphone chain needs no re-blocking.
SAMPLE_RATE = 48000
BLOCK = 480

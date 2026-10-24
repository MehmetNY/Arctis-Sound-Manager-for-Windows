# Copyright (C) 2026 loteran — Windows portability layer
# SPDX-License-Identifier: GPL-3.0-or-later

"""Mark functions that only make sense on a Linux desktop (systemd, PipeWire, ...).

``@linux_only(default)`` (alias ``@linux_audio_only``) is a decorator with a deliberately lopsided
contract:

* on Linux it returns **the very same function object**: no wrapper, no
  overhead, no change in behaviour, nothing for a test's ``mock.patch`` to trip
  over;
* on Windows it returns a stand-in that does nothing and answers ``default``
  (sync or async, to match the original).

That keeps the Linux code path byte-for-byte what upstream ships, and gives the
Windows port one obvious, greppable place where "the audio graph" begins and
ends, until the Windows audio engine replaces these no-ops with a backend.
"""
from __future__ import annotations

import functools
import inspect
from typing import Any, Callable

from arctis_sound_manager.constants import IS_WINDOWS


def linux_only(default: Any = None) -> Callable[[Callable], Callable]:
    def decorate(fn: Callable) -> Callable:
        if not IS_WINDOWS:
            return fn
        if inspect.iscoroutinefunction(fn):
            @functools.wraps(fn)
            async def idle_async(*_args, **_kwargs):
                return default
            return idle_async

        @functools.wraps(fn)
        def idle(*_args, **_kwargs):
            return default
        return idle
    return decorate


# Same thing, named for what it marks at the call site: the PipeWire/PulseAudio graph.
linux_audio_only = linux_only

# Copyright (C) 2026 loteran — Windows portability layer
# SPDX-License-Identifier: GPL-3.0-or-later
"""The microphone effect: how the processed microphone reaches other programs without a virtual device.

Programs record from a *device*. Making one needs a signed kernel driver, which ASM does not have, and a third-party
one (VB-CABLE) is what this replaces. Instead a small audio processing object (``native/micfx``) is attached to the
headset's own microphone: Windows' audio engine hands every block of the microphone to it, it passes the block on
to the ASM audio engine through shared memory (:mod:`.shared`), and the processed block comes back the same way.
The programs see one device, "Microphone (Arctis Nova Pro Wireless)", and nothing else.

* :mod:`.shared`   the shared-memory protocol and the engine's side of it
* :mod:`.attach`   registering the effect with Windows and attaching it to the headset's microphone
* :mod:`.cli`      ``ArctisSoundManager.exe --fx attach|detach|status|check``
"""

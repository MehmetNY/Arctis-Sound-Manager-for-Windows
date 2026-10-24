# Copyright (C) 2026 loteran — Windows portability layer
# SPDX-License-Identifier: GPL-3.0-or-later

"""Minimal HID report-descriptor parser.

Why this exists: on Linux, pyusb hands ASM raw USB descriptors and raw control
transfers, so it never has to care whether a HID interface uses *numbered*
reports. Windows' HID stack (and therefore hidapi) does care: every write
buffer must start with the report id, ``0x00`` when the device has none, and
Windows wants buffers of exactly the report's length. Both facts live in the
report descriptor, which is what this module reads.

Pure Python, no OS calls, so it is fully testable on any platform.
"""
from __future__ import annotations

from dataclasses import dataclass, field

_SIZES = (0, 1, 2, 4)  # bSize code -> number of data bytes

# Item types
_MAIN, _GLOBAL, _LOCAL = 0, 1, 2
# Main tags
_INPUT, _OUTPUT, _COLLECTION, _FEATURE, _END_COLLECTION = 8, 9, 10, 11, 12
# Global tags
_USAGE_PAGE, _REPORT_SIZE, _REPORT_ID, _REPORT_COUNT, _PUSH, _POP = 0, 7, 8, 9, 10, 11
# Local tags
_USAGE = 0


@dataclass
class Collection:
    """One top-level HID collection (what Windows exposes as one device node)."""

    usage_page: int = 0
    usage: int = 0
    report_ids: set[int] = field(default_factory=set)
    # (kind, report_id) -> total payload bits
    _bits: dict[tuple[str, int], int] = field(default_factory=dict)

    @property
    def uses_report_ids(self) -> bool:
        """True when reports are numbered: the id is the first byte on the wire."""
        return bool(self.report_ids)

    def _length(self, kind: str) -> int:
        """Largest report of ``kind`` in bytes, report-id byte included when numbered.

        This is the raw-USB view (the data stage of the transfer), which is what
        the rest of ASM was written against, not Windows' ``*ReportByteLength``
        (which always adds one byte for the id slot).
        """
        best = 0
        for (k, rid), bits in self._bits.items():
            if k == kind:
                best = max(best, (bits + 7) // 8 + (1 if rid else 0))
        return best

    @property
    def input_len(self) -> int:
        return self._length("input")

    @property
    def output_len(self) -> int:
        return self._length("output")

    @property
    def feature_len(self) -> int:
        return self._length("feature")


def parse_report_descriptor(desc: bytes | bytearray | list[int]) -> list[Collection]:
    """Parse a HID report descriptor into its top-level collections.

    Tolerant by design: a truncated or odd descriptor yields what could be read
    rather than raising, because the caller only uses this to *refine* defaults.
    """
    data = bytes(desc)
    n = len(data)
    i = 0

    g = {"usage_page": 0, "report_size": 0, "report_count": 0, "report_id": 0}
    g_stack: list[dict[str, int]] = []
    local_usages: list[tuple[int, int]] = []  # (page, usage)

    depth = 0
    current: Collection | None = None
    out: list[Collection] = []

    while i < n:
        prefix = data[i]
        if prefix == 0xFE:  # long item: 0xFE, bDataSize, bLongItemTag, data...
            if i + 1 >= n:
                break
            i += 3 + data[i + 1]
            continue

        size = _SIZES[prefix & 0x03]
        typ = (prefix >> 2) & 0x03
        tag = prefix >> 4
        raw = data[i + 1:i + 1 + size]
        value = int.from_bytes(raw, "little") if size else 0
        i += 1 + size

        if typ == _GLOBAL:
            if tag == _USAGE_PAGE:
                g["usage_page"] = value
            elif tag == _REPORT_SIZE:
                g["report_size"] = value
            elif tag == _REPORT_COUNT:
                g["report_count"] = value
            elif tag == _REPORT_ID:
                g["report_id"] = value
                if current is not None and value:
                    current.report_ids.add(value)
            elif tag == _PUSH:
                g_stack.append(dict(g))
            elif tag == _POP and g_stack:
                g = g_stack.pop()

        elif typ == _LOCAL:
            if tag == _USAGE:
                if size == 4:  # extended usage: page in the high 16 bits
                    local_usages.append((value >> 16, value & 0xFFFF))
                else:
                    local_usages.append((g["usage_page"], value))

        elif typ == _MAIN:
            if tag == _COLLECTION:
                if depth == 0:
                    page, usage = local_usages[0] if local_usages else (g["usage_page"], 0)
                    current = Collection(usage_page=page, usage=usage)
                    if g["report_id"]:  # report id declared before the collection opened
                        current.report_ids.add(g["report_id"])
                    out.append(current)
                depth += 1
                local_usages.clear()
            elif tag == _END_COLLECTION:
                depth = max(0, depth - 1)
                if depth == 0:
                    current = None
                local_usages.clear()
            elif tag in (_INPUT, _OUTPUT, _FEATURE):
                if current is not None:
                    kind = {_INPUT: "input", _OUTPUT: "output", _FEATURE: "feature"}[tag]
                    key = (kind, g["report_id"])
                    current._bits[key] = current._bits.get(key, 0) + g["report_size"] * g["report_count"]
                local_usages.clear()

    return out

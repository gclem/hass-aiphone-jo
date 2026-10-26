"""SDP mid normalization (same behaviour as ``aiphone_jo.sdp``).

The relay names its media sections ``audio`` and ``video`` and advertises the
``sdes:mid`` header extension; go2rtc expects numeric mids. Used when the
bundled library does not provide ``aiphone_jo.sdp``.
"""

from __future__ import annotations

import re
from collections.abc import Callable

SDES_MID_EXTENSION = "urn:ietf:params:rtp-hdrext:sdes:mid"

_MID = re.compile(r"^a=mid:(?P<mid>\S+)\s*$")
_GROUP = re.compile(r"^a=group:(?P<semantics>\S+)(?P<mids>(?: \S+)*)\s*$")

Restore = Callable[[str], str]


def _split(sdp: str) -> tuple[list[str], str, bool]:
    newline = "\r\n" if "\r\n" in sdp else "\n"
    trailing = sdp.endswith(newline)
    lines = sdp.split(newline)
    if trailing:
        lines.pop()
    return lines, newline, trailing


def _join(lines: list[str], newline: str, trailing: bool) -> str:
    return newline.join(lines) + (newline if trailing else "")


def _rename(sdp: str, mapping: dict[str, str]) -> str:
    lines, newline, trailing = _split(sdp)
    result: list[str] = []
    for line in lines:
        if mid := _MID.match(line):
            result.append(f"a=mid:{mapping.get(mid['mid'], mid['mid'])}")
            continue
        group = _GROUP.match(line)
        if group and group["mids"]:
            names = [mapping.get(name, name) for name in group["mids"].split()]
            result.append(f"a=group:{group['semantics']} {' '.join(names)}")
            continue
        result.append(line)
    return _join(result, newline, trailing)


def normalize_mids(offer: str) -> tuple[str, Restore]:
    """Rewrite an offer with numeric mids; return it and ``restore(answer)``."""
    lines, newline, trailing = _split(offer)
    renamed: dict[str, str] = {}
    section = -1
    for line in lines:
        if line.startswith("m="):
            section += 1
            continue
        mid = _MID.match(line)
        if mid and section >= 0 and mid["mid"] not in renamed:
            renamed[mid["mid"]] = str(section)
    kept = [
        line
        for line in lines
        if not (line.startswith("a=extmap:") and SDES_MID_EXTENSION in line)
    ]
    normalized = _rename(_join(kept, newline, trailing), renamed)
    original = {new: old for old, new in renamed.items()}

    def restore(answer: str) -> str:
        return _rename(answer, original)

    return normalized, restore

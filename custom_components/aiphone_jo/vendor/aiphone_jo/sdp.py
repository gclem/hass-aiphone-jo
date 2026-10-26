"""Pure SDP helpers for bridging the relay's offer to other WebRTC stacks.

The relay (a Janus gateway) names its media sections ``audio`` and ``video``
(``a=mid:audio``) and advertises the ``sdes:mid`` RTP header extension. Some
WebRTC servers, for example go2rtc, expect numeric mids and answer
``inactive`` otherwise. :func:`normalize_mids` rewrites the offer for them and
returns a function that restores the original mids in their answer, which is
what the relay expects back.

:func:`extract_section` turns the offer into a standalone offer for a single
media section (for example the audio of a call, for a peer that only handles
audio) and rebuilds the complete answer, other sections rejected, from the
answer to that section.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

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
    """Rename ``a=mid`` and ``a=group`` entries according to ``mapping``."""
    lines, newline, trailing = _split(sdp)
    result: list[str] = []
    for line in lines:
        mid = _MID.match(line)
        if mid:
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
    """Rewrite an offer with numeric mids (``0``, ``1``... in m-line order).

    Also removes the ``sdes:mid`` header extension (``a=extmap`` lines).
    Returns the rewritten offer and ``restore(answer)``, which puts the
    original mids back into an answer to the rewritten offer. Line endings
    are preserved. An offer without mids is returned unchanged.
    """
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
        line for line in lines if not (line.startswith("a=extmap:") and SDES_MID_EXTENSION in line)
    ]
    normalized = _rename(_join(kept, newline, trailing), renamed)
    original = {new: old for old, new in renamed.items()}

    def restore(answer: str) -> str:
        return _rename(answer, original)

    return normalized, restore


DIRECTIONS = frozenset({"sendrecv", "sendonly", "recvonly", "inactive"})
DEFAULT_DIRECTION = "sendrecv"


@dataclass(frozen=True, slots=True)
class MediaSection:
    """Summary of one ``m=`` section of an SDP.

    ``direction`` is the section's direction attribute, else the session-level
    one, else ``sendrecv`` (RFC 4566). ``port`` 0 means the section is
    rejected or disabled.
    """

    index: int
    kind: str
    port: int
    proto: str
    formats: tuple[str, ...]
    mid: str | None
    direction: str


@dataclass(slots=True)
class _Parsed:
    session: list[str]
    sections: list[list[str]]
    newline: str
    trailing: bool


def _parse(sdp: str) -> _Parsed:
    lines, newline, trailing = _split(sdp)
    session: list[str] = []
    sections: list[list[str]] = []
    for line in lines:
        if line.startswith("m="):
            sections.append([line])
        elif sections:
            sections[-1].append(line)
        else:
            session.append(line)
    return _Parsed(session, sections, newline, trailing)


def _direction(lines: list[str]) -> str | None:
    for line in lines:
        if line.startswith("a=") and line[2:].strip() in DIRECTIONS:
            return line[2:].strip()
    return None


def _describe(index: int, lines: list[str], session_direction: str) -> MediaSection:
    fields = lines[0][2:].split()
    if len(fields) < 3:
        raise ValueError(f"Invalid m= line in section {index}.")
    port = fields[1].split("/", 1)[0]
    if not port.isdigit():
        raise ValueError(f"Invalid port in section {index}.")
    mid = next((match["mid"] for line in lines if (match := _MID.match(line))), None)
    return MediaSection(
        index=index,
        kind=fields[0],
        port=int(port),
        proto=fields[2],
        formats=tuple(fields[3:]),
        mid=mid,
        direction=_direction(lines[1:]) or session_direction,
    )


def media_sections(sdp: str) -> list[MediaSection]:
    """Describe the media sections of ``sdp`` in m-line order."""
    parsed = _parse(sdp)
    session_direction = _direction(parsed.session) or DEFAULT_DIRECTION
    return [
        _describe(index, lines, session_direction) for index, lines in enumerate(parsed.sections)
    ]


def _keep_group_mids(lines: list[str], keep: set[str]) -> list[str]:
    """Restrict every ``a=group`` line to ``keep``; drop groups left empty."""
    result: list[str] = []
    for line in lines:
        group = _GROUP.match(line)
        if group is None:
            result.append(line)
            continue
        names = [name for name in group["mids"].split() if name in keep]
        if names:
            result.append(f"a=group:{group['semantics']} {' '.join(names)}")
    return result


def _rejected_section(section: MediaSection, connection: bool) -> list[str]:
    # JSEP (RFC 8829, 5.3.1): a rejected section keeps the offered kind and
    # proto, port 0, at least one format, and its mid; it leaves the BUNDLE group.
    fmt = section.formats[0] if section.formats else "0"
    lines = [f"m={section.kind} 0 {section.proto} {fmt}"]
    if connection:
        lines.append("c=IN IP4 0.0.0.0")
    if section.mid is not None:
        lines.append(f"a=mid:{section.mid}")
    lines.append("a=inactive")
    return lines


def extract_section(offer: str, kind: str, *, direction: str | None = None) -> tuple[str, Restore]:
    """Extract one media section of ``offer`` as a standalone offer.

    Selects the first section whose media ``kind`` (``audio``, ``video``...)
    and, when given, offered ``direction`` (see :class:`MediaSection`) match,
    skipping sections with port 0. The standalone offer keeps every
    session-level line; ``a=group`` lines (BUNDLE) only keep the selected mid
    and are dropped when nothing is left.

    Returns the standalone offer and ``restore(section_answer)``, which builds
    the answer to the complete ``offer``: the given answer section in its
    original position, every other section rejected (port 0, ``a=inactive``,
    mid kept, removed from BUNDLE). ``restore`` raises :class:`ValueError` if
    the answer does not contain exactly one section of the same kind. Line
    endings are preserved. Raises :class:`ValueError` when no section matches.
    """
    if direction is not None and direction not in DIRECTIONS:
        raise ValueError(f"Unknown SDP direction {direction!r}.")
    parsed = _parse(offer)
    session_direction = _direction(parsed.session) or DEFAULT_DIRECTION
    described = [
        _describe(index, lines, session_direction) for index, lines in enumerate(parsed.sections)
    ]
    selected = next(
        (
            section
            for section in described
            if section.kind == kind
            and section.port != 0
            and (direction is None or section.direction == direction)
        ),
        None,
    )
    if selected is None:
        wanted = f"{kind} ({direction})" if direction else kind
        raise ValueError(f"The offer has no active {wanted} section.")
    keep = {selected.mid} if selected.mid is not None else set()
    single = _keep_group_mids(parsed.session, keep) + parsed.sections[selected.index]
    standalone = _join(single, parsed.newline, parsed.trailing)

    def restore(section_answer: str) -> str:
        answer = _parse(section_answer)
        if len(answer.sections) != 1:
            raise ValueError("The answer must contain exactly one media section.")
        answered = _describe(0, answer.sections[0], DEFAULT_DIRECTION)
        if answered.kind != kind:
            raise ValueError(f"The answer section is {answered.kind!r}, expected {kind!r}.")
        session_connection = any(line.startswith("c=") for line in answer.session)
        answer_keep = {answered.mid} if answered.mid is not None else set()
        lines = _keep_group_mids(answer.session, answer_keep)
        for section in described:
            if section.index == selected.index:
                lines.extend(answer.sections[0])
            else:
                lines.extend(_rejected_section(section, not session_connection))
        return _join(lines, answer.newline, answer.trailing)

    return standalone, restore

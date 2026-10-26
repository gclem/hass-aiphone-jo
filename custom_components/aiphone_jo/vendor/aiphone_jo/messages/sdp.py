"""Outgoing SDP for the relay.

Attribute order: BUNDLE group, ice-ufrag, ice-pwd, fingerprint,
setup:actpass, then media sections.
"""

from __future__ import annotations

import secrets
import time
from collections.abc import Sequence
from datetime import datetime

from .._util import require_text

# Codec index -> (name, clock rate, default payload type).
_AUDIO_CODECS = {1: ("PCMU", 8000, 0), 2: ("PCMA", 8000, 8), 3: ("G722", 8000, 9)}
_VIDEO_CODECS = {1: ("H264", 90000, 107), 2: ("JPEG", 90000, 96)}
_H264_FMTP = "level-asymmetry-allowed=1;packetization-mode=1;profile-level-id=42e01f"

# Placeholder host candidate sent with STA_RELAY_REQ. The value is arbitrary
# (RFC 5737 documentation address): the relay does not use it for media; the
# real negotiation happens later through ADD_RCV_USER / UPDATE_RCV_USER.
SYNTHETIC_CANDIDATE = "candidate:1 1 UDP 2130706431 192.0.2.10 51712 typ host"
SYNTHETIC_PORT = 51712


def _media_section(
    kind: str,
    *,
    codecs: Sequence[int],
    payload_types: Sequence[int],
    fec_payload_type: int,
    direction: str,
    ssrc: int | None,
    port: int,
    candidates: Sequence[str],
) -> list[str] | None:
    table = _AUDIO_CODECS if kind == "audio" else _VIDEO_CODECS
    formats: list[tuple[int, str, int, str | None]] = []
    for slot, index in enumerate(codecs):
        if index not in table:
            continue
        name, clock, default_pt = table[index]
        pt = (
            payload_types[slot]
            if slot < len(payload_types) and payload_types[slot] >= 0
            else default_pt
        )
        fmtp = _H264_FMTP if kind == "video" and name == "H264" else None
        formats.append((pt, name, clock, fmtp))
    if not formats:
        return None
    pts = [str(pt) for pt, *_ in formats]
    if fec_payload_type >= 0:
        pts.append(str(fec_payload_type))
    lines = [f"m={kind} {port} UDP/TLS/RTP/SAVP {' '.join(pts)}", "c=IN IP4 0.0.0.0"]
    for pt, name, clock, fmtp in formats:
        lines.append(f"a=rtpmap:{pt} {name}/{clock}")
        if fmtp:
            lines.append(f"a=fmtp:{pt} {fmtp}")
    if fec_payload_type >= 0:
        lines.append(f"a=rtpmap:{fec_payload_type} ulpfec/{formats[0][2]}")
    lines += [f"a={direction}", f"a=mid:{kind}", "a=rtcp-mux"]
    if direction == "sendonly" and ssrc is not None:
        lines.append(f"a=ssrc:{ssrc}")
    lines += [f"a={candidate.removeprefix('a=')}" for candidate in candidates]
    return lines


def build_relay_sdp(
    *,
    fingerprint: str,
    ufrag: str,
    pwd: str,
    candidates: Sequence[str] = (),
    port: int = 9,
    address: str = "0.0.0.0",  # noqa: S104 - SDP placeholder, not a bind address
    audio_codecs: Sequence[int] = (1, 0),
    video_codecs: Sequence[int] = (0, 0),
    audio_payload_types: Sequence[int] = (),
    video_payload_types: Sequence[int] = (),
    fec_payload_type: int = -1,
    direction: str = "recvonly",
    ssrc: int | None = None,
    video_ssrc: int | None = None,
    now: datetime | None = None,
) -> str:
    """Build an SDP in the protocol's attribute order (CRLF line endings)."""
    require_text(fingerprint, "fingerprint", 200)
    require_text(ufrag, "ice-ufrag", 256)
    require_text(pwd, "ice-pwd", 256)
    session_id = int(now.timestamp()) if now else int(time.time())
    lines = [
        "v=0",
        f"o=- {session_id} 1 IN IP4 {address}",
        "s=-",
        f"c=IN IP4 {address}",
        "t=0 0",
        "a=group:BUNDLE audio video",
        f"a=ice-ufrag:{ufrag}",
        f"a=ice-pwd:{pwd}",
        f"a=fingerprint:{fingerprint}",
        "a=setup:actpass",
    ]
    audio = _media_section(
        "audio",
        codecs=audio_codecs,
        payload_types=audio_payload_types,
        fec_payload_type=fec_payload_type,
        direction=direction,
        ssrc=ssrc,
        port=port,
        candidates=candidates,
    )
    video = _media_section(
        "video",
        codecs=video_codecs,
        payload_types=video_payload_types,
        fec_payload_type=fec_payload_type,
        direction=direction,
        ssrc=video_ssrc,
        port=port,
        candidates=candidates,
    )
    for section in (audio, video):
        if section:
            lines += section
    return "\r\n".join(lines) + "\r\n"


def build_synthetic_relay_sdp(now: datetime | None = None) -> str:
    """Synthetic SDP sent with STA_RELAY_REQ (30001).

    Random ufrag (4 bytes hex), pwd (16 bytes hex), DTLS fingerprint (32 random
    bytes) and SSRC; PCMU audio only, ``sendonly``, one placeholder host
    candidate (:data:`SYNTHETIC_CANDIDATE`, arbitrary documentation address).
    It opens the relay session (31001 RSLT 200) needed by monitor_req and the
    door commands. The real media negotiation happens later through
    ADD_RCV_USER (Janus offer) / UPDATE_RCV_USER (our answer).
    """
    fingerprint = ":".join(f"{byte:02X}" for byte in secrets.token_bytes(32))
    return build_relay_sdp(
        fingerprint=f"sha-256 {fingerprint}",
        ufrag=secrets.token_hex(4),
        pwd=secrets.token_hex(16),
        candidates=[SYNTHETIC_CANDIDATE],
        audio_codecs=(1, 0),
        video_codecs=(0, 0),
        direction="sendonly",
        ssrc=int.from_bytes(secrets.token_bytes(4), "big"),
        port=SYNTHETIC_PORT,
        now=now,
    )

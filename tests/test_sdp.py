"""Tests for the SDP mid normalization used with go2rtc."""

from __future__ import annotations

import importlib

import pytest

from custom_components.aiphone_jo import sdp as fallback

from .fake_go2rtc import GO2RTC_ANSWER, JANUS_OFFER


def _implementations() -> list:  # type: ignore[type-arg]
    found = [fallback.normalize_mids]
    try:
        vendored = importlib.import_module(
            "custom_components.aiphone_jo.vendor.aiphone_jo.sdp"
        )
    except ImportError:
        return found
    return [*found, vendored.normalize_mids]


@pytest.fixture(params=_implementations())
def normalize(request: pytest.FixtureRequest):  # type: ignore[no-untyped-def]
    """Each available implementation."""
    return request.param


def test_offer_uses_numeric_mids(normalize) -> None:  # type: ignore[no-untyped-def]
    """Named mids become 0/1, in m-line order, BUNDLE included."""
    offer, _ = normalize(JANUS_OFFER)
    lines = offer.split("\r\n")
    assert "a=group:BUNDLE 0 1" in lines
    mids = [line for line in lines if line.startswith("a=mid:")]
    assert mids == ["a=mid:0", "a=mid:1"]


def test_offer_drops_sdes_mid_extension(normalize) -> None:  # type: ignore[no-untyped-def]
    """The sdes:mid header extension is removed; other lines are kept."""
    offer, _ = normalize(JANUS_OFFER)
    assert "sdes:mid" not in offer
    assert "a=rtpmap:96 H264/90000" in offer
    assert offer.count("a=candidate:") == 2
    expected = len(JANUS_OFFER.split("\r\n")) - 2
    assert len(offer.split("\r\n")) == expected


def test_line_endings_preserved(normalize) -> None:  # type: ignore[no-untyped-def]
    """CRLF stays CRLF, LF stays LF, and the trailing newline is kept."""
    offer, _ = normalize(JANUS_OFFER)
    assert offer.endswith("\r\n")
    assert "\n" not in offer.replace("\r\n", "")
    lf, _ = normalize(JANUS_OFFER.replace("\r\n", "\n"))
    assert "\r" not in lf
    assert lf.endswith("\n")


def test_answer_restored(normalize) -> None:  # type: ignore[no-untyped-def]
    """The answer gets the original mids back for the relay."""
    _, restore = normalize(JANUS_OFFER)
    answer = restore(GO2RTC_ANSWER).split("\r\n")
    assert "a=group:BUNDLE audio video" in answer
    assert [line for line in answer if line.startswith("a=mid:")] == [
        "a=mid:audio",
        "a=mid:video",
    ]


def test_offer_without_mids_unchanged(normalize) -> None:  # type: ignore[no-untyped-def]
    """An offer without mids is returned as is."""
    sdp = "v=0\r\nm=audio 9 RTP/AVP 0\r\n"
    offer, restore = normalize(sdp)
    assert offer == sdp
    assert restore(sdp) == sdp

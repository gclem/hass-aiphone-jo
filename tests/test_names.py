"""Tests for the station labels."""

from __future__ import annotations

import pytest

from custom_components.aiphone_jo.names import StationNamer


@pytest.mark.parametrize(
    ("name", "label"),
    [
        ("玄関1", "Entrance 1"),
        ("玄関2", "Entrance 2"),
        ("玄関 3", "Entrance 3"),
        ("玄関１", "Entrance 1"),
        ("玄関", "Entrance"),
        ("Front", "Front"),
        ("玄関ホール", "玄関ホール"),
        ("Back 玄関1", "Back 玄関1"),
        ("", ""),
    ],
)
def test_default_names_are_translated(name: str, label: str) -> None:
    """Only the factory default entrance names are replaced."""
    assert StationNamer()(name) == label


def test_translated_template() -> None:
    """The template comes from the translations."""
    assert StationNamer("Entrée {number}")("玄関2") == "Entrée 2"
    assert StationNamer("Entrée {number}")("玄関") == "Entrée"

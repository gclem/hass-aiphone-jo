"""Every error, abort and exception key used in code has a translation."""

from __future__ import annotations

import json
import re
from pathlib import Path

from custom_components.aiphone_jo.errors import REASON_KEYS

ROOT = Path(__file__).parent.parent / "custom_components" / "aiphone_jo"


def _source() -> str:
    return "\n".join(path.read_text() for path in ROOT.glob("*.py"))


def _strings(name: str) -> dict:
    return json.loads((ROOT / name).read_text(encoding="utf-8"))


def test_error_keys_are_translated() -> None:
    """Form errors exist in every language."""
    keys = set(re.findall(r'FlowError\(\s*"(\w+)"', _source())) | set(
        REASON_KEYS.values()
    )
    keys |= set(re.findall(r'key = "(\w+)"', _source()))
    keys |= set(re.findall(r'"(\w+)" if ', _source()))
    for name in ("strings.json", "translations/en.json", "translations/fr.json"):
        assert keys <= set(_strings(name)["config"]["error"]), name


def test_abort_and_exception_keys_are_translated() -> None:
    """Abort reasons and exception messages exist in every language."""
    source = _source()
    aborts = set(re.findall(r'reason="(\w+)"', source))
    exceptions = set(re.findall(r'translation_key="(\w+)"', source))
    for name in ("strings.json", "translations/en.json", "translations/fr.json"):
        strings = _strings(name)
        known = set(strings["config"]["abort"]) | set(
            strings["options"].get("abort", {})
        )
        assert aborts <= known, name
        assert exceptions <= set(strings["exceptions"]), name


def test_languages_are_in_sync() -> None:
    """English and French translations have the same keys; en matches strings.json."""

    def keys(data: dict, prefix: str = "") -> set[str]:
        out: set[str] = set()
        for key, value in data.items():
            out |= (
                keys(value, f"{prefix}{key}.")
                if isinstance(value, dict)
                else {prefix + key}
            )
        return out

    assert _strings("strings.json") == _strings("translations/en.json")
    assert keys(_strings("translations/en.json")) == keys(
        _strings("translations/fr.json")
    )


def test_station_label_is_translated() -> None:
    """The entrance station label exists in every language with its placeholder."""
    from custom_components.aiphone_jo.names import STATION_ENTRANCE_KEY

    for name in ("strings.json", "translations/en.json", "translations/fr.json"):
        message = _strings(name)["exceptions"][STATION_ENTRANCE_KEY]["message"]
        assert "{number}" in message, name

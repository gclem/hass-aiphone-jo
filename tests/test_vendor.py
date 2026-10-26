"""Tests for the vendored aiphone-jo library."""

import importlib
import pkgutil
from importlib import resources
from pathlib import Path

from custom_components.aiphone_jo.vendor import aiphone_jo
from scripts.release_tools import VENDORED_VERSION, parse_vendored_version


def test_vendored_package_imports() -> None:
    """Every vendored module imports from its vendored location."""
    for module in pkgutil.walk_packages(aiphone_jo.__path__, f"{aiphone_jo.__name__}."):
        importlib.import_module(module.name)


def test_vendored_version_matches_package() -> None:
    """VENDORED_VERSION describes the vendored package."""
    info = parse_vendored_version(VENDORED_VERSION.read_text(encoding="utf-8"))

    assert info["version"] == aiphone_jo.__version__
    assert len(info["sha"]) == 40


def test_vendored_package_data_is_available() -> None:
    """Data files are readable relative to the vendored package."""
    package = resources.files(aiphone_jo.__name__)
    root = Path(aiphone_jo.__file__).parent
    data = [
        path.relative_to(root)
        for path in root.rglob("*")
        if path.is_file() and path.suffix != ".py" and "__pycache__" not in path.parts
    ]
    for relative in data:
        assert package.joinpath(*relative.parts).read_bytes() is not None

"""Tests for the release tooling."""

import json
import tarfile
from pathlib import Path

import pytest

from scripts import release_tools as rt

CHANGELOG = """# Changelog

Intro.

## Unreleased

- Pending change.

## 0.1.0

- First release.
"""


@pytest.mark.parametrize(
    ("old", "new", "level"),
    [
        ("0.1.0", "0.1.1", "patch"),
        ("0.1.3", "0.2.0", "minor"),
        ("0.9.9", "1.0.0", "major"),
        ("0.0.0", "0.1.0", "minor"),
        ("0.2.0", "0.2.0", None),
        ("0.2.0b1", "0.2.0", None),
        ("0.1.0", "0.2.0rc1", "minor"),
    ],
)
def test_bump_level(old: str, new: str, level: str | None) -> None:
    """The bump level is the highest changed component."""
    assert rt.bump_level(old, new) == level


def test_bump_level_rejects_downgrade() -> None:
    """A library downgrade is refused."""
    with pytest.raises(rt.ReleaseError):
        rt.bump_level("0.2.0", "0.1.9")


@pytest.mark.parametrize(
    ("level", "expected"),
    [("patch", "1.2.4"), ("minor", "1.3.0"), ("major", "2.0.0")],
)
def test_bump(level: str, expected: str) -> None:
    """Versions are bumped at the requested level."""
    assert rt.bump("1.2.3", level) == expected


def test_manifest_requirements_skip_home_assistant_core_packages() -> None:
    """Dependencies shipped by Home Assistant core are not required again."""
    deps = ["aiohttp>=3.9", "aiomqtt>=2.3", "cryptography>=42", "ifaddr>=0.2"]

    assert rt.manifest_requirements(deps) == ["aiomqtt>=2.3"]


def test_release_changelog_promotes_unreleased() -> None:
    """The Unreleased section becomes the new version, with the new entry."""
    text = rt.release_changelog(CHANGELOG, "0.2.0", "- Library update.")

    assert rt.changelog_versions(text) == ["0.2.0", "0.1.0"]
    assert rt.changelog_section(text, "0.2.0") == "- Pending change.\n- Library update."
    assert rt.changelog_section(text, "0.1.0") == "- First release."
    assert text.startswith("# Changelog\n\nIntro.\n\n## 0.2.0\n")


def test_release_changelog_without_unreleased() -> None:
    """A version section is created when there is no Unreleased section."""
    source = CHANGELOG.replace("## Unreleased\n\n- Pending change.\n\n", "")
    text = rt.release_changelog(source, "0.1.1", "- Library update.")

    assert rt.changelog_versions(text) == ["0.1.1", "0.1.0"]
    assert rt.changelog_section(text, "0.1.1") == "- Library update."


def test_repository_release_metadata_is_consistent() -> None:
    """Manifest, CHANGELOG and vendored library agree in this repository."""
    assert rt.check() == []


SHA = "a" * 40


def _fake_package(directory: Path, version: str, marker: str) -> Path:
    directory.mkdir(parents=True)
    (directory / "__init__.py").write_text(f'__version__ = "{version}"\n# {marker}\n')
    return directory


def _fake_sdist(root: Path, version: str, marker: str) -> Path:
    """Build an sdist shaped like the library's hatchling output."""
    base = root / f"aiphone_jo-{version}"
    _fake_package(base / "src" / "aiphone_jo", version, marker)
    (base / "PKG-INFO").write_text(
        "Metadata-Version: 2.4\n"
        f"Name: aiphone-jo\nVersion: {version}\n"
        "Requires-Dist: aiohttp>=3.9\n"
        "Requires-Dist: aiomqtt>=2.4\n"
        "Requires-Dist: pytest; extra == 'dev'\n"
        "\nRequires-Dist: not-a-header\n"
    )
    sdist = root / f"aiphone_jo-{version}.tar.gz"
    with tarfile.open(sdist, "w:gz") as archive:
        archive.add(base, arcname=base.name)
    return sdist


def _sync(sdist: Path, tag: str, *extra: str) -> int:
    args = ["sync-lib", "--sdist", str(sdist), "--tag", tag, "--sha", SHA]
    return rt.main([*args, *extra])


@pytest.fixture
def sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the release tooling at a throw-away integration copy."""
    vendor = tmp_path / "vendor"
    _fake_package(vendor / "aiphone_jo", "0.1.0", "old")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"version": "0.1.0", "requirements": []}))
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text(CHANGELOG)
    monkeypatch.setattr(rt, "MANIFEST", manifest)
    monkeypatch.setattr(rt, "VENDORED_PACKAGE", vendor / "aiphone_jo")
    monkeypatch.setattr(rt, "VENDORED_VERSION", vendor / "VENDORED_VERSION")
    monkeypatch.setattr(rt, "CHANGELOG", changelog)
    monkeypatch.setattr(rt, "release_tags", list)
    return tmp_path


@pytest.mark.parametrize(
    ("released", "new", "expected"),
    [
        ("0.1.0", "0.1.1", "0.1.1"),
        ("0.1.0", "0.2.0", "0.2.0"),
        ("0.0.0", "0.1.0", "0.2.0"),
        ("0.2.0rc1", "0.2.0", "0.1.1"),
    ],
)
def test_sync_lib(
    sandbox: Path,
    monkeypatch: pytest.MonkeyPatch,
    released: str,
    new: str,
    expected: str,
) -> None:
    """Syncing vendors the library and bumps the integration at the same level."""
    monkeypatch.setattr(rt, "released_lib_version", lambda tag: released)
    sdist = _fake_sdist(sandbox / "dist", new, "new")

    assert _sync(sdist, f"v{new}") == 0

    manifest = json.loads(rt.MANIFEST.read_text())
    assert manifest == {"version": expected, "requirements": ["aiomqtt>=2.4"]}
    assert "# new" in (rt.VENDORED_PACKAGE / "__init__.py").read_text()
    info = rt.parse_vendored_version(rt.VENDORED_VERSION.read_text())
    assert info == {"version": new, "sha": SHA}
    assert rt.changelog_versions(rt.CHANGELOG.read_text())[0] == expected


def test_sync_lib_noop_when_already_released(
    sandbox: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Re-syncing an already released, identical library changes nothing."""
    monkeypatch.setattr(rt, "released_lib_version", lambda tag: "0.1.0")
    sdist = _fake_sdist(sandbox / "dist", "0.1.0", "old")
    before = rt.MANIFEST.read_text()

    assert _sync(sdist, "v0.1.0") == 0
    assert rt.MANIFEST.read_text() == before
    assert not rt.VENDORED_VERSION.exists()


def test_sync_lib_skips_prerelease_by_default(
    sandbox: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pre-releases are only synced when explicitly allowed."""
    monkeypatch.setattr(rt, "released_lib_version", lambda tag: "0.1.0")
    sdist = _fake_sdist(sandbox / "dist", "0.2.0b1", "new")
    before = rt.MANIFEST.read_text()

    assert _sync(sdist, "v0.2.0b1") == 0
    assert rt.MANIFEST.read_text() == before

    assert _sync(sdist, "v0.2.0b1", "--allow-prerelease") == 0
    assert json.loads(rt.MANIFEST.read_text())["version"] == "0.2.0"


def test_sync_lib_rejects_mismatched_tag(
    sandbox: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The tag must match the library version."""
    monkeypatch.setattr(rt, "released_lib_version", lambda tag: "0.1.0")
    sdist = _fake_sdist(sandbox / "dist", "0.2.0", "new")

    assert _sync(sdist, "v0.3.0") == 1
    assert _sync(sdist, "0.2.0") == 1

"""Maintenance helpers."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from collections.abc import Iterable
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INTEGRATION = ROOT / "custom_components" / "aiphone_jo"
MANIFEST = INTEGRATION / "manifest.json"
VENDOR = INTEGRATION / "vendor"
VENDORED_PACKAGE = VENDOR / "aiphone_jo"
VENDORED_VERSION = VENDOR / "VENDORED_VERSION"
CHANGELOG = ROOT / "CHANGELOG.md"

VENDORED_VERSION_PATH = "custom_components/aiphone_jo/vendor/VENDORED_VERSION"

HA_PROVIDED = frozenset({"aiohttp", "cryptography", "ifaddr"})

UNRELEASED = "Unreleased"
LEVELS = ("major", "minor", "patch")

_SEMVER = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")
_TAG = re.compile(r"^v(\d+\.\d+\.\d+)$")
_LIB_SEMVER = re.compile(r"^(\d+)\.(\d+)\.(\d+)(?:(a|b|rc)(\d+))?$")
_LIB_TAG = re.compile(r"^v(\d+\.\d+\.\d+(?:(?:a|b|rc)\d+)?)$")
_PRE_RANK = {"a": 0, "b": 1, "rc": 2, None: 3}
_REQUIREMENT_NAME = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")
_HEADING = re.compile(r"^## (.+?)\s*$", re.MULTILINE)
_LIB_VERSION = re.compile(r'^__version__ = "([^"]+)"$', re.MULTILINE)


class ReleaseError(Exception):
    """Inconsistent release state."""


def parse_version(value: str) -> tuple[int, int, int]:
    """Parse a strict X.Y.Z version."""
    match = _SEMVER.match(value.strip())
    if not match:
        raise ReleaseError(f"invalid version {value!r}, expected X.Y.Z")
    return int(match[1]), int(match[2]), int(match[3])


def format_version(version: tuple[int, int, int]) -> str:
    """Format a version tuple."""
    return ".".join(str(part) for part in version)


def tag_to_version(tag: str) -> str:
    """Return the version of an integration vX.Y.Z tag."""
    match = _TAG.match(tag.strip())
    if not match:
        raise ReleaseError(f"invalid tag {tag!r}, expected vX.Y.Z")
    return match[1]


def lib_tag_to_version(tag: str) -> str:
    """Return the version of a library tag (vX.Y.Z, optionally aN, bN or rcN)."""
    match = _LIB_TAG.match(tag.strip())
    if not match:
        raise ReleaseError(f"invalid library tag {tag!r}, expected vX.Y.Z[aN|bN|rcN]")
    return match[1]


def parse_lib_version(value: str) -> tuple[int, int, int, int, int]:
    """Sortable key of a library version (pre-releases sort before the release)."""
    match = _LIB_SEMVER.match(value.strip())
    if not match:
        raise ReleaseError(f"invalid library version {value!r}")
    pre = int(match[5]) if match[5] else 0
    return int(match[1]), int(match[2]), int(match[3]), _PRE_RANK[match[4]], pre


def is_prerelease(version: str) -> bool:
    """True for aN, bN and rcN library versions."""
    return parse_lib_version(version)[3] != _PRE_RANK[None]


def bump_level(old: str, new: str) -> str | None:
    """Highest X.Y.Z component that changed between two library versions."""
    old_v, new_v = parse_lib_version(old), parse_lib_version(new)
    if new_v < old_v:
        raise ReleaseError(f"version goes backwards: {old} -> {new}")
    for level, old_part, new_part in zip(LEVELS, old_v[:3], new_v[:3], strict=True):
        if old_part != new_part:
            return level
    return None


def bump(version: str, level: str) -> str:
    """Bump a version at the given level."""
    major, minor, patch = parse_version(version)
    if level == "major":
        return format_version((major + 1, 0, 0))
    if level == "minor":
        return format_version((major, minor + 1, 0))
    if level == "patch":
        return format_version((major, minor, patch + 1))
    raise ReleaseError(f"unknown level {level!r}")


def read_manifest() -> dict:
    """Read manifest.json."""
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def write_manifest(manifest: dict) -> None:
    """Write manifest.json."""
    MANIFEST.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")


def parse_vendored_version(text: str) -> dict[str, str]:
    """Parse the `key = value` lines of VENDORED_VERSION."""
    info: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition("=")
        if not sep:
            raise ReleaseError(f"invalid VENDORED_VERSION line {line!r}")
        info[key.strip()] = value.strip()
    return info


def format_vendored_version(version: str, sha: str) -> str:
    """Render VENDORED_VERSION."""
    return f"version = {version}\nsha = {sha}\n"


def requirement_name(requirement: str) -> str:
    """Return the normalized distribution name of a requirement."""
    match = _REQUIREMENT_NAME.match(requirement)
    if not match:
        raise ReleaseError(f"invalid requirement {requirement!r}")
    return re.sub(r"[-_.]+", "-", match[1]).lower()


def manifest_requirements(dependencies: Iterable[str]) -> list[str]:
    """Library dependencies that Home Assistant core does not provide."""
    return sorted(
        (dep for dep in dependencies if requirement_name(dep) not in HA_PROVIDED),
        key=requirement_name,
    )


def changelog_versions(text: str) -> list[str]:
    """Version headings of the CHANGELOG, top to bottom, without Unreleased."""
    return [h for h in _HEADING.findall(text) if h != UNRELEASED]


def changelog_section(text: str, heading: str) -> str:
    """Body of a `## heading` CHANGELOG section."""
    match = re.search(rf"^## {re.escape(heading)}\s*$", text, re.MULTILINE)
    if not match:
        raise ReleaseError(f"CHANGELOG has no '## {heading}' section")
    following = _HEADING.search(text, match.end())
    end = following.start() if following else len(text)
    return text[match.end() : end].strip()


def release_changelog(text: str, version: str, entry: str) -> str:
    """Turn `## Unreleased` into `## version` (or add it) and append an entry."""
    unreleased = re.search(rf"^## {UNRELEASED}\s*$", text, re.MULTILINE)
    if unreleased:
        following = _HEADING.search(text, unreleased.end())
        end = following.start() if following else len(text)
        body = text[unreleased.end() : end].strip()
        before, after = text[: unreleased.start()], text[end:]
    else:
        first = _HEADING.search(text)
        cut = first.start() if first else len(text)
        body = ""
        before, after = text[:cut], text[cut:]
    body = f"{body}\n{entry}".strip()
    section = f"## {version}\n\n{body}\n"
    return f"{before.rstrip()}\n\n{section}\n{after.lstrip()}".rstrip() + "\n"


def package_version(package: Path) -> str:
    """`__version__` declared by an aiphone_jo package directory."""
    text = (package / "__init__.py").read_text(encoding="utf-8")
    match = _LIB_VERSION.search(text)
    if not match:
        raise ReleaseError(f"{package} has no __version__")
    return match[1]


def vendored_lib_version() -> str:
    """`__version__` declared by the vendored package."""
    return package_version(VENDORED_PACKAGE)


def pkg_info_dependencies(pkg_info: str) -> list[str]:
    """Runtime `Requires-Dist` entries of core metadata (extras excluded)."""
    headers = pkg_info.split("\n\n", 1)[0]
    deps = []
    for line in headers.splitlines():
        key, _, value = line.partition(":")
        if key.strip().lower() == "requires-dist" and "extra ==" not in value:
            deps.append(value.strip())
    return deps


def extract_sdist(sdist: Path, dest: Path) -> tuple[Path, list[str]]:
    """Extract a library sdist; return its package directory and dependencies."""
    with tarfile.open(sdist) as archive:
        archive.extractall(dest, filter="data")
    roots = [path for path in dest.iterdir() if path.is_dir()]
    if len(roots) != 1:
        raise ReleaseError(f"unexpected sdist layout in {sdist.name}")
    root = roots[0]
    for package in (root / "src" / "aiphone_jo", root / "aiphone_jo"):
        if (package / "__init__.py").is_file():
            break
    else:
        raise ReleaseError(f"no aiphone_jo package in {sdist.name}")
    pkg_info = (root / "PKG-INFO").read_text(encoding="utf-8")
    return package, pkg_info_dependencies(pkg_info)


def git(*args: str, cwd: Path = ROOT) -> str:
    """Run git and return its stripped output."""
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def release_tags() -> list[str]:
    """vX.Y.Z tags of this repository, newest first."""
    tags = [t for t in git("tag", "--list", "v*").split() if _TAG.match(t)]
    return sorted(tags, key=lambda t: parse_version(tag_to_version(t)), reverse=True)


def released_lib_version(tag: str | None) -> str:
    """Library version shipped by an integration release (0.0.0 if none)."""
    if tag is None:
        return "0.0.0"
    try:
        text = git("show", f"{tag}:{VENDORED_VERSION_PATH}")
    except subprocess.CalledProcessError:
        return "0.0.0"
    return parse_vendored_version(text).get("version", "0.0.0")


def set_output(**values: str) -> None:
    """Write step outputs."""
    lines = [f"{key}={value}" for key, value in values.items()]
    print("\n".join(lines))
    if output := os.environ.get("GITHUB_OUTPUT"):
        with open(output, "a", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")


def _same_tree(left: Path, right: Path) -> bool:
    def files(base: Path) -> dict[str, bytes]:
        return {
            str(path.relative_to(base)): path.read_bytes()
            for path in sorted(base.rglob("*"))
            if path.is_file() and "__pycache__" not in path.parts
        }

    return left.is_dir() and right.is_dir() and files(left) == files(right)


def cmd_sync_lib(args: argparse.Namespace) -> None:
    """Vendor a library release sdist and bump the integration version."""
    lib_version = lib_tag_to_version(args.tag)
    if not re.fullmatch(r"[0-9a-f]{40}", args.sha):
        raise ReleaseError(f"invalid commit SHA {args.sha!r}")
    if is_prerelease(lib_version) and not args.allow_prerelease:
        print(f"aiphone-jo {lib_version} is a pre-release, not syncing it.")
        set_output(changed="false")
        return

    with tempfile.TemporaryDirectory() as tmp:
        source, dependencies = extract_sdist(Path(args.sdist), Path(tmp))
        if package_version(source) != lib_version:
            raise ReleaseError(
                f"tag {args.tag} does not match library version "
                f"{package_version(source)}"
            )

        baseline = released_lib_version(next(iter(release_tags()), None))
        level = bump_level(baseline, lib_version)
        if level is None and not _same_tree(source, VENDORED_PACKAGE):
            level = "patch"
        if level is None:
            print(f"aiphone-jo {lib_version} is already released, nothing to do.")
            set_output(changed="false")
            return

        shutil.rmtree(VENDORED_PACKAGE, ignore_errors=True)
        shutil.copytree(
            source, VENDORED_PACKAGE, ignore=shutil.ignore_patterns("__pycache__")
        )

    VENDORED_VERSION.write_text(
        format_vendored_version(lib_version, args.sha),
        encoding="utf-8",
    )
    manifest = read_manifest()
    version = bump(manifest["version"], level)
    manifest["requirements"] = manifest_requirements(dependencies)
    manifest["version"] = version
    write_manifest(manifest)

    entry = f"- Update the aiphone-jo library to {lib_version}."
    CHANGELOG.write_text(
        release_changelog(CHANGELOG.read_text(encoding="utf-8"), version, entry),
        encoding="utf-8",
    )
    set_output(changed="true", version=version, level=level, lib_version=lib_version)


def check() -> list[str]:
    """Return every release inconsistency found."""
    errors: list[str] = []
    version = read_manifest()["version"]
    try:
        parse_version(version)
    except ReleaseError as err:
        return [f"manifest: {err}"]

    versions = changelog_versions(CHANGELOG.read_text(encoding="utf-8"))
    if not versions or versions[0] != version:
        errors.append(
            f"CHANGELOG: latest version section is "
            f"{versions[0] if versions else 'missing'}, manifest says {version}"
        )

    tags = release_tags()
    if tags and parse_version(tag_to_version(tags[0])) > parse_version(version):
        errors.append(f"manifest version {version} is older than tag {tags[0]}")

    info = parse_vendored_version(VENDORED_VERSION.read_text(encoding="utf-8"))
    if info.get("version") != vendored_lib_version():
        errors.append(
            f"VENDORED_VERSION says {info.get('version')}, vendored package "
            f"declares {vendored_lib_version()}"
        )
    return errors


def cmd_check(_: argparse.Namespace) -> None:
    """Fail if the release state is inconsistent."""
    if errors := check():
        raise ReleaseError("\n".join(errors))
    print("Release metadata is consistent.")


def cmd_version(_: argparse.Namespace) -> None:
    """Print the manifest version."""
    print(read_manifest()["version"])


def cmd_notes(args: argparse.Namespace) -> None:
    """Print release notes for a version."""
    print(changelog_section(CHANGELOG.read_text(encoding="utf-8"), args.version))


def main(argv: list[str] | None = None) -> int:
    """Entry point."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sync = sub.add_parser("sync-lib", help=cmd_sync_lib.__doc__)
    sync.add_argument("--sdist", required=True, help="verified library sdist")
    sync.add_argument("--tag", required=True, help="library tag, e.g. v0.2.0")
    sync.add_argument("--sha", required=True, help="commit SHA of the tag")
    sync.add_argument(
        "--allow-prerelease",
        action="store_true",
        help="also sync aN, bN and rcN versions",
    )
    sync.set_defaults(func=cmd_sync_lib)
    sub.add_parser("check", help=cmd_check.__doc__).set_defaults(func=cmd_check)
    sub.add_parser("version", help=cmd_version.__doc__).set_defaults(func=cmd_version)
    notes = sub.add_parser("notes", help=cmd_notes.__doc__)
    notes.add_argument("version")
    notes.set_defaults(func=cmd_notes)

    args = parser.parse_args(argv)
    try:
        args.func(args)
    except ReleaseError as err:
        print(f"error: {err}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

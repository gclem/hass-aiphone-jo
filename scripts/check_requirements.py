"""Check that the manifest requirements resolve with Home Assistant's constraints.

Usage: python scripts/check_requirements.py [REF ...]

REF is a home-assistant/core branch or tag; "latest" means the latest release
(default: "latest" and "dev"). Requires uv.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = ROOT / "custom_components" / "aiphone_jo" / "manifest.json"
RAW = "https://raw.githubusercontent.com/home-assistant/core/{ref}/{path}"
LATEST = "https://api.github.com/repos/home-assistant/core/releases/latest"
CONSTRAINTS = "homeassistant/package_constraints.txt"


def fetch(url: str) -> str:
    """GET a URL as text."""
    request = urllib.request.Request(url)
    if token := os.environ.get("GITHUB_TOKEN"):
        request.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read().decode()


def resolve_ref(ref: str) -> str:
    """Branch or tag name; "latest" is the latest release tag."""
    if ref == "latest":
        return json.loads(fetch(LATEST))["tag_name"]
    return ref


def python_version(pyproject: str) -> str:
    """Minimum Python version (X.Y) required by Home Assistant."""
    match = re.search(r'requires-python\s*=\s*">=\s*(\d+\.\d+)', pyproject)
    if not match:
        raise SystemExit("requires-python not found in Home Assistant's pyproject.toml")
    return match[1]


def check(ref: str, requirements: list[str]) -> bool:
    """True when the requirements resolve with the constraints of ``ref``."""
    tag = resolve_ref(ref)
    constraints = fetch(RAW.format(ref=tag, path=CONSTRAINTS))
    version = python_version(fetch(RAW.format(ref=tag, path="pyproject.toml")))
    with tempfile.TemporaryDirectory() as tmp:
        reqs = Path(tmp, "requirements.txt")
        cons = Path(tmp, "constraints.txt")
        reqs.write_text("\n".join(requirements) + "\n")
        cons.write_text(constraints)
        result = subprocess.run(
            [
                "uv",
                "pip",
                "compile",
                str(reqs),
                "--constraint",
                str(cons),
                "--python-version",
                version,
                "--quiet",
                "--no-header",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
    if result.returncode != 0:
        print(f"Home Assistant {tag} (Python {version}): requirements conflict")
        print(result.stderr)
        return False
    print(f"Home Assistant {tag} (Python {version}): requirements OK")
    return True


def main(argv: list[str]) -> int:
    """Entry point."""
    refs = argv or ["latest", "dev"]
    requirements = json.loads(MANIFEST.read_text())["requirements"]
    if not requirements:
        print("No requirements.")
        return 0
    results = [check(ref, requirements) for ref in refs]
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

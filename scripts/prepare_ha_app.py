"""Stage the bridge sources into the Home Assistant app candidate and check the versions.

The Supervisor builds a local app from its own directory only, so the bridge
package metadata, lockfile, TypeScript config and sources are copied into
ha_app/. CI runs this script and fails when the committed copy drifts.
"""

from __future__ import annotations

import json
from pathlib import Path
import re
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT / "bridge"
APP = ROOT / "ha_app"
STAGED_FILES = ("package.json", "package-lock.json", "tsconfig.json")


def stage() -> None:
    for name in STAGED_FILES:
        shutil.copy2(BRIDGE / name, APP / name)
    if (APP / "src").exists():
        shutil.rmtree(APP / "src")
    shutil.copytree(BRIDGE / "src", APP / "src")


def check_versions() -> list[str]:
    problems: list[str] = []
    bridge_version = json.loads((BRIDGE / "package.json").read_text())["version"]
    config = (APP / "config.yaml").read_text()
    match = re.search(r"^version:\s*\"?([0-9]+\.[0-9]+\.[0-9]+)\"?\s*$", config, re.MULTILINE)
    if match is None or match.group(1) != bridge_version:
        problems.append(
            f"ha_app/config.yaml version {match.group(1) if match else 'missing'} "
            f"differs from bridge {bridge_version}"
        )
    source_version = re.search(
        r"BRIDGE_VERSION = '([0-9]+\.[0-9]+\.[0-9]+)'", (BRIDGE / "src" / "version.ts").read_text()
    )
    if source_version is None or source_version.group(1) != bridge_version:
        problems.append("bridge/src/version.ts BRIDGE_VERSION differs from bridge/package.json")
    bases = {
        path.name: re.findall(r"^FROM (\S+)", path.read_text(), re.MULTILINE)
        for path in (BRIDGE / "Dockerfile", APP / "Dockerfile")
    }
    if len({tuple(images) for images in bases.values()}) != 1 or any(
        "@sha256:" not in image for images in bases.values() for image in images
    ):
        problems.append("both Dockerfiles must use the same digest-pinned base images")
    return problems


def main() -> int:
    stage()
    problems = check_versions()
    for problem in problems:
        print(f"prepare_ha_app: {problem}", file=sys.stderr)
    print(APP)
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())

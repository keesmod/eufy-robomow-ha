"""The changelogs follow the versions: every version bump adds an entry on top.

CHANGELOG.md covers the integration, bridge/CHANGELOG.md the bridge and its
app, which share one version. Each entry has a `## x.y.z - YYYY-MM-DD`
heading and states its upgrade, rollback and evidence. Parallel work on
issue #8 bumps versions often, so these tests catch a missing entry in CI.
"""

from __future__ import annotations

from datetime import date
import json
from pathlib import Path
import re

import pytest

ROOT = Path(__file__).resolve().parents[1]
CHANGELOGS = ("CHANGELOG.md", "bridge/CHANGELOG.md")
HEADING = re.compile(r"^## (?P<title>.+)$", re.MULTILINE)
VERSION = re.compile(r"^(?P<version>\d+\.\d+\.\d+) - (?P<date>\d{4}-\d{2}-\d{2})$")
# The integration changelog ends with the upstream releases from before the fork.
OTHER_SECTIONS = {"CHANGELOG.md": {"Upstream releases"}, "bridge/CHANGELOG.md": set()}
REQUIRED = ("Upgrade:", "Rollback:", "Evidence:")


def _entries(name: str) -> list[tuple[str, date, str]]:
    """Each version entry as (version, date, body), in file order."""
    text = (ROOT / name).read_text()
    headings = list(HEADING.finditer(text))
    entries: list[tuple[str, date, str]] = []
    for index, heading in enumerate(headings):
        title = heading.group("title").strip()
        end = headings[index + 1].start() if index + 1 < len(headings) else len(text)
        match = VERSION.match(title)
        if match is None:
            assert title in OTHER_SECTIONS[name], f"{name}: '## {title}' is no `## x.y.z - YYYY-MM-DD` heading"
            continue
        entries.append((match["version"], date.fromisoformat(match["date"]), text[heading.end() : end]))
    assert entries, f"{name} has no version entry"
    return entries


def _key(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


def test_the_top_entries_are_the_current_versions() -> None:
    manifest = json.loads((ROOT / "custom_components/eufy_robomow/manifest.json").read_text())["version"]
    bridge = json.loads((ROOT / "bridge/package.json").read_text())["version"]
    app = re.search(r"^version:\s*\"?([0-9.]+)\"?\s*$", (ROOT / "ha_app/config.yaml").read_text(), re.MULTILINE)
    assert app is not None
    assert _entries("CHANGELOG.md")[0][0] == manifest, (
        f"add a CHANGELOG.md entry for integration {manifest} on top"
    )
    assert _entries("bridge/CHANGELOG.md")[0][0] == bridge == app.group(1), (
        f"add a bridge/CHANGELOG.md entry for bridge and app {bridge} on top"
    )


@pytest.mark.parametrize("name", CHANGELOGS)
def test_entries_run_newest_first_with_one_entry_per_version(name: str) -> None:
    entries = _entries(name)
    versions = [_key(version) for version, _, _ in entries]
    assert len(set(versions)) == len(versions), f"{name} lists a version twice"
    assert versions == sorted(versions, reverse=True), f"{name} is not newest first"
    dates = [released for _, released, _ in entries]
    assert dates == sorted(dates, reverse=True), f"{name} has a date out of order"


@pytest.mark.parametrize("name", CHANGELOGS)
def test_every_entry_states_its_upgrade_rollback_and_evidence(name: str) -> None:
    for version, _, body in _entries(name):
        missing = [word for word in REQUIRED if word not in body]
        assert not missing, f"{name} {version} lacks {', '.join(missing)}"

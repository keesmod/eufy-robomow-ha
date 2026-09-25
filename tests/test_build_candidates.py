"""Tests for the reproducible release candidates of scripts/build_candidates.py."""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tarfile

import pytest

ROOT = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location("build_candidates", ROOT / "scripts" / "build_candidates.py")
assert _SPEC and _SPEC.loader
candidates = importlib.util.module_from_spec(_SPEC)
# Dataclasses resolve their module through sys.modules while the script loads.
sys.modules[_SPEC.name] = candidates
_SPEC.loader.exec_module(candidates)

COMPONENTS = {
    "integration": "custom_components/eufy_robomow",
    "app": "ha_app",
    "bridge": "bridge",
}


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, check=True, text=True).stdout


def _files(directory: Path) -> dict[str, bytes]:
    return {path.name: path.read_bytes() for path in sorted(directory.iterdir())}


def test_one_commit_builds_byte_identical_candidates(tmp_path: Path) -> None:
    first = candidates.build("HEAD", tmp_path / "first")
    second = candidates.build("HEAD", tmp_path / "second")
    assert first == second
    assert _files(tmp_path / "first") == _files(tmp_path / "second")


def test_the_archives_hold_exactly_the_component_files_of_the_commit(tmp_path: Path) -> None:
    record = candidates.build("HEAD", tmp_path)
    commit = _git("rev-parse", "HEAD").strip()
    for component, prefix in COMPONENTS.items():
        entry = record[component]
        changed_in, seconds = _git("log", "-1", "--format=%H %ct", commit, "--", prefix).split()
        assert entry["changed_in"] == changed_in
        expected = sorted(_git("ls-tree", "-r", "--name-only", "--full-tree", commit, "--", prefix).split("\n")[:-1])
        with tarfile.open(tmp_path / entry["archive"]) as archive:
            members = archive.getmembers()
            assert [member.name for member in members] == sorted(member.name for member in members)
            files = [member for member in members if member.isfile()]
            assert [member.name for member in files] == [
                f"{entry['root']}/{path[len(prefix) + 1:]}" for path in expected
            ]
            assert entry["files"] == len(files)
            for member in members:
                assert member.name == entry["root"] or member.name.startswith(f"{entry['root']}/")
                assert (member.uid, member.gid, member.uname, member.gname) == (0, 0, "", "")
                assert member.mtime == int(seconds)
                assert member.isdir() or member.isfile(), "no links or devices"
                assert member.mode in (0o644, 0o755)
            for member, path in zip(files, expected, strict=True):
                content = archive.extractfile(member)
                assert content is not None
                assert content.read() == subprocess.run(
                    ["git", "show", f"{commit}:{path}"], cwd=ROOT, capture_output=True, check=True
                ).stdout


def test_the_record_carries_the_versions_the_pins_and_every_checksum(tmp_path: Path) -> None:
    record = candidates.build("HEAD", tmp_path)
    manifest = json.loads((ROOT / "custom_components/eufy_robomow/manifest.json").read_text())
    package = json.loads((ROOT / "bridge/package.json").read_text())
    assert json.loads((tmp_path / "candidate.json").read_text()) == record
    assert record["published"] is False
    assert record["commit"] == _git("rev-parse", "HEAD").strip()
    assert record["integration"]["version"] == manifest["version"]
    assert record["integration"]["requirements"] == manifest["requirements"]
    assert record["bridge"]["version"] == package["version"] == record["app"]["version"]
    library = record["bridge"]["library"]
    assert library["resolved"] == package["dependencies"][candidates.LIBRARY]
    assert library["resolved"].endswith(f"-{library['version']}.tgz")
    assert library["integrity"].startswith("sha512-")
    assert all("@sha256:" in image for image in record["bridge"]["base_images"])

    sums = dict(reversed(line.split("  ")) for line in (tmp_path / "SHA256SUMS").read_text().splitlines())
    assert set(sums) == {path.name for path in tmp_path.iterdir()} - {"SHA256SUMS"}
    for name, digest in sums.items():
        assert hashlib.sha256((tmp_path / name).read_bytes()).hexdigest() == digest
    for component in COMPONENTS:
        assert sums[record[component]["archive"]] == record[component]["sha256"]


def test_a_files_list_verifies_an_extracted_folder_like_sha256sum(tmp_path: Path) -> None:
    record = candidates.build("HEAD", tmp_path / "candidate")
    for component in COMPONENTS:
        entry = record[component]
        target = tmp_path / component
        with tarfile.open(tmp_path / "candidate" / entry["archive"]) as archive:
            archive.extractall(target, filter="data")
        listing = (tmp_path / "candidate" / f"{entry['archive']}.files.sha256").read_text().splitlines()
        assert len(listing) == entry["files"]
        for line in listing:
            digest, path = line.split("  ", 1)
            assert hashlib.sha256((target / path).read_bytes()).hexdigest() == digest, path


def test_it_writes_only_to_an_empty_directory_outside_the_repository(tmp_path: Path) -> None:
    with pytest.raises(candidates.CandidateError, match="outside the repository"):
        candidates.build("HEAD", ROOT / "candidate-output")
    assert not (ROOT / "candidate-output").exists()
    (tmp_path / "used").mkdir()
    (tmp_path / "used" / "old.tar").write_bytes(b"")
    with pytest.raises(candidates.CandidateError, match="not an empty directory"):
        candidates.build("HEAD", tmp_path / "used")
    assert candidates.main(["--out", str(ROOT / "candidate-output")]) == 1


def test_it_refuses_a_commit_whose_app_copy_or_pins_disagree(monkeypatch: pytest.MonkeyPatch) -> None:
    commit = candidates.resolve_commit("HEAD")
    real_entries = candidates.entries
    real_show = candidates.show

    def drifted_entries(ref: str, prefix: str) -> list:
        found = real_entries(ref, prefix)
        if prefix == "ha_app":
            return [
                candidates.Entry(entry.path, entry.mode, "0" * 40) if entry.path == "src/bridge.ts" else entry
                for entry in found
            ]
        return found

    monkeypatch.setattr(candidates, "entries", drifted_entries)
    with pytest.raises(candidates.CandidateError, match="ha_app/src/bridge.ts differs"):
        candidates.describe(commit)
    monkeypatch.setattr(candidates, "entries", real_entries)

    def other_app_version(ref: str, path: str) -> bytes:
        data = real_show(ref, path)
        if path == "ha_app/config.yaml":
            return data.replace(b"\nversion: ", b"\nversion: 9.9.9\nold_version: ", 1)
        return data

    monkeypatch.setattr(candidates, "show", other_app_version)
    with pytest.raises(candidates.CandidateError, match="config.yaml version 9.9.9"):
        candidates.describe(commit)

    def unpinned_library(ref: str, path: str) -> bytes:
        data = real_show(ref, path)
        if path == "bridge/package-lock.json":
            lock = json.loads(data)
            lock["packages"][f"node_modules/{candidates.LIBRARY}"]["integrity"] = ""
            return json.dumps(lock).encode()
        return data

    monkeypatch.setattr(candidates, "show", unpinned_library)
    with pytest.raises(candidates.CandidateError, match="no sha512 integrity"):
        candidates.describe(commit)

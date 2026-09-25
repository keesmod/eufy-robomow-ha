"""Build reproducible release candidates of the integration and the mower bridge from one commit.

Nothing is published. The candidate is written to a directory outside the
repository and consists of three plain tar archives, a per-file checksum list
for each, a ``candidate.json`` that records the versions and pinned inputs, and
``SHA256SUMS``. The archives are built from Git objects, never from the working
tree, with sorted paths, owner 0 and normalized modes. Every modification time
is the time of the last commit that changed the component, so an unchanged
component gives a byte-identical archive from any later commit of a full clone.

- ``eufy_robomow-<version>.tar``: the integration folder, extracted into
  ``/config/custom_components``.
- ``eufy-mower-bridge-app-<version>.tar``: the Home Assistant app candidate,
  extracted into ``/addons`` as ``eufy_mower_bridge``.
- ``eufy-mower-bridge-<version>.src.tar``: the bridge sources, the build context
  of the standalone container image.

The build refuses a commit whose app copy, versions, base images or library pin
disagree, the same rules ``scripts/prepare_ha_app.py`` and CI enforce.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import io
import json
from pathlib import Path
import re
import subprocess
import sys
import tarfile

ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = "keesmod/eufy-robomow-ha"
LIBRARY = "@keesmod/eufy-mega-client"
FORMAT = 1
# Bridge files the app candidate carries as a staged copy, see scripts/prepare_ha_app.py.
STAGED_FILES = ("package.json", "package-lock.json", "tsconfig.json")


class CandidateError(Exception):
    """The commit cannot give a coherent candidate."""


@dataclass(frozen=True)
class Entry:
    """One file of a component at the commit."""

    path: str
    mode: str
    oid: str


def _git(*args: str) -> bytes:
    result = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, check=False)
    if result.returncode != 0:
        raise CandidateError(f"git {' '.join(args)} failed: {result.stderr.decode().strip()}")
    return result.stdout


def resolve_commit(ref: str) -> str:
    return _git("rev-parse", "--verify", f"{ref}^{{commit}}").decode().strip()


def commit_time(commit: str) -> int:
    return int(_git("show", "-s", "--format=%ct", commit).decode().strip())


def last_change(commit: str, prefix: str) -> tuple[str, int]:
    """The last commit at or before ``commit`` that changed ``prefix``, and its time."""
    sha, _, seconds = _git("log", "-1", "--format=%H %ct", commit, "--", prefix).decode().strip().partition(" ")
    if not sha:
        raise CandidateError(f"{prefix} has no history at {commit}")
    return sha, int(seconds)


def show(commit: str, path: str) -> bytes:
    return _git("show", f"{commit}:{path}")


def entries(commit: str, prefix: str) -> list[Entry]:
    """Every file below ``prefix`` at the commit, with the path relative to it."""
    raw = _git("ls-tree", "-r", "-z", "--full-tree", commit, "--", prefix)
    found: list[Entry] = []
    for record in raw.split(b"\0"):
        if not record:
            continue
        meta, path = record.decode().split("\t", 1)
        mode, kind, oid = meta.split(" ")
        if not path.startswith(f"{prefix}/"):
            raise CandidateError(f"{path} is outside {prefix}")
        if kind != "blob" or mode not in ("100644", "100755"):
            raise CandidateError(f"{path} is a {kind} with mode {mode}, a candidate holds regular files only")
        found.append(Entry(path=path[len(prefix) + 1 :], mode=mode, oid=oid))
    if not found:
        raise CandidateError(f"{prefix} has no files at {commit}")
    return sorted(found, key=lambda entry: entry.path)


def blob(oid: str) -> bytes:
    return _git("cat-file", "blob", oid)


def build_tar(root: str, files: list[Entry], mtime: int) -> tuple[bytes, list[tuple[str, str]]]:
    """A deterministic tar of ``files`` below ``root`` and each file's SHA-256."""
    directories = {root}
    for entry in files:
        parts = entry.path.split("/")[:-1]
        for depth in range(1, len(parts) + 1):
            directories.add("/".join([root, *parts[:depth]]))
    members: list[tuple[str, Entry | None]] = [(name, None) for name in directories]
    members += [(f"{root}/{entry.path}", entry) for entry in files]
    digests: list[tuple[str, str]] = []
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.PAX_FORMAT) as archive:
        for name, entry in sorted(members, key=lambda member: member[0]):
            info = tarfile.TarInfo(name)
            info.mtime = mtime
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            if entry is None:
                info.type = tarfile.DIRTYPE
                info.mode = 0o755
                archive.addfile(info)
                continue
            data = blob(entry.oid)
            info.size = len(data)
            info.mode = 0o755 if entry.mode == "100755" else 0o644
            archive.addfile(info, io.BytesIO(data))
            digests.append((hashlib.sha256(data).hexdigest(), name))
    return buffer.getvalue(), digests


def _version_in_yaml(text: str) -> str | None:
    match = re.search(r"^version:\s*\"?([0-9]+\.[0-9]+\.[0-9]+)\"?\s*$", text, re.MULTILINE)
    return match.group(1) if match else None


def _slug_in_yaml(text: str) -> str | None:
    match = re.search(r"^slug:\s*\"?([a-z0-9_]+)\"?\s*$", text, re.MULTILINE)
    return match.group(1) if match else None


def describe(commit: str) -> dict:
    """Versions and pinned inputs at the commit, refusing any disagreement."""
    manifest = json.loads(show(commit, "custom_components/eufy_robomow/manifest.json"))
    package = json.loads(show(commit, "bridge/package.json"))
    lock = json.loads(show(commit, "bridge/package-lock.json"))
    config = show(commit, "ha_app/config.yaml").decode()
    version_ts = show(commit, "bridge/src/version.ts").decode()
    card = show(commit, "custom_components/eufy_robomow/frontend/eufy-mower-card.js").decode()
    problems: list[str] = []

    bridge_version = package["version"]
    app_version = _version_in_yaml(config)
    if app_version != bridge_version:
        problems.append(f"ha_app/config.yaml version {app_version} differs from bridge {bridge_version}")
    source_version = re.search(r"BRIDGE_VERSION = '([0-9]+\.[0-9]+\.[0-9]+)'", version_ts)
    if source_version is None or source_version.group(1) != bridge_version:
        problems.append("bridge/src/version.ts BRIDGE_VERSION differs from bridge/package.json")

    bridge_files = {entry.path: entry.oid for entry in entries(commit, "bridge")}
    app_files = {entry.path: entry.oid for entry in entries(commit, "ha_app")}
    staged = [*STAGED_FILES, *sorted(path for path in bridge_files if path.startswith("src/"))]
    for path in staged:
        if bridge_files.get(path) != app_files.get(path):
            problems.append(f"ha_app/{path} differs from bridge/{path}")
    extra = sorted(path for path in app_files if path.startswith("src/") and path not in bridge_files)
    problems += [f"ha_app/{path} has no bridge source" for path in extra]

    bases = {
        path: re.findall(r"^FROM (\S+)", show(commit, path).decode(), re.MULTILINE)
        for path in ("bridge/Dockerfile", "ha_app/Dockerfile")
    }
    base_images = sorted({image for images in bases.values() for image in images})
    if len({tuple(images) for images in bases.values()}) != 1 or any("@sha256:" not in image for image in base_images):
        problems.append("both Dockerfiles must use the same digest-pinned base images")

    wanted = package.get("dependencies", {}).get(LIBRARY)
    locked = lock.get("packages", {}).get(f"node_modules/{LIBRARY}", {})
    if not wanted or locked.get("resolved") != wanted:
        problems.append(f"the lockfile does not resolve {LIBRARY} to the tarball bridge/package.json pins")
    if not str(locked.get("integrity", "")).startswith("sha512-"):
        problems.append(f"the lockfile has no sha512 integrity for {LIBRARY}")

    card_version = re.search(r"CARD_VERSION\s*=\s*[\"']([0-9.]+)[\"']", card)
    if problems:
        raise CandidateError("; ".join(problems))
    return {
        "integration": {
            "version": manifest["version"],
            "requirements": manifest.get("requirements", []),
            "card_version": card_version.group(1) if card_version else None,
        },
        "bridge": {
            "version": bridge_version,
            "node_engine": package.get("engines", {}).get("node"),
            "base_images": base_images,
            "library": {
                "package": LIBRARY,
                "version": locked.get("version"),
                "resolved": locked.get("resolved"),
                "integrity": locked.get("integrity"),
            },
        },
        "app": {"version": app_version, "slug": _slug_in_yaml(config)},
    }


def _inside(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def build(ref: str, out: Path) -> dict:
    """Write the candidate for ``ref`` into ``out`` and return its record."""
    out = out.resolve()
    if _inside(out, ROOT.resolve()):
        raise CandidateError("write candidates outside the repository, they must never be committed")
    if out.exists() and (not out.is_dir() or any(out.iterdir())):
        raise CandidateError(f"{out} is not an empty directory")
    commit = resolve_commit(ref)
    record = describe(commit)
    layout = (
        ("integration", "custom_components/eufy_robomow", "eufy_robomow", "eufy_robomow-{}.tar"),
        ("app", "ha_app", record["app"]["slug"], "eufy-mower-bridge-app-{}.tar"),
        ("bridge", "bridge", "eufy-mower-bridge", "eufy-mower-bridge-{}.src.tar"),
    )
    out.mkdir(parents=True, exist_ok=True)
    written: list[str] = []
    for component, prefix, root, pattern in layout:
        name = pattern.format(record[component]["version"])
        changed_in, mtime = last_change(commit, prefix)
        data, digests = build_tar(root, entries(commit, prefix), mtime)
        (out / name).write_bytes(data)
        listing = f"{name}.files.sha256"
        (out / listing).write_text("".join(f"{digest}  {path}\n" for digest, path in digests))
        record[component].update(
            archive=name,
            changed_in=changed_in,
            root=root,
            files=len(digests),
            sha256=hashlib.sha256(data).hexdigest(),
        )
        written += [name, listing]
    candidate = {
        "format": FORMAT,
        "repository": REPOSITORY,
        "commit": commit,
        "commit_time": datetime.fromtimestamp(commit_time(commit), UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "published": False,
        **record,
    }
    (out / "candidate.json").write_text(json.dumps(candidate, indent=2, sort_keys=True) + "\n")
    written.append("candidate.json")
    sums = "".join(f"{hashlib.sha256((out / name).read_bytes()).hexdigest()}  {name}\n" for name in sorted(written))
    (out / "SHA256SUMS").write_text(sums)
    return candidate


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--ref", default="HEAD", help="commit, tag or branch to build (default HEAD)")
    parser.add_argument("--out", required=True, type=Path, help="empty directory outside the repository")
    args = parser.parse_args(argv)
    try:
        candidate = build(args.ref, args.out)
    except CandidateError as exc:
        print(f"build_candidates: {exc}", file=sys.stderr)
        return 1
    print(
        f"{args.out.resolve()}: integration {candidate['integration']['version']}, "
        f"bridge and app {candidate['bridge']['version']} on {LIBRARY} "
        f"{candidate['bridge']['library']['version']}, commit {candidate['commit'][:12]}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

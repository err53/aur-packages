#!/usr/bin/env python3
"""Update the source-built Podkit package to a stable release."""

from __future__ import annotations

import argparse
import hashlib
import json
import io
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tarfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

REPOSITORY = "jvgomg/podkit"
TAG_RE = re.compile(r"podkit@(\d+)\.(\d+)\.(\d+)")
ROOT = Path(__file__).resolve().parents[1]
PACKAGE_DIR = ROOT / "packages" / "podkit"
PKGBUILD = PACKAGE_DIR / "PKGBUILD"
SRCINFO = PACKAGE_DIR / ".SRCINFO"


def request(url: str) -> bytes:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "podkit-aur-updater",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    token = os.environ.get("GITHUB_TOKEN")
    if token and urllib.parse.urlparse(url).hostname == "api.github.com":
        headers["Authorization"] = f"Bearer {token}"
    try:
        with urllib.request.urlopen(
            urllib.request.Request(url, headers=headers), timeout=60
        ) as response:
            return response.read()
    except urllib.error.HTTPError as error:
        raise RuntimeError(f"download failed ({error.code}): {url}") from error
    except urllib.error.URLError as error:
        raise RuntimeError(f"download failed: {url}: {error.reason}") from error


def releases() -> list[dict[str, object]]:
    found: list[dict[str, object]] = []
    for page in range(1, 101):
        url = (
            f"https://api.github.com/repos/{REPOSITORY}/releases"
            f"?per_page=100&page={page}"
        )
        batch = json.loads(request(url))
        if not isinstance(batch, list):
            raise RuntimeError("GitHub Releases API returned an unexpected response")
        found.extend(batch)
        if len(batch) < 100:
            return found
    raise RuntimeError("refusing to inspect more than 10,000 GitHub releases")


def select_release(version: str | None) -> tuple[str, dict[str, object]]:
    candidates: list[tuple[tuple[int, int, int], str, dict[str, object]]] = []
    for release in releases():
        if release.get("draft") or release.get("prerelease"):
            continue
        tag = release.get("tag_name")
        if not isinstance(tag, str):
            continue
        match = TAG_RE.fullmatch(tag)
        if not match:
            continue
        parsed = tuple(int(part) for part in match.groups())
        candidates.append((parsed, ".".join(match.groups()), release))

    if version is not None:
        if not re.fullmatch(r"\d+\.\d+\.\d+", version):
            raise RuntimeError(f"version is not semantic X.Y.Z: {version}")
        matches = [item for item in candidates if item[1] == version]
        if len(matches) != 1:
            raise RuntimeError(
                f"expected exactly one stable podkit@{version} release; found {len(matches)}"
            )
        return matches[0][1], matches[0][2]

    if not candidates:
        raise RuntimeError("no stable podkit@X.Y.Z release found")
    _, selected_version, selected_release = max(candidates, key=lambda item: item[0])
    return selected_version, selected_release


def replace_one(text: str, pattern: str, replacement: str, label: str) -> str:
    updated, count = re.subn(pattern, replacement, text, flags=re.MULTILINE)
    if count != 1:
        raise RuntimeError(f"expected exactly one {label} assignment; found {count}")
    return updated


def update_pkgbuild(version: str, checksum: str) -> str:
    original = PKGBUILD.read_text(encoding="utf-8")
    current = re.findall(r"^pkgver=(\d+\.\d+\.\d+)$", original, re.MULTILINE)
    if len(current) != 1:
        raise RuntimeError("expected exactly one pkgver assignment")
    updated = replace_one(original, r"^pkgver=\d+\.\d+\.\d+$", f"pkgver={version}", "pkgver")
    if current[0] != version:
        updated = replace_one(updated, r"^pkgrel=\d+$", "pkgrel=1", "pkgrel")
    return replace_one(updated, r"^sha256sums=\('[0-9a-f]{64}'\)$",
                       f"sha256sums=('{checksum}')", "sha256sums")


def generate_srcinfo(pkgbuild: str) -> str:
    makepkg = shutil.which("makepkg")
    if makepkg is None:
        raise RuntimeError("makepkg is required to generate packages/podkit/.SRCINFO")
    with tempfile.TemporaryDirectory(prefix="podkit-srcinfo-") as directory:
        Path(directory, "PKGBUILD").write_text(pkgbuild, encoding="utf-8")
        result = subprocess.run(
            [makepkg, "--printsrcinfo"],
            cwd=directory,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    return result.stdout


def atomic_write(path: Path, content: str) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    try:
        temporary.write_text(content, encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("version", nargs="?", help="stable release version (X.Y.Z)")
    args = parser.parse_args()

    version, _ = select_release(args.version)
    archive = request(
        f"https://github.com/{REPOSITORY}/archive/refs/tags/podkit%40{version}.tar.gz"
    )
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as source:
        prefix = f"podkit-podkit-{version}/"
        for required in ("LICENSE", "bun.lock", "packages/libgpod-node/binding.gyp",
                         "packages/podkit-cli/scripts/compile.sh"):
            matches = [member for member in source.getmembers() if member.name == prefix + required]
            if len(matches) != 1 or not matches[0].isfile():
                raise RuntimeError(f"source archive missing unique regular file: {required}")
        manifest = source.extractfile(prefix + "packages/podkit-cli/package.json")
        if manifest is None or json.load(manifest)["version"] != version:
            raise RuntimeError("source CLI version does not match release")
    pkgbuild = update_pkgbuild(version, hashlib.sha256(archive).hexdigest())
    srcinfo = generate_srcinfo(pkgbuild)
    atomic_write(PKGBUILD, pkgbuild)
    atomic_write(SRCINFO, srcinfo)
    print(f"Updated podkit to {version}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, subprocess.CalledProcessError, json.JSONDecodeError) as error:
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(1)

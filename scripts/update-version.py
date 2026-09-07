#!/usr/bin/env python3
"""Update podkit-bin to a verified stable Podkit release."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

REPOSITORY = "jvgomg/podkit"
TAG_RE = re.compile(r"podkit@(\d+)\.(\d+)\.(\d+)")
ASSETS = (
    "podkit-linux-x64.tar.gz",
    "podkit-linux-arm64.tar.gz",
    "SHA256SUMS.txt",
)
ROOT = Path(__file__).resolve().parents[1]
PKGBUILD = ROOT / "aur" / "PKGBUILD"
SRCINFO = ROOT / "aur" / ".SRCINFO"


def request(url: str) -> bytes:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "podkit-aur-updater",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    token = os.environ.get("GITHUB_TOKEN")
    if token:
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


def asset_urls(release: dict[str, object]) -> dict[str, str]:
    raw_assets = release.get("assets")
    if not isinstance(raw_assets, list):
        raise RuntimeError("selected release has no asset list")
    urls: dict[str, str] = {}
    for required in ASSETS:
        matches = [asset for asset in raw_assets if asset.get("name") == required]
        if len(matches) != 1:
            raise RuntimeError(
                f"expected exactly one {required} release asset; found {len(matches)}"
            )
        url = matches[0].get("browser_download_url")
        if not isinstance(url, str) or not url:
            raise RuntimeError(f"release asset {required} has no download URL")
        urls[required] = url
    return urls


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def published_hash(checksums: str, filename: str) -> str:
    pattern = re.compile(rf"^([0-9a-fA-F]{{64}})  {re.escape(filename)}$")
    matches = [match.group(1).lower() for line in checksums.splitlines() if (match := pattern.fullmatch(line))]
    if len(matches) != 1:
        raise RuntimeError(
            f"expected exactly one checksum line for {filename}; found {len(matches)}"
        )
    return matches[0]


def replace_one(text: str, pattern: str, replacement: str, label: str) -> str:
    updated, count = re.subn(pattern, replacement, text, flags=re.MULTILINE)
    if count != 1:
        raise RuntimeError(f"expected exactly one {label} assignment; found {count}")
    return updated


def update_pkgbuild(version: str, hashes: dict[str, str]) -> str:
    original = PKGBUILD.read_text(encoding="utf-8")
    updated = replace_one(original, r"^pkgver=\d+\.\d+\.\d+$", f"pkgver={version}", "pkgver")
    updated = replace_one(updated, r"^pkgrel=\d+$", "pkgrel=1", "pkgrel")
    updated = replace_one(
        updated,
        r"^sha256sums=\('[0-9a-f]{64}'\)$",
        f"sha256sums=('{hashes['LICENSE']}')",
        "sha256sums",
    )
    updated = replace_one(
        updated,
        r"^sha256sums_x86_64=\('[0-9a-f]{64}'\)$",
        f"sha256sums_x86_64=('{hashes['podkit-linux-x64.tar.gz']}')",
        "sha256sums_x86_64",
    )
    return replace_one(
        updated,
        r"^sha256sums_aarch64=\('[0-9a-f]{64}'\)$",
        f"sha256sums_aarch64=('{hashes['podkit-linux-arm64.tar.gz']}')",
        "sha256sums_aarch64",
    )


def generate_srcinfo(pkgbuild: str) -> str:
    makepkg = shutil.which("makepkg")
    if makepkg is None:
        raise RuntimeError("makepkg is required to generate aur/.SRCINFO")
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

    version, release = select_release(args.version)
    urls = asset_urls(release)
    downloads = {name: request(url) for name, url in urls.items()}
    license_url = (
        f"https://raw.githubusercontent.com/{REPOSITORY}/"
        f"podkit%40{urllib.parse.quote(version)}/LICENSE"
    )
    license_data = request(license_url)
    if not license_data:
        raise RuntimeError("tagged LICENSE is empty")

    try:
        checksum_text = downloads["SHA256SUMS.txt"].decode("utf-8")
    except UnicodeDecodeError as error:
        raise RuntimeError("SHA256SUMS.txt is not UTF-8") from error

    hashes = {"LICENSE": sha256(license_data)}
    for filename in ASSETS[:2]:
        computed = sha256(downloads[filename])
        expected = published_hash(checksum_text, filename)
        if computed != expected:
            raise RuntimeError(
                f"checksum mismatch for {filename}: computed {computed}, published {expected}"
            )
        hashes[filename] = computed

    pkgbuild = update_pkgbuild(version, hashes)
    srcinfo = generate_srcinfo(pkgbuild)
    atomic_write(PKGBUILD, pkgbuild)
    atomic_write(SRCINFO, srcinfo)
    print(f"Updated podkit-bin to {version}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, subprocess.CalledProcessError, json.JSONDecodeError) as error:
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(1)

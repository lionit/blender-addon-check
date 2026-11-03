"""Resolve and download a Blender build for Linux x86-64, headless use.

Usage:
    python scripts/fetch_blender.py 4.5          # prints the path to the blender binary
    python scripts/fetch_blender.py --resolve 5.3  # prints the download URL only

Release builds come from download.blender.org (newest patch in the series).
Daily builds come from builder.blender.org's JSON listing.
"""

from __future__ import annotations

import argparse
import re
import tarfile
from pathlib import Path

from common import CACHE_DIR, CONFIG_DIR, download, http_get, http_get_json, log, parse_version, read_json

RELEASE_INDEX = "https://download.blender.org/release/Blender{series}/"
DAILY_INDEX = "https://builder.blender.org/download/daily/?format=json&v=1"


def version_entry(key: str) -> dict:
    for entry in read_json(CONFIG_DIR / "versions.json")["versions"]:
        if entry["key"] == key:
            return entry
    raise SystemExit(f"unknown version key {key!r}; see config/versions.json")


def resolve_release(series: str) -> str:
    """Newest blender-<series>.<patch>-linux-x64.tar.xz in the release directory."""
    html = http_get(RELEASE_INDEX.format(series=series)).decode("utf-8", "replace")
    pattern = re.compile(rf"href=\"(blender-({re.escape(series)}\.\d+)-linux-x64\.tar\.xz)\"")
    candidates = {parse_version(v): name for name, v in pattern.findall(html)}
    if not candidates:
        raise SystemExit(f"no linux-x64 tar.xz found for series {series}")
    newest = candidates[max(candidates)]
    return RELEASE_INDEX.format(series=series) + newest


def resolve_daily(series: str) -> str:
    """Newest linux x86_64 tar.xz daily build whose version starts with the series."""
    builds = http_get_json(DAILY_INDEX)
    wanted = []
    for b in builds:
        if b.get("platform") != "linux":
            continue
        if b.get("architecture") not in ("x86_64", "amd64"):
            continue
        if b.get("file_extension") not in ("xz", "tar.xz"):
            continue
        if not str(b.get("url", "")).endswith(".tar.xz"):
            continue
        if not str(b.get("version", "")).startswith(series + "."):
            continue
        wanted.append(b)
    if not wanted:
        raise SystemExit(f"no daily linux build found for series {series}")
    # The listing has a 'file_mtime' epoch on each build; newest first.
    wanted.sort(key=lambda b: (b.get("file_mtime") or 0, b.get("version", "")), reverse=True)
    return wanted[0]["url"]


def resolve(key: str) -> str:
    entry = version_entry(key)
    if entry["channel"] == "release":
        return resolve_release(entry["series"])
    if entry["channel"] == "daily":
        return resolve_daily(entry["series"])
    raise SystemExit(f"unknown channel {entry['channel']!r}")


def ensure_blender(key: str) -> Path:
    """Download and extract the build for `key`; return the path to the `blender` binary."""
    url = resolve(key)
    archive = CACHE_DIR / "blender" / url.rsplit("/", 1)[-1]
    extract_dir = archive.with_suffix("").with_suffix("")  # strip .tar.xz
    binary = extract_dir / "blender"
    if binary.exists():
        return binary
    download(url, archive)
    log("extracting", archive.name)
    extract_dir.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, "r:xz") as tar:
        members = tar.getmembers()
        # Archives contain a single top-level directory; flatten it into extract_dir.
        top = members[0].name.split("/", 1)[0]
        for m in members:
            if m.name == top:
                continue
            m.name = m.name.split("/", 1)[1] if "/" in m.name else m.name
        tar.extractall(extract_dir, members=[m for m in members if m.name != top], filter="data")
    if not binary.exists():
        raise SystemExit(f"extracted {archive} but found no blender binary in {extract_dir}")
    binary.chmod(0o755)
    return binary


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("key", help="version key from config/versions.json, e.g. 4.5")
    ap.add_argument("--resolve", action="store_true", help="print the download URL and exit")
    args = ap.parse_args()
    if args.resolve:
        print(resolve(args.key))
        return
    print(ensure_blender(args.key))


if __name__ == "__main__":
    main()

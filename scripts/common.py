"""Shared helpers: paths, HTTP, JSON, version parsing. Standard library only."""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
DATA_DIR = ROOT / "data"
RESULTS_DIR = ROOT / "results"
SITE_DIR = ROOT / "site"
CACHE_DIR = Path(os.environ.get("BAC_CACHE_DIR", ROOT / ".cache"))

USER_AGENT = "blender-addon-check/0.1 (+https://github.com/lionit/blender-addon-check)"


def log(*parts: object) -> None:
    print("[bac]", *parts, file=sys.stderr, flush=True)


def read_json(path: Path):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, sort_keys=True)
        fh.write("\n")


def http_get(url: str, timeout: int = 60, headers: dict | None = None) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def http_get_json(url: str, timeout: int = 60):
    return json.loads(http_get(url, timeout=timeout).decode("utf-8"))


def download(url: str, dest: Path, timeout: int = 600, headers: dict | None = None) -> Path:
    """Download url to dest unless dest already exists. Returns dest."""
    if dest.exists() and dest.stat().st_size > 0:
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    log("downloading", url)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as resp, open(tmp, "wb") as out:
        while True:
            chunk = resp.read(1 << 20)
            if not chunk:
                break
            out.write(chunk)
    tmp.replace(dest)
    return dest


_VERSION_RE = re.compile(r"^(\d+)\.(\d+)(?:\.(\d+))?")


def parse_version(text: str) -> tuple[int, int, int]:
    """'4.5.3' -> (4, 5, 3); '5.3' -> (5, 3, 0). Raises ValueError on junk."""
    m = _VERSION_RE.match(text.strip())
    if not m:
        raise ValueError(f"not a version: {text!r}")
    return int(m.group(1)), int(m.group(2)), int(m.group(3) or 0)


def slugify(text: str) -> str:
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text.strip().lower())
    return text.strip("-") or "addon"

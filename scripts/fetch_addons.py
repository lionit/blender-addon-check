"""Load the add-on list and make sure each add-on's zip is in the cache.

Usage:
    python scripts/fetch_addons.py              # fetch every add-on
    python scripts/fetch_addons.py --only a,b   # a subset by id

Each add-on ends up at .cache/addons/<id>.zip in a form Blender can install:
either an extension (blender_manifest.toml at the top) or a legacy add-on whose
top-level folder is the module name.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import shutil
import zipfile
from pathlib import Path

from common import CACHE_DIR, CONFIG_DIR, ROOT, download, http_get, http_get_json, log, parse_version, read_json

ADDONS_CACHE = CACHE_DIR / "addons"


def load_addons(only: set[str] | None = None) -> list[dict]:
    addons: list[dict] = []
    for name in ("addons.json", "addons.generated.json"):
        path = CONFIG_DIR / name
        if not path.exists():
            continue
        for entry in read_json(path)["addons"]:
            if entry.get("enabled", True) is False:
                continue
            if only and entry["id"] not in only:
                continue
            addons.append(entry)
    # First definition of an id wins (curated list before generated list).
    seen: set[str] = set()
    unique = []
    for a in addons:
        if a["id"] in seen:
            continue
        seen.add(a["id"])
        unique.append(a)
    return unique


def github_headers() -> dict:
    token = os.environ.get("GITHUB_TOKEN")
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def github_json(url: str):
    return json.loads(http_get(url, headers=github_headers()).decode("utf-8"))


def github_release_asset_url(repo: str, pattern: str | None) -> str:
    rel = github_json(f"https://api.github.com/repos/{repo}/releases/latest")
    assets = rel.get("assets", [])
    rx = re.compile(pattern) if pattern else re.compile(r".*\.zip$")
    for a in assets:
        if rx.fullmatch(a["name"]) or rx.match(a["name"]):
            return a["browser_download_url"]
    for a in assets:
        if a["name"].endswith(".zip"):
            log(f"{repo}: no asset matched {pattern!r}, using {a['name']}")
            return a["browser_download_url"]
    log(f"{repo}: release has no zip asset, using source zipball")
    return rel["zipball_url"]


def repack(src: Path, dest: Path, module: str, subdir: str | None) -> None:
    """Rewrite a zip so its top-level folder is `module` (optionally lifting `subdir`)."""
    with zipfile.ZipFile(src) as zin:
        names = zin.namelist()
        tops = {n.split("/", 1)[0] for n in names if "/" in n}
        top = next(iter(tops)) if len(tops) == 1 else None
        prefix = (top + "/") if top else ""
        if subdir:
            prefix = prefix + subdir.strip("/") + "/"
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zout:
            for info in zin.infolist():
                if not info.filename.startswith(prefix) or info.filename == prefix:
                    continue
                rel = info.filename[len(prefix):]
                if not rel:
                    continue
                data = zin.read(info)
                zout.writestr(f"{module}/{rel}", data)
    dest.write_bytes(buf.getvalue())


def is_extension_zip(path: Path) -> bool:
    with zipfile.ZipFile(path) as z:
        for n in z.namelist():
            parts = n.split("/")
            if parts[-1] == "blender_manifest.toml" and len(parts) <= 2:
                return True
    return False


def inspect_zip(path: Path) -> dict:
    """Describe an add-on zip: kind (extension | legacy | legacy-single), module, declared versions."""
    import re
    import tomllib

    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        tops = sorted({n.split("/", 1)[0] for n in names})
        manifest_name = next((n for n in names if n.split("/")[-1] == "blender_manifest.toml" and n.count("/") <= 1), None)
        if manifest_name:
            manifest = tomllib.loads(z.read(manifest_name).decode("utf-8", "replace"))
            return {
                "kind": "extension",
                "module": manifest.get("id") or tops[0],
                "declared_min": manifest.get("blender_version_min"),
                "declared_max": manifest.get("blender_version_max"),
                "version": manifest.get("version"),
            }
        init_name = next((n for n in names if n.endswith("/__init__.py") and n.count("/") == 1), None)
        if init_name:
            src = z.read(init_name).decode("utf-8", "replace")
            m = re.search(r"[\"']blender[\"']\s*:\s*\(\s*(\d+)\s*,\s*(\d+)\s*(?:,\s*(\d+))?\s*\)", src)
            return {
                "kind": "legacy",
                "module": init_name.split("/", 1)[0],
                "declared_min": f"{m.group(1)}.{m.group(2)}.{m.group(3) or 0}" if m else None,
                "declared_max": None,
                "version": None,
            }
        single = [n for n in names if n.endswith(".py") and "/" not in n]
        if len(single) == 1:
            return {"kind": "legacy-single", "module": single[0][:-3], "declared_min": None, "declared_max": None, "version": None}
    raise ValueError(f"cannot tell what kind of add-on {path.name} is; top-level entries: {tops[:10]}")


def pick_build(builds: list[dict], blender_version: str | None) -> dict | None:
    """The build whose declared Blender range contains `blender_version` (highest minimum wins).
    Falls back to the newest build when nothing matches, so the test still reports what happens."""
    if not builds:
        return None
    if not blender_version:
        return builds[0]
    target = parse_version(blender_version)
    matching = []
    for b in builds:
        lo = parse_version(b.get("blender_version_min") or "0")
        hi = parse_version(b["blender_version_max"]) if b.get("blender_version_max") else None
        if lo <= target and (hi is None or target < hi):
            matching.append(b)
    if matching:
        return max(matching, key=lambda b: parse_version(b.get("blender_version_min") or "0"))
    return builds[0]


def fetch_one(addon: dict, blender_version: str | None = None) -> Path:
    ADDONS_CACHE.mkdir(parents=True, exist_ok=True)
    src = addon["source"]
    kind = src["type"]
    if kind == "extensions":
        build = pick_build(src.get("builds") or [], blender_version)
        url = (build or {}).get("archive_url") or src.get("archive_url")
        if not url:
            ext = http_get_json(f"https://extensions.blender.org/api/v1/extensions/{src['id']}/")
            data = ext.get("data", ext) if isinstance(ext, dict) else ext
            rows = data if isinstance(data, list) else [data]
            url = pick_build(rows, blender_version)["archive_url"]
        suffix = re.sub(r"[^a-z0-9]", "", url.rsplit("/", 1)[-1].lower())[-24:]
        dest = ADDONS_CACHE / f"{addon['id']}-{suffix}.zip"
        if not (dest.exists() and dest.stat().st_size > 0):
            download(url, dest)
        return dest
    dest = ADDONS_CACHE / f"{addon['id']}.zip"
    if dest.exists() and dest.stat().st_size > 0:
        return dest
    if kind == "local":
        local = ROOT / src["path"]
        if not local.exists():
            raise FileNotFoundError(f"{addon['id']}: local file {local} not found")
        shutil.copyfile(local, dest)
        return dest
    if kind == "private_github_file":
        token = os.environ.get("PRIVATE_ADDONS_TOKEN")
        if not token:
            raise RuntimeError(f"{addon['id']}: PRIVATE_ADDONS_TOKEN is not set")
        ref = src.get("ref", "main")
        url = f"https://api.github.com/repos/{src['repo']}/contents/{src['path'].strip('/')}?ref={ref}"
        data = http_get(url, headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github.raw"})
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        return dest
    raw = ADDONS_CACHE / f"{addon['id']}.raw.zip"
    if kind == "github_release":
        url = github_release_asset_url(src["repo"], src.get("asset_pattern"))
    elif kind == "github_zipball":
        ref = src.get("ref") or "HEAD"
        url = f"https://api.github.com/repos/{src['repo']}/zipball" + ("" if ref == "HEAD" else f"/{ref}")
    else:
        raise ValueError(f"{addon['id']}: unknown source type {kind!r}")
    download(url, raw)
    if is_extension_zip(raw) and not src.get("subdir"):
        shutil.copyfile(raw, dest)
    else:
        repack(raw, dest, src.get("module") or src["repo"].split("/")[-1], src.get("subdir"))
    return dest


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--only", help="comma-separated add-on ids")
    args = ap.parse_args()
    only = set(args.only.split(",")) if args.only else None
    failures = 0
    for addon in load_addons(only):
        try:
            path = fetch_one(addon)
            log(f"{addon['id']}: {path.name} ({path.stat().st_size // 1024} KB)")
        except Exception as exc:  # noqa: BLE001 - we want to continue past any single failure
            failures += 1
            log(f"{addon['id']}: FETCH FAILED: {exc}")
    if failures:
        log(f"{failures} add-on(s) could not be fetched")


if __name__ == "__main__":
    main()

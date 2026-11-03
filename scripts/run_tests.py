"""Run every add-on against one Blender version and write results/<version>/<addon>.json.

Usage:
    python scripts/run_tests.py --version 4.5 [--only a,b] [--jobs 4] [--timeout 240]

Each add-on runs in its own headless Blender process with a private user directory,
so installs never leak between runs. A run that produces no result file is recorded
as a crash; one that exceeds the timeout is recorded as a timeout.
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import re
import shutil
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from common import RESULTS_DIR, ROOT, log, read_json, write_json
from fetch_addons import fetch_one, inspect_zip, load_addons
from fetch_blender import ensure_blender, version_entry

HARNESS = ROOT / "scripts" / "harness.py"


def blender_version_string(binary: Path) -> str:
    out = subprocess.run([str(binary), "--version"], capture_output=True, text=True, timeout=120)
    first = (out.stdout or out.stderr).strip().splitlines()[0] if (out.stdout or out.stderr).strip() else "unknown"
    return first.replace("Blender ", "")


def run_one(binary: Path, addon: dict, out_dir: Path, timeout: int) -> dict:
    result_path = out_dir / f"{addon['id']}.json"
    log_path = out_dir / f"{addon['id']}.log"
    base = {
        "addon_id": addon["id"],
        "name": addon.get("name", addon["id"]),
        "group": addon.get("group", ""),
        "developer": addon.get("developer", ""),
        "homepage": addon.get("homepage", ""),
        "source_type": addon["source"]["type"],
        "ran_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
    }
    try:
        zip_path = fetch_one(addon)
    except Exception as exc:  # noqa: BLE001
        payload = {**base, "status": "unavailable", "error": f"could not fetch add-on: {exc}", "error_stage": "fetch"}
        write_json(result_path, payload)
        return payload

    try:
        info = inspect_zip(zip_path)
    except Exception as exc:  # noqa: BLE001
        payload = {**base, "status": "fail", "error": str(exc), "error_stage": "inspect"}
        write_json(result_path, payload)
        return payload
    base.update({"kind": info["kind"], "declared_min": info["declared_min"], "declared_max": info["declared_max"], "addon_version": info["version"]})

    tmp_user = Path(tempfile.mkdtemp(prefix=f"bac-{addon['id']}-"))
    tmp_result = tmp_user / "result.json"
    user_resources = tmp_user / "blender"
    user_resources.mkdir(parents=True, exist_ok=True)
    env = {
        **os.environ,
        "HOME": str(tmp_user),
        "XDG_CONFIG_HOME": str(tmp_user / "config"),
        "XDG_CACHE_HOME": str(tmp_user / "cache"),
        "BLENDER_USER_RESOURCES": str(user_resources),
        "PYTHONUNBUFFERED": "1",
    }
    output = ""
    status_override = None
    install_failed = ""

    # Extensions are installed by Blender's own command line, which is built for headless use.
    if info["kind"] == "extension":
        cmd_install = [str(binary), "-b", "--command", "extension", "install-file", "--repo", "user_default", str(zip_path)]
        try:
            proc = subprocess.run(cmd_install, capture_output=True, text=True, timeout=timeout, env=env, cwd=str(tmp_user))
            output += "--- extension install-file ---\n" + (proc.stdout or "") + (proc.stderr or "")
            # Blender's extension command prints "ERROR ..." but still exits 0, so check the text too.
            if proc.returncode != 0 or re.search(r"^ERROR\b", (proc.stdout or "") + (proc.stderr or ""), re.M):
                install_failed = f"`blender --command extension install-file` exited with {proc.returncode}\n" + output[-4000:]
        except subprocess.TimeoutExpired:
            install_failed = "extension install-file timed out"
            status_override = "timeout"

    if install_failed:
        payload = {**base, "status": status_override or "fail", "error": install_failed, "error_stage": "install", "module": f"bl_ext.user_default.{info['module']}"}
    else:
        cmd = [
            str(binary), "-b", "--factory-startup", "-noaudio",
            "--python", str(HARNESS), "--",
            "--zip", str(zip_path), "--out", str(tmp_result), "--addon-id", addon["id"],
            "--kind", info["kind"], "--module", info["module"],
        ]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env, cwd=str(tmp_user))
            output += "\n--- harness ---\n" + (proc.stdout or "") + "\n--- stderr ---\n" + (proc.stderr or "")
            if proc.returncode != 0 and not tmp_result.exists():
                status_override = "crash"
        except subprocess.TimeoutExpired as exc:
            partial = exc.stdout.decode("utf-8", "replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
            output += "\n--- harness ---\n" + partial + "\n--- timeout ---"
            status_override = "timeout"
        if tmp_result.exists() and status_override != "timeout":
            payload = {**base, **read_json(tmp_result)}
        else:
            payload = {**base, "status": status_override or "crash", "error": output[-6000:], "error_stage": "process"}

    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(output[-60000:], encoding="utf-8")
    payload["log"] = log_path.name
    write_json(result_path, payload)
    shutil.rmtree(tmp_user, ignore_errors=True)
    return payload


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", required=True, help="version key from config/versions.json")
    ap.add_argument("--only", help="comma-separated add-on ids")
    ap.add_argument("--jobs", type=int, default=min(4, os.cpu_count() or 1))
    ap.add_argument("--timeout", type=int, default=240, help="seconds per add-on")
    args = ap.parse_args()

    entry = version_entry(args.version)
    binary = ensure_blender(args.version)
    full_version = blender_version_string(binary)
    log(f"Blender {full_version} at {binary}")

    addons = load_addons(set(args.only.split(",")) if args.only else None)
    out_dir = RESULTS_DIR / args.version
    out_dir.mkdir(parents=True, exist_ok=True)
    log(f"testing {len(addons)} add-ons with {args.jobs} parallel jobs")

    counts: dict[str, int] = {}
    failures: list[dict] = []
    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        futures = {pool.submit(run_one, binary, a, out_dir, args.timeout): a for a in addons}
        for fut in as_completed(futures):
            a = futures[fut]
            try:
                payload = fut.result()
            except Exception as exc:  # noqa: BLE001
                payload = {"addon_id": a["id"], "status": "crash", "error": str(exc), "error_stage": "runner"}
                write_json(out_dir / f"{a['id']}.json", payload)
            status = payload.get("status", "unknown")
            counts[status] = counts.get(status, 0) + 1
            log(f"{a['id']:40s} {status:12s} {payload.get('error_stage', '')}")
            if status != "pass":
                failures.append(payload)

    # Print the tail of each failure so the job log alone tells the story.
    for p in sorted(failures, key=lambda p: p.get("addon_id", "")):
        print(f"\n===== {p.get('addon_id')} [{p.get('status')}] stage={p.get('error_stage', '')} kind={p.get('kind', '')} module={p.get('module', '')}")
        print("\n".join((p.get("error") or "").strip().splitlines()[-40:]))

    write_json(out_dir / "summary.json", {
        "version_key": args.version,
        "label": entry["label"],
        "channel": entry["channel"],
        "blender_version": full_version,
        "ran_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "addons": len(addons),
        "counts": counts,
    })
    log(f"done: {counts}")


if __name__ == "__main__":
    main()

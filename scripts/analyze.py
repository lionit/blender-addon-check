"""Name the cause of each failure in one sentence, and suggest a fix where one is known.

Usage:
    python scripts/analyze.py            # rewrites every results/<version>/<addon>.json with a 'cause' block
    python scripts/analyze.py --dry-run  # prints the causes without writing

Rules are regexes over the error text. The first rule that matches wins; add new
rules at the top of RULES when a new breakage pattern appears. Unmatched failures get
the generic cause so the report never shows a blank.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

from common import RESULTS_DIR, log, read_json, write_json

# (id, regex, cause, fix)
RULES: list[tuple[str, str, str, str]] = [
    (
        "platform-not-shipped",
        r"This platform \(linux-x64\) isn't one of",
        "The extension ships builds only for other platforms (usually Windows), so it cannot be installed on the Linux machine this test runs on.",
        "Nothing to fix for Windows users. To be tested here, publish a linux-x64 build or a pure-Python version.",
    ),
    (
        "sequence-strip-rename",
        r"cannot import name 'Sequence' from 'bpy\.types'|bpy\.types\.Sequence\b|'Sequences' object has no attribute|has no attribute 'sequences'|has no attribute 'sequence_editor'",
        "Uses the video-sequencer 'Sequence' API that Blender 4.4 renamed to 'Strip' (bpy.types.Strip, scene.sequence_editor.strips).",
        "Replace Sequence/sequences with Strip/strips, guarded by a bpy.app.version check if you still support 4.2.",
    ),
    (
        "bgl-removed",
        r"No module named 'bgl'|module 'bgl' has no attribute",
        "Uses the legacy bgl OpenGL wrapper, deprecated since Blender 3.4 and removed in 4.x/5.0.",
        "Port the drawing code to the gpu module (gpu.shader, gpu.state, gpu_extras.batch).",
    ),
    (
        "needs-display",
        r"GPU functions for drawing (are not available in background mode|requires the gpu module to be initialized)|gpu\.types\.GPUShader[^\n]*background|bgl[^\n]*not available in background",
        "Creates GPU shaders or draw handlers while registering, which cannot run in a headless Blender. It may well work with a display; this test cannot tell.",
        "Create shaders and draw handlers lazily (first draw or first operator call) and skip them when bpy.app.background is True; that also makes the add-on testable in CI.",
    ),
    (
        "unregistered-operator",
        r'Polling operator "bpy\.ops\.[a-z0-9_.]+" error, could not be found',
        "Defines an operator class that is never registered, so bpy.ops cannot reach it.",
        "Register the class or delete it; this is harmless to users but hides dead code.",
    ),
    (
        "compiled-python-mismatch",
        r"cpython-3(11|12)[^\n]*\.so|ImportError: [^\n]*(undefined symbol|wrong ELF|cannot open shared object)|ModuleNotFoundError: No module named '[^']*cpython|ask if someone can create a build for you|compiled for a different (Blender|Python) version",
        "Ships a compiled library built for an older Python. Blender 5.1 moved to Python 3.13, so binaries built for 3.11 no longer load.",
        "Rebuild the compiled parts (or the wheels in the manifest) for Python 3.13, and ship one build per Blender Python version.",
    ),
    (
        "stdlib-module-removed",
        r"ModuleNotFoundError: No module named '(imp|cgi|cgitb|distutils|telnetlib|asynchat|asyncore|smtpd|nntplib|crypt|pipes|sndhdr|imghdr|audioop|chunk|mailcap|msilib|nis|ossaudiodev|spwd|sunau|uu|xdrlib|lib2to3)'",
        "Imports a standard-library module that Python 3.12 or 3.13 removed.",
        "Replace the removed module: imp -> importlib, distutils -> setuptools or shutil, cgi -> urllib.parse / email, imghdr -> a small header check.",
    ),
    (
        "scene-node-tree-removed",
        r"'Scene' object has no attribute 'node_tree'|scene\.node_tree",
        "Uses scene.node_tree, which Blender 5.0 removed when compositing moved to scene.compositing_node_group.",
        "Read the compositor tree from scene.compositing_node_group on 5.0+, guarded by a bpy.app.version check.",
    ),
    (
        "legacy-action-api",
        r"'Action' object has no attribute '(fcurves|groups)'|action\.fcurves|has no attribute 'action_slot",
        "Uses the legacy Action API (action.fcurves / action.groups) that Blender 5.0 replaced with layered, slotted actions.",
        "Use action.layers[...].strips[...].channelbag(slot) to reach F-curves on 5.0+, with a version guard for older Blender.",
    ),
    (
        "grease-pencil-v3",
        r"grease_pencils(?!_v3)[^\n]*(has no attribute|KeyError)|'GreasePencil' object has no attribute|bpy\.types\.GPencil",
        "Uses the Grease Pencil v2 data model that Blender 5.0 replaced with Grease Pencil v3.",
        "Port to the GPv3 API (bpy.types.GreasePencilv3 / drawings / layers) and drop bpy.data.grease_pencils lookups for annotations.",
    ),
    (
        "eevee-engine-id",
        r"BLENDER_EEVEE_NEXT|enum \"BLENDER_EEVEE\" not found",
        "References a render engine identifier that was renamed (BLENDER_EEVEE_NEXT became BLENDER_EEVEE).",
        "Use 'BLENDER_EEVEE' and guard any 'BLENDER_EEVEE_NEXT' reference with a version check.",
    ),
    (
        "removed-bpy-attribute",
        r"AttributeError: '[A-Za-z_]+' object has no attribute '[A-Za-z_]+'|AttributeError: module 'bpy\.[a-z_]+' has no attribute",
        "Calls a bpy attribute that no longer exists in this Blender version.",
        "Check the Python API change log for this release and add a version guard around the renamed or removed attribute.",
    ),
    (
        "register-class-conflict",
        r"register_class\(\.\.\.\):[^\n]*(already registered|expected a subclass)|ValueError: register_class",
        "A class fails to register: it is already registered or no longer has a valid bpy.types base.",
        "Unregister cleanly in unregister(), avoid double registration, and check that bl_rna properties use the current annotation syntax.",
    ),
    (
        "keyword-argument-removed",
        r"TypeError: [^\n]*(got an unexpected keyword argument|takes [0-9]+ positional argument|missing [0-9]+ required)",
        "Calls a Blender function with arguments that this version no longer accepts.",
        "Compare the call against the API reference for this version and adapt the arguments, guarded by bpy.app.version.",
    ),
    (
        "manifest-invalid",
        r"blender_manifest\.toml|manifest[^\n]*(invalid|missing|error)|Extension[^\n]*(schema|manifest)",
        "The extension manifest is missing or invalid for this Blender version.",
        "Validate blender_manifest.toml with `blender --command extension validate` and update schema_version and blender_version_min.",
    ),
    (
        "declared-unsupported",
        r"(requires|supports) Blender [0-9.]+|incompatible with this Blender version|blender_version_min|doesn.t meet the minimum supported version|exceeds the maximum supported version|must be less than the maximum version",
        "The add-on declares it does not support this Blender version, and Blender refuses to enable it.",
        "Raise blender_version_min / bl_info['blender'] once the add-on is confirmed to work, or publish a version for this release.",
    ),
    (
        "syntax-error",
        r"SyntaxError|IndentationError",
        "The add-on's Python no longer parses on this Python version.",
        "Run the file through Python 3.13 and fix the reported line.",
    ),
    (
        "missing-dependency",
        r"ModuleNotFoundError: No module named '([A-Za-z0-9_]+)'",
        "Imports a Python package that is not bundled with Blender.",
        "Ship the package as a wheel in blender_manifest.toml, or vendor it inside the add-on folder.",
    ),
    (
        "process-crash",
        r"Segmentation fault|SIGSEGV|SIGABRT|EXCEPTION_ACCESS_VIOLATION|Error   : EXCEPTION",
        "Blender itself crashed while loading the add-on.",
        "Look for compiled code or GPU calls that run at import time; move them behind a check for bpy.app.background.",
    ),
]

COMPILED = [(rid, re.compile(rx, re.IGNORECASE | re.MULTILINE), cause, fix) for rid, rx, cause, fix in RULES]

# Causes that change what the result means: the add-on is not "broken", the test could not decide.
STATUS_OVERRIDE = {
    "needs-display": "inconclusive",
    "platform-not-shipped": "inconclusive",
    "declared-unsupported": "unsupported",
}

GENERIC = {
    "id": "unclassified",
    "cause": "Fails on this Blender version; the log does not match a known breakage pattern yet.",
    "fix": "Read the traceback in the log. If this turns out to be a common cause, add a rule to scripts/analyze.py.",
}


def classify(error_text: str, status: str) -> dict:
    if status in ("pass",):
        return {"id": "none", "cause": "", "fix": ""}
    if status == "timeout":
        return {"id": "timeout", "cause": "Blender did not finish loading the add-on within the time limit.", "fix": "Look for network calls, modal operators or heavy work at import time; defer it until the user acts."}
    if status == "unavailable":
        return {"id": "unavailable", "cause": "The add-on could not be downloaded, so it was not tested.", "fix": "Check the source URL in config/addons.json."}
    text = error_text or ""
    for rid, rx, cause, fix in COMPILED:
        if rx.search(text):
            return {"id": rid, "cause": cause, "fix": fix}
    return dict(GENERIC)


def analyze_file(path: Path, dry_run: bool) -> dict:
    payload = read_json(path)
    text = "\n".join([
        payload.get("error", "") or "",
        payload.get("captured_output", "") or "",
        *[s.get("detail", "") for s in payload.get("stages", {}).values() if not s.get("ok", True)],
    ])
    status = payload.get("status", "unknown")
    if status in ("inconclusive", "unsupported") and payload.get("status_before_analysis"):
        status = payload["status_before_analysis"]  # re-analysis starts from the raw outcome
    cause = classify(text, status)
    payload["cause"] = cause
    if status in ("fail", "crash") and cause["id"] in STATUS_OVERRIDE:
        payload["status_before_analysis"] = status
        payload["status"] = STATUS_OVERRIDE[cause["id"]]
    else:
        payload["status"] = status
    if not dry_run:
        write_json(path, payload)
    return cause


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    counts: dict[str, int] = {}
    for path in sorted(RESULTS_DIR.glob("*/*.json")):
        if path.name == "summary.json":
            continue
        cause = analyze_file(path, args.dry_run)
        counts[cause["id"]] = counts.get(cause["id"], 0) + 1
        if cause["id"] not in ("none",):
            log(f"{path.parent.name:6s} {path.stem:40s} {cause['id']}")
    log("causes:", dict(sorted(counts.items(), key=lambda kv: -kv[1])))


if __name__ == "__main__":
    main()

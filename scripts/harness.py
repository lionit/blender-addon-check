"""Runs INSIDE Blender. Installs (legacy) or locates (extension) one add-on, enables it,
pokes it, writes a JSON result.

Invoked by run_tests.py as:
    blender -b --factory-startup --python scripts/harness.py -- \
        --zip X.zip --out result.json --addon-id ID --kind extension|legacy|legacy-single --module NAME

For extensions, run_tests.py has already installed the zip with
`blender --command extension install-file`, so this script only enables
`bl_ext.user_default.<id>`. For legacy add-ons it installs the zip here with
bpy.ops.preferences.addon_install, which is synchronous and works headless.

Stages, each recorded with its own outcome:
    install   place the add-on where Blender can import it
    enable    addon_utils.enable with error capture
    poll      call poll() on every operator the add-on registered
    reenable  disable, then enable again (catches bad unregister code)

The script never raises: every failure is captured into the result file. If Blender
itself crashes, no result file is written and the runner records a crash.
"""

from __future__ import annotations

import io
import json
import os
import sys
import time
import traceback
import zipfile
from contextlib import redirect_stderr, redirect_stdout

RESULT: dict = {
    "addon_id": None,
    "blender_version": None,
    "python_version": None,
    "kind": None,
    "module": None,
    "stages": {},
    "status": "unknown",
    "error": "",
    "error_stage": "",
    "operators_total": 0,
    "operators_poll_failed": 0,
    "classes_registered": 0,
    "captured_output": "",
    "timing_s": {},
}


def parse_args() -> dict:
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    out = {}
    i = 0
    while i < len(argv):
        key = argv[i]
        if key.startswith("--") and i + 1 < len(argv):
            out[key[2:].replace("-", "_")] = argv[i + 1]
            i += 2
        else:
            i += 1
    return out


def write_result(path: str) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(RESULT, fh, indent=2, sort_keys=True)


def stage(name: str, ok: bool, detail: str = "") -> None:
    RESULT["stages"][name] = {"ok": ok, "detail": detail[-4000:]}
    if not ok and not RESULT["error"]:
        RESULT["error"] = detail[-6000:]
        RESULT["error_stage"] = name


def finish(out_path: str, buf: io.StringIO, status: str) -> None:
    RESULT["status"] = status
    RESULT["captured_output"] = buf.getvalue()[-8000:]
    write_result(out_path)


def main() -> None:
    args = parse_args()
    out_path = args.get("out", "result.json")
    zip_path = args["zip"]
    kind = args.get("kind", "legacy")
    module = args.get("module", "")
    RESULT["addon_id"] = args.get("addon_id", os.path.basename(zip_path))
    RESULT["kind"] = kind

    import bpy  # noqa: E402  (only importable inside Blender)
    import addon_utils  # noqa: E402

    RESULT["blender_version"] = bpy.app.version_string
    RESULT["python_version"] = sys.version.split()[0]
    errors: list[str] = []

    def handle_error(exc: BaseException) -> None:
        errors.append("".join(traceback.format_exception(type(exc), exc, exc.__traceback__)))

    buf = io.StringIO()

    # ---- install -----------------------------------------------------------
    t0 = time.time()
    full_module = module
    try:
        with redirect_stdout(buf), redirect_stderr(buf):
            if kind == "extension":
                full_module = f"bl_ext.user_default.{module}"
                if hasattr(addon_utils, "extensions_refresh"):
                    addon_utils.extensions_refresh()
                known = {m.__name__ for m in addon_utils.modules(refresh=True)}
                if full_module not in known:
                    raise RuntimeError(
                        f"{full_module} is not among the add-ons Blender can see after install-file. "
                        f"Known extension modules: {sorted(k for k in known if k.startswith('bl_ext.'))[:20]}"
                    )
            elif kind == "legacy":
                bpy.ops.preferences.addon_install(filepath=zip_path, overwrite=True)
                addon_utils.modules(refresh=True)
            else:  # legacy-single: extract the .py and install it
                import tempfile

                tmpdir = tempfile.mkdtemp(prefix="bac-")
                with zipfile.ZipFile(zip_path) as z:
                    z.extract(module + ".py", tmpdir)
                bpy.ops.preferences.addon_install(filepath=os.path.join(tmpdir, module + ".py"), overwrite=True)
                addon_utils.modules(refresh=True)
        stage("install", True, buf.getvalue()[-2000:])
    except Exception:  # noqa: BLE001
        stage("install", False, traceback.format_exc() + "\n--- output ---\n" + buf.getvalue()[-3000:])
        finish(out_path, buf, "fail")
        return
    RESULT["module"] = full_module
    RESULT["timing_s"]["install"] = round(time.time() - t0, 2)

    # ---- enable ------------------------------------------------------------
    t0 = time.time()
    errors.clear()
    try:
        with redirect_stdout(buf), redirect_stderr(buf):
            mod = addon_utils.enable(full_module, default_set=False, persistent=False, handle_error=handle_error)
        if mod is None or errors:
            stage("enable", False, "\n".join(errors) or f"addon_utils.enable returned None for {full_module}\n--- output ---\n{buf.getvalue()[-3000:]}")
            finish(out_path, buf, "fail")
            return
        stage("enable", True)
    except Exception:  # noqa: BLE001
        stage("enable", False, traceback.format_exc())
        finish(out_path, buf, "fail")
        return
    RESULT["timing_s"]["enable"] = round(time.time() - t0, 2)

    # ---- poll every operator the add-on registered -------------------------
    t0 = time.time()
    poll_failures: list[str] = []
    total = 0
    registered = 0
    try:
        for cls in list(bpy.types.Operator.__subclasses__()):
            modname = getattr(cls, "__module__", "") or ""
            if not (modname == full_module or modname.startswith(full_module + ".")):
                continue
            idname = getattr(cls, "bl_idname", None)
            if not idname or "." not in idname:
                continue
            total += 1
            group, name = idname.split(".", 1)
            try:
                op = getattr(getattr(bpy.ops, group), name)
                with redirect_stdout(buf), redirect_stderr(buf):
                    op.poll()
            except Exception:  # noqa: BLE001
                poll_failures.append(f"{idname}\n{traceback.format_exc()}")
        for cls in list(bpy.types.bpy_struct.__subclasses__()):
            modname = getattr(cls, "__module__", "") or ""
            if modname == full_module or modname.startswith(full_module + "."):
                registered += 1
        RESULT["operators_total"] = total
        RESULT["operators_poll_failed"] = len(poll_failures)
        RESULT["classes_registered"] = registered
        stage("poll", not poll_failures, "\n\n".join(poll_failures)[-6000:])
    except Exception:  # noqa: BLE001
        stage("poll", False, traceback.format_exc())
    RESULT["timing_s"]["poll"] = round(time.time() - t0, 2)

    # ---- disable and enable again ------------------------------------------
    t0 = time.time()
    errors.clear()
    try:
        with redirect_stdout(buf), redirect_stderr(buf):
            addon_utils.disable(full_module, default_set=False, handle_error=handle_error)
            disable_errors = list(errors)
            errors.clear()
            mod = addon_utils.enable(full_module, default_set=False, persistent=False, handle_error=handle_error)
        problems = disable_errors + errors
        if mod is None:
            problems.append(f"second enable returned None for {full_module}")
        stage("reenable", not problems, "\n".join(problems))
    except Exception:  # noqa: BLE001
        stage("reenable", False, traceback.format_exc())
    RESULT["timing_s"]["reenable"] = round(time.time() - t0, 2)

    stages = RESULT["stages"]
    if not stages["install"]["ok"] or not stages["enable"]["ok"]:
        status = "fail"
    elif not stages.get("poll", {}).get("ok", True) or not stages.get("reenable", {}).get("ok", True):
        status = "warn"
    else:
        status = "pass"
    finish(out_path, buf, status)


if __name__ == "__main__":
    try:
        main()
    except Exception:  # noqa: BLE001 - last resort so the runner always gets a file
        RESULT["status"] = "fail"
        RESULT["error"] = traceback.format_exc()
        RESULT["error_stage"] = RESULT["error_stage"] or "harness"
        try:
            write_result(parse_args().get("out", "result.json"))
        except Exception:  # noqa: BLE001
            pass

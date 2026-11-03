"""Unit tests for the parts that run outside Blender: analysis, report, zip repacking."""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import analyze  # noqa: E402
import common  # noqa: E402
import fetch_addons  # noqa: E402


class AnalyzeRules(unittest.TestCase):
    def test_python_313_binary_mismatch(self):
        err = "ImportError: /x/ifcopenshell/_ifcopenshell_wrapper.cpython-311-x86_64-linux-gnu.so: undefined symbol: PyUnicode_AsUTF8"
        self.assertEqual(analyze.classify(err, "fail")["id"], "compiled-python-mismatch")

    def test_removed_stdlib(self):
        self.assertEqual(analyze.classify("ModuleNotFoundError: No module named 'imp'", "fail")["id"], "stdlib-module-removed")

    def test_scene_node_tree(self):
        self.assertEqual(analyze.classify("AttributeError: 'Scene' object has no attribute 'node_tree'", "fail")["id"], "scene-node-tree-removed")

    def test_generic_attribute_error(self):
        self.assertEqual(analyze.classify("AttributeError: 'Object' object has no attribute 'foo_bar'", "warn")["id"], "removed-bpy-attribute")

    def test_missing_dependency_is_last_resort(self):
        self.assertEqual(analyze.classify("ModuleNotFoundError: No module named 'numpy_quaternion'", "fail")["id"], "missing-dependency")

    def test_pass_has_no_cause(self):
        self.assertEqual(analyze.classify("", "pass")["id"], "none")

    def test_timeout(self):
        self.assertEqual(analyze.classify("", "timeout")["id"], "timeout")

    def test_unclassified(self):
        self.assertEqual(analyze.classify("RuntimeError: something odd", "fail")["id"], "unclassified")


class Repack(unittest.TestCase):
    def _zip(self, entries: dict[str, str]) -> Path:
        tmp = Path(tempfile.mkdtemp()) / "src.zip"
        with zipfile.ZipFile(tmp, "w") as z:
            for name, body in entries.items():
                z.writestr(name, body)
        return tmp

    def test_zipball_top_folder_renamed(self):
        src = self._zip({"nortikin-sverchok-abc123/__init__.py": "bl_info = {}", "nortikin-sverchok-abc123/nodes/a.py": "x = 1"})
        dest = src.with_name("out.zip")
        fetch_addons.repack(src, dest, "sverchok", None)
        with zipfile.ZipFile(dest) as z:
            self.assertEqual(sorted(z.namelist()), ["sverchok/__init__.py", "sverchok/nodes/a.py"])

    def test_subdir_lifted(self):
        src = self._zip({"repo-x/blender/addons/2.8/mira_tools/__init__.py": "bl_info = {}", "repo-x/README.md": "hi"})
        dest = src.with_name("out.zip")
        fetch_addons.repack(src, dest, "mira_tools", "blender/addons/2.8/mira_tools")
        with zipfile.ZipFile(dest) as z:
            self.assertEqual(z.namelist(), ["mira_tools/__init__.py"])

    def test_extension_detected(self):
        src = self._zip({"my_ext/blender_manifest.toml": 'id = "my_ext"', "my_ext/__init__.py": ""})
        self.assertTrue(fetch_addons.is_extension_zip(src))
        legacy = self._zip({"my_addon/__init__.py": "bl_info = {}"})
        self.assertFalse(fetch_addons.is_extension_zip(legacy))


class Report(unittest.TestCase):
    def test_site_builds_from_sample_results(self):
        import build_report

        tmp = Path(tempfile.mkdtemp())
        results = tmp / "results"
        site = tmp / "site"
        sample = {
            "4.5": {"a": "pass", "b": "fail"},
            "5.2": {"a": "warn", "b": "crash"},
        }
        for vk, addons in sample.items():
            for aid, status in addons.items():
                common.write_json(results / vk / f"{aid}.json", {
                    "addon_id": aid, "name": aid.upper(), "developer": "dev", "homepage": "https://example.com",
                    "status": status, "error": "AttributeError: 'Scene' object has no attribute 'node_tree'" if status != "pass" else "",
                    "error_stage": "enable", "stages": {"install": {"ok": True}, "enable": {"ok": status == "pass"}},
                    "cause": analyze.classify("AttributeError: 'Scene' object has no attribute 'node_tree'", status),
                })
            common.write_json(results / vk / "summary.json", {"blender_version": vk + ".0", "ran_at": "2026-11-10T02:00:00+00:00"})
        old = (build_report.RESULTS_DIR, build_report.SITE_DIR)
        build_report.RESULTS_DIR, build_report.SITE_DIR = results, site
        try:
            versions, res = build_report.load_results()
            self.assertEqual([v["key"] for v in versions], ["4.5", "5.2"])
            html = build_report.build_index(versions, res, "https://x", "https://signup")
            self.assertIn("A</a>", html)
            self.assertIn("Crashes Blender", html)
            self.assertIn("scene-node-tree-removed", html)
            page = build_report.build_addon_page("b", res["b"], versions, "https://signup")
            self.assertIn("Suggested fix", page)
        finally:
            build_report.RESULTS_DIR, build_report.SITE_DIR = old

    def test_worst_status(self):
        import build_report

        self.assertEqual(build_report.worst_status({"a": {"status": "pass"}, "b": {"status": "warn"}}), "warn")
        self.assertEqual(build_report.worst_status({"a": {"status": "pass"}, "b": {"status": "crash"}}), "crash")


class Config(unittest.TestCase):
    def test_config_files_parse_and_have_unique_ids(self):
        versions = common.read_json(ROOT / "config" / "versions.json")["versions"]
        self.assertEqual(len({v["key"] for v in versions}), len(versions))
        addons = common.read_json(ROOT / "config" / "addons.json")["addons"]
        ids = [a["id"] for a in addons]
        self.assertEqual(len(set(ids)), len(ids))
        for a in addons:
            self.assertIn(a["source"]["type"], {"extensions", "github_release", "github_zipball", "local", "private_github_file"})

    def test_parse_version(self):
        self.assertEqual(common.parse_version("4.5.3"), (4, 5, 3))
        self.assertEqual(common.parse_version("5.3"), (5, 3, 0))



class StatusOverrideTests(unittest.TestCase):
    def test_headless_gpu_is_inconclusive_not_broken(self):
        c = analyze.classify("SystemError: GPU functions for drawing are not available in background mode", "fail")
        self.assertEqual(c["id"], "needs-display")
        self.assertEqual(analyze.STATUS_OVERRIDE[c["id"]], "inconclusive")

    def test_declared_minimum_version(self):
        c = analyze.classify("ERROR molecularnodes: This Blender version (4.5.15) doesn\u2019t meet the minimum supported version (5.2.0)", "fail")
        self.assertEqual(c["id"], "declared-unsupported")
        self.assertEqual(analyze.STATUS_OVERRIDE[c["id"]], "unsupported")

    def test_unregistered_operator_is_not_removed_attribute(self):
        c = analyze.classify('AttributeError: Polling operator "bpy.ops.uv.univ_pack" error, could not be found', "warn")
        self.assertEqual(c["id"], "unregistered-operator")


if __name__ == "__main__":
    unittest.main()

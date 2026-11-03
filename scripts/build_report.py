"""Turn results/ into a static site: site/index.html, site/addons/<id>.html, site/results.json.

Usage:
    python scripts/build_report.py [--site-url https://lionit.github.io/blender-addon-check]

No dependencies: plain string templates and inline CSS, so it builds anywhere.
"""

from __future__ import annotations

import argparse
import datetime as dt
import html
from pathlib import Path

from common import CONFIG_DIR, RESULTS_DIR, SITE_DIR, log, read_json, write_json

STATUS_LABEL = {
    "pass": ("Works", "ok"),
    "warn": ("Loads, with errors", "warn"),
    "fail": ("Broken", "bad"),
    "crash": ("Crashes Blender", "bad"),
    "timeout": ("Hangs", "bad"),
    "inconclusive": ("Needs a display", "na"),
    "unsupported": ("Not supported (declared)", "na"),
    "unavailable": ("Not tested", "na"),
    "unknown": ("Not tested", "na"),
}

CSS = """
:root{--bg:#fff;--fg:#1b1b1b;--muted:#666;--line:#e3e3e3;--ok:#d9f2dd;--okfg:#14662a;--warn:#fff1cc;--warnfg:#7a5300;--bad:#fbdcdc;--badfg:#8f1d1d;--na:#efefef;--nafg:#555;--accent:#2b5fd9}
@media(prefers-color-scheme:dark){:root{--bg:#141414;--fg:#ececec;--muted:#a3a3a3;--line:#2c2c2c;--ok:#15351c;--okfg:#8be0a0;--warn:#3a2d05;--warnfg:#ffd76a;--bad:#3d1414;--badfg:#ff9c9c;--na:#262626;--nafg:#bbb;--accent:#7ea2ff}}
*{box-sizing:border-box}body{margin:0;font:15px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif;background:var(--bg);color:var(--fg)}
main{max-width:1100px;margin:0 auto;padding:24px 16px 64px}h1{font-size:28px;margin:0 0 4px}h2{font-size:20px;margin:32px 0 8px}.sub{color:var(--muted);margin:0 0 20px}
.stats{display:flex;gap:16px;flex-wrap:wrap;margin:16px 0 24px}.stat{border:1px solid var(--line);border-radius:10px;padding:12px 16px;min-width:160px}.stat b{display:block;font-size:26px}.stat span{color:var(--muted);font-size:13px}
table{border-collapse:collapse;width:100%;font-size:14px}th,td{border-bottom:1px solid var(--line);padding:8px 10px;text-align:left;vertical-align:top}th{position:sticky;top:0;background:var(--bg)}
.s{display:inline-block;border-radius:6px;padding:2px 8px;font-size:12.5px;font-weight:600;white-space:nowrap}.ok{background:var(--ok);color:var(--okfg)}.warn{background:var(--warn);color:var(--warnfg)}.bad{background:var(--bad);color:var(--badfg)}.na{background:var(--na);color:var(--nafg)}
.cta{border:2px solid var(--accent);border-radius:12px;padding:16px 20px;margin:24px 0}.cta a{color:var(--accent);font-weight:600}
pre{background:var(--na);padding:12px;border-radius:8px;overflow:auto;font-size:12.5px;max-height:480px}a{color:var(--accent)}.muted{color:var(--muted)}
.legend span{margin-right:12px}.filter{margin:8px 0 16px}input[type=search]{font:inherit;padding:6px 10px;border:1px solid var(--line);border-radius:8px;background:var(--bg);color:var(--fg);width:min(100%,360px)}
"""

FILTER_JS = """
document.getElementById('q').addEventListener('input',function(e){var q=e.target.value.toLowerCase();document.querySelectorAll('tbody tr').forEach(function(r){r.style.display=r.dataset.name.indexOf(q)>-1?'':'none';});});
"""


def esc(text: object) -> str:
    return html.escape(str(text if text is not None else ""))


def load_results() -> tuple[list[dict], dict[str, dict[str, dict]]]:
    """Return (versions in config order with summaries, results[addon_id][version_key])."""
    config_versions = read_json(CONFIG_DIR / "versions.json")["versions"]
    versions = []
    results: dict[str, dict[str, dict]] = {}
    for entry in config_versions:
        vdir = RESULTS_DIR / entry["key"]
        if not vdir.exists():
            continue
        summary = read_json(vdir / "summary.json") if (vdir / "summary.json").exists() else {}
        versions.append({**entry, **summary})
        for path in vdir.glob("*.json"):
            if path.name == "summary.json":
                continue
            payload = read_json(path)
            results.setdefault(payload.get("addon_id", path.stem), {})[entry["key"]] = payload
    return versions, results


def addon_meta(addon_id: str, per_version: dict[str, dict]) -> dict:
    any_result = next(iter(per_version.values()))
    return {
        "id": addon_id,
        "name": any_result.get("name") or addon_id,
        "group": any_result.get("group", ""),
        "developer": any_result.get("developer", ""),
        "homepage": any_result.get("homepage", ""),
    }


def worst_status(per_version: dict[str, dict]) -> str:
    order = ["crash", "timeout", "fail", "warn", "inconclusive", "unsupported", "unavailable", "unknown", "pass"]
    statuses = [r.get("status", "unknown") for r in per_version.values()]
    for s in order:
        if s in statuses:
            return s
    return "unknown"


def status_cell(result: dict | None, addon_id: str) -> str:
    if result is None:
        return '<td><span class="s na">Not tested</span></td>'
    label, cls = STATUS_LABEL.get(result.get("status", "unknown"), STATUS_LABEL["unknown"])
    title = esc((result.get("cause") or {}).get("cause", ""))
    return f'<td><a href="addons/{esc(addon_id)}.html" title="{title}"><span class="s {cls}">{label}</span></a></td>'


def build_index(versions: list[dict], results: dict[str, dict[str, dict]], site_url: str, signup_url: str) -> str:
    def sort_key(i: str) -> tuple[int, str]:
        owner_first = -1 if any(r.get("group") == "owner" for r in results[i].values()) else 0
        return owner_first, addon_meta(i, results[i])["name"].lower()

    ids = sorted(results, key=sort_key)
    tested = [i for i in ids if any(r.get("status") not in ("unavailable", "unknown") for r in results[i].values())]
    broken = [i for i in tested if worst_status(results[i]) in ("fail", "crash", "timeout")]
    warned = [i for i in tested if worst_status(results[i]) == "warn"]
    cause_counts: dict[str, int] = {}
    for i in tested:
        for r in results[i].values():
            c = (r.get("cause") or {}).get("id", "")
            if c and c not in ("none",):
                cause_counts[c] = cause_counts.get(c, 0) + 1
    top_cause = max(cause_counts, key=cause_counts.get) if cause_counts else "none yet"
    newest_run = max((v.get("ran_at", "") for v in versions), default="")
    heads = "".join(f"<th>{esc(v['label'])}<br><span class='muted' style='font-weight:400'>{esc(v.get('blender_version', ''))}</span></th>" for v in versions)
    rows = []
    for i in ids:
        meta = addon_meta(i, results[i])
        cells = "".join(status_cell(results[i].get(v["key"]), i) for v in versions)
        link = f'<a href="{esc(meta["homepage"])}">{esc(meta["name"])}</a>' if meta["homepage"] else esc(meta["name"])
        rows.append(f'<tr data-name="{esc(meta["name"].lower())}"><td>{link}<br><span class="muted">{esc(meta["developer"])}</span></td>{cells}</tr>')
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Which Blender add-ons work on {esc(versions[-1]['label'] if versions else 'the latest Blender')}?</title><style>{CSS}</style></head><body><main>
<h1>Which Blender add-ons work on the current Blender versions?</h1>
<p class="sub">Every add-on below was installed and enabled in a fresh, headless Blender for each version. Last run: {esc(newest_run[:16].replace('T', ' '))} UTC. <a href="results.json">Raw results</a>.</p>
<div class="stats"><div class="stat"><b>{len(tested)}</b><span>add-ons tested</span></div><div class="stat"><b>{len(broken)}</b><span>broken on at least one version</span></div><div class="stat"><b>{len(warned)}</b><span>load but throw errors</span></div><div class="stat"><b>{esc(top_cause)}</b><span>most common cause</span></div></div>
<div class="cta"><b>Is your add-on paid, or not in this list?</b> We could not test what we cannot download. <a href="{esc(signup_url)}">Send us your add-on</a> and it joins the nightly run, free, with the same report and a suggested fix for anything that breaks.</div>
<p class="legend"><span class="s ok">Works</span> installs, enables, every operator polls, disables and re-enables cleanly. <span class="s warn">Loads, with errors</span> enables but something throws. <span class="s bad">Broken</span> fails to install or enable, hangs, or crashes Blender. <span class="s na">Not supported (declared)</span> the add-on itself says it does not support this version. <span class="s na">Needs a display</span> sets up GPU drawing while registering, so a headless test cannot judge it. <span class="s na">Not tested</span> could not be downloaded.</p>
<div class="filter"><input id="q" type="search" placeholder="Filter by add-on name"></div>
<table><thead><tr><th>Add-on</th>{heads}</tr></thead><tbody>{''.join(rows)}</tbody></table>
<h2>How this works</h2><p>Each night a script downloads the newest build of every version above, installs each add-on into a clean user profile, enables it, calls <code>poll()</code> on every operator it registered, then disables and re-enables it. Nothing is rendered and no file is opened, so a green mark means "loads and registers correctly", not "every feature works". Source and method: <a href="https://github.com/lionit/blender-addon-check">github.com/lionit/blender-addon-check</a>.</p>
<script>{FILTER_JS}</script></main></body></html>"""


def build_addon_page(addon_id: str, per_version: dict[str, dict], versions: list[dict], signup_url: str) -> str:
    meta = addon_meta(addon_id, per_version)
    sections = []
    for v in versions:
        r = per_version.get(v["key"])
        if r is None:
            continue
        label, cls = STATUS_LABEL.get(r.get("status", "unknown"), STATUS_LABEL["unknown"])
        cause = r.get("cause") or {}
        detail = ""
        if r.get("status") != "pass":
            detail = f"<p><b>Cause:</b> {esc(cause.get('cause', ''))}</p><p><b>Suggested fix:</b> {esc(cause.get('fix', ''))}</p>"
            err = r.get("error") or ""
            if err:
                detail += f"<details><summary>Error output (stage: {esc(r.get('error_stage', ''))})</summary><pre>{esc(err[-6000:])}</pre></details>"
        stages = r.get("stages", {})
        stage_line = " · ".join(f"{k}: {'ok' if s.get('ok') else 'failed'}" for k, s in stages.items()) if stages else ""
        ops = f"{r.get('operators_total', 0)} operators, {r.get('operators_poll_failed', 0)} failed poll, {r.get('classes_registered', 0)} classes registered" if stages else ""
        declared = f"Declares Blender {esc(r.get('declared_min') or '?')}" + (f" to {esc(r['declared_max'])}" if r.get("declared_max") else "") if r.get("declared_min") else ""
        sections.append(f"<h2>{esc(v['label'])} <span class='muted' style='font-weight:400'>{esc(r.get('blender_version') or v.get('blender_version', ''))}</span> <span class='s {cls}'>{label}</span></h2><p class='muted'>{esc(stage_line)}{' · ' if stage_line and ops else ''}{esc(ops)}{' · ' if declared and (stage_line or ops) else ''}{declared}</p>{detail}")
    link = f'<a href="{esc(meta["homepage"])}">{esc(meta["homepage"])}</a>' if meta["homepage"] else ""
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(meta['name'])} on current Blender versions</title><style>{CSS}</style></head><body><main>
<p><a href="../index.html">&larr; All add-ons</a></p><h1>{esc(meta['name'])}</h1><p class="sub">{esc(meta['developer'])} {link}</p>
{''.join(sections)}
<div class="cta"><b>Are you the developer?</b> <a href="{esc(signup_url)}">Add this add-on to the nightly run</a> to get an email the day a Blender build breaks it, with the fix.</div>
</main></body></html>"""


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--site-url", default="https://lionit.github.io/blender-addon-check")
    ap.add_argument("--signup-url", default="https://github.com/lionit/blender-addon-check/issues/new?template=test-my-addon.yml")
    args = ap.parse_args()
    versions, results = load_results()
    if not versions:
        raise SystemExit("no results found under results/")
    SITE_DIR.mkdir(parents=True, exist_ok=True)
    (SITE_DIR / "addons").mkdir(exist_ok=True)
    (SITE_DIR / "index.html").write_text(build_index(versions, results, args.site_url, args.signup_url), encoding="utf-8")
    for addon_id, per_version in results.items():
        (SITE_DIR / "addons" / f"{addon_id}.html").write_text(build_addon_page(addon_id, per_version, versions, args.signup_url), encoding="utf-8")
    slim = {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "versions": [{k: v.get(k) for k in ("key", "label", "blender_version", "ran_at", "counts")} for v in versions],
        "addons": {
            i: {**addon_meta(i, per), "results": {vk: {"status": r.get("status"), "cause": (r.get("cause") or {}).get("id", ""), "error_stage": r.get("error_stage", "")} for vk, r in per.items()}}
            for i, per in results.items()
        },
    }
    write_json(SITE_DIR / "results.json", slim)
    log(f"site built: {len(results)} add-ons x {len(versions)} versions -> {SITE_DIR}")


if __name__ == "__main__":
    main()

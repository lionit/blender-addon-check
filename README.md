# blender-addon-check

Nightly compatibility tests for Blender add-ons across the current Blender versions,
published as a public table with the cause of each failure and a suggested fix.

**Report:** https://lionit.github.io/blender-addon-check

## What it does

Every night, for each version in `config/versions.json` (today: 4.5 LTS, 5.1, 5.2 LTS and the
5.3 pre-release), a GitHub Actions job:

1. downloads the newest build of that Blender version (cached),
2. downloads every add-on in `config/addons.json` and `config/addons.generated.json`,
3. starts a fresh headless Blender per add-on, installs it, enables it, calls `poll()` on every
   operator it registered, then disables and re-enables it,
4. records a result per add-on and version (`pass`, `warn`, `fail`, `crash`, `timeout`).

A final job names the cause of each failure (`scripts/analyze.py`), builds the static site
(`scripts/build_report.py`), commits the results and deploys the site to GitHub Pages.

A green mark means "loads and registers correctly on this version", not "every feature works".

## Layout

| Path | Purpose |
| --- | --- |
| `config/versions.json` | Which Blender versions to test and how to find their builds |
| `config/addons.json` | Curated add-ons: GitHub-hosted tools and private ones |
| `config/addons.generated.json` | Most-downloaded add-ons on extensions.blender.org, refreshed weekly |
| `scripts/fetch_blender.py` | Resolve and download a Blender build for Linux |
| `scripts/collect_extensions.py` | Build the generated add-on list from extensions.blender.org |
| `scripts/fetch_addons.py` | Download and normalise add-on zips |
| `scripts/harness.py` | Runs inside Blender: install, enable, poll, re-enable, write JSON |
| `scripts/run_tests.py` | Run every add-on against one version in parallel |
| `scripts/analyze.py` | Regex rules that turn a traceback into a cause and a fix |
| `scripts/build_report.py` | Results to static HTML and `results.json` |
| `results/<version>/` | One JSON and one log per add-on, committed nightly |
| `.github/workflows/nightly.yml` | The nightly matrix, report and Pages deploy |

## Running locally

Linux x86-64 with Python 3.11+. No third-party packages.

```bash
python scripts/collect_extensions.py --top 60      # refresh the generated list
python scripts/run_tests.py --version 5.2 --only ucupaint,sverchok
python scripts/analyze.py
python scripts/build_report.py && open site/index.html
```

## Adding an add-on

Append an entry to `config/addons.json`. Source types:

- `extensions`: `{"type": "extensions", "id": "<slug on extensions.blender.org>"}`
- `github_release`: newest release asset matching `asset_pattern` (falls back to the source zipball)
- `github_zipball`: the repository at `ref`, repacked so the top folder is `module` (use `subdir` for monorepos)
- `private_github_file`: a file in a private repository, fetched with the `PRIVATE_ADDONS_TOKEN` secret;
  use this for paid add-ons so they never land in this public repository
- `local`: a file under `addons/` (ignored by git; for local runs only)

## Private add-ons

Paid add-ons must not be committed here. Put their zips in a private repository, create a
fine-grained token with read access to it, store it as the `PRIVATE_ADDONS_TOKEN` secret, and
reference each file with a `private_github_file` source.

## Requesting a test

Developers can ask for their add-on to be added through the
[issue form](https://github.com/lionit/blender-addon-check/issues/new?template=test-my-addon.yml).

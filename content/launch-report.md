# We tested 94 popular Blender add-ons on 4.5 LTS, 5.1, 5.2 LTS and the 5.3 beta. Here is what breaks, and why.

On Blender 5.2 LTS, 74 of 94 add-ons install, enable and re-enable cleanly. Every one of the 60 most-downloaded extensions on extensions.blender.org works or says honestly which versions it supports. The breakage is all on the GitHub side: 14 of 30 popular GitHub-hosted add-ons fail or misbehave, and 13 of those were already broken on 4.5 LTS. Blender 5 did not break them. Nobody has maintained them since Blender 4.

**How we tested.** Each add-on was installed into a fresh, headless Blender on a Linux machine, enabled, every operator it registers was polled, then it was disabled and enabled again. This catches import errors, removed APIs, binary mismatches and sloppy unregister code. It does not click buttons or render, so a green mark means "loads and registers correctly", not "every feature works". The full table with the error output for each add-on is on [the live page](https://lionit.github.io/blender-addon-check/) and the method is [open source](https://github.com/lionit/blender-addon-check).

## Results on 5.2 LTS (94 add-ons)

| Outcome | Count | Meaning |
| --- | --- | --- |
| Works | 74 | installs, enables, all operators poll, re-enables |
| Broken | 11 | fails to install or enable, or hangs |
| Loads with errors | 3 | enables once, but cannot be disabled and re-enabled |
| Not testable headless | 4 | sets up GPU drawing while registering; needs a real display |
| Declares no support | 2 | the add-on itself refuses this version |

## Finding 1: the Extensions platform works

56 of the top 60 extensions pass on 5.2 and on the 5.3 beta. The remaining four either need a display (FreePicker, PolyZamboni) or declare their own limits (Bonsai stops at 5.1, Amaranth starts at 5.3). The platform's manifest, version range and review process are doing their job.

## Finding 2: 4.5 LTS users are being left behind faster than they think

14 of the top 60 extensions no longer install on 4.5 LTS because their minimum version is now 5.0 or higher: Amaranth, BagaPaste, Bioxel Nodes, CloudRig, Dimensions, FreePicker, MeasureIt, Molecular Nodes, Persian type, PolyZamboni, QuickSnap, Retarget, Surface Psycho and ToothPaste. If you are on 4.5 LTS for stability, a quarter of the most popular add-ons have moved on without you.

## Finding 3: GitHub-hosted add-ons are the risk

Of 30 popular add-ons distributed only through GitHub, 15 work, 10 are broken, 3 misbehave on re-enable, 1 hangs and 1 needs a display. Only one of them broke between 4.5 and 5.x (Lily Surface Scraper, which ships an lxml binary built for the old Python). The rest have been broken since Blender 4 and nobody noticed, because nobody runs them nightly.

## What breaks, by cause (5.2 LTS)

| Cause | Add-ons | Fix |
| --- | --- | --- |
| Compiled module built for Python 3.11 | Animation Nodes, Modular Tree, Lily Surface Scraper | Rebuild for Python 3.13 (Blender 5.1+), ship one wheel per Blender Python |
| Cannot survive disable + enable | BlenderGPT, Malt, Mesh Align Plus | Make unregister() undo everything register() does |
| Uses the removed bgl module | Archipack, Blosm | Port drawing code to the gpu module |
| Imports a bpy.types class that no longer exists | Sorcar (NodeSocketInterface) | Use the replacement class behind a version check |
| Sequencer API renamed Sequence to Strip | Mixer | Rename, guarded by bpy.app.version |
| Missing bundled dependency | BlendLuxCore (pyluxcore) | Ship it as a wheel in the manifest |
| Installs heavy packages while enabling | Dream Textures (times out) | Defer downloads until the user asks |
| Other | GoB, SketchUp Importer | Preferences path handling, partially initialised compiled import |

## What this means for developers

Three of the five causes above are not version-specific at all. Re-enable failures, bgl and missing dependencies break on every version and are invisible until a user reports them. A nightly install on every current Blender build finds them the day they appear.

## Add your add-on

The test runs every night against the newest 4.5, 5.1, 5.2 and 5.3 builds. If you develop an add-on, paid or free, [send it to us](https://github.com/lionit/blender-addon-check/issues/new?template=test-my-addon.yml) and it joins the nightly run for free, with the same report and a suggested fix for anything that breaks. Paid add-ons stay private; only the result is published.

*Tested on 3 November 2026 with Blender 4.5.15 LTS, 5.1.2, 5.2.3 LTS and 5.3.0 beta (daily build). One add-on, Blender for Unreal Engine, was excluded because our packaging of its release archive was wrong, not the add-on.*

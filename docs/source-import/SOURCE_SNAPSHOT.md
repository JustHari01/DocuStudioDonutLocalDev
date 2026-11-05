# Current source snapshot

Snapshot captured 2026-11-05T08:00:15.888502+00:00 from the working tree at `D:\donutbrowser-main`.

| Component | Source version |
|---|---|
| Donut Local | 0.36.0 |
| DocuRandom Studio | 2.4.338 |
| Chat Bridge | 1.0.158 |
| Donut extension | 1.1.43 |
| Flow extension | 1.3.52 |
| Master library | 11.1.0-r1 (20 standalone masters) |

Includes 1437 source/assets/configuration files, including current modified and untracked Studio files. Studio's previous Git HEAD was `120385b8384a0f20e7066f40f926eac8da16851e`; this import captures its current working tree, not only that older commit. Old Git history is retained at its original location and is not uploaded here.

`source-manifest.json` records original SHA-256 values. The README and repository housekeeping changes are listed separately; application source bytes were copied without modification. Small archive files under `src-tauri/test-assets` are synthetic extraction fixtures, not profile backups.

The old `LOCAL_SOURCE_REVISION.txt`, `README_SUITE_VI.md` and installed version marker contain historical versions. The table above comes from current source metadata. No claim is made that older prose or installed runtime markers describe this snapshot accurately.

Private/runtime exclusions include profiles, cookies, Local Storage, IndexedDB, session state, workspaces, queue/checkpoint files, API keys, generated local configuration, installed browsers, third-party binaries and dependency/build caches. Dependency lockfiles, build scripts, extension examples and the PyInstaller definitions are included.

Pattern scanning found no live credential in the exported source. Hits were AWS documentation examples in signing tests, and synthetic local credentials in automated tests. This scan is a bounded check, not proof that all possible secret formats can be recognized.

Import verification covers file/hash fidelity, source selection, Git objects and remote recovery. It does not constitute a fresh full build or an end-to-end workflow test. Some packaging definitions and helper probes retain original machine-specific paths; adjust them deliberately before using a different installation. Never point a clone at live profile data merely to test a build.

Licenses/notices remain with their components. The root Donut license is not a new blanket license grant for all Studio or third-party code.

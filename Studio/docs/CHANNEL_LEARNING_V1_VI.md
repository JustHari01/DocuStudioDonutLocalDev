# Channel learning pilot — DaiTa14

Source: `D:\donutbrowser-main\Studio`. Profile/cookies stay in the existing Donut data directory.

## Approved DaiTa14 cadence experiment

- One Public slot per day at 13:30, Asia/Saigon, from 2026-10-22 through 2026-11-18 inclusive (28 slots). The existing two-hour publication window and metadata/thumbnail gates still apply. A failed slot is not a promise of one successful publication.
- Original shifts, first/last slots and authorization are retained in `D:\Youtube\daita14\.docurandom\cadence-trial.json`. At the first scheduler tick on/after 2026-11-19, saved shifts return with future dates. A later manual clock/shift edit cancels automatic restoration. Disarming the workspace remains respected.
- The active production, queue, receipts and existing published videos are retained. No other workspace schedule is changed. Metrics are observational: the experiment also introduces voice/content changes, so cadence alone cannot be identified as the cause of a later view difference.

## Verified local canary, 2026-10-21

- Read actual Overview, Content and Audience reports; retained missing-data flags and obtained a saved ChatGPT channel analysis.
- Audited all 183 DaiTa14 VOICEVOX boxes in nine byte-bounded chunks. Synthesized a local test WAV, then ran large-v3 CPU INT8 recognition over 183/183 lines: no gross-disagreement warnings. This is not proof of perfect Japanese pronunciation or phoneme-level subtitle timing.
- Rendered and fully decoded a local 16:9 test with a Flow image and labelled portrait pattern; inspected the foreground edges and blurred background. The test MP4 was not uploaded.
- A Commons query for this subject returned no suitable candidate; the original image sequence remains the fallback. Pexels has no configured API key.

## Implemented in the pilot

- Channel-ID-scoped SQLite snapshots, immutable evidence exports, versioned `CHANNEL_MEMORY.md`, lesson revisions and original operator charter.
- Real read-only Studio accessibility collector for Overview, Content and Audience. It never calls the 1mm.me ordering path or `Runtime.evaluate`. Each report carries its actual date range, source URL and explicit unavailable-field flags. Raw scoped report text remains available when a metric has not been normalized.
- Daily/weekly ChatGPT analysis in a separate saved channel conversation; exact submitted requests are retained. New production receives memory without changing an existing in-flight Master/turn identity.
- Optional Pexels (key required), Commons (supported licenses only), and a user-owned local footage catalogue. Query and contact-sheet checks use ChatGPT Web, with a two-minute retrieval budget. No suitable material means the normal video continues.
- Source/license/credit sidecars, recent-source and visual-fingerprint ledger, fixed narration-time insertions, no footage loops, original clip speed/colours, and a maximum 25% optional footage contribution. Library exhaustion preserves the base video.
- New portrait framing retains the full foreground; matching 16:9 material fills the canvas. This conservative fit is not a claim of an installed general-purpose object tracker.
- VOICEVOX audit chunks limited by UTF-8 bytes as well as box count, a dedicated audit conversation, same-speaker styles, validated floating-point engine controls, full-coverage optional large-v3 CPU INT8 QA and a gate when that full check is incomplete or flags gross disagreement.

## Boundaries

- Pilot policy is opt-in per workspace in `.docurandom/learning-policy.json`. Other channels have no enabled policy by default. Existing Public/Unlisted videos and disabled workspace schedules are not changed by the feature.
- This release does not claim statistical proof of better views, validated native A/B automation, structured extraction of every Advanced Mode report, phoneme-level subtitle alignment, or fully automatic resolution of every pronunciation warning. It keeps an unresolved QA report instead of declaring a faulty voice complete.
- Pexels is unavailable without an operator-configured `DOCURANDOM_PEXELS_API_KEY`. Pixabay bulk crawling is not implemented.
- Files in a local footage library use `*.source.json` with `file`, `keywords`, `rights_confirmed: true`, `license`, and source/author/credit information. Automatic collection does not guess rights from a filename.
- The 20 GB download-cache ceiling stops new acquisitions. Current production references are not deleted. Historical fingerprints and credit records remain in the SQLite ledger.

## Verification commands

Run from the Studio source with the managed environment pointing to the portable installation:

```powershell
$env:DOCURANDOM_HOME='D:\donutbrowser-main\portable-dev'
$env:DOCURANDOM_DONUT_DATA_ROOT='D:\donutbrowser-main\portable-dev\DonutBrowser\Data'
.\.build-venv\Scripts\python.exe -X utf8 tools/channel_learning_probe.py status --workspace D:\Youtube\daita14
.\.build-venv\Scripts\python.exe -X utf8 tools/channel_learning_probe.py collect --workspace D:\Youtube\daita14
.\.build-venv\Scripts\python.exe -X utf8 tools/channel_learning_probe.py render-canary --workspace D:\Youtube\daita14
```

`render-canary` uses an existing DaiTa14 Flow image and a labelled synthetic portrait test pattern. It produces a local MP4 and full-decode report under `Studio\artifacts`; it never uploads that test.

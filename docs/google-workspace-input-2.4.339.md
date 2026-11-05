# Google Workspace input and profile efficiency — 2.4.339

The current Google feature already batches multiple steps under one profile lease.
This update removes an extra Docs/Sheets landing-page load before a requested
artifact, and avoids opening a browser for daily files whose successful receipts
already exist. Pending plans keep their original steps and request identities.

Docs/Sheets title and cell-address fields now receive ordinary browser mouse
input after uniqueness, visibility, stable geometry, hit-target and focus checks.
Keyboard shortcuts and Unicode insertion use the same owned CDP target; focus
is rechecked before typing into a field. Key/button release is attempted after
an uncertain press acknowledgement. Saved content must still match the server
export. This does not control the Windows desktop cursor.

Profile release requires a positive integer PID, the matching browser-session
identity and unchanged non-owned tabs. Missing PIDs or reused process numbers do
not grant ownership of a manually opened profile.

`cached` explicitly means reused local success evidence, not fresh server
verification. It does not add a browser-cycle quota slot or refresh the recorded
last-success timestamp. `profile_cache_hits` counts avoided launches.

## Validation

- 105 input/Google/receipt/lifecycle tests passed, including a real isolated
  Chromium test for click, Japanese/Vietnamese text, Ctrl+A, Tab and an obstructed
  input. The fixture uses its own temporary profile and a local data page.
- 37 shared-coordinator, maintenance, extension and Public-gate regression tests passed.
- 170 YouTube/MCP tests and 31 subtests passed.
- Total: 312 tests and 31 subtests. No Google account was used for these tests.

## Source/package scope

The running application was 2.4.337, while the imported Git snapshot was 2.4.338.
A static code-identity comparison, ignoring filenames/source-line positions,
matched 83 modules. Differences were the version, GoogleRunner/GoogleService,
the upload-source resolver and upload-coordinator/shared CDP declarations;
`google_editor` was new. The relevant Google/YouTube/coordinator regressions above
cover that baseline alongside the new changes. This is not a claim that the
entire installed application was exercised end to end.

Build staging and verification are kept outside Git under the local build
directory. Existing Bridge/Flow/Codex payloads are reused. Application/profile
data, schedules, cookies and workspace checkpoint files are not part of the
source changes. Deployment requires a controlled portable shutdown; a successful
build is not evidence that the running installation has already been upgraded.

This update changes no scheduled browsing topics and adds no random Gmail,
YouTube-viewing or account-warming activity. Search/Maps retain their existing
explicit query tasks and the shared profile lane.

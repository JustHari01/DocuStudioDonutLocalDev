# Donut Local 0.36.0

Source: D:\donutbrowser-main. Build: pnpm build:local using local pnpm 12.6.0 with automatic version management disabled and the frozen upstream lockfile.
The portable Studio launcher uses Components\DonutLocal-0.36.0 and the existing portable-dev\DonutBrowser Data/Cache/Ephemeral/WebView2 roots. Launch the existing profiles with START_DOCURANDOM_PORTABLE.cmd; the standalone local-release START.cmd uses separate data.

## Preserved local functions

The local-features Cargo feature enables real local REST/MCP handlers, automation, bulk launch/stop, extensions, synchronization and fingerprint configuration without application-side local usage limits. It does not fabricate CloudUser, payment state or vendor tokens. Wayfern and Cloud authorization remain real requirements where their services enforce them. UI configuration support does not prove the vendor engine applied every setting.

The authenticated loopback MCP server preserves the encrypted token and saved port. The CDP adapter exposes get_debug_connection and cdp_command. Existing DocuRandom Studio sessions retain raw profiles/PID fields; new clients get the compact, paginated upstream contract. Remote bridge clients retain upstream redaction and local-file restrictions.

## New upstream functions

0.36 adds the Rust sync server, application-traffic proxy configuration, portable-update infrastructure, MCP tool annotations, press-and-hold interaction, RAM-host cleanup and launch reliability fixes. Application traffic proxy settings are separate from each profile's proxy and are not enabled automatically. The Local application-update policy remains notification-only even with upstream portable self-updates available.

0.35 adds agent triage, facts and worker logs, Windows RAM profiles, browser-side migration, MCP forms/capture and UI/settings improvements. Windows RAM storage requires an actual WinFsp installation; this update does not install its system driver or convert existing disk profiles to RAM. Local startup also avoids automatic identity-storage conversion of existing profiles.

0.34 includes profile templates, bulk editing/creation, statuses, start pages, per-profile DNS, device-only 2FA codes, proxy change-IP links and Local Storage / IndexedDB transfer. Templates exclude device identity; each newly created profile gets its own. 2FA secrets are sealed locally and not exported through REST/MCP or synchronized.

Agent Console can observe local agent/MCP activity. Cloud recipe schedules, team management and remote services need actual service configuration and permissions. Restore points require a configured sync server; they are not independent local snapshots.

The included Rust donut-sync supports self-hosted sync. pnpm sync:local builds the native server with the frozen Cargo lockfile, binds it to loopback ports 3929/3930 and keeps keys/data in .local-sync. Preserve that directory. Existing profiles are not automatically connected to a different server. Official documentation: https://donutbrowser.com/docs/self-hosting/.

## Data and update policy

Official application updates notify only. Native application download/install commands refuse installation. Browser engine updates remain enabled in local builds, independently of the application-update setting: downloads use the upstream updater, and profile versions advance only after the profile closes. GeoIP City/ASN data is checked hourly and refreshed when missing or more than seven days old, including while the portable application stays open. Existing cookies, sessions and device identity are retained.

Startup does not rewrite saved device identity, host OS or PID metadata. Cookies, Local State, os_crypt_key, storage and sessions are retained. The cookie reader supports old Windows raw-key AES-GCM plus the upstream multi-OS os_crypt formats. Native KDF worker isolation, idempotent stop, window placement and cached upstream traffic summaries remain.

## Build and verification

The frontend stages on NTFS under LOCALAPPDATA for exFAT sources. The local pnpm installation uses a hoisted copy layout without workspace symlinks. The builder honors CARGO_TARGET_DIR. Builds without local-features retain upstream plan policy. AGPL and dependency licenses are preserved.

BUILD_MANIFEST.json in the installed component records hashes and verified runtime scope. Unit/build success does not prove completion of a YouTube upload, a Public schedule or a ChatGPT/Flow job.

Windows type checking uses the pinned TypeScript compiler because the upstream tsrs release does not ship a Windows binary. The upstream sync server remains optional and requires its own installation/configuration.

## 0.35.2 update

Hourly official application checks remain notification-only. Local download/install refusal is retained. Browser engines and GeoIP continue their existing automatic update paths. The upstream agent model fallback belongs to Donut Agent and does not change DocuRandom ChatGPT Web model selection. Legacy daemon cleanup is retained from upstream; existing profile data is not migrated or cleared.

## 0.35.3 update

Upstream cookie-bot exit checks, remote pull fixes and generation browser seat accounting are retained. Existing Local integration, profile storage and notification-only application updates remain. No conversion or cache/cookie clearing is introduced by this update.

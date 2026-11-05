//! Capabilities implemented by this source build, separate from cloud billing.
//! No vendor account, subscription, or browser-engine token is manufactured.
use serde::Serialize;

pub(crate) const fn enabled() -> bool {
  cfg!(feature = "local-features")
}

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
pub struct LocalFeatures {
  enabled: bool,
  browser_automation: bool,
  fingerprint_configuration: bool,
  extension_management: bool,
  local_mcp: bool,
  unlimited_local_requests: bool,
}

#[tauri::command]
pub fn get_local_features() -> LocalFeatures {
  let enabled = enabled();
  LocalFeatures {
    enabled,
    browser_automation: enabled,
    fingerprint_configuration: enabled,
    extension_management: enabled,
    local_mcp: enabled,
    unlimited_local_requests: enabled,
  }
}

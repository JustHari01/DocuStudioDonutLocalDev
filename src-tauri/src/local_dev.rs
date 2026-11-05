/// Local-only capability mode for an explicitly feature-enabled debug build.
///
/// This opens Donut's own loopback API/MCP and profile-configuration paths so
/// they can be developed without a cloud account. It does not issue a Wayfern
/// token or change checks enforced inside the browser engine.
pub(crate) fn enabled() -> bool {
  if !cfg!(all(debug_assertions, feature = "local-dev")) {
    return false;
  }

  std::env::var("DONUT_LOCAL_DEV")
    .is_ok_and(|value| value == "1" || value.eq_ignore_ascii_case("true"))
}

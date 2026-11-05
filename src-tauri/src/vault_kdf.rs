//! Keep native KDF failures outside the browser manager process.
//!
//! Only a private stdin/stdout pipe carries the material. The worker starts
//! before Tauri, touches no settings/profile files, and returns exactly one key.
use argon2::{Algorithm, Argon2, Params, Version};
use sha2::{Digest, Sha256};
use std::collections::HashMap;
use std::io::{Read, Write};
use std::process::{Command, Stdio};
use std::sync::{LazyLock, Mutex};
use std::time::{Duration, Instant};

pub const WORKER_ARGUMENT: &str = "--internal-vault-kdf-worker";
const MAX_INPUT: usize = 1024 * 1024;
const CACHE_LIMIT: usize = 256;
type KeyCache = HashMap<[u8; 32], [u8; 32]>;
static CACHE: LazyLock<Mutex<KeyCache>> = LazyLock::new(|| Mutex::new(HashMap::new()));

fn in_process(password: &[u8], salt: &[u8]) -> Result<[u8; 32], String> {
  // Explicitly pin the historical vault format; no dependency default drift.
  let params = Params::new(19456, 2, 1, Some(32)).map_err(|e| e.to_string())?;
  let mut key = [0u8; 32];
  Argon2::new(Algorithm::Argon2id, Version::V0x13, params)
    .hash_password_into(password, salt, &mut key)
    .map_err(|e| format!("Argon2 key derivation failed: {e}"))?;
  Ok(key)
}

fn worker_protocol(input: impl Read, mut output: impl Write) -> Result<(), String> {
  let mut input = input.take((MAX_INPUT + 9) as u64);
  let mut header = [0u8; 8];
  input
    .read_exact(&mut header)
    .map_err(|_| "Invalid KDF request")?;
  let password_len = u32::from_le_bytes(header[..4].try_into().unwrap()) as usize;
  let salt_len = u32::from_le_bytes(header[4..].try_into().unwrap()) as usize;
  let len = password_len
    .checked_add(salt_len)
    .ok_or("Invalid KDF request")?;
  if len > MAX_INPUT {
    return Err("KDF request is too large".into());
  }
  let mut material = vec![0u8; len];
  input
    .read_exact(&mut material)
    .map_err(|_| "Incomplete KDF request")?;
  let mut extra = [0u8; 1];
  if input.read(&mut extra).map_err(|_| "Invalid KDF request")? != 0 {
    return Err("Trailing KDF request data".into());
  }
  let key = in_process(&material[..password_len], &material[password_len..])?;
  output
    .write_all(&key)
    .map_err(|_| "Could not return KDF result".to_string())
}

pub fn run_worker() -> i32 {
  // No logging initialization, UI, auth, or on-disk vault migration here.
  match worker_protocol(std::io::stdin().lock(), std::io::stdout().lock()) {
    Ok(()) => 0,
    Err(_) => 2,
  }
}

fn isolated(
  command: &mut Command,
  password: &[u8],
  salt: &[u8],
  timeout: Duration,
) -> Result<[u8; 32], String> {
  if password.len().saturating_add(salt.len()) > MAX_INPUT {
    return Err("KDF request is too large".into());
  }
  let mut request = Vec::with_capacity(8 + password.len() + salt.len());
  request.extend_from_slice(&(password.len() as u32).to_le_bytes());
  request.extend_from_slice(&(salt.len() as u32).to_le_bytes());
  request.extend_from_slice(password);
  request.extend_from_slice(salt);
  command
    .stdin(Stdio::piped())
    .stdout(Stdio::piped())
    .stderr(Stdio::null());
  #[cfg(windows)]
  {
    use std::os::windows::process::CommandExt;
    command.creation_flags(0x08000000); // CREATE_NO_WINDOW
  }
  let mut child = command
    .spawn()
    .map_err(|e| format!("Could not start vault KDF worker: {e}"))?;
  let mut stdin = child.stdin.take().ok_or("Vault KDF stdin is unavailable")?;
  let stdout = child
    .stdout
    .take()
    .ok_or("Vault KDF stdout is unavailable")?;
  // Pipe I/O must not block the deadline, even if a worker fails before reading.
  let writer = std::thread::spawn(move || stdin.write_all(&request));
  let reader = std::thread::spawn(move || {
    let mut data = Vec::new();
    stdout.take(33).read_to_end(&mut data).map(|_| data)
  });
  let deadline = Instant::now() + timeout;
  let status = loop {
    match child.try_wait() {
      Ok(Some(status)) => break Ok(status),
      Ok(None) if Instant::now() < deadline => std::thread::sleep(Duration::from_millis(10)),
      Ok(None) => break Err("Vault KDF worker timed out; encrypted data was kept".to_string()),
      Err(e) => break Err(format!("Could not wait for vault KDF worker: {e}")),
    }
  };
  if status.is_err() {
    let _ = child.kill();
    let _ = child.wait();
  }
  let written = writer.join().map_err(|_| "Vault KDF input thread failed")?;
  let received = reader
    .join()
    .map_err(|_| "Vault KDF output thread failed")?;
  let status = status?;
  if !status.success() {
    return Err(format!(
      "Vault KDF worker exited ({status}); encrypted data was kept"
    ));
  }
  written.map_err(|_| "Vault KDF input failed")?;
  let received = received.map_err(|_| "Vault KDF output failed")?;
  received
    .try_into()
    .map_err(|_| "Invalid vault KDF result; encrypted data was kept".into())
}

pub fn derive_key(password: &[u8], salt: &[u8]) -> Result<[u8; 32], String> {
  if password.len().saturating_add(salt.len()) > MAX_INPUT {
    return Err("KDF request is too large".into());
  }
  let mut hash = Sha256::new();
  hash.update((password.len() as u64).to_le_bytes());
  hash.update(password);
  hash.update((salt.len() as u64).to_le_bytes());
  hash.update(salt);
  let fingerprint: [u8; 32] = hash.finalize().into();
  // Coalesce identical concurrent requests instead of running many Argon2 jobs
  // for every API/MCP-token poll. Bound retained entries and never cache failures.
  let mut cache = CACHE.lock().map_err(|_| "Vault KDF cache is unavailable")?;
  if let Some(key) = cache.get(&fingerprint) {
    return Ok(*key);
  }
  #[cfg(not(test))]
  let key = {
    let exe = std::env::current_exe().map_err(|e| e.to_string())?;
    // Integration test executables use Rust's test runner, not the application
    // CLI. Only the isolated E2E build accepts a real worker fixture path.
    #[cfg(feature = "e2e")]
    let exe = std::env::var_os("DONUT_E2E_VAULT_WORKER")
      .map(std::path::PathBuf::from)
      .unwrap_or(exe);
    let exe = if exe.file_stem().and_then(|s| s.to_str()) == Some("donut-proxy") {
      exe.with_file_name(if cfg!(windows) {
        "donutbrowser.exe"
      } else {
        "donutbrowser"
      })
    } else {
      exe
    };
    isolated(
      Command::new(exe).arg(WORKER_ARGUMENT),
      password,
      salt,
      Duration::from_secs(30),
    )?
  };
  // The Rust test runner has no application CLI. Runtime checks exercise the
  // real executable/worker path as well as these format/compatibility tests.
  #[cfg(test)]
  let key = in_process(password, salt)?;
  if cache.len() >= CACHE_LIMIT {
    cache.clear();
  }
  cache.insert(fingerprint, key);
  Ok(key)
}

#[cfg(test)]
mod tests {
  use super::*;

  fn request(password: &[u8], salt: &[u8]) -> Vec<u8> {
    let mut data = Vec::new();
    data.extend_from_slice(&(password.len() as u32).to_le_bytes());
    data.extend_from_slice(&(salt.len() as u32).to_le_bytes());
    data.extend_from_slice(password);
    data.extend_from_slice(salt);
    data
  }

  #[test]
  fn worker_keeps_the_existing_vault_key_vector() {
    let mut output = Vec::new();
    worker_protocol(
      request(b"correct horse battery staple", &[7; 16]).as_slice(),
      &mut output,
    )
    .unwrap();
    let hex: String = output.iter().map(|b| format!("{b:02x}")).collect();
    assert_eq!(
      hex,
      "799f12b9e17710824482d829835acb69f5a9355bf774c4f07342823b11b90928"
    );
  }

  #[test]
  fn malformed_requests_do_not_return_a_key() {
    for data in [vec![], vec![0; 7], request(b"pw", b"bad"), vec![255; 8]] {
      let mut output = Vec::new();
      assert!(worker_protocol(data.as_slice(), &mut output).is_err());
      assert!(output.is_empty());
    }
  }

  #[test]
  fn subprocess_fixture() {
    match std::env::var("DONUT_KDF_TEST_FIXTURE").ok().as_deref() {
      Some("fail") => std::process::exit(17),
      Some("timeout") => std::thread::sleep(Duration::from_secs(10)),
      Some("malformed") => std::process::exit(0),
      _ => (),
    }
  }

  fn fixture(mode: &str) -> Command {
    let mut command = Command::new(std::env::current_exe().unwrap());
    command
      .args([
        "--exact",
        "vault_kdf::tests::subprocess_fixture",
        "--nocapture",
      ])
      .env("DONUT_KDF_TEST_FIXTURE", mode);
    command
  }

  #[test]
  fn failed_worker_is_an_error_instead_of_a_browser_crash() {
    let error = isolated(
      &mut fixture("fail"),
      b"pw",
      &[1; 16],
      Duration::from_secs(5),
    )
    .unwrap_err();
    assert!(error.contains("exited"));
  }

  #[test]
  fn worker_timeout_is_bounded_and_reaped() {
    let start = Instant::now();
    let error = isolated(
      &mut fixture("timeout"),
      b"pw",
      &[1; 16],
      Duration::from_millis(250),
    )
    .unwrap_err();
    assert!(error.contains("timed out"));
    assert!(start.elapsed() < Duration::from_secs(3));
  }

  #[test]
  fn malformed_worker_output_is_not_accepted_as_a_key() {
    assert!(isolated(
      &mut fixture("malformed"),
      b"pw",
      &[1; 16],
      Duration::from_secs(5)
    )
    .is_err());
  }
}

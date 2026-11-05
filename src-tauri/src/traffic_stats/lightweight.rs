//! Bounded real-time reads; the detail API retains the full history.
use super::{TrafficSnapshot, TrafficStats};
use serde::{Deserialize, Serialize};
use std::collections::HashMap;
use std::fs::{self, File, Metadata};
use std::io::Read;
use std::path::{Path, PathBuf};
use std::sync::{LazyLock, Mutex};
use std::time::UNIX_EPOCH;

const HEADER_LIMIT: u64 = 64 * 1024;
const SMALL_FILE_LIMIT: u64 = 512 * 1024;

#[derive(Clone, Serialize, Deserialize)]
pub(super) struct StoredSnapshot {
  pub snapshot: TrafficSnapshot,
  pub last_flush_timestamp: u64,
}

#[derive(Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
struct Fingerprint {
  size: u64,
  modified_nanos: u128,
}

impl Fingerprint {
  fn from_metadata(meta: &Metadata) -> Self {
    Self {
      size: meta.len(),
      modified_nanos: meta
        .modified()
        .ok()
        .and_then(|time| time.duration_since(UNIX_EPOCH).ok())
        .map(|duration| duration.as_nanos())
        .unwrap_or(0),
    }
  }
}

#[derive(Serialize, Deserialize)]
struct Sidecar {
  fingerprint: Fingerprint,
  data: StoredSnapshot,
}

#[derive(Deserialize)]
struct Header {
  proxy_id: String,
  profile_id: Option<String>,
  session_start: u64,
  last_update: u64,
  #[serde(default)]
  last_flush_timestamp: u64,
  total_bytes_sent: u64,
  total_bytes_received: u64,
  total_requests: u64,
}

type Cache = HashMap<PathBuf, (Fingerprint, StoredSnapshot)>;
static CACHE: LazyLock<Mutex<Cache>> = LazyLock::new(|| Mutex::new(HashMap::new()));

pub(super) fn invalidate() {
  CACHE
    .lock()
    .unwrap_or_else(|error| error.into_inner())
    .clear();
}

/// A summary belongs to one exact atomic history-file revision.
pub(super) fn save(path: &Path, stats: &TrafficStats) -> std::io::Result<()> {
  let sidecar = Sidecar {
    fingerprint: Fingerprint::from_metadata(&fs::metadata(path)?),
    data: StoredSnapshot {
      snapshot: stats.to_snapshot(),
      last_flush_timestamp: stats.last_flush_timestamp,
    },
  };
  let bytes = serde_json::to_vec(&sidecar)?;
  let temp = path.with_extension("snapshot.tmp");
  fs::write(&temp, bytes)?;
  fs::rename(temp, path.with_extension("snapshot"))
}

pub(super) fn load(path: &Path) -> Option<StoredSnapshot> {
  let mut cache = CACHE.lock().unwrap_or_else(|error| error.into_inner());
  let file = File::open(path).ok()?;
  let fingerprint = Fingerprint::from_metadata(&file.metadata().ok()?);
  if let Some((previous, data)) = cache.get(path) {
    if *previous == fingerprint {
      return Some(data.clone());
    }
  }
  let sidecar = File::open(path.with_extension("snapshot"))
    .ok()
    .and_then(|file| serde_json::from_reader::<_, Sidecar>(file.take(HEADER_LIMIT)).ok())
    .filter(|sidecar| sidecar.fingerprint == fingerprint);
  let data = if let Some(sidecar) = sidecar {
    sidecar.data
  } else if fingerprint.size <= SMALL_FILE_LIMIT {
    let stats: TrafficStats = serde_json::from_reader(file.take(SMALL_FILE_LIMIT + 1)).ok()?;
    StoredSnapshot {
      snapshot: stats.to_snapshot(),
      last_flush_timestamp: stats.last_flush_timestamp,
    }
  } else {
    let mut prefix = Vec::new();
    file.take(HEADER_LIMIT).read_to_end(&mut prefix).ok()?;
    header_snapshot(&prefix)?
  };
  if cache.len() >= 512 && !cache.contains_key(path) {
    cache.clear();
  }
  cache.insert(path.to_path_buf(), (fingerprint, data.clone()));
  Some(data)
}

fn header_snapshot(prefix: &[u8]) -> Option<StoredSnapshot> {
  // Scalar fields precede histories in TrafficStats' serialized schema.
  // This fallback keeps old large files and their exact totals untouched.
  let keys: [&[u8]; 4] = [
    b"\"bandwidth_history\"",
    b"\"domains\"",
    b"\"domain_access_history\"",
    b"\"unique_ips\"",
  ];
  let end = keys
    .iter()
    .filter_map(|key| prefix.windows(key.len()).position(|window| window == *key))
    .min()?;
  let mut header = prefix[..end].to_vec();
  while header.last().is_some_and(u8::is_ascii_whitespace) {
    header.pop();
  }
  if header.pop()? != b',' {
    return None;
  }
  header.push(b'}');
  let header: Header = serde_json::from_slice(&header).ok()?;
  if header.proxy_id.is_empty() {
    return None;
  }
  Some(StoredSnapshot {
    last_flush_timestamp: header.last_flush_timestamp,
    snapshot: TrafficSnapshot {
      profile_id: header.profile_id,
      session_start: header.session_start,
      last_update: header.last_update,
      total_bytes_sent: header.total_bytes_sent,
      total_bytes_received: header.total_bytes_received,
      total_requests: header.total_requests,
      current_bytes_sent: 0,
      current_bytes_received: 0,
      recent_bandwidth: Vec::new(),
    },
  })
}

#[cfg(test)]
mod tests {
  use super::*;
  use std::io::Write;

  #[test]
  fn huge_history_is_read_from_its_header_without_mutation() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("profile.json");
    let stats = TrafficStats::new("proxy".into(), Some("profile".into()));
    let mut file = File::create(&path).unwrap();
    file
      .write_all(&serde_json::to_vec(&stats).unwrap())
      .unwrap();
    // A trailing invalid body proves the real-time path does not parse history.
    file.set_len(SMALL_FILE_LIMIT + 1).unwrap();
    drop(file);
    let before = Fingerprint::from_metadata(&fs::metadata(&path).unwrap());
    let data = load(&path).unwrap();
    assert_eq!(data.snapshot.profile_id.as_deref(), Some("profile"));
    assert_eq!(data.snapshot.total_bytes_sent, 0);
    assert!(before == Fingerprint::from_metadata(&fs::metadata(&path).unwrap()));
  }

  #[test]
  fn legacy_header_keeps_totals_and_flush_boundary() {
    let prefix = br#"{"proxy_id":"proxy","profile_id":"profile","session_start":1,"last_update":20,"last_flush_timestamp":19,"total_bytes_sent":100,"total_bytes_received":200,"total_requests":3,"bandwidth_history":["#;
    let data = header_snapshot(prefix).unwrap();
    assert_eq!(data.snapshot.total_bytes_sent, 100);
    assert_eq!(data.snapshot.total_bytes_received, 200);
    assert_eq!(data.snapshot.total_requests, 3);
    assert_eq!(data.last_flush_timestamp, 19);
  }

  #[test]
  fn incomplete_header_is_rejected() {
    assert!(header_snapshot(br#"{"proxy_id":"proxy","bandwidth_history":[]}"#).is_none());
  }

  #[test]
  fn sidecar_preserves_chart_and_rejects_an_old_file_revision() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("profile.json");
    let mut stats = TrafficStats::new("proxy".into(), Some("profile".into()));
    stats.record_bandwidth(100, 200);
    fs::write(&path, serde_json::to_vec(&stats).unwrap()).unwrap();
    File::options()
      .write(true)
      .open(&path)
      .unwrap()
      .set_len(SMALL_FILE_LIMIT + 1)
      .unwrap();
    save(&path, &stats).unwrap();
    let data = load(&path).unwrap();
    assert_eq!(data.snapshot.recent_bandwidth.len(), 1);
    assert_eq!(data.snapshot.total_bytes_sent, 100);
    stats.record_bandwidth(300, 500);
    fs::write(&path, serde_json::to_vec_pretty(&stats).unwrap()).unwrap();
    let data = load(&path).unwrap();
    assert_eq!(data.snapshot.total_bytes_sent, 400);
    assert_eq!(data.snapshot.total_bytes_received, 700);
  }

  #[test]
  #[ignore = "Read-only verification against an explicitly supplied installed cache"]
  fn installed_large_cache_stays_bounded_and_keeps_totals() {
    let cache = PathBuf::from(std::env::var("DONUT_TRAFFIC_VERIFY_CACHE").unwrap());
    let _guard = crate::app_dirs::set_test_cache_dir(cache);
    let snapshots = super::super::get_all_traffic_snapshots_realtime();
    assert!(snapshots.len() >= 12);
    let data = snapshots
      .iter()
      .find(|snapshot| {
        snapshot.profile_id.as_deref() == Some("9b4dc342-00c1-4c91-9837-5456f6493576")
      })
      .unwrap();
    assert_eq!(data.total_requests, 1_818_428);
    for _ in 0..50 {
      assert_eq!(
        super::super::get_all_traffic_snapshots_realtime().len(),
        snapshots.len()
      );
    }
  }
}

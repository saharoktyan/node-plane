//! The controller's links to its relays.
//!
//! The driver keeps the controller's link key and the relays it pinned in
//! `relay-link/` next to its operation journal, and holds a link to each.
//! The links come back with the driver after a restart; the node agents are
//! asked only when a relay is set up, changed or removed.
use std::collections::HashMap;
use std::fs;
use std::path::PathBuf;
use std::sync::{Arc, Mutex};

use node_plane_relay::{keys, link};
use ring::signature::Ed25519KeyPair;
use serde::{Deserialize, Serialize};
use tokio::task::JoinHandle;

/// A relay the controller links to.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct Entry {
    pub node_key: String,
    pub url: String,
    /// The relay's public key in hex.
    pub relay_key: String,
}

struct Running {
    entry: Entry,
    status: Arc<Mutex<link::Status>>,
    task: JoinHandle<()>,
}

pub struct Relays {
    dir: PathBuf,
    key: Arc<Ed25519KeyPair>,
    api: String,
    links: Mutex<HashMap<String, Running>>,
}

/// The URL the link dials. Behind a reverse proxy the proxy answers on 443.
pub fn url(domain: &str, port: u32, tls: &str) -> String {
    if tls == "proxy" || port == 443 {
        format!("https://{domain}")
    } else {
        format!("https://{domain}:{port}")
    }
}

impl Relays {
    /// Loads the key and the pinned relays, and links to each of them.
    pub fn open(dir: PathBuf, api: String) -> Result<Arc<Self>, String> {
        let key = Arc::new(keys::load_or_create(&dir.join("link.key"))?);
        let relays = Arc::new(Self {
            dir,
            key,
            api,
            links: Mutex::new(HashMap::new()),
        });
        for entry in relays.load()? {
            relays.start(entry);
        }
        Ok(relays)
    }

    /// An empty set in a directory of its own, for tests of other parts.
    #[cfg(test)]
    pub fn scratch() -> Arc<Self> {
        static NEXT: std::sync::atomic::AtomicUsize = std::sync::atomic::AtomicUsize::new(0);
        let n = NEXT.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
        let dir = std::env::temp_dir().join(format!("driver-relays-{}-{n}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        Self::open(dir, "http://127.0.0.1:9".into()).unwrap()
    }

    pub fn link_key(&self) -> Vec<u8> {
        keys::public(&self.key)
    }

    fn list_path(&self) -> PathBuf {
        self.dir.join("relays.json")
    }

    fn load(&self) -> Result<Vec<Entry>, String> {
        match fs::read(self.list_path()) {
            Ok(bytes) => serde_json::from_slice(&bytes)
                .map_err(|e| format!("{}: {e}", self.list_path().display())),
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(Vec::new()),
            Err(e) => Err(format!("{}: {e}", self.list_path().display())),
        }
    }

    fn save(&self, links: &HashMap<String, Running>) -> Result<(), String> {
        let mut entries: Vec<_> = links.values().map(|r| r.entry.clone()).collect();
        entries.sort_by(|a, b| a.node_key.cmp(&b.node_key));
        let json = serde_json::to_vec_pretty(&entries).map_err(|e| e.to_string())?;
        keys::write_private(&self.list_path(), &json)
    }

    fn start(&self, entry: Entry) {
        let Some(relay_key) = keys::unhex(&entry.relay_key) else {
            eprintln!("relay {}: the pinned key is not hex", entry.node_key);
            return;
        };
        let status = Arc::new(Mutex::new(link::Status::default()));
        let config = link::Config {
            relay: entry.url.clone(),
            relay_key,
            key: self.key.clone(),
            api: self.api.clone(),
            ca: None,
            limits: link::Limits::default(),
            status: status.clone(),
        };
        let task = tokio::spawn(link::run(config));
        let previous = self.links.lock().unwrap().insert(
            entry.node_key.clone(),
            Running {
                entry,
                status,
                task,
            },
        );
        if let Some(previous) = previous {
            previous.task.abort();
        }
    }

    /// Pins the relay and links to it; an unchanged relay keeps its link.
    pub fn pin(&self, entry: Entry) -> Result<(), String> {
        let unchanged = self
            .links
            .lock()
            .unwrap()
            .get(&entry.node_key)
            .is_some_and(|running| running.entry == entry);
        if !unchanged {
            self.start(entry);
        }
        self.save(&self.links.lock().unwrap())
    }

    /// Drops the link to the node's relay.
    pub fn forget(&self, node_key: &str) -> Result<(), String> {
        let mut links = self.links.lock().unwrap();
        if let Some(running) = links.remove(node_key) {
            running.task.abort();
        }
        self.save(&links)
    }

    pub fn status(&self, node_key: &str) -> Option<(Entry, link::Status)> {
        let links = self.links.lock().unwrap();
        let running = links.get(node_key)?;
        Some((
            running.entry.clone(),
            running.status.lock().unwrap().clone(),
        ))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn entry(node_key: &str, url: &str) -> Entry {
        Entry {
            node_key: node_key.into(),
            url: url.into(),
            relay_key: keys::hex(&[1; 32]),
        }
    }

    #[test]
    fn the_url_follows_the_tls_mode() {
        assert_eq!(
            url("cab.example.com", 443, "acme"),
            "https://cab.example.com"
        );
        assert_eq!(
            url("cab.example.com", 8443, "files"),
            "https://cab.example.com:8443"
        );
        assert_eq!(
            url("cab.example.com", 8443, "proxy"),
            "https://cab.example.com"
        );
    }

    #[tokio::test]
    async fn pinned_relays_survive_a_restart() {
        let dir = std::env::temp_dir().join(format!("driver-relays-{}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        let relays = Relays::open(dir.clone(), "http://127.0.0.1:9".into()).unwrap();
        let key = relays.link_key();
        relays.pin(entry("n1", "https://127.0.0.1:9")).unwrap();
        relays.pin(entry("n2", "https://127.0.0.1:9")).unwrap();
        relays.forget("n2").unwrap();
        assert!(relays.status("n2").is_none());
        drop(relays);

        let again = Relays::open(dir.clone(), "http://127.0.0.1:9".into()).unwrap();
        assert_eq!(again.link_key(), key);
        let (pinned, status) = again.status("n1").unwrap();
        assert_eq!(pinned, entry("n1", "https://127.0.0.1:9"));
        assert!(!status.connected);
        assert!(again.status("n2").is_none());
        use std::os::unix::fs::PermissionsExt;
        let mode = |name: &str| fs::metadata(dir.join(name)).unwrap().permissions().mode() & 0o777;
        assert_eq!((mode("link.key"), mode("relays.json")), (0o600, 0o600));
        fs::remove_dir_all(&dir).unwrap();
    }
}

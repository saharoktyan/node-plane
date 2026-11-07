//! Workstation through the relay: no SSH, every request signed with this
//! installation's own Ed25519 key, which a staff member linked to their
//! account by confirming a code in the web cabinet. The relay forwards the
//! requests and can neither forge nor replay them; system operations stay
//! with SSH.
use std::{
    path::{Path, PathBuf},
    time::{Duration, SystemTime, UNIX_EPOCH},
};

use anyhow::{Context, Result, ensure};
use base64::{Engine, engine::general_purpose::URL_SAFE_NO_PAD};
use ring::{
    digest,
    rand::{SecureRandom, SystemRandom},
    signature::{Ed25519KeyPair, KeyPair},
};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use uuid::Uuid;
use zeroize::Zeroizing;

use crate::backend::ApiError;

/// A private CA the relay's certificate comes from, besides the system's.
pub const CA_ENV: &str = "NODE_PLANE_RELAY_CA_FILE";

/// How an installation is reached through its relay.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Link {
    /// The relay's address, such as `https://cab.example.com`.
    pub url: String,
    /// The key's id the controller gave when the code was confirmed.
    pub key_id: String,
}

/// The relay's address as typed: HTTPS, a host and maybe a port, nothing else.
pub fn base_url(value: &str) -> Result<String> {
    let value = value.trim().trim_end_matches('/');
    let url = reqwest::Url::parse(value)
        .context("Enter the relay's address, such as https://cab.example.com.")?;
    ensure!(
        url.scheme() == "https"
            && url.host_str().is_some()
            && url.username().is_empty()
            && url.password().is_none()
            && url.path() == "/"
            && url.query().is_none()
            && url.fragment().is_none(),
        "Enter the relay's address, such as https://cab.example.com."
    );
    Ok(value.to_owned())
}

/// Where a key lives: under its id once linked, under the request's while
/// the code waits for confirmation.
pub fn key_path(state_dir: &Path, name: &str) -> PathBuf {
    state_dir.join("relay-keys").join(format!("{name}.key"))
}

/// This installation's key, made on first use and kept private.
pub fn load_or_create_key(path: &Path) -> Result<Zeroizing<[u8; 32]>> {
    let mut seed = Zeroizing::new([0u8; 32]);
    if path.exists() {
        let bytes = Zeroizing::new(std::fs::read(path).context("Could not read the relay key")?);
        ensure!(bytes.len() == 32, "The relay key file is damaged.");
        seed.copy_from_slice(&bytes);
        return Ok(seed);
    }
    SystemRandom::new()
        .fill(seed.as_mut())
        .map_err(|_| anyhow::anyhow!("No randomness for a key"))?;
    let directory = path.parent().context("Relay key path has no directory")?;
    std::fs::create_dir_all(directory)?;
    let mut file = tempfile::NamedTempFile::new_in(directory)?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        file.as_file()
            .set_permissions(std::fs::Permissions::from_mode(0o600))?;
    }
    std::io::Write::write_all(&mut file, seed.as_ref())?;
    file.persist(path).context("Could not save the relay key")?;
    Ok(seed)
}

fn keypair(seed: &[u8; 32]) -> Ed25519KeyPair {
    Ed25519KeyPair::from_seed_unchecked(seed).expect("a 32-byte seed is an Ed25519 key")
}

pub fn public_key(seed: &[u8; 32]) -> String {
    URL_SAFE_NO_PAD.encode(keypair(seed).public_key().as_ref())
}

/// The bytes the controller checks: the same as `client_keys.message`.
fn message(method: &str, target: &str, body: &[u8], timestamp: u64, nonce: &str) -> Vec<u8> {
    let hash = digest::digest(&digest::SHA256, body);
    let hash: String = hash.as_ref().iter().map(|b| format!("{b:02x}")).collect();
    format!(
        "np-sig-v1\n{}\n{target}\n{hash}\n\n{timestamp}\n{nonce}",
        method.to_ascii_uppercase()
    )
    .into_bytes()
}

/// The `Authorization` header of one request.
pub fn authorization(
    seed: &[u8; 32],
    key_id: &str,
    method: &str,
    target: &str,
    body: &[u8],
    timestamp: u64,
    nonce: &str,
) -> String {
    let signature = keypair(seed).sign(&message(method, target, body, timestamp, nonce));
    format!(
        "NP-Signature key={key_id}, ts={timestamp}, nonce={nonce}, sig={}",
        URL_SAFE_NO_PAD.encode(signature.as_ref())
    )
}

fn nonce() -> String {
    let mut bytes = [0u8; 16];
    SystemRandom::new()
        .fill(&mut bytes)
        .expect("system randomness");
    URL_SAFE_NO_PAD.encode(bytes)
}

fn now() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_or(0, |d| d.as_secs())
}

pub fn client() -> Result<reqwest::Client> {
    let mut builder = reqwest::Client::builder()
        .user_agent(concat!(
            "node-plane-workstation/",
            env!("CARGO_PKG_VERSION")
        ))
        .connect_timeout(Duration::from_secs(10))
        .redirect(reqwest::redirect::Policy::none());
    if let Some(path) = std::env::var_os(CA_ENV) {
        let pem = std::fs::read(&path).context("Could not read the relay's CA file")?;
        builder = builder.add_root_certificate(reqwest::Certificate::from_pem(&pem)?);
    }
    Ok(builder.build()?)
}

fn transport_error() -> ApiError {
    ApiError {
        status: None,
        code: "backend_unavailable".into(),
    }
}

async fn answer(response: reqwest::Response) -> std::result::Result<Value, ApiError> {
    let status = response.status().as_u16();
    if response
        .content_length()
        .is_some_and(|length| length > 1024 * 1024)
    {
        return Err(ApiError {
            status: Some(status),
            code: "response_too_large".into(),
        });
    }
    let bytes = response.bytes().await.map_err(|_| transport_error())?;
    let value: Value = if bytes.is_empty() && status == 204 {
        Value::Null
    } else {
        serde_json::from_slice(&bytes).map_err(|_| ApiError {
            status: Some(status),
            code: "invalid_backend_response".into(),
        })?
    };
    if !(200..300).contains(&status) {
        let code = value
            .pointer("/error/code")
            .and_then(Value::as_str)
            .filter(|code| {
                code.len() <= 128 && code.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'_')
            })
            .unwrap_or("backend_request_failed");
        return Err(ApiError {
            status: Some(status),
            code: code.into(),
        });
    }
    Ok(value)
}

/// The relay's end of an installation, for every request of a session.
pub struct Transport {
    pub link: Link,
    seed: Zeroizing<[u8; 32]>,
    http: reqwest::Client,
}

impl Transport {
    pub fn open(link: Link, state_dir: &Path) -> Result<Self> {
        let key = key_path(state_dir, &link.key_id);
        ensure!(
            key.exists(),
            "This installation's relay key is missing; link Workstation again."
        );
        Ok(Self {
            seed: load_or_create_key(&key)?,
            http: client()?,
            link,
        })
    }

    /// One signed request; the answer as `backend::request` gives it.
    pub async fn request(
        &self,
        method: &hyper::Method,
        path: &str,
        body: Option<&Value>,
        command: Option<Uuid>,
        revision: Option<u64>,
    ) -> std::result::Result<Value, ApiError> {
        let bytes = body
            .map(|value| serde_json::to_vec(value).expect("JSON Value serializes"))
            .unwrap_or_default();
        let header = authorization(
            &self.seed,
            &self.link.key_id,
            method.as_str(),
            path,
            &bytes,
            now(),
            &nonce(),
        );
        let method = reqwest::Method::from_bytes(method.as_str().as_bytes())
            .map_err(|_| transport_error())?;
        let mut request = self
            .http
            .request(method, format!("{}{path}", self.link.url))
            .header(reqwest::header::AUTHORIZATION, header)
            .header(reqwest::header::CONTENT_TYPE, "application/json")
            .timeout(Duration::from_secs(75))
            .body(bytes);
        if let Some(key) = command {
            request = request.header("Idempotency-Key", key.to_string());
        }
        if let Some(revision) = revision {
            request = request.header("If-Match", format!("\"{revision}\""));
        }
        answer(request.send().await.map_err(|_| transport_error())?).await
    }
}

/// A request to link this Workstation, as the controller answered it.
#[derive(Clone, Debug, Deserialize, PartialEq, Eq)]
pub struct Pairing {
    pub id: String,
    pub code: String,
    pub expires_at: String,
    pub url: Option<String>,
}

/// Asks to link the key; a staff member confirms the code in the web cabinet.
pub async fn pair(
    base: &str,
    seed: &[u8; 32],
    name: &str,
) -> std::result::Result<Pairing, ApiError> {
    let http = client().map_err(|_| transport_error())?;
    let response = http
        .post(format!("{base}/api/v1/workstation-pairings"))
        .json(&json!({"public_key": public_key(seed), "name": name}))
        .timeout(Duration::from_secs(20))
        .send()
        .await
        .map_err(|_| transport_error())?;
    serde_json::from_value(answer(response).await?).map_err(|_| ApiError {
        status: None,
        code: "invalid_backend_response".into(),
    })
}

/// What became of the request: `pending`, `approved` with the key's id,
/// `rejected` or `expired`.
pub async fn pairing_state(
    base: &str,
    id: &str,
) -> std::result::Result<(String, Option<String>), ApiError> {
    let http = client().map_err(|_| transport_error())?;
    let response = http
        .get(format!("{base}/api/v1/workstation-pairings/{id}"))
        .timeout(Duration::from_secs(20))
        .send()
        .await
        .map_err(|_| transport_error())?;
    let value = answer(response).await?;
    Ok((
        value["status"].as_str().unwrap_or("").to_owned(),
        value["key_id"].as_str().map(str::to_owned),
    ))
}

/// What `node-plane link` reports while it waits.
pub trait Progress {
    fn code(&self, pairing: &Pairing);
    fn waiting(&self);
}

/// Links this Workstation to an installation through its relay and saves
/// the installation: asks with a new key, waits until a staff member
/// confirms the code in the web cabinet, then keeps the key under its id.
pub async fn link(
    state_dir: &Path,
    address: &str,
    name: &str,
    progress: &dyn Progress,
) -> Result<crate::connections::Installation> {
    let base = base_url(address)?;
    let request_name = Uuid::new_v4().simple().to_string();
    let pending = key_path(state_dir, &format!("pending-{request_name}"));
    let seed = load_or_create_key(&pending)?;
    let outcome = async {
        let pairing = pair(&base, &seed, name).await.map_err(|error| {
            anyhow::anyhow!("The relay did not take the request: {}", error.code)
        })?;
        progress.code(&pairing);
        let deadline = tokio::time::Instant::now() + Duration::from_secs(11 * 60);
        loop {
            tokio::time::sleep(Duration::from_secs(2)).await;
            match pairing_state(&base, &pairing.id).await {
                Ok((status, Some(key_id))) if status == "approved" => return Ok(key_id),
                Ok((status, _)) if status == "rejected" => {
                    anyhow::bail!("The code was declined in the web cabinet.")
                }
                Ok((status, _)) if status == "expired" => {
                    anyhow::bail!("The code expired. Run the command again.")
                }
                // A dropped connection or a pending request: keep waiting.
                _ => progress.waiting(),
            }
            ensure!(
                tokio::time::Instant::now() < deadline,
                "The code expired. Run the command again."
            );
        }
    }
    .await;
    let key_id = match outcome {
        Ok(key_id) => key_id,
        Err(error) => {
            let _ = std::fs::remove_file(&pending);
            return Err(error);
        }
    };
    ensure!(
        key_id.len() == 32 && key_id.bytes().all(|b| b.is_ascii_hexdigit()),
        "The controller returned an invalid key id."
    );
    std::fs::rename(&pending, key_path(state_dir, &key_id))
        .context("Could not keep the relay key")?;
    let url = reqwest::Url::parse(&base)?;
    let host = url
        .host_str()
        .unwrap_or_default()
        .trim_matches(['[', ']'])
        .to_owned();
    let mut connections = crate::connections::Connections::load(state_dir)?;
    let mut title = host.clone();
    let mut suffix = 2;
    while connections.validate_name(&title, None).is_err() {
        title = format!("{host} ({suffix})");
        suffix += 1;
    }
    let installation = crate::connections::Installation {
        id: Uuid::new_v4(),
        name: title,
        host,
        port: url.port().unwrap_or(443),
        user: "relay".into(),
        branch: String::new(),
        admin_ids: String::new(),
        account: String::new(),
        installed: true,
        relay: Some(Link { url: base, key_id }),
    };
    connections.installations.push(installation.clone());
    connections.selected = Some(installation.id);
    connections.save(state_dir)?;
    Ok(installation)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn seed() -> [u8; 32] {
        std::array::from_fn(|i| i as u8)
    }

    #[test]
    fn signs_exactly_like_the_controller_checks() {
        // The same key, request and signature as the controller's own
        // `client_keys.message` and Python's Ed25519 give.
        assert_eq!(
            public_key(&seed()),
            "A6EHv_POEL4dcN0Y50vAmWfk1jCbpQ1fHdyGZBJVMbg"
        );
        let header = authorization(
            &seed(),
            &"a".repeat(32),
            "put",
            "/api/v1/me/login?x=1",
            br#"{"login":"ops"}"#,
            1_794_000_000,
            "abcdefghijklmnopqrstuv",
        );
        assert_eq!(
            header,
            format!(
                "NP-Signature key={}, ts=1794000000, nonce=abcdefghijklmnopqrstuv, sig=no-lCSdoKareMcaAwZtTyNnmqmAvXCCXylqRSpeichZYaveTcK_YGfFhRmulwtqu9qCfKaHXqjuRtk4xdANeDg",
                "a".repeat(32)
            )
        );
        assert_eq!(nonce().len(), 22);
    }

    #[test]
    fn the_relay_address_is_https_and_nothing_else() {
        assert_eq!(
            base_url(" https://cab.example.com/ ").unwrap(),
            "https://cab.example.com"
        );
        assert_eq!(
            base_url("https://cab.example.com:8443").unwrap(),
            "https://cab.example.com:8443"
        );
        for wrong in [
            "http://cab.example.com",
            "cab.example.com",
            "https://cab.example.com/x",
            "https://u@cab.example.com",
        ] {
            assert!(base_url(wrong).is_err(), "{wrong}");
        }
    }

    #[test]
    fn the_key_is_made_once_and_kept_private() {
        let dir = tempfile::tempdir().unwrap();
        let path = key_path(dir.path(), "pending");
        let first = load_or_create_key(&path).unwrap();
        assert_eq!(*load_or_create_key(&path).unwrap(), *first);
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            assert_eq!(
                std::fs::metadata(&path).unwrap().permissions().mode() & 0o777,
                0o600
            );
        }
        std::fs::write(&path, b"short").unwrap();
        assert!(load_or_create_key(&path).is_err());
    }

    /// Against a running controller: `NODE_PLANE_RELAY_LIVE_STATE` names a
    /// state directory `node-plane link` saved an installation in.
    #[tokio::test]
    #[ignore]
    async fn live_signed_requests() {
        let state = PathBuf::from(std::env::var("NODE_PLANE_RELAY_LIVE_STATE").unwrap());
        let connections = crate::connections::Connections::load(&state).unwrap();
        let link = connections.active().unwrap().relay.clone().unwrap();
        let transport = Transport::open(link, &state).unwrap();
        let me = transport
            .request(&hyper::Method::GET, "/api/v1/me", None, None, None)
            .await
            .unwrap();
        assert!(me["id"].is_string());
        let changed = transport
            .request(
                &hyper::Method::PUT,
                "/api/v1/me/login",
                Some(&json!({"login": "relayed"})),
                Some(Uuid::new_v4()),
                None,
            )
            .await
            .unwrap();
        assert_eq!(changed["login"], "relayed");
        let refused = transport
            .request(
                &hyper::Method::GET,
                "/api/v1/nodes?limit=1&x=y",
                None,
                None,
                None,
            )
            .await;
        assert!(refused.is_ok(), "a query is signed too");
    }
}

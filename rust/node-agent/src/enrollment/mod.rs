//! First contact with the controller, without SSH or sudo on its side.
//!
//! The installer leaves a one-time token next to the TLS files. The agent
//! generates its own key, serves `NodeEnrollmentService` without TLS and
//! waits: the controller dials in (it may have no public address), checks
//! the agent's proof of the token over its public key, and issues a
//! certificate for that key with its own proof. The agent then keeps the
//! certificate and the controller's CA, forgets the token, limits the agent
//! port to the controller's address and starts the mutual-TLS service.
use std::fs;
use std::io::Write;
use std::net::IpAddr;
use std::os::unix::fs::OpenOptionsExt;
use std::path::{Path, PathBuf};
use std::sync::Mutex;

use ring::hmac;
use ring::rand::SystemRandom;
use ring::signature::{ECDSA_P256_SHA256_ASN1_SIGNING, EcdsaKeyPair, KeyPair};
use tokio::sync::oneshot;
use tonic::{Request, Response, Status};

use crate::agent::v1::node_enrollment_service_server::{
    NodeEnrollmentService, NodeEnrollmentServiceServer,
};
use crate::agent::v1::{
    EnrollmentChallengeRequest, EnrollmentChallengeResponse, EnrollmentCompleteRequest,
    EnrollmentCompleteResponse,
};
use crate::config::AgentConfig;
use crate::intents::leases::{Host, System};

pub(crate) mod der;
pub(crate) mod handover;

/// The TLS files of the agent and the token that enrolls them.
pub(crate) struct Files {
    pub(crate) key: PathBuf,
    pub(crate) certificate: PathBuf,
    pub(crate) ca: PathBuf,
    pub(crate) token: PathBuf,
    /// The node key the installer replaced, if any.
    pub(crate) previous_node: PathBuf,
    pub(crate) journal: PathBuf,
    pub(crate) state: PathBuf,
}

impl Files {
    pub(crate) fn of(config: &AgentConfig) -> Self {
        let certificate = PathBuf::from(&config.tls_certificate_path);
        Self {
            key: config.tls_key_path.clone().into(),
            ca: config.tls_client_ca_path.clone().into(),
            token: certificate.with_file_name("enrollment.token"),
            previous_node: certificate.with_file_name("enrollment.previous-node"),
            journal: Path::new(&config.node_env_path).with_file_name("profile-intents.sqlite3"),
            state: config.state_dir.clone().into(),
            certificate,
        }
    }
}

/// Whether the agent waits for the controller instead of serving.
pub(crate) fn pending(config: &AgentConfig) -> bool {
    Files::of(config).token.exists()
}

/// `<expiry unix seconds>.<64 hex>`, as the controller issues it.
#[derive(Debug, PartialEq, Eq)]
pub(crate) struct Token {
    expires: i64,
    secret: Vec<u8>,
}

impl Token {
    pub(crate) fn parse(text: &str) -> Result<Self, String> {
        let invalid = || "invalid enrollment token".to_string();
        let (expires, secret) = text.trim().split_once('.').ok_or_else(invalid)?;
        let expires = expires.parse::<i64>().map_err(|_| invalid())?;
        if secret.len() != 64 || !secret.bytes().all(|c| c.is_ascii_hexdigit()) {
            return Err(invalid());
        }
        let secret = (0..64)
            .step_by(2)
            .map(|index| u8::from_str_radix(&secret[index..index + 2], 16))
            .collect::<Result<Vec<_>, _>>()
            .map_err(|_| invalid())?;
        Ok(Self { expires, secret })
    }

    fn check(&self, now: i64) -> Result<(), Status> {
        if now >= self.expires {
            return Err(Status::permission_denied("enrollment token expired"));
        }
        Ok(())
    }

    /// The role and the length-prefixed fields a proof covers.
    fn message(role: &str, fields: &[&[u8]]) -> Vec<u8> {
        let mut message = role.as_bytes().to_vec();
        message.push(0);
        for field in fields {
            message.extend((field.len() as u32).to_be_bytes());
            message.extend(*field);
        }
        message
    }

    /// HMAC-SHA256 of the message under the secret.
    fn proof(&self, role: &str, fields: &[&[u8]]) -> hmac::Tag {
        let key = hmac::Key::new(hmac::HMAC_SHA256, &self.secret);
        hmac::sign(&key, &Self::message(role, fields))
    }

    fn verify(&self, role: &str, fields: &[&[u8]], proof: &[u8]) -> Result<(), Status> {
        let key = hmac::Key::new(hmac::HMAC_SHA256, &self.secret);
        hmac::verify(&key, &Self::message(role, fields), proof)
            .map_err(|_| Status::permission_denied("enrollment proof rejected"))
    }
}

/// The agent's P-256 key: kept across restarts, never sent anywhere.
pub(crate) fn key(path: &Path) -> Result<Vec<u8>, String> {
    let rng = SystemRandom::new();
    let pkcs8 = match fs::read_to_string(path) {
        Ok(text) => der::unpem("PRIVATE KEY", &text).ok_or("unreadable agent key")?,
        Err(err) if err.kind() == std::io::ErrorKind::NotFound => {
            let pkcs8 = EcdsaKeyPair::generate_pkcs8(&ECDSA_P256_SHA256_ASN1_SIGNING, &rng)
                .map_err(|_| "agent key generation failed")?;
            write_private(path, der::pem("PRIVATE KEY", pkcs8.as_ref()).as_bytes())?;
            pkcs8.as_ref().to_vec()
        }
        Err(err) => return Err(format!("agent key: {err}")),
    };
    let pair = EcdsaKeyPair::from_pkcs8(&ECDSA_P256_SHA256_ASN1_SIGNING, &pkcs8, &rng)
        .map_err(|_| "agent key is not a P-256 key")?;
    Ok(der::p256_key(pair.public_key().as_ref()))
}

fn write_private(path: &Path, data: &[u8]) -> Result<(), String> {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent).map_err(|err| err.to_string())?;
    }
    let mut file = fs::OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(0o600)
        .open(path)
        .map_err(|err| format!("{}: {err}", path.display()))?;
    file.write_all(data)
        .and_then(|()| file.sync_all())
        .map_err(|err| err.to_string())
}

/// Replaces `path` whole, so a crash leaves the old or the new file.
fn replace(path: &Path, data: &[u8]) -> Result<(), String> {
    let temporary = path.with_extension("new");
    let _ = fs::remove_file(&temporary);
    let mut file = fs::OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(0o644)
        .open(&temporary)
        .map_err(|err| err.to_string())?;
    file.write_all(data)
        .and_then(|()| file.sync_all())
        .and_then(|()| fs::rename(&temporary, path))
        .map_err(|err| format!("{}: {err}", path.display()))
}

/// Allows only `peer` to the agent port and drops the installer's open
/// rule. Returns what was done; enrollment succeeds either way.
pub(crate) fn restrict(host: &dyn Host, peer: Option<IpAddr>, port: u16) -> String {
    if !host.installed("ufw") {
        return "ufw is not installed".into();
    }
    let Ok(status) = host.check(&["env", "LC_ALL=C", "ufw", "status"], Some(30)) else {
        return "ufw status unavailable; the agent port is unchanged".into();
    };
    if !status.starts_with("Status: active") {
        return "ufw is inactive".into();
    }
    let Some(peer) = peer.map(|ip| ip.to_canonical()) else {
        return "controller address unknown; the agent port stays open".into();
    };
    let (peer, port) = (peer.to_string(), port.to_string());
    let allow = [
        "ufw",
        "allow",
        "proto",
        "tcp",
        "from",
        &peer,
        "to",
        "any",
        "port",
        &port,
        "comment",
        "Node Plane agent",
    ];
    if !host.succeeds(&allow, Some(30)) {
        return "could not allow the controller; the agent port stays open".into();
    }
    let open = format!("{port}/tcp");
    host.succeeds(&["ufw", "delete", "allow", &open], Some(30));
    format!("agent port {port} allowed from {peer} only")
}

/// The firewall step: `restrict` on the real host, a recorder in tests.
type Firewall = fn(Option<IpAddr>, u16) -> String;

struct Enrollment {
    node_key: String,
    firewall: Firewall,
    token: Token,
    public_key: Vec<u8>,
    previous: handover::Previous,
    files: Files,
    port: u16,
    done: Mutex<Option<oneshot::Sender<()>>>,
}

fn now() -> i64 {
    chrono::Utc::now().timestamp()
}

#[tonic::async_trait]
impl NodeEnrollmentService for Enrollment {
    async fn challenge(
        &self,
        request: Request<EnrollmentChallengeRequest>,
    ) -> Result<Response<EnrollmentChallengeResponse>, Status> {
        self.token.check(now())?;
        let nonce = request.into_inner().nonce;
        if !(16..=64).contains(&nonce.len()) {
            return Err(Status::invalid_argument("invalid enrollment nonce"));
        }
        let previous = &self.previous;
        let proof = self.token.proof(
            "agent",
            &[
                self.node_key.as_bytes(),
                &nonce,
                &self.public_key,
                previous.node_key.as_bytes(),
                previous.controller.as_bytes(),
                previous.ca_sha256.as_bytes(),
            ],
        );
        Ok(Response::new(EnrollmentChallengeResponse {
            node_key: self.node_key.clone(),
            public_key: self.public_key.clone(),
            proof: proof.as_ref().to_vec(),
            previous_node_key: previous.node_key.clone(),
            previous_controller: previous.controller.clone(),
            previous_ca_sha256: previous.ca_sha256.clone(),
        }))
    }

    async fn complete(
        &self,
        request: Request<EnrollmentCompleteRequest>,
    ) -> Result<Response<EnrollmentCompleteResponse>, Status> {
        self.token.check(now())?;
        let peer = request.remote_addr().map(|addr| addr.ip());
        let request = request.into_inner();
        self.token.verify(
            "controller",
            &[
                self.node_key.as_bytes(),
                request.controller_id.as_bytes(),
                request.ca_pem.as_bytes(),
                request.certificate_pem.as_bytes(),
            ],
            &request.proof,
        )?;
        let certificate = der::unpem("CERTIFICATE", &request.certificate_pem)
            .ok_or_else(|| Status::invalid_argument("invalid certificate"))?;
        if der::certificate_key(&certificate) != Some(&self.public_key[..]) {
            return Err(Status::invalid_argument(
                "certificate is not for this agent's key",
            ));
        }
        if der::unpem("CERTIFICATE", &request.ca_pem)
            .and_then(|ca| der::certificate_key(&ca).map(<[u8]>::len))
            .is_none()
        {
            return Err(Status::invalid_argument("invalid CA certificate"));
        }
        let archive = handover::hand_over(
            &self.previous,
            &request.controller_id,
            &crate::protocols::sha256_hex(request.ca_pem.as_bytes()),
            &self.node_key,
            &self.files.journal,
            &self.files.state,
            &chrono::Utc::now().format("%Y%m%dT%H%M%SZ").to_string(),
        )
        .map_err(Status::failed_precondition)?;
        let store =
            |path: &Path, text: &str| replace(path, text.as_bytes()).map_err(Status::internal);
        store(&self.files.ca, &request.ca_pem)?;
        store(&self.files.certificate, &request.certificate_pem)?;
        fs::remove_file(&self.files.token).map_err(|err| Status::internal(err.to_string()))?;
        let _ = fs::remove_file(&self.files.previous_node);
        let (port, restrict) = (self.port, self.firewall);
        let firewall = tokio::task::spawn_blocking(move || restrict(peer, port))
            .await
            .unwrap_or_else(|_| "firewall step failed; the agent port is unchanged".into());
        if let Some(done) = self.done.lock().ok().and_then(|mut done| done.take()) {
            let _ = done.send(());
        }
        Ok(Response::new(EnrollmentCompleteResponse {
            firewall,
            journal_archive: archive
                .map(|path| path.display().to_string())
                .unwrap_or_default(),
        }))
    }
}

/// Serves enrollment until the controller completes it.
pub(crate) async fn serve(config: &AgentConfig) -> Result<(), Box<dyn std::error::Error>> {
    serve_with(config, |peer, port| restrict(&System, peer, port)).await
}

async fn serve_with(
    config: &AgentConfig,
    firewall: Firewall,
) -> Result<(), Box<dyn std::error::Error>> {
    let files = Files::of(config);
    let token = Token::parse(&fs::read_to_string(&files.token)?)?;
    let public_key = key(&files.key)?;
    let previous = handover::previous(&files.previous_node, &files.ca, &files.state);
    let addr: std::net::SocketAddr = config.listen_addr.parse()?;
    let (done, finished) = oneshot::channel();
    let enrollment = Enrollment {
        node_key: config.node_key.clone(),
        firewall,
        token,
        public_key,
        previous,
        files,
        port: addr.port(),
        done: Mutex::new(Some(done)),
    };
    println!(
        "waiting for the controller to enroll node {}",
        config.node_key
    );
    tonic::transport::Server::builder()
        .add_service(NodeEnrollmentServiceServer::new(enrollment))
        .serve_with_shutdown(addr, async {
            let _ = finished.await;
            // Let the reply reach the controller before the listener closes.
            tokio::time::sleep(std::time::Duration::from_millis(200)).await;
        })
        .await?;
    println!("node {} enrolled", config.node_key);
    Ok(())
}

/// A fresh nonce, for the controller side of tests.
#[cfg(test)]
fn nonce() -> Vec<u8> {
    use ring::rand::SecureRandom;
    let mut nonce = vec![0; 32];
    SystemRandom::new().fill(&mut nonce).unwrap();
    nonce
}

#[cfg(test)]
mod tests;

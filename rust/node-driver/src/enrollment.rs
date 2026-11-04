//! The controller side of agent enrollment (see the agent's `enrollment`
//! module): check that the agent holds the token, issue a certificate for
//! the key it generated, and remember where it listens.
use std::collections::HashMap;
use std::fs;
use std::path::{Path, PathBuf};
use std::process::Command;
use std::time::Duration;

use ring::hmac;
use ring::rand::{SecureRandom, SystemRandom};
use tonic::Status;

use crate::agent::v1::node_enrollment_service_client::NodeEnrollmentServiceClient;
use crate::agent::v1::{EnrollmentChallengeRequest, EnrollmentCompleteRequest};

/// `<expiry unix seconds>.<64 hex>`.
pub(crate) struct Token {
    expires: i64,
    secret: Vec<u8>,
}

impl Token {
    pub(crate) fn parse(text: &str) -> Result<Self, Status> {
        let invalid = || Status::invalid_argument("invalid enrollment token");
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

    /// The same message layout as the agent: role, NUL, then each field
    /// prefixed by its 32-bit length.
    fn message(role: &str, fields: &[&[u8]]) -> Vec<u8> {
        let mut message = role.as_bytes().to_vec();
        message.push(0);
        for field in fields {
            message.extend((field.len() as u32).to_be_bytes());
            message.extend(*field);
        }
        message
    }

    fn key(&self) -> hmac::Key {
        hmac::Key::new(hmac::HMAC_SHA256, &self.secret)
    }

    fn proof(&self, role: &str, fields: &[&[u8]]) -> Vec<u8> {
        hmac::sign(&self.key(), &Self::message(role, fields))
            .as_ref()
            .to_vec()
    }

    fn verify(&self, role: &str, fields: &[&[u8]], proof: &[u8]) -> bool {
        hmac::verify(&self.key(), &Self::message(role, fields), proof).is_ok()
    }
}

/// `host:port` of an agent: the certificate names the host.
pub(crate) fn subject_name(target: &str) -> Result<String, Status> {
    let invalid = || Status::invalid_argument("invalid agent target");
    let (host, port) = target.rsplit_once(':').ok_or_else(invalid)?;
    port.parse::<u16>()
        .ok()
        .filter(|port| *port > 0)
        .ok_or_else(invalid)?;
    let host = host.trim_start_matches('[').trim_end_matches(']');
    if let Ok(address) = host.parse::<std::net::IpAddr>() {
        return Ok(format!("IP:{address}"));
    }
    let label = |label: &str| {
        !label.is_empty()
            && label.len() <= 63
            && label
                .bytes()
                .all(|c| c.is_ascii_alphanumeric() || c == b'-')
            && !label.starts_with('-')
            && !label.ends_with('-')
    };
    if host.len() <= 253 && host.split('.').all(label) {
        return Ok(format!("DNS:{host}"));
    }
    Err(invalid())
}

fn pem(label: &str, der: &[u8]) -> String {
    const ALPHABET: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    let mut body = String::new();
    for chunk in der.chunks(3) {
        let bytes = [
            chunk[0],
            *chunk.get(1).unwrap_or(&0),
            *chunk.get(2).unwrap_or(&0),
        ];
        let group = u32::from(bytes[0]) << 16 | u32::from(bytes[1]) << 8 | u32::from(bytes[2]);
        for index in 0..4 {
            body.push(if index <= chunk.len() {
                char::from(ALPHABET[(group >> (18 - 6 * index) & 63) as usize])
            } else {
                '='
            });
        }
    }
    let mut text = format!("-----BEGIN {label}-----\n");
    for line in body.as_bytes().chunks(64) {
        text.push_str(std::str::from_utf8(line).expect("base64 is ASCII"));
        text.push('\n');
    }
    text + &format!("-----END {label}-----\n")
}

/// A server certificate for `public_key` (DER SubjectPublicKeyInfo),
/// signed by the CA whose key sits next to its certificate.
pub(crate) fn issue(
    ca: &Path,
    node_key: &str,
    subject: &str,
    public_key: &[u8],
) -> Result<String, Status> {
    let work = tempdir()?;
    let result = (|| {
        fs::write(work.join("agent.pub"), pem("PUBLIC KEY", public_key))?;
        fs::write(
            work.join("server.ext"),
            format!(
                "basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature,keyEncipherment\n\
                 extendedKeyUsage=serverAuth\nsubjectAltName={subject}\n"
            ),
        )?;
        let mut serial = [0u8; 16];
        SystemRandom::new()
            .fill(&mut serial)
            .map_err(|_| std::io::Error::other("no randomness"))?;
        serial[0] &= 0x7f;
        let serial: String = serial.iter().map(|byte| format!("{byte:02x}")).collect();
        let output = Command::new("openssl")
            .args(["x509", "-new", "-force_pubkey"])
            .arg(work.join("agent.pub"))
            .args(["-subj", &format!("/CN={node_key}"), "-CA"])
            .arg(ca)
            .arg("-CAkey")
            .arg(ca.with_file_name("ca.key"))
            .args([
                "-set_serial",
                &format!("0x{serial}"),
                "-days",
                "825",
                "-sha256",
            ])
            .arg("-extfile")
            .arg(work.join("server.ext"))
            .arg("-out")
            .arg(work.join("server.crt"))
            .output()?;
        if !output.status.success() {
            return Err(std::io::Error::other(
                String::from_utf8_lossy(&output.stderr).trim().to_string(),
            ));
        }
        fs::read_to_string(work.join("server.crt"))
    })();
    let _ = fs::remove_dir_all(&work);
    result.map_err(|err| Status::internal(format!("agent certificate not issued: {err}")))
}

fn tempdir() -> Result<PathBuf, Status> {
    let mut suffix = [0u8; 8];
    SystemRandom::new()
        .fill(&mut suffix)
        .map_err(|_| Status::internal("no randomness"))?;
    let suffix: String = suffix.iter().map(|byte| format!("{byte:02x}")).collect();
    let path = std::env::temp_dir().join(format!("node-plane-enroll-{suffix}"));
    std::fs::DirBuilder::new()
        .mode(0o700)
        .create(&path)
        .map_err(|err| Status::internal(err.to_string()))?;
    Ok(path)
}

use std::os::unix::fs::DirBuilderExt;

/// What the agent did when it was enrolled.
pub(crate) struct Enrolled {
    pub(crate) firewall: String,
    pub(crate) journal_archive: String,
}

/// Who asks for the agent.
pub(crate) struct Controller<'a> {
    pub(crate) id: &'a str,
    pub(crate) retired_node_keys: &'a [String],
}

/// Refuses to take the agent of another active node of this controller.
pub(crate) fn check_owner(
    controller: &Controller,
    ca_sha256: &str,
    node_key: &str,
    previous_node_key: &str,
    previous_controller: &str,
    previous_ca_sha256: &str,
) -> Result<(), Status> {
    let same = if previous_controller.is_empty() {
        !previous_ca_sha256.is_empty() && previous_ca_sha256 == ca_sha256
    } else {
        previous_controller == controller.id
    };
    if same
        && !previous_node_key.is_empty()
        && previous_node_key != node_key
        && !controller
            .retired_node_keys
            .iter()
            .any(|key| key == previous_node_key)
    {
        return Err(Status::failed_precondition(format!(
            "the agent belongs to node {previous_node_key} of this controller"
        )));
    }
    Ok(())
}

/// Enrolls the agent at `target`.
pub(crate) async fn enroll(
    ca: &Path,
    controller: &Controller<'_>,
    node_key: &str,
    target: &str,
    token: &Token,
    now: i64,
) -> Result<Enrolled, Status> {
    if now >= token.expires {
        return Err(Status::permission_denied("enrollment token expired"));
    }
    let subject = subject_name(target)?;
    let endpoint = tonic::transport::Endpoint::from_shared(format!("http://{target}"))
        .map_err(|_| Status::invalid_argument("invalid agent target"))?
        .connect_timeout(Duration::from_secs(5))
        .timeout(Duration::from_secs(30));
    let channel = endpoint
        .connect()
        .await
        .map_err(|_| Status::unavailable("agent is not waiting for enrollment"))?;
    let mut client = NodeEnrollmentServiceClient::new(channel);
    let mut nonce = vec![0; 32];
    SystemRandom::new()
        .fill(&mut nonce)
        .map_err(|_| Status::internal("no randomness"))?;
    let challenge = client
        .challenge(EnrollmentChallengeRequest {
            nonce: nonce.clone(),
        })
        .await
        .map_err(|status| match status.code() {
            // Nothing answered on the connection.
            tonic::Code::Unknown => Status::unavailable("agent is not waiting for enrollment"),
            _ => status,
        })?
        .into_inner();
    if challenge.node_key != node_key {
        return Err(Status::failed_precondition(
            "the agent was installed for another node",
        ));
    }
    if !token.verify(
        "agent",
        &[
            node_key.as_bytes(),
            &nonce,
            &challenge.public_key,
            challenge.previous_node_key.as_bytes(),
            challenge.previous_controller.as_bytes(),
            challenge.previous_ca_sha256.as_bytes(),
        ],
        &challenge.proof,
    ) {
        return Err(Status::permission_denied(
            "the agent does not hold this node's enrollment token",
        ));
    }
    let ca_pem = fs::read_to_string(ca)
        .map_err(|err| Status::failed_precondition(format!("agent CA unavailable: {err}")))?;
    let ca_sha256: String = ring::digest::digest(&ring::digest::SHA256, ca_pem.as_bytes())
        .as_ref()
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect();
    check_owner(
        controller,
        &ca_sha256,
        node_key,
        &challenge.previous_node_key,
        &challenge.previous_controller,
        &challenge.previous_ca_sha256,
    )?;
    let certificate_pem = issue(ca, node_key, &subject, &challenge.public_key)?;
    let proof = token.proof(
        "controller",
        &[
            node_key.as_bytes(),
            controller.id.as_bytes(),
            ca_pem.as_bytes(),
            certificate_pem.as_bytes(),
        ],
    );
    let completed = client
        .complete(EnrollmentCompleteRequest {
            ca_pem,
            certificate_pem,
            proof,
            controller_id: controller.id.to_string(),
        })
        .await?
        .into_inner();
    Ok(Enrolled {
        firewall: completed.firewall,
        journal_archive: completed.journal_archive,
    })
}

/// Agents enrolled by token, `node_key=target` per line, next to the CA.
pub(crate) fn registry(ca: &Path) -> PathBuf {
    ca.with_file_name("enrolled-agents")
}

pub(crate) fn load(path: &Path) -> HashMap<String, String> {
    fs::read_to_string(path)
        .unwrap_or_default()
        .lines()
        .filter_map(|line| {
            let (node_key, target) = line.split_once('=')?;
            Some((node_key.to_string(), target.to_string()))
        })
        .collect()
}

pub(crate) fn save(path: &Path, targets: &HashMap<String, String>) -> std::io::Result<()> {
    let mut lines: Vec<String> = targets
        .iter()
        .map(|(node_key, target)| format!("{node_key}={target}\n"))
        .collect();
    lines.sort();
    let temporary = path.with_extension("new");
    fs::write(&temporary, lines.concat())?;
    fs::File::open(&temporary)?.sync_all()?;
    fs::rename(temporary, path)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn targets_name_the_certificate_subject() {
        assert_eq!(subject_name("203.0.113.5:50061").unwrap(), "IP:203.0.113.5");
        assert_eq!(
            subject_name("[2001:db8::1]:50061").unwrap(),
            "IP:2001:db8::1"
        );
        assert_eq!(
            subject_name("node.example:50061").unwrap(),
            "DNS:node.example"
        );
        for bad in [
            "node.example",
            "a b:1",
            "-x:1",
            "x:0",
            "x:65536",
            "x,DNS:y:1",
        ] {
            assert!(subject_name(bad).is_err(), "{bad}");
        }
    }

    #[test]
    fn proofs_match_the_agent_layout() {
        let token = Token::parse(&format!("1.{}", "ab".repeat(32))).unwrap();
        let proof = token.proof("agent", &[b"n1", b"x"]);
        assert!(token.verify("agent", &[b"n1", b"x"], &proof));
        assert!(!token.verify("agent", &[b"n1x", b""], &proof));
        // HMAC-SHA256(key=0xab*32, "agent\0" 00000002 "n1" 00000001 "x")
        let key = hmac::Key::new(hmac::HMAC_SHA256, &[0xab; 32]);
        let expected = hmac::sign(&key, b"agent\0\0\0\0\x02n1\0\0\0\x01x");
        assert_eq!(proof, expected.as_ref());
    }

    #[test]
    fn an_agent_of_another_active_node_is_not_taken() {
        let retired = ["n0".to_string()];
        let controller = Controller {
            id: "c1",
            retired_node_keys: &retired,
        };
        let check = |previous: &str, by: &str, ca: &str| {
            check_owner(&controller, "ca", "n1", previous, by, ca).is_ok()
        };
        assert!(check("", "", ""));
        assert!(check("n1", "c1", ""));
        assert!(!check("n2", "c1", ""));
        // A legacy agent is ours when it trusts our CA.
        assert!(!check("n2", "", "ca"));
        assert!(check("n2", "", "other"));
        // Another controller's node, or one we retired, is handed over.
        assert!(check("n2", "c2", "ca"));
        assert!(check("n0", "c1", ""));
    }

    #[test]
    fn the_registry_survives_a_restart() {
        let path = std::env::temp_dir().join(format!("np-enrolled-{}", std::process::id()));
        let targets = HashMap::from([
            ("b".to_string(), "198.51.100.2:50061".to_string()),
            ("a".to_string(), "node.example:50061".to_string()),
        ]);
        save(&path, &targets).unwrap();
        assert_eq!(
            fs::read_to_string(&path).unwrap(),
            "a=node.example:50061\nb=198.51.100.2:50061\n"
        );
        assert_eq!(load(&path), targets);
        fs::remove_file(path).unwrap();
    }
}

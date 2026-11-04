use std::net::{Ipv4Addr, SocketAddr, TcpListener};
use std::os::unix::fs::PermissionsExt;
use std::process::Command;

use tonic::transport::{Certificate, ClientTlsConfig, Endpoint, Identity, ServerTlsConfig};

use super::*;
use crate::agent::v1::node_enrollment_service_client::NodeEnrollmentServiceClient;
use crate::intents::leases::fake::Scripted;

const SECRET: &str = "00112233445566778899aabbccddeeff00112233445566778899aabbccddeeff";

fn token(expires: i64) -> String {
    format!("{expires}.{SECRET}")
}

#[test]
fn tokens_carry_an_expiry_and_a_256_bit_secret() {
    let parsed = Token::parse(&format!("{}\n", token(1_900_000_000))).unwrap();
    assert_eq!(parsed.expires, 1_900_000_000);
    assert_eq!(parsed.secret.len(), 32);
    assert!(parsed.check(1_899_999_999).is_ok());
    assert!(parsed.check(1_900_000_000).is_err());
    for bad in [
        "",
        "1.abc",
        &format!("x.{SECRET}"),
        &format!("1.{}", &SECRET[1..]),
    ] {
        assert!(Token::parse(bad).is_err(), "{bad}");
    }
}

#[test]
fn proofs_bind_the_role_and_every_field() {
    let token = Token::parse(&token(i64::MAX)).unwrap();
    let proof = token.proof("agent", &[b"n1", b"nonce", b"key"]);
    assert!(
        token
            .verify("agent", &[b"n1", b"nonce", b"key"], proof.as_ref())
            .is_ok()
    );
    assert!(
        token
            .verify("controller", &[b"n1", b"nonce", b"key"], proof.as_ref())
            .is_err()
    );
    assert!(
        token
            .verify("agent", &[b"n1", b"noncek", b"ey"], proof.as_ref())
            .is_err()
    );
    let other = Token::parse(&format!("1.{}", SECRET.replace('0', "1"))).unwrap();
    assert!(
        other
            .verify("agent", &[b"n1", b"nonce", b"key"], proof.as_ref())
            .is_err()
    );
}

#[test]
fn the_key_is_generated_once_and_private() {
    let dir = crate::protocols::test_dir("enrollment-key");
    let path = dir.join("tls/server.key");
    let first = key(&path).unwrap();
    assert_eq!(first.len(), 91);
    assert_eq!(
        fs::metadata(&path).unwrap().permissions().mode() & 0o777,
        0o600
    );
    assert_eq!(key(&path).unwrap(), first);
    fs::write(&path, "not a key").unwrap();
    assert!(key(&path).is_err());
}

#[test]
fn only_the_controller_keeps_the_agent_port() {
    let host = |status: &'static str| {
        Scripted::new(move |args| match args {
            ["env", "LC_ALL=C", "ufw", "status"] => (0, status.into()),
            _ => (0, String::new()),
        })
    };
    let active = host("Status: active\n\nTo Action From\n");
    let peer = "::ffff:198.51.100.7".parse().ok();
    assert_eq!(
        restrict(&active, peer, 50061),
        "agent port 50061 allowed from 198.51.100.7 only"
    );
    assert_eq!(active.ran(&["ufw", "allow"]).len(), 1);
    assert!(active.ran(&["ufw", "allow"])[0].contains(&"198.51.100.7".to_string()));
    assert_eq!(
        active.ran(&["ufw", "delete", "allow", "50061/tcp"]).len(),
        1
    );

    let inactive = host("Status: inactive\n");
    assert_eq!(restrict(&inactive, peer, 50061), "ufw is inactive");
    assert!(inactive.ran(&["ufw", "allow"]).is_empty());
    let unknown = host("Status: active\n");
    assert!(restrict(&unknown, None, 50061).contains("stays open"));
    assert!(unknown.ran(&["ufw", "delete"]).is_empty());
}

fn openssl(dir: &Path, args: &[&str]) {
    let output = Command::new("openssl")
        .args(args)
        .current_dir(dir)
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
}

/// What the controller does with the agent's public key.
fn issue(dir: &Path, public_key: &[u8]) -> (String, String) {
    fs::write(dir.join("agent.pub"), der::pem("PUBLIC KEY", public_key)).unwrap();
    openssl(
        dir,
        &[
            "req",
            "-x509",
            "-newkey",
            "ec",
            "-pkeyopt",
            "ec_paramgen_curve:P-256",
            "-nodes",
            "-keyout",
            "ca.key",
            "-out",
            "ca.crt",
            "-days",
            "1",
            "-subj",
            "/CN=test CA",
            "-addext",
            "basicConstraints=critical,CA:TRUE",
            "-addext",
            "keyUsage=critical,keyCertSign",
        ],
    );
    fs::write(
        dir.join("server.ext"),
        "basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature\n\
         extendedKeyUsage=serverAuth\nsubjectAltName=DNS:localhost\n",
    )
    .unwrap();
    openssl(
        dir,
        &[
            "x509",
            "-new",
            "-force_pubkey",
            "agent.pub",
            "-subj",
            "/CN=n1",
            "-CA",
            "ca.crt",
            "-CAkey",
            "ca.key",
            "-days",
            "1",
            "-extfile",
            "server.ext",
            "-out",
            "agent.crt",
        ],
    );
    (
        fs::read_to_string(dir.join("ca.crt")).unwrap(),
        fs::read_to_string(dir.join("agent.crt")).unwrap(),
    )
}

fn free_port() -> u16 {
    TcpListener::bind((Ipv4Addr::LOCALHOST, 0))
        .unwrap()
        .local_addr()
        .unwrap()
        .port()
}

#[tokio::test]
async fn the_controller_enrolls_the_agent_and_its_key_serves_mutual_tls() {
    let dir = crate::protocols::test_dir("enrollment-flow");
    let tls = dir.join("tls");
    fs::create_dir_all(&tls).unwrap();
    let port = free_port();
    let config = AgentConfig {
        node_key: "n1".into(),
        listen_addr: format!("127.0.0.1:{port}"),
        tls_certificate_path: tls.join("server.crt").display().to_string(),
        tls_key_path: tls.join("server.key").display().to_string(),
        tls_client_ca_path: tls.join("ca.crt").display().to_string(),
        ..AgentConfig::default()
    };
    fs::write(tls.join("enrollment.token"), token(now() + 600)).unwrap();
    assert!(pending(&config));
    let served = {
        let config = config.clone();
        tokio::spawn(async move {
            serve_with(&config, |peer, port| format!("{peer:?} {port}"))
                .await
                .map_err(|err| err.to_string())
        })
    };

    let endpoint = format!("http://127.0.0.1:{port}");
    let mut client = loop {
        match NodeEnrollmentServiceClient::connect(endpoint.clone()).await {
            Ok(client) => break client,
            Err(_) => tokio::time::sleep(std::time::Duration::from_millis(20)).await,
        }
    };
    let controller = Token::parse(&token(i64::MAX)).unwrap();
    let sent = nonce();
    let challenge = client
        .challenge(EnrollmentChallengeRequest {
            nonce: sent.clone(),
        })
        .await
        .unwrap()
        .into_inner();
    assert_eq!(challenge.node_key, "n1");
    controller
        .verify(
            "agent",
            &[b"n1", &sent, &challenge.public_key],
            &challenge.proof,
        )
        .unwrap();
    assert!(
        client
            .challenge(EnrollmentChallengeRequest { nonce: vec![1; 8] })
            .await
            .is_err()
    );

    let (ca_pem, certificate_pem) = issue(&dir, &challenge.public_key);
    let complete =
        |ca_pem: &str, certificate_pem: &str, proof: Vec<u8>| EnrollmentCompleteRequest {
            ca_pem: ca_pem.into(),
            certificate_pem: certificate_pem.into(),
            proof,
        };
    let proof = |ca: &str, cert: &str| {
        controller
            .proof("controller", &[b"n1", ca.as_bytes(), cert.as_bytes()])
            .as_ref()
            .to_vec()
    };
    // Someone without the token cannot install a certificate.
    let forged = client
        .complete(complete(&ca_pem, &certificate_pem, vec![0; 32]))
        .await
        .unwrap_err();
    assert_eq!(forged.code(), tonic::Code::PermissionDenied);
    // A certificate for another key is refused even with the token.
    let elsewhere = crate::protocols::test_dir("enrollment-other");
    let other = issue(&elsewhere, &key(&elsewhere.join("other.key")).unwrap());
    let mismatched = client
        .complete(complete(&other.0, &other.1, proof(&other.0, &other.1)))
        .await
        .unwrap_err();
    assert_eq!(mismatched.code(), tonic::Code::InvalidArgument);
    assert!(!tls.join("server.crt").exists());

    client
        .complete(complete(
            &ca_pem,
            &certificate_pem,
            proof(&ca_pem, &certificate_pem),
        ))
        .await
        .unwrap();
    served.await.unwrap().unwrap();
    assert!(!pending(&config));
    assert_eq!(fs::read_to_string(tls.join("ca.crt")).unwrap(), ca_pem);

    // The kept key and the issued certificate serve mutual TLS to a client
    // of the same CA.
    fs::write(
        dir.join("client.ext"),
        "basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature\n\
         extendedKeyUsage=clientAuth\n",
    )
    .unwrap();
    openssl(
        &dir,
        &[
            "req",
            "-new",
            "-newkey",
            "ec",
            "-pkeyopt",
            "ec_paramgen_curve:P-256",
            "-nodes",
            "-keyout",
            "client.key",
            "-out",
            "client.csr",
            "-subj",
            "/CN=driver",
        ],
    );
    openssl(
        &dir,
        &[
            "x509",
            "-req",
            "-in",
            "client.csr",
            "-CA",
            "ca.crt",
            "-CAkey",
            "ca.key",
            "-days",
            "1",
            "-extfile",
            "client.ext",
            "-out",
            "client.crt",
        ],
    );
    let identity = Identity::from_pem(
        fs::read(tls.join("server.crt")).unwrap(),
        fs::read(tls.join("server.key")).unwrap(),
    );
    let tls_port = free_port();
    let server = tonic::transport::Server::builder()
        .tls_config(
            ServerTlsConfig::new()
                .identity(identity)
                .client_ca_root(Certificate::from_pem(&ca_pem)),
        )
        .unwrap()
        .add_service(NodeEnrollmentServiceServer::new(Enrollment {
            node_key: "n1".into(),
            firewall: |_, _| String::new(),
            token: Token::parse(&token(i64::MAX)).unwrap(),
            public_key: challenge.public_key.clone(),
            files: Files::of(&config),
            port: tls_port,
            done: Mutex::new(None),
        }))
        .serve(SocketAddr::from((Ipv4Addr::LOCALHOST, tls_port)));
    let server = tokio::spawn(server);
    let channel = Endpoint::from_shared(format!("https://localhost:{tls_port}"))
        .unwrap()
        .tls_config(
            ClientTlsConfig::new()
                .ca_certificate(Certificate::from_pem(&ca_pem))
                .identity(Identity::from_pem(
                    fs::read(dir.join("client.crt")).unwrap(),
                    fs::read(dir.join("client.key")).unwrap(),
                ))
                .domain_name("localhost"),
        )
        .unwrap();
    let channel = loop {
        match channel.connect().await {
            Ok(channel) => break channel,
            Err(_) => tokio::time::sleep(std::time::Duration::from_millis(20)).await,
        }
    };
    let reply = NodeEnrollmentServiceClient::new(channel)
        .challenge(EnrollmentChallengeRequest { nonce: nonce() })
        .await
        .unwrap();
    assert_eq!(reply.into_inner().public_key, challenge.public_key);
    server.abort();
}

//! How `serve` meets browsers: its own TLS (an ACME certificate or one from
//! files) or plain HTTP behind a reverse proxy that holds the certificate.
//! The cabinet and the controller's link share the port either way.
use std::{path::PathBuf, sync::Arc, time::Duration};

use axum::Router;
use hyper_util::{
    rt::{TokioExecutor, TokioIo},
    server::conn::auto,
    service::TowerToHyperService,
};
use rustls_acme::{AcmeConfig, caches::DirCache};
use tokio::net::TcpListener;
use tokio_rustls::{
    TlsAcceptor,
    rustls::{ServerConfig, crypto::ring::default_provider},
};
use tokio_stream::StreamExt;

/// Negotiated only by the ACME server's TLS-ALPN-01 validation.
const ACME_TLS_ALPN: &[u8] = b"acme-tls/1";
const HANDSHAKE_TIMEOUT: Duration = Duration::from_secs(10);

#[derive(Clone, Debug)]
pub enum Mode {
    /// Plain HTTP/1.1 and h2c; a reverse proxy in front holds TLS.
    Proxy,
    /// A certificate chain and key in PEM files; read at start.
    Files { certificate: PathBuf, key: PathBuf },
    /// A Let's Encrypt certificate for `domain`, validated over TLS-ALPN-01,
    /// so the relay must be reachable on port 443. Renewed in the background.
    Acme {
        domain: String,
        contact: Option<String>,
        cache: PathBuf,
        staging: bool,
    },
}

pub async fn serve(listener: TcpListener, router: Router, mode: Mode) -> Result<(), String> {
    let config = match mode {
        Mode::Proxy => {
            return axum::serve(listener, router)
                .await
                .map_err(|e| e.to_string());
        }
        Mode::Files { certificate, key } => files(&certificate, &key)?,
        Mode::Acme {
            domain,
            contact,
            cache,
            staging,
        } => acme(domain, contact, cache, staging)?,
    };
    let acceptor = TlsAcceptor::from(Arc::new(config));
    loop {
        let (stream, _) = match listener.accept().await {
            Ok(accepted) => accepted,
            Err(error) => {
                // Out of descriptors and the like: wait instead of spinning.
                eprintln!("accept: {error}");
                tokio::time::sleep(Duration::from_millis(100)).await;
                continue;
            }
        };
        let (acceptor, router) = (acceptor.clone(), router.clone());
        tokio::spawn(async move {
            let Ok(Ok(stream)) =
                tokio::time::timeout(HANDSHAKE_TIMEOUT, acceptor.accept(stream)).await
            else {
                return;
            };
            if stream.get_ref().1.alpn_protocol() == Some(ACME_TLS_ALPN) {
                // The handshake itself answered the validation.
                return;
            }
            let service = TowerToHyperService::new(router);
            let _ = auto::Builder::new(TokioExecutor::new())
                .serve_connection(TokioIo::new(stream), service)
                .await;
        });
    }
}

fn builder() -> Result<
    tokio_rustls::rustls::ConfigBuilder<ServerConfig, tokio_rustls::rustls::WantsVerifier>,
    String,
> {
    ServerConfig::builder_with_provider(Arc::new(default_provider()))
        .with_safe_default_protocol_versions()
        .map_err(|e| e.to_string())
}

fn alpn(mut config: ServerConfig, acme: bool) -> ServerConfig {
    config.alpn_protocols = vec![b"h2".to_vec(), b"http/1.1".to_vec()];
    if acme {
        config.alpn_protocols.push(ACME_TLS_ALPN.to_vec());
    }
    config
}

fn files(certificate: &PathBuf, key: &PathBuf) -> Result<ServerConfig, String> {
    let read = |path: &PathBuf| std::fs::read(path).map_err(|e| format!("{}: {e}", path.display()));
    let chain = rustls_pemfile::certs(&mut read(certificate)?.as_slice())
        .collect::<Result<Vec<_>, _>>()
        .map_err(|e| format!("{}: {e}", certificate.display()))?;
    if chain.is_empty() {
        return Err(format!("{}: no certificate", certificate.display()));
    }
    let private = rustls_pemfile::private_key(&mut read(key)?.as_slice())
        .map_err(|e| format!("{}: {e}", key.display()))?
        .ok_or_else(|| format!("{}: no private key", key.display()))?;
    let config = builder()?
        .with_no_client_auth()
        .with_single_cert(chain, private)
        .map_err(|e| format!("{}: {e}", certificate.display()))?;
    Ok(alpn(config, false))
}

fn acme(
    domain: String,
    contact: Option<String>,
    cache: PathBuf,
    staging: bool,
) -> Result<ServerConfig, String> {
    let mut state = AcmeConfig::new([domain])
        .contact(contact.map(|email| format!("mailto:{email}")))
        .cache(DirCache::new(cache))
        .directory_lets_encrypt(!staging)
        .state();
    let resolver = state.resolver();
    tokio::spawn(async move {
        while let Some(event) = state.next().await {
            match event {
                Ok(ok) => eprintln!("acme: {ok:?}"),
                Err(error) => eprintln!("acme: {error}"),
            }
        }
    });
    let config = builder()?
        .with_no_client_auth()
        .with_cert_resolver(resolver);
    Ok(alpn(config, true))
}

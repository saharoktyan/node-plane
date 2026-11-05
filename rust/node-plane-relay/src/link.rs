//! The controller's side: holds the connection to the relay and performs the
//! requests that come over it against the local API.
use std::{sync::Arc, time::Duration};

use axum::{
    body::Bytes,
    http::{HeaderValue, Method, Uri},
};
use http_body_util::{BodyExt, Full, Limited};
use hyper_util::{
    client::legacy::{Client, connect::HttpConnector},
    rt::TokioExecutor,
};
use node_plane_api::{codes, error_body, headers as api_headers};
use tokio::sync::{Semaphore, mpsc};
use tokio_stream::wrappers::ReceiverStream;
use tonic::transport::{ClientTlsConfig, Endpoint};
use uuid::Uuid;

use crate::{
    headers,
    proto::{self, Hello, LinkMessage, link_client::LinkClient, link_message, relay_message},
};

#[derive(Clone, Copy, Debug)]
pub struct Limits {
    pub response_body: usize,
    pub concurrent: usize,
    pub timeout: Duration,
}

impl Default for Limits {
    fn default() -> Self {
        Self {
            response_body: 4 << 20,
            concurrent: 64,
            timeout: Duration::from_secs(30),
        }
    }
}

#[derive(Clone)]
pub struct Config {
    /// `https://cab.example.com`, or `http://` for a relay on this host.
    pub relay: String,
    pub token: String,
    /// The controller API, `http://127.0.0.1:8080`.
    pub api: String,
    pub limits: Limits,
}

type Http = Client<HttpConnector, Full<Bytes>>;

/// Keeps the link up for good, reconnecting with backoff.
pub async fn run(config: Config) {
    let mut delay = Duration::from_secs(1);
    loop {
        let mut established = false;
        match session(&config, &mut established).await {
            Ok(()) => eprintln!("link closed by the relay"),
            Err(error) => eprintln!("link: {error}"),
        }
        if established {
            delay = Duration::from_secs(1);
        }
        tokio::time::sleep(delay).await;
        delay = (delay * 2).min(Duration::from_secs(30));
    }
}

/// One connection to the relay, until it ends.
pub async fn session(config: &Config, established: &mut bool) -> Result<(), String> {
    let mut endpoint = Endpoint::from_shared(config.relay.clone())
        .map_err(|e| e.to_string())?
        .connect_timeout(Duration::from_secs(10))
        .http2_keep_alive_interval(Duration::from_secs(20))
        .keep_alive_timeout(Duration::from_secs(10))
        .keep_alive_while_idle(true);
    if config.relay.starts_with("https://") {
        endpoint = endpoint
            .tls_config(ClientTlsConfig::new().with_webpki_roots())
            .map_err(|e| e.to_string())?;
    }
    let channel = endpoint.connect().await.map_err(|e| e.to_string())?;
    let mut client = LinkClient::new(channel).max_decoding_message_size(2 << 20);
    let (tx, rx) = mpsc::channel(config.limits.concurrent);
    let hello = LinkMessage {
        message: Some(link_message::Message::Hello(Hello {
            version: crate::VERSION.to_string(),
        })),
    };
    tx.send(hello).await.map_err(|e| e.to_string())?;
    let mut request = tonic::Request::new(ReceiverStream::new(rx));
    let token = format!("Bearer {}", config.token)
        .parse()
        .map_err(|_| "the link token is not a valid header value".to_string())?;
    request.metadata_mut().insert("authorization", token);
    let mut inbound = client
        .attach(request)
        .await
        .map_err(|s| format!("{}: {}", s.code(), s.message()))?
        .into_inner();
    *established = true;
    eprintln!("link connected to {}", config.relay);
    let http: Http = Client::builder(TokioExecutor::new()).build_http();
    let permits = Arc::new(Semaphore::new(config.limits.concurrent));
    while let Some(message) = inbound.message().await.map_err(|s| s.to_string())? {
        let Some(relay_message::Message::Request(request)) = message.message else {
            continue;
        };
        let (http, tx, permits) = (http.clone(), tx.clone(), permits.clone());
        let (api, limits) = (config.api.clone(), config.limits);
        tokio::spawn(async move {
            let Ok(_permit) = permits.acquire_owned().await else {
                return;
            };
            let response = forward(&http, &api, request, limits).await;
            let _ = tx
                .send(LinkMessage {
                    message: Some(link_message::Message::Response(response)),
                })
                .await;
        });
    }
    Ok(())
}

/// Performs one relayed request. The relay is not trusted to have filtered
/// it: only `/api/v1/*` and the allowed headers go through, plus the mark.
async fn forward(
    http: &Http,
    api: &str,
    request: proto::Request,
    limits: Limits,
) -> proto::Response {
    let id = request.id;
    let fail = |status: u32, code: &str| proto::Response {
        id,
        status,
        headers: vec![proto::Header {
            name: "content-type".into(),
            value: b"application/json".to_vec(),
        }],
        body: error_body(code, &Uuid::new_v4().to_string())
            .to_string()
            .into_bytes(),
    };
    if !request.path_and_query.starts_with("/api/v1/") {
        return fail(404, codes::NOT_FOUND);
    }
    let (Ok(method), Ok(uri)) = (
        Method::from_bytes(request.method.as_bytes()),
        format!("{api}{}", request.path_and_query).parse::<Uri>(),
    ) else {
        return fail(400, "bad_request");
    };
    let mut builder = axum::http::Request::builder().method(method).uri(uri);
    for header in &request.headers {
        if headers::allowed(&header.name, headers::REQUEST)
            && let Ok(value) = HeaderValue::from_bytes(&header.value)
        {
            builder = builder.header(header.name.as_str(), value);
        }
    }
    builder = builder.header(api_headers::VIA, api_headers::VIA_RELAY);
    let Ok(outgoing) = builder.body(Full::new(Bytes::from(request.body))) else {
        return fail(400, "bad_request");
    };
    let response = match tokio::time::timeout(limits.timeout, http.request(outgoing)).await {
        Ok(Ok(response)) => response,
        Ok(Err(_)) => return fail(502, codes::CONTROLLER_UNAVAILABLE),
        Err(_) => return fail(504, codes::CONTROLLER_TIMEOUT),
    };
    let (parts, body) = response.into_parts();
    let body = match tokio::time::timeout(
        limits.timeout,
        Limited::new(body, limits.response_body).collect(),
    )
    .await
    {
        Ok(Ok(body)) => body.to_bytes(),
        Ok(Err(_)) => return fail(502, codes::RESPONSE_TOO_LARGE),
        Err(_) => return fail(504, codes::CONTROLLER_TIMEOUT),
    };
    proto::Response {
        id,
        status: u32::from(parts.status.as_u16()),
        headers: headers::pick(&parts.headers, headers::RESPONSE),
        body: body.to_vec(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn the_link_reaches_only_the_api() {
        let http: Http = Client::builder(TokioExecutor::new()).build_http();
        for path in ["/health", "/api/v1", "//evil/api/v1/x", "/api/v1x"] {
            let request = proto::Request {
                id: 7,
                method: "GET".into(),
                path_and_query: path.into(),
                headers: vec![],
                body: vec![],
            };
            // Nothing listens there: an attempt would fail with 502, not 404.
            let response = forward(&http, "http://127.0.0.1:9", request, Limits::default()).await;
            assert_eq!((response.id, response.status), (7, 404), "{path}");
        }
    }
}

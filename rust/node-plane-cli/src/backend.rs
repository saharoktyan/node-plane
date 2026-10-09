//! Account-authenticated HTTP over a verified SSH channel; no public API listener.
use crate::{config::shell_quote, ssh::SshSession};
use anyhow::{Context, Result, ensure};
use bytes::Bytes;
use http_body_util::{BodyExt, Full};
use hyper::{Method, Request as HttpRequest, header};
use hyper_util::rt::TokioIo;
use serde::Deserialize;
use serde_json::{Value, json};
use std::{fmt, time::Duration};
use uuid::Uuid;
use zeroize::Zeroizing;

#[derive(Debug)]
struct AdminSelection(Vec<(String, String)>);
impl fmt::Display for AdminSelection {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(
            f,
            "Choose an administrator for this workstation key using --account."
        )
    }
}
impl std::error::Error for AdminSelection {}

#[derive(Clone, Deserialize)]
pub struct Credential {
    pub token: Zeroizing<String>,
    pub account_id: String,
}
pub struct Authorization {
    credential: Credential,
    session_id: Uuid,
    selector: String,
    issued: std::time::Instant,
    valid: bool,
}
impl Authorization {
    fn usable(&self, account: &str) -> bool {
        self.usable_at(account, std::time::Instant::now())
    }
    fn usable_at(&self, account: &str, now: std::time::Instant) -> bool {
        self.valid
            && now.saturating_duration_since(self.issued) < Duration::from_secs(25 * 60)
            && (account.is_empty()
                || account == self.selector
                || account == self.credential.account_id)
    }
}
pub fn authorized(session: &SshSession, account: &str) -> bool {
    session.persistent
        && !session.is_closed()
        && session
            .authorization
            .lock()
            .unwrap()
            .as_ref()
            .is_some_and(|auth| auth.usable(account))
}
pub fn rejected_authorization(session: &SshSession) -> bool {
    session
        .authorization
        .lock()
        .unwrap()
        .as_ref()
        .is_some_and(|auth| !auth.valid)
}
pub async fn end_authorization(session: &mut SshSession) -> Result<()> {
    let authorization = session.authorization.lock().unwrap().take();
    if let Some(auth) = authorization {
        revoke(session, auth.session_id).await?;
    }
    Ok(())
}

#[derive(Debug)]
pub struct ApiError {
    pub status: Option<u16>,
    pub code: String,
}
impl fmt::Display for ApiError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self.status {
            Some(status) => write!(
                f,
                "Backend refused the request: {} (HTTP {status}).",
                self.code
            ),
            None => write!(
                f,
                "Backend connection is unavailable; any submitted action has an unconfirmed result."
            ),
        }
    }
}
impl std::error::Error for ApiError {}
impl ApiError {
    pub fn transient(&self) -> bool {
        self.status.is_none() || matches!(self.status, Some(429 | 502 | 503 | 504))
    }
}

pub async fn helper(session: &mut SshSession, mut input: Value) -> Result<Value> {
    // Lookup and enrollment audit use the live credential's session, while their
    // command IDs continue to identify each separate operation.
    if matches!(
        input["action"].as_str(),
        Some("lookup-update" | "lookup-node" | "lookup-node-create" | "audit-enrollment")
    ) && let Some(auth) = session.authorization.lock().unwrap().as_ref()
    {
        input["session_id"] = json!(auth.session_id);
    }
    let source = include_str!("../../../app/backend/workstation_cli.py");
    let script = format!(
        "export NODE_PLANE_APP_DIR=/opt/node-plane/current NODE_PLANE_SHARED_DIR=/opt/node-plane/shared PYTHONPATH=/opt/node-plane/current/app\nexec /opt/node-plane/current/.venv/bin/python -c {}",
        shell_quote(source)
    );
    let command = format!(
        "if [ \"$(id -u)\" = 0 ]; then /bin/bash -c {}; else sudo -n /bin/bash -c {}; fi",
        shell_quote(&script),
        shell_quote(&script)
    );
    let payload = serde_json::to_vec(&input)?;
    let mut output = Zeroizing::new(Vec::<u8>::new());
    let mut oversized = false;
    // The authenticate response contains a bearer secret: never send it to the
    // ordinary journal or print stderr from the helper.
    let status = session
        .run(&command, Some(&payload), |stream, bytes| {
            if matches!(stream, crate::ssh::OutputStream::Stdout) {
                if output.len() + bytes.len() <= 256 * 1024 {
                    output.extend_from_slice(bytes);
                } else {
                    oversized = true;
                }
            }
        })
        .await
        .context("Cannot access trusted controller account provisioning")?;
    ensure!(
        !oversized,
        "Controller account provisioning returned oversized output."
    );
    let value: Value = serde_json::from_slice(&output)
        .context("Controller account provisioning did not return a supported response")?;
    if status != 0 || value.get("ok") != Some(&Value::Bool(true)) {
        let code = value
            .pointer("/error/code")
            .and_then(Value::as_str)
            .unwrap_or("workstation_authentication_failed");
        if code == "session_reauthentication_required"
            && let Some(auth) = session.authorization.lock().unwrap().as_mut()
        {
            auth.valid = false;
        }
        let choices = value
            .pointer("/error/choices")
            .or_else(|| value.get("choices"));
        if code == "admin_selection_required" {
            let choices = choices
                .and_then(Value::as_array)
                .into_iter()
                .flatten()
                .filter_map(|choice| {
                    let id = choice["account_id"].as_str()?;
                    Uuid::parse_str(id).ok()?;
                    let label: String = choice["label"]
                        .as_str()
                        .unwrap_or(id)
                        .chars()
                        .filter(|c| !c.is_control())
                        .take(200)
                        .collect();
                    Some((id.to_owned(), label))
                })
                .collect();
            return Err(AdminSelection(choices).into());
        }
        let mut message = format!("Controller authorization: {code}.");
        if let Some(choices) = choices.and_then(Value::as_array) {
            for account in choices.iter().take(20) {
                if let Some(id) = account
                    .get("account_id")
                    .or_else(|| account.get("id"))
                    .and_then(Value::as_str)
                    && Uuid::parse_str(id).is_ok()
                {
                    message.push_str(&format!("\nUse --account {id}"));
                }
            }
        }
        anyhow::bail!("{message}");
    }
    Ok(value)
}

pub async fn authenticate(
    session: &mut SshSession,
    id: Uuid,
    account: &str,
    tx: &std::sync::mpsc::Sender<crate::events::Event>,
) -> Result<Credential> {
    if authorized(session, account) {
        return Ok(session
            .authorization
            .lock()
            .unwrap()
            .as_ref()
            .unwrap()
            .credential
            .clone());
    }
    end_authorization(session).await?;
    let id = if session.persistent {
        Uuid::new_v4()
    } else {
        id
    };
    let mut input = json!({"version":1,"action":"authenticate","session_id":id,
        "attribution":{"ssh_user":session.ssh_user,"device_fingerprint":session.workstation_fingerprint}});
    if !account.is_empty() {
        if account.bytes().all(|b| b.is_ascii_digit()) {
            input["telegram_id"] = json!(account.parse::<u64>()?);
        } else {
            input["account_id"] = json!(Uuid::parse_str(account)?);
        }
    }
    let mut result = match helper(session, input.clone()).await {
        Ok(result) => result,
        Err(error) => {
            let Some(selection) = error.downcast_ref::<AdminSelection>() else {
                return Err(error);
            };
            ensure!(
                !selection.0.is_empty(),
                "No approved administrators available"
            );
            let index = if selection.0.len() == 1 {
                0
            } else {
                crate::events::select_administrator(tx, selection.0.clone())?
            };
            let (account_id, label) = selection
                .0
                .get(index)
                .context("Invalid administrator selection")?;
            ensure!(
                crate::events::confirm(
                    tx,
                    "Register workstation access",
                    format!(
                        "Bind this workstation key to {label}?\nAccount: {account_id}\nKey: {}\n\nThis is privileged SSH registration, not Telegram identity verification. Future sessions use this account. Cancel leaves this key unregistered.",
                        session.workstation_fingerprint
                    ),
                )?,
                "Workstation registration cancelled"
            );
            input["account_id"] = json!(account_id);
            helper(session, input).await?
        }
    };
    let token = Zeroizing::new(
        result
            .get("token")
            .and_then(Value::as_str)
            .context("Credential response is missing a token")?
            .to_owned(),
    );
    ensure!(
        token.starts_with("np_")
            && token.len() == 79
            && token
                .bytes()
                .all(|b| b.is_ascii_alphanumeric() || b"_.-".contains(&b)),
        "Invalid controller credential response."
    );
    let account_id = result["account_id"]
        .as_str()
        .context("Credential response is missing an account")?
        .to_owned();
    Uuid::parse_str(&account_id).context("Invalid controller account response")?;
    // Avoid retaining an extra plaintext secret in a generic JSON object.
    if let Some(Value::String(secret)) = result.get_mut("token") {
        use zeroize::Zeroize;
        secret.zeroize();
    }
    let credential = Credential { token, account_id };
    if session.persistent {
        *session.authorization.lock().unwrap() = Some(Authorization {
            credential: credential.clone(),
            session_id: id,
            selector: account.into(),
            issued: std::time::Instant::now(),
            valid: true,
        });
    }
    Ok(credential)
}

pub async fn revoke(session: &mut SshSession, id: Uuid) -> Result<()> {
    if session.persistent && session.authorization.lock().unwrap().is_some() {
        return Ok(());
    }
    helper(
        session,
        json!({"version":1,"action":"revoke","session_id":id}),
    )
    .await?;
    Ok(())
}

pub async fn audit_enrollment(
    session: &mut SshSession,
    session_id: Uuid,
    operation_id: Uuid,
    target: &str,
    key_fingerprint: &str,
    outcome: &str,
) -> Result<()> {
    helper(
        session,
        json!({"version":1,"action":"audit-enrollment",
        "session_id":session_id,"command_id":operation_id,"target":target,
        "key_fingerprint":key_fingerprint,"outcome":outcome}),
    )
    .await?;
    Ok(())
}

pub async fn request(
    session: &SshSession,
    credential: &Credential,
    method: Method,
    path: &str,
    body: Option<Value>,
    command: Option<Uuid>,
) -> std::result::Result<Value, ApiError> {
    ensure_api_path(path)?;
    let timeout = if method == Method::GET {
        Duration::from_secs(15)
    } else {
        Duration::from_secs(75)
    };
    let operation = async {
        let stream = session
            .backend_stream()
            .await
            .map_err(|_| transport_error())?;
        exchange(stream, credential, method, path, body, command).await
    };
    let result = tokio::time::timeout(timeout, operation)
        .await
        .map_err(|_| transport_error())?;
    if result
        .as_ref()
        .is_err_and(|error| matches!(error.status, Some(401 | 403)))
        && let Some(auth) = session.authorization.lock().unwrap().as_mut()
    {
        auth.valid = false;
    }
    result
}
async fn exchange<S>(
    stream: S,
    credential: &Credential,
    method: Method,
    path: &str,
    body: Option<Value>,
    command: Option<Uuid>,
) -> std::result::Result<Value, ApiError>
where
    S: tokio::io::AsyncRead + tokio::io::AsyncWrite + Unpin + Send + 'static,
{
    let (mut sender, connection) = hyper::client::conn::http1::handshake(TokioIo::new(stream))
        .await
        .map_err(|_| transport_error())?;
    let driver = AbortDriver(tokio::spawn(async move {
        let _ = connection.await;
    }));
    let result = async {
        let mut request = HttpRequest::builder()
            .method(method)
            .uri(path)
            .header(header::HOST, "localhost")
            .header(header::CONTENT_TYPE, "application/json")
            .header(
                header::AUTHORIZATION,
                format!("Bearer {}", *credential.token),
            );
        if let Some(key) = command {
            request = request.header("Idempotency-Key", key.to_string());
        }
        let bytes = body
            .map(|value| serde_json::to_vec(&value).expect("JSON Value serializes"))
            .unwrap_or_default();
        let request = request
            .body(Full::new(Bytes::from(bytes)))
            .map_err(|_| transport_error())?;
        let response = sender
            .send_request(request)
            .await
            .map_err(|_| transport_error())?;
        let status = response.status().as_u16();
        let mut body = response.into_body();
        let mut bytes = Zeroizing::new(Vec::new());
        while let Some(frame) = body.frame().await {
            let frame = frame.map_err(|_| transport_error())?;
            if let Ok(data) = frame.into_data() {
                if bytes.len() + data.len() > 1024 * 1024 {
                    return Err(ApiError {
                        status: Some(status),
                        code: "response_too_large".into(),
                    });
                }
                bytes.extend_from_slice(&data);
            }
        }
        let value: Value = serde_json::from_slice(&bytes).map_err(|_| ApiError {
            status: Some(status),
            code: "invalid_backend_response".into(),
        })?;
        if !(200..300).contains(&status) {
            let code = value
                .pointer("/error/code")
                .and_then(Value::as_str)
                .unwrap_or("backend_request_failed");
            let code = if code.len() <= 128
                && code.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'_')
            {
                code
            } else {
                "backend_request_failed"
            };
            return Err(ApiError {
                status: Some(status),
                code: code.into(),
            });
        }
        Ok(value)
    }
    .await;
    drop(driver);
    result
}

struct AbortDriver(tokio::task::JoinHandle<()>);
impl Drop for AbortDriver {
    fn drop(&mut self) {
        self.0.abort();
    }
}
fn transport_error() -> ApiError {
    ApiError {
        status: None,
        code: "backend_unavailable".into(),
    }
}
fn ensure_api_path(path: &str) -> std::result::Result<(), ApiError> {
    let resource = path.split('?').next().unwrap_or_default();
    if (path.starts_with("/api/v1/system/")
        || resource == "/api/v1/nodes"
        || resource.starts_with("/api/v1/nodes/")
        || resource.starts_with("/api/v1/node-jobs/")
        || resource.starts_with("/api/v1/agent-rollouts/"))
        && !path.contains(['\r', '\n', '#'])
        && !resource.contains("..")
        && !resource.contains('%')
        && !resource.contains('\\')
    {
        Ok(())
    } else {
        Err(ApiError {
            status: Some(422),
            code: "invalid_backend_path".into(),
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use tokio::io::{AsyncReadExt, AsyncWriteExt};
    #[test]
    fn cached_authorization_is_bounded_and_cannot_select_another_account() {
        let mut auth = Authorization {
            credential: Credential {
                token: Zeroizing::new("fixture".into()),
                account_id: Uuid::new_v4().to_string(),
            },
            session_id: Uuid::new_v4(),
            selector: "101".into(),
            issued: std::time::Instant::now(),
            valid: true,
        };
        assert!(auth.usable(""));
        assert!(auth.usable("101"));
        assert!(auth.usable(&auth.credential.account_id));
        assert!(!auth.usable("102"));
        auth.valid = false;
        assert!(!auth.usable("101"));
        auth.valid = true;
        assert!(!auth.usable_at("101", auth.issued + Duration::from_secs(26 * 60)));
    }

    async fn http_fixture(
        status: u16,
        body: &str,
        method: Method,
        command: Option<Uuid>,
    ) -> (std::result::Result<Value, ApiError>, String) {
        let (client, mut server) = tokio::io::duplex(65536);
        let reply = format!(
            "HTTP/1.1 {status} Test\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
            body.len()
        );
        let peer = tokio::spawn(async move {
            let mut data = Vec::new();
            let mut chunk = [0u8; 4096];
            loop {
                let n = server.read(&mut chunk).await.unwrap();
                if n == 0 {
                    break;
                }
                data.extend_from_slice(&chunk[..n]);
                if let Some(index) = data.windows(4).position(|w| w == b"\r\n\r\n") {
                    let headers = String::from_utf8_lossy(&data[..index]).to_ascii_lowercase();
                    let length = headers
                        .lines()
                        .find_map(|line| {
                            line.strip_prefix("content-length: ")
                                .and_then(|v| v.parse::<usize>().ok())
                        })
                        .unwrap_or(0);
                    if data.len() >= index + 4 + length {
                        break;
                    }
                }
            }
            server.write_all(reply.as_bytes()).await.unwrap();
            String::from_utf8(data).unwrap()
        });
        let credential = Credential {
            token: Zeroizing::new("private_fixture_bearer".into()),
            account_id: Uuid::new_v4().to_string(),
        };
        let result = exchange(
            client,
            &credential,
            method,
            "/api/v1/system/updates/run",
            Some(json!({"kind":"stack"})),
            command,
        )
        .await;
        (result, peer.await.unwrap())
    }
    #[tokio::test]
    async fn http_wire_uses_account_bearer_and_saved_command_without_delegation() {
        let id = Uuid::new_v4();
        let (result, wire) =
            http_fixture(202, "{\"id\":\"accepted\"}", Method::POST, Some(id)).await;
        assert_eq!(result.unwrap()["id"], "accepted");
        let wire = wire.to_ascii_lowercase();
        assert!(wire.starts_with("post /api/v1/system/updates/run http/1.1"));
        assert!(wire.contains("authorization: bearer private_fixture_bearer"));
        assert!(wire.contains(&format!("idempotency-key: {id}")));
        assert!(!wire.contains("telegram"));
        assert!(wire.ends_with("{\"kind\":\"stack\"}"));
    }
    #[tokio::test]
    async fn error_payload_does_not_expose_secrets_or_unsanitized_details() {
        let (result, _) = http_fixture(
            409,
            "{\"error\":{\"code\":\"maintenance_busy\",\"message\":\"secret_database_url\"}}",
            Method::POST,
            None,
        )
        .await;
        let error = result.unwrap_err();
        assert_eq!(error.status, Some(409));
        assert_eq!(error.code, "maintenance_busy");
        assert!(!error.to_string().contains("secret_database_url"));
        let (result, _) = http_fixture(
            500,
            "{\"error\":{\"code\":\"bad\\ncontrol\"}}",
            Method::GET,
            None,
        )
        .await;
        assert_eq!(result.unwrap_err().code, "backend_request_failed");
    }
    #[tokio::test]
    async fn peer_disconnect_after_dispatch_is_unconfirmed() {
        let (client, mut peer) = tokio::io::duplex(65536);
        tokio::spawn(async move {
            let mut buffer = [0u8; 4096];
            let _ = peer.read(&mut buffer).await;
        });
        let credential = Credential {
            token: Zeroizing::new("test".into()),
            account_id: Uuid::new_v4().to_string(),
        };
        let result = exchange(
            client,
            &credential,
            Method::POST,
            "/api/v1/system/updates/run",
            Some(json!({"kind":"stack"})),
            Some(Uuid::new_v4()),
        )
        .await;
        assert!(result.unwrap_err().status.is_none());
    }
    #[test]
    fn api_never_targets_external_urls_or_delegates_telegram() {
        assert!(ensure_api_path("/api/v1/system/updates").is_ok());
        assert!(ensure_api_path("/api/v1/nodes?order=region&cursor=a%2Fb").is_ok());
        assert!(ensure_api_path("/api/v1/nodes/lv1/overview").is_ok());
        assert!(ensure_api_path("/api/v1/nodes/../profiles").is_err());
        assert!(ensure_api_path("/api/v1/nodes/%2e%2e/profiles").is_err());
        for value in [
            "http://other.example/api/v1/system/updates",
            "/api/v1/integrations/telegram",
            "/api/v1/system/updates\r\nAuthorization: bad",
        ] {
            assert!(ensure_api_path(value).is_err());
        }
    }
    #[test]
    fn transport_and_permission_errors_have_different_retry_rules() {
        assert!(transport_error().transient());
        assert!(
            ApiError {
                status: Some(503),
                code: "unavailable".into()
            }
            .transient()
        );
        assert!(
            !ApiError {
                status: Some(403),
                code: "permission_denied".into()
            }
            .transient()
        );
    }
}

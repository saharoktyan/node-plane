//! Conventions of the Node Plane controller API that clients and the relay
//! share. The full contract is `api/openapi.json`; `CONTRACT_VERSION` follows
//! `backend/api_contract.py`.
use serde_json::{Value, json};

pub const CONTRACT_VERSION: &str = "1.0.0";

/// Header names, lowercase as HTTP/2 carries them.
pub mod headers {
    /// Set by the controller's relay link on every request it forwards.
    pub const VIA: &str = "x-node-plane-via";
    pub const VIA_RELAY: &str = "relay";
    /// An adapter acting for a Telegram identity.
    pub const DELEGATION: &str = "x-node-plane-telegram-user-id";
    pub const REQUEST_ID: &str = "x-request-id";
    pub const IDEMPOTENCY_KEY: &str = "idempotency-key";
    pub const IF_MATCH: &str = "if-match";
    /// Every Node Plane header starts with this; a relay never passes one on.
    pub const PREFIX: &str = "x-node-plane-";
}

/// Error codes a client can meet outside the controller's own handlers.
pub mod codes {
    pub const CONTROLLER_UNAVAILABLE: &str = "controller_unavailable";
    pub const CONTROLLER_TIMEOUT: &str = "controller_timeout";
    pub const RELAY_BUSY: &str = "relay_busy";
    pub const REQUEST_TOO_LARGE: &str = "request_too_large";
    pub const RESPONSE_TOO_LARGE: &str = "response_too_large";
    pub const NOT_FOUND: &str = "not_found";
    pub const NOT_AVAILABLE_THROUGH_RELAY: &str = "not_available_through_relay";
}

/// The contract's error body: `{"error": {code, message, request_id, details}}`.
pub fn error_body(code: &str, request_id: &str) -> Value {
    json!({"error": {"code": code, "message": code, "request_id": request_id, "details": {}}})
}

/// The error code of a contract error body.
pub fn error_code(body: &Value) -> Option<&str> {
    body.pointer("/error/code").and_then(Value::as_str)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn an_error_body_has_the_contract_shape() {
        let body = error_body(codes::CONTROLLER_UNAVAILABLE, "r1");
        assert_eq!(error_code(&body), Some("controller_unavailable"));
        assert_eq!(body["error"]["request_id"], "r1");
        assert_eq!(body["error"]["details"], json!({}));
        assert!(headers::VIA.starts_with(headers::PREFIX));
        assert!(headers::DELEGATION.starts_with(headers::PREFIX));
    }
}

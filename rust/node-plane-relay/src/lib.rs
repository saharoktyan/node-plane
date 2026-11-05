//! The web cabinet's public entry point and the controller's link to it.
//!
//! `serve` runs on a public server: it answers `/api/v1/*` by passing each
//! request over the link the controller holds open to it. `link` runs on the
//! controller: it dials the relay, performs the requests against the local
//! API and sends the responses back. The relay has no powers of its own;
//! the controller's API checks every request (see `relay-design.md`).
pub mod headers;
pub mod link;
pub mod serve;

pub mod proto {
    tonic::include_proto!("nodeplane.relay.v1");
}

pub const VERSION: &str = env!("NODE_PLANE_BINARY_VERSION");

/// Reads a shared link token; it must be long enough to resist guessing.
pub fn read_token(path: &str) -> Result<String, String> {
    let token = std::fs::read_to_string(path).map_err(|error| format!("{path}: {error}"))?;
    let token = token.trim().to_string();
    if token.len() < 32 || token.chars().any(char::is_whitespace) {
        return Err(format!(
            "{path}: the link token must be at least 32 characters"
        ));
    }
    Ok(token)
}

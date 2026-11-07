//! The headers that cross the relay. Everything else is dropped, Node Plane
//! headers above all: the controller's link sets the via mark itself. The
//! one exception is the Telegram user the bot on a node acts for: its
//! signature covers the header, and the controller takes it from no other
//! client through the relay.
use http::HeaderMap;

use crate::proto::Header;

/// Browser to controller.
pub const REQUEST: &[&str] = &[
    "accept",
    "accept-language",
    "authorization",
    "content-type",
    "idempotency-key",
    "if-match",
    "if-none-match",
    "user-agent",
    node_plane_api::headers::DELEGATION,
];

/// Controller to browser.
pub const RESPONSE: &[&str] = &[
    "cache-control",
    "content-disposition",
    "content-type",
    "etag",
    "location",
    "retry-after",
    "www-authenticate",
    "x-request-id",
];

pub fn allowed(name: &str, allow: &[&str]) -> bool {
    let name = name.to_ascii_lowercase();
    allow.contains(&name.as_str())
}

pub fn pick(map: &HeaderMap, allow: &[&str]) -> Vec<Header> {
    map.iter()
        .filter(|(name, _)| allowed(name.as_str(), allow))
        .map(|(name, value)| Header {
            name: name.as_str().to_string(),
            value: value.as_bytes().to_vec(),
        })
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn node_plane_headers_never_cross() {
        for allow in [REQUEST, RESPONSE] {
            assert!(allow.iter().all(|name| {
                !name.starts_with(node_plane_api::headers::PREFIX)
                    || *name == node_plane_api::headers::DELEGATION
            }));
        }
        assert!(allowed("X-Node-Plane-Telegram-User-ID", REQUEST));
        assert!(allowed("Authorization", REQUEST));
        assert!(!allowed("X-Node-Plane-Via", REQUEST));
        assert!(!allowed("cookie", REQUEST) && !allowed("set-cookie", RESPONSE));
    }
}

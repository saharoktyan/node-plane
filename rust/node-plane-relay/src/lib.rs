//! The web cabinet's public entry point and the controller's link to it.
//!
//! `serve` runs on a public server: it answers `/api/v1/*` by passing each
//! request over the link the controller holds open to it. `link` runs on the
//! controller: it dials the relay, performs the requests against the local
//! API and sends the responses back. The relay has no powers of its own;
//! the controller's API checks every request (see `relay-design.md`).
pub mod headers;
pub mod keys;
#[cfg(feature = "link")]
pub mod link;
#[cfg(feature = "serve")]
pub mod serve;
#[cfg(feature = "serve")]
pub mod tls;

/// Attach metadata: the controller's key and its challenge to the relay.
pub const LINK_KEY: &str = "x-node-plane-link-key";
pub const LINK_NONCE: &str = "x-node-plane-link-nonce";

pub mod proto {
    tonic::include_proto!("nodeplane.relay.v1");
}

pub const VERSION: &str = env!("NODE_PLANE_BINARY_VERSION");

//! Generated Xray API types from `proto/vendor/xray` (client only).
//! Module nesting mirrors the proto packages: generated code refers to
//! sibling packages by relative `super::` paths.
#![allow(dead_code, clippy::all)]

pub(crate) mod xray {
    pub(crate) mod common {
        pub(crate) mod geodata {
            tonic::include_proto!("xray.common.geodata");
        }
        pub(crate) mod net {
            tonic::include_proto!("xray.common.net");
        }
        pub(crate) mod protocol {
            tonic::include_proto!("xray.common.protocol");
        }
        pub(crate) mod serial {
            tonic::include_proto!("xray.common.serial");
        }
    }
    pub(crate) mod core {
        tonic::include_proto!("xray.core");
    }
    pub(crate) mod app {
        pub(crate) mod proxyman {
            pub(crate) mod command {
                tonic::include_proto!("xray.app.proxyman.command");
            }
        }
        pub(crate) mod router {
            tonic::include_proto!("xray.app.router");
            pub(crate) mod command {
                tonic::include_proto!("xray.app.router.command");
            }
        }
        pub(crate) mod stats {
            pub(crate) mod command {
                tonic::include_proto!("xray.app.stats.command");
            }
        }
    }
}

/// `xray.proxy.vless.Account`, only the fields an inbound user carries.
/// The full upstream file pulls in the transport tree (see vendor README).
#[derive(Clone, PartialEq, prost::Message)]
pub(crate) struct VlessAccount {
    #[prost(string, tag = "1")]
    pub(crate) id: String,
    #[prost(string, tag = "2")]
    pub(crate) flow: String,
    /// Must stay empty for inbound users.
    #[prost(string, tag = "3")]
    pub(crate) encryption: String,
}

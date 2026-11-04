fn main() {
    binary_metadata();
    release_key();
    println!("cargo:rerun-if-changed=../../proto/agent/v1/types.proto");
    println!("cargo:rerun-if-changed=../../proto/agent/v1/agent_service.proto");

    tonic_build::configure()
        .build_server(true)
        .build_client(true)
        .compile_protos(
            &[
                "../../proto/agent/v1/types.proto",
                "../../proto/agent/v1/agent_service.proto",
            ],
            &["../../proto"],
        )
        .expect("failed to compile node-agent protobufs");

    // Vendored Xray API: client only, see proto/vendor/xray/README.md.
    let xray = [
        "app/proxyman/command/command.proto",
        "app/router/command/command.proto",
        "app/router/config.proto",
        "app/stats/command/command.proto",
    ];
    println!("cargo:rerun-if-changed=../../proto/vendor/xray");
    tonic_build::configure()
        .build_server(false)
        .build_client(true)
        .compile_protos(
            &xray.map(|path| format!("../../proto/vendor/xray/{path}")),
            &["../../proto/vendor/xray"],
        )
        .expect("failed to compile vendored Xray protobufs");
}

fn binary_metadata() {
    use std::{path::PathBuf, process::Command};
    let root = PathBuf::from(std::env::var("CARGO_MANIFEST_DIR").unwrap()).join("../..");
    println!("cargo:rerun-if-changed=../../VERSION");
    println!("cargo:rerun-if-changed=../../BUILD_COMMIT");
    println!("cargo:rerun-if-changed=../../.git/HEAD");
    println!("cargo:rerun-if-changed=../../.git/refs");
    let version =
        std::fs::read_to_string(root.join("VERSION")).unwrap_or_else(|_| "unknown".into());
    let commit = std::fs::read_to_string(root.join("BUILD_COMMIT"))
        .ok()
        .or_else(|| {
            Command::new("git")
                .args(["rev-parse", "HEAD"])
                .current_dir(&root)
                .output()
                .ok()
                .filter(|o| o.status.success())
                .map(|o| String::from_utf8_lossy(&o.stdout).into_owned())
        })
        .unwrap_or_else(|| "unknown".into());
    println!(
        "cargo:rustc-env=NODE_PLANE_BINARY_VERSION={}",
        version.trim()
    );
    println!("cargo:rustc-env=NODE_PLANE_BINARY_COMMIT={}", commit.trim());
}

/// The public key self-updates must be signed with: `release-key.hex` next to
/// this file, or `NODE_PLANE_RELEASE_KEY` for a fork's own key.
fn release_key() {
    println!("cargo:rerun-if-changed=release-key.hex");
    println!("cargo:rerun-if-env-changed=NODE_PLANE_RELEASE_KEY");
    let key = std::env::var("NODE_PLANE_RELEASE_KEY")
        .ok()
        .or_else(|| std::fs::read_to_string("release-key.hex").ok())
        .unwrap_or_default();
    let key = key.trim();
    if !key.is_empty() {
        assert!(
            key.len() == 64 && key.bytes().all(|b| b.is_ascii_hexdigit()),
            "the release key must be 32 bytes of hex"
        );
        println!("cargo:rustc-env=NODE_PLANE_RELEASE_KEY={key}");
    }
}

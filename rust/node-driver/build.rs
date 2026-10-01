fn main() {
    binary_metadata();
    println!("cargo:rerun-if-changed=../../proto/driver/v1/types.proto");
    println!("cargo:rerun-if-changed=../../proto/driver/v1/node_service.proto");
    println!("cargo:rerun-if-changed=../../proto/driver/v1/provisioning_service.proto");
    println!("cargo:rerun-if-changed=../../proto/driver/v1/runtime_service.proto");
    println!("cargo:rerun-if-changed=../../proto/driver/v1/telemetry_service.proto");
    println!("cargo:rerun-if-changed=../../proto/driver/v1/operation_service.proto");
    println!("cargo:rerun-if-changed=../../proto/agent/v1/types.proto");
    println!("cargo:rerun-if-changed=../../proto/agent/v1/agent_service.proto");

    tonic_build::configure()
        .build_server(true)
        .build_client(true)
        .compile_protos(
            &[
                "../../proto/driver/v1/types.proto",
                "../../proto/driver/v1/node_service.proto",
                "../../proto/driver/v1/provisioning_service.proto",
                "../../proto/driver/v1/runtime_service.proto",
                "../../proto/driver/v1/telemetry_service.proto",
                "../../proto/driver/v1/operation_service.proto",
                "../../proto/agent/v1/types.proto",
                "../../proto/agent/v1/agent_service.proto",
            ],
            &["../../proto"],
        )
        .expect("failed to compile driver protobufs");
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

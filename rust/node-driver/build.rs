fn main() {
    println!("cargo:rerun-if-changed=../../proto/driver/v1/types.proto");
    println!("cargo:rerun-if-changed=../../proto/driver/v1/node_service.proto");
    println!("cargo:rerun-if-changed=../../proto/driver/v1/provisioning_service.proto");
    println!("cargo:rerun-if-changed=../../proto/driver/v1/runtime_service.proto");
    println!("cargo:rerun-if-changed=../../proto/driver/v1/telemetry_service.proto");
    println!("cargo:rerun-if-changed=../../proto/driver/v1/operation_service.proto");

    tonic_build::configure()
        .build_server(true)
        .build_client(false)
        .compile_protos(
            &[
                "../../proto/driver/v1/types.proto",
                "../../proto/driver/v1/node_service.proto",
                "../../proto/driver/v1/provisioning_service.proto",
                "../../proto/driver/v1/runtime_service.proto",
                "../../proto/driver/v1/telemetry_service.proto",
                "../../proto/driver/v1/operation_service.proto",
            ],
            &["../../proto"],
        )
        .expect("failed to compile driver protobufs");
}

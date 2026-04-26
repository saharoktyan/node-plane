fn main() {
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
}

//! Extra admission for forced cancellation; loopback alone is not authority.
use sha2::{Digest, Sha256};
use std::fs::{self, OpenOptions};
use std::io::Read;
use std::os::unix::fs::{MetadataExt, OpenOptionsExt};
use std::path::{Component, Path};
use tonic::Status;

pub(crate) fn verify(
    shared: &Path,
    request: &crate::driver::v1::ForceRecoveryRequest,
    now: i64,
) -> Result<(), Status> {
    let denied = || Status::permission_denied("force recovery approval is missing or invalid");
    if !shared.is_absolute()
        || shared
            .components()
            .any(|p| matches!(p, Component::ParentDir | Component::CurDir))
    {
        return Err(denied());
    }
    let data = shared.join("data");
    let owner = fs::symlink_metadata(&data).map_err(|_| denied())?.uid();
    for path in data.ancestors() {
        let metadata = fs::symlink_metadata(path).map_err(|_| denied())?;
        if !metadata.is_dir()
            || ![0, owner].contains(&metadata.uid())
            || (metadata.mode() & 0o022 != 0 && metadata.mode() & 0o1000 == 0)
        {
            return Err(denied());
        }
    }
    let directory = data.join("force-recovery-approvals");
    let info = fs::symlink_metadata(&directory).map_err(|_| denied())?;
    if !info.is_dir() || ![0, owner].contains(&info.uid()) || info.mode() & 0o077 != 0 {
        return Err(denied());
    }
    // Driver is Linux-only. Refuse a final-component symlink at open, too.
    const O_NOFOLLOW: i32 = 0o400000;
    let file = OpenOptions::new()
        .read(true)
        .custom_flags(O_NOFOLLOW)
        .open(directory.join(format!("{}.json", request.ticket_id)))
        .map_err(|_| denied())?;
    let info = file.metadata().map_err(|_| denied())?;
    if !info.is_file()
        || ![0, owner].contains(&info.uid())
        || info.mode() & 0o077 != 0
        || info.len() > 4096
    {
        return Err(denied());
    }
    let mut content = Vec::new();
    file.take(4097)
        .read_to_end(&mut content)
        .map_err(|_| denied())?;
    let approval: serde_json::Value = serde_json::from_slice(&content).map_err(|_| denied())?;
    let expires = approval["expires_at"].as_i64().ok_or_else(denied)?;
    let hash: String = Sha256::digest(request.intent_json.as_bytes())
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect();
    if approval["node_key"] != request.node_key
        || approval["command_id"] != request.command_id
        || approval["ticket_id"] != request.ticket_id
        || approval["kind"] != request.operation_kind
        || approval["intent_sha256"] != hash
        || expires < now
        || expires > now + 300
    {
        return Err(denied());
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::os::unix::fs::PermissionsExt;

    struct TestFixture {
        shared: std::path::PathBuf,
        directory: std::path::PathBuf,
    }

    impl TestFixture {
        fn new() -> Self {
            let shared =
                std::env::temp_dir().join(format!("np-test-appr-{}", uuid::Uuid::new_v4()));
            let data = shared.join("data");
            let directory = data.join("force-recovery-approvals");
            fs::create_dir_all(&directory).unwrap();
            fs::set_permissions(&shared, fs::Permissions::from_mode(0o700)).unwrap();
            fs::set_permissions(&data, fs::Permissions::from_mode(0o700)).unwrap();
            fs::set_permissions(&directory, fs::Permissions::from_mode(0o700)).unwrap();
            Self { shared, directory }
        }

        fn write_approval(
            &self,
            ticket_id: &str,
            content: &serde_json::Value,
        ) -> std::path::PathBuf {
            let path = self.directory.join(format!("{ticket_id}.json"));
            fs::write(&path, content.to_string()).unwrap();
            fs::set_permissions(&path, fs::Permissions::from_mode(0o600)).unwrap();
            path
        }
    }

    impl Drop for TestFixture {
        fn drop(&mut self) {
            let _ = fs::remove_dir_all(&self.shared);
        }
    }

    fn sample_request() -> crate::driver::v1::ForceRecoveryRequest {
        crate::driver::v1::ForceRecoveryRequest {
            node_key: "node-1".into(),
            ticket_id: uuid::Uuid::new_v4().to_string(),
            command_id: uuid::Uuid::new_v4().to_string(),
            operation_kind: "settings".into(),
            intent_json: r#"{"kind":"node_settings","revision":1}"#.into(),
            phase: "prepare".into(),
        }
    }

    fn hash_intent(intent_json: &str) -> String {
        Sha256::digest(intent_json.as_bytes())
            .iter()
            .map(|byte| format!("{byte:02x}"))
            .collect()
    }

    #[test]
    fn absence_of_approval_is_denied() {
        let fixture = TestFixture::new();
        let request = sample_request();
        let err = verify(&fixture.shared, &request, 1000).unwrap_err();
        assert_eq!(err.code(), tonic::Code::PermissionDenied);
    }

    #[test]
    fn valid_approval_succeeds() {
        let fixture = TestFixture::new();
        let request = sample_request();
        let now = 1000;
        fixture.write_approval(
            &request.ticket_id,
            &serde_json::json!({
                "node_key": request.node_key,
                "command_id": request.command_id,
                "ticket_id": request.ticket_id,
                "kind": request.operation_kind,
                "intent_sha256": hash_intent(&request.intent_json),
                "expires_at": now + 150,
            }),
        );
        assert!(verify(&fixture.shared, &request, now).is_ok());
    }

    #[test]
    fn expiry_and_future_are_denied() {
        let fixture = TestFixture::new();
        let request = sample_request();
        let now = 1000;

        // Past expiry (< now)
        fixture.write_approval(
            &request.ticket_id,
            &serde_json::json!({
                "node_key": request.node_key,
                "command_id": request.command_id,
                "ticket_id": request.ticket_id,
                "kind": request.operation_kind,
                "intent_sha256": hash_intent(&request.intent_json),
                "expires_at": now - 1,
            }),
        );
        assert_eq!(
            verify(&fixture.shared, &request, now).unwrap_err().code(),
            tonic::Code::PermissionDenied
        );

        // Future expiry beyond 300s window (> now + 300)
        fixture.write_approval(
            &request.ticket_id,
            &serde_json::json!({
                "node_key": request.node_key,
                "command_id": request.command_id,
                "ticket_id": request.ticket_id,
                "kind": request.operation_kind,
                "intent_sha256": hash_intent(&request.intent_json),
                "expires_at": now + 301,
            }),
        );
        assert_eq!(
            verify(&fixture.shared, &request, now).unwrap_err().code(),
            tonic::Code::PermissionDenied
        );

        // Exact boundaries (now and now + 300) are valid
        fixture.write_approval(
            &request.ticket_id,
            &serde_json::json!({
                "node_key": request.node_key,
                "command_id": request.command_id,
                "ticket_id": request.ticket_id,
                "kind": request.operation_kind,
                "intent_sha256": hash_intent(&request.intent_json),
                "expires_at": now,
            }),
        );
        assert!(verify(&fixture.shared, &request, now).is_ok());

        fixture.write_approval(
            &request.ticket_id,
            &serde_json::json!({
                "node_key": request.node_key,
                "command_id": request.command_id,
                "ticket_id": request.ticket_id,
                "kind": request.operation_kind,
                "intent_sha256": hash_intent(&request.intent_json),
                "expires_at": now + 300,
            }),
        );
        assert!(verify(&fixture.shared, &request, now).is_ok());
    }

    #[test]
    fn exact_binding_mismatches_are_denied() {
        let fixture = TestFixture::new();
        let request = sample_request();
        let now = 1000;

        let base_approval = serde_json::json!({
            "node_key": request.node_key,
            "command_id": request.command_id,
            "ticket_id": request.ticket_id,
            "kind": request.operation_kind,
            "intent_sha256": hash_intent(&request.intent_json),
            "expires_at": now + 200,
        });

        // Mismatched node_key
        let mut bad = base_approval.clone();
        bad["node_key"] = "other-node".into();
        fixture.write_approval(&request.ticket_id, &bad);
        assert_eq!(
            verify(&fixture.shared, &request, now).unwrap_err().code(),
            tonic::Code::PermissionDenied
        );

        // Mismatched command_id
        let mut bad = base_approval.clone();
        bad["command_id"] = uuid::Uuid::new_v4().to_string().into();
        fixture.write_approval(&request.ticket_id, &bad);
        assert_eq!(
            verify(&fixture.shared, &request, now).unwrap_err().code(),
            tonic::Code::PermissionDenied
        );

        // Mismatched ticket_id inside content
        let mut bad = base_approval.clone();
        bad["ticket_id"] = uuid::Uuid::new_v4().to_string().into();
        fixture.write_approval(&request.ticket_id, &bad);
        assert_eq!(
            verify(&fixture.shared, &request, now).unwrap_err().code(),
            tonic::Code::PermissionDenied
        );

        // Mismatched kind
        let mut bad = base_approval.clone();
        bad["kind"] = "profile".into();
        fixture.write_approval(&request.ticket_id, &bad);
        assert_eq!(
            verify(&fixture.shared, &request, now).unwrap_err().code(),
            tonic::Code::PermissionDenied
        );

        // Mismatched intent_sha256
        let mut bad = base_approval.clone();
        bad["intent_sha256"] =
            "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef".into();
        fixture.write_approval(&request.ticket_id, &bad);
        assert_eq!(
            verify(&fixture.shared, &request, now).unwrap_err().code(),
            tonic::Code::PermissionDenied
        );
    }

    #[test]
    fn private_permissions_and_symlinks_are_denied() {
        let fixture = TestFixture::new();
        let request = sample_request();
        let now = 1000;
        let content = serde_json::json!({
            "node_key": request.node_key,
            "command_id": request.command_id,
            "ticket_id": request.ticket_id,
            "kind": request.operation_kind,
            "intent_sha256": hash_intent(&request.intent_json),
            "expires_at": now + 200,
        });

        // 1. Non-absolute path or paths with ParentDir/CurDir
        assert_eq!(
            verify(Path::new("relative/shared"), &request, now)
                .unwrap_err()
                .code(),
            tonic::Code::PermissionDenied
        );
        assert_eq!(
            verify(&fixture.shared.join("../shared"), &request, now)
                .unwrap_err()
                .code(),
            tonic::Code::PermissionDenied
        );

        // 2. Loose permissions on directory (0o755 instead of 0o700)
        let file_path = fixture.write_approval(&request.ticket_id, &content);
        fs::set_permissions(&fixture.directory, fs::Permissions::from_mode(0o755)).unwrap();
        assert_eq!(
            verify(&fixture.shared, &request, now).unwrap_err().code(),
            tonic::Code::PermissionDenied
        );
        fs::set_permissions(&fixture.directory, fs::Permissions::from_mode(0o700)).unwrap();

        // 3. Loose permissions on file (0o644 instead of 0o600)
        fs::set_permissions(&file_path, fs::Permissions::from_mode(0o644)).unwrap();
        assert_eq!(
            verify(&fixture.shared, &request, now).unwrap_err().code(),
            tonic::Code::PermissionDenied
        );
        fs::set_permissions(&file_path, fs::Permissions::from_mode(0o600)).unwrap();

        // 4. Symlink file is refused
        let real_file = fixture.shared.join("real_approval.json");
        fs::write(&real_file, content.to_string()).unwrap();
        fs::set_permissions(&real_file, fs::Permissions::from_mode(0o600)).unwrap();
        let symlink_file = fixture
            .directory
            .join(format!("{}.json", request.ticket_id));
        let _ = fs::remove_file(&symlink_file);
        std::os::unix::fs::symlink(&real_file, &symlink_file).unwrap();
        assert_eq!(
            verify(&fixture.shared, &request, now).unwrap_err().code(),
            tonic::Code::PermissionDenied
        );
        let _ = fs::remove_file(&symlink_file);

        // 5. Oversized file (> 4096 bytes)
        let mut large = content.clone();
        large["padding"] = "x".repeat(5000).into();
        fixture.write_approval(&request.ticket_id, &large);
        assert_eq!(
            verify(&fixture.shared, &request, now).unwrap_err().code(),
            tonic::Code::PermissionDenied
        );
    }
}

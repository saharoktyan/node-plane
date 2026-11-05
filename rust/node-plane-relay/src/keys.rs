//! Keys, signatures and the files that hold them.
//!
//! Each side has an Ed25519 key it generates itself; the private half never
//! leaves its host. The controller pins the relay's public key and the relay
//! the controller's (handed over through the relay node's agent). Every
//! signed message has a fixed layout: a label, then values of fixed length
//! (32-byte keys and nonces).
use std::{
    fs,
    io::Write,
    os::unix::fs::{OpenOptionsExt, PermissionsExt},
    path::Path,
};

use ring::{
    rand::{SecureRandom, SystemRandom},
    signature::{self, Ed25519KeyPair, KeyPair},
};

pub const ATTACH_RELAY: &[u8] = b"node-plane relay attach relay";
pub const ATTACH_LINK: &[u8] = b"node-plane relay attach link";
pub const KEY_LEN: usize = 32;

/// The key at `path`, created (0600) when there is none yet.
pub fn load_or_create(path: &Path) -> Result<Ed25519KeyPair, String> {
    let bytes = match fs::read(path) {
        Ok(bytes) => bytes,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
            let pkcs8 = Ed25519KeyPair::generate_pkcs8(&SystemRandom::new())
                .map_err(|_| "could not generate a key".to_string())?;
            write_private(path, pkcs8.as_ref())?;
            pkcs8.as_ref().to_vec()
        }
        Err(error) => return Err(format!("{}: {error}", path.display())),
    };
    Ed25519KeyPair::from_pkcs8(&bytes)
        .map_err(|_| format!("{}: not an Ed25519 key", path.display()))
}

pub fn public(key: &Ed25519KeyPair) -> Vec<u8> {
    key.public_key().as_ref().to_vec()
}

pub fn sign(key: &Ed25519KeyPair, label: &[u8], parts: &[&[u8]]) -> Vec<u8> {
    key.sign(&message(label, parts)).as_ref().to_vec()
}

pub fn verify(public: &[u8], label: &[u8], parts: &[&[u8]], signature: &[u8]) -> bool {
    public.len() == KEY_LEN
        && signature::UnparsedPublicKey::new(&signature::ED25519, public)
            .verify(&message(label, parts), signature)
            .is_ok()
}

fn message(label: &[u8], parts: &[&[u8]]) -> Vec<u8> {
    let mut out = label.to_vec();
    for part in parts {
        out.extend_from_slice(part);
    }
    out
}

pub fn nonce() -> [u8; 32] {
    let mut nonce = [0; 32];
    SystemRandom::new()
        .fill(&mut nonce)
        .expect("system randomness");
    nonce
}

pub fn hex(bytes: &[u8]) -> String {
    bytes.iter().map(|b| format!("{b:02x}")).collect()
}

pub fn unhex(text: &str) -> Option<Vec<u8>> {
    let text = text.trim();
    if !text.len().is_multiple_of(2) {
        return None;
    }
    (0..text.len())
        .step_by(2)
        .map(|i| u8::from_str_radix(text.get(i..i + 2)?, 16).ok())
        .collect()
}

/// Replaces `path` with `bytes`, readable by the owner only.
pub fn write_private(path: &Path, bytes: &[u8]) -> Result<(), String> {
    let error = |e: std::io::Error| format!("{}: {e}", path.display());
    if let Some(dir) = path.parent() {
        fs::create_dir_all(dir).map_err(error)?;
    }
    let temporary = path.with_extension("tmp");
    let mut file = fs::OpenOptions::new()
        .write(true)
        .create(true)
        .truncate(true)
        .mode(0o600)
        .open(&temporary)
        .map_err(error)?;
    file.set_permissions(fs::Permissions::from_mode(0o600))
        .map_err(error)?;
    file.write_all(bytes).map_err(error)?;
    file.sync_all().map_err(error)?;
    fs::rename(&temporary, path).map_err(error)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn signatures_bind_their_values() {
        let dir = std::env::temp_dir().join(format!("relay-keys-{}", std::process::id()));
        let path = dir.join("k.key");
        let key = load_or_create(&path).unwrap();
        assert_eq!(public(&load_or_create(&path).unwrap()), public(&key));
        assert_eq!(
            fs::metadata(&path).unwrap().permissions().mode() & 0o777,
            0o600
        );
        let nonce = nonce();
        let signature = sign(&key, ATTACH_LINK, &[&nonce]);
        assert!(verify(&public(&key), ATTACH_LINK, &[&nonce], &signature));
        assert!(!verify(&public(&key), ATTACH_RELAY, &[&nonce], &signature));
        assert_eq!(unhex(&hex(&[0, 255, 16])), Some(vec![0, 255, 16]));
        assert_eq!(unhex("0g"), None);
        fs::remove_dir_all(&dir).unwrap();
    }
}

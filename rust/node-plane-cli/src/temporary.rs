//! Temporary access uses controller-owned operations and never repeats unknown issuance.
use crate::nodes::Node;
use serde_json::{Value, json};
use uuid::Uuid;

pub const NOTICE: &str = "After expiry or revocation, new connections are blocked. Existing connections may continue until they disconnect.";
#[derive(Clone)]
pub enum Command {
    List {
        node: Option<String>,
        page: usize,
        page_size: usize,
    },
    Servers,
    Card(String),
    Artifact(String),
    Create {
        id: Uuid,
        body: Value,
    },
    Revoke(String),
}
#[derive(Default, PartialEq, Eq)]
pub enum View {
    #[default]
    List,
    Server,
    Protocol,
    Transport,
    Duration,
    Confirm,
    Card,
    Revoke,
    Artifact,
}
#[derive(Default)]
pub struct Browser {
    pub profile: Option<Uuid>,
    pub filter: Option<Node>,
    pub view: View,
    pub items: Vec<Value>,
    pub nodes: Vec<Node>,
    pub total: usize,
    pub loaded_size: usize,
    pub page: usize,
    pub capacity: std::cell::Cell<usize>,
    pub selected: usize,
    pub card: Option<Value>,
    pub artifact: Option<Value>,
    pub node: Option<Node>,
    pub protocol: String,
    pub transport: String,
    pub seconds: u64,
    pub id: Option<Uuid>,
    pub scroll: u16,
    pub notice: String,
}
impl Browser {
    pub fn list(&self) -> Command {
        Command::List {
            node: self.filter.as_ref().map(|n| n.key.clone()),
            page: self.page,
            page_size: self.capacity.get().clamp(1, 100),
        }
    }
    pub fn new_config(&mut self) -> Command {
        self.id = Some(Uuid::new_v4());
        self.artifact = None;
        self.selected = 0;
        self.page = 0;
        if let Some(node) = self.filter.clone() {
            self.choose_node(node);
            self.list()
        } else {
            self.view = View::Server;
            Command::Servers
        }
    }
    pub fn choose_node(&mut self, node: Node) {
        self.node = Some(node);
        self.view = View::Protocol;
        let node = self.node.as_ref().unwrap();
        if node.protocols.len() == 1 {
            self.protocol = node.protocols[0].clone();
            self.choose_protocol();
        }
        self.selected = 0;
    }
    pub fn choose_protocol(&mut self) {
        self.view = View::Transport;
        let node = self.node.as_ref().unwrap();
        if self.protocol == "awg" {
            self.transport = "vpn".into();
            self.view = View::Duration;
        } else if node.xray_transports.len() == 1 {
            self.transport = node.xray_transports[0].clone();
            self.view = View::Duration;
        }
        self.selected = 0;
    }
    pub fn back(&mut self) {
        self.artifact = None;
        self.view = match self.view {
            View::Artifact | View::Revoke => View::Card,
            View::Confirm => View::Duration,
            View::Duration
                if self.protocol == "xray"
                    && self
                        .node
                        .as_ref()
                        .is_some_and(|n| n.xray_transports.len() > 1) =>
            {
                View::Transport
            }
            View::Duration | View::Transport
                if self.node.as_ref().is_some_and(|n| n.protocols.len() > 1) =>
            {
                View::Protocol
            }
            View::Duration | View::Transport | View::Protocol if self.filter.is_none() => {
                View::Server
            }
            _ => View::List,
        };
        self.selected = 0;
    }
    pub fn create(&self) -> Command {
        Command::Create {
            id: self.id.expect("creation identity"),
            body: json!({
            "node_key":self.node.as_ref().unwrap().key,"protocol":self.protocol,
            "transport":self.transport,"duration_seconds":self.seconds}),
        }
    }
}

pub fn save_artifact(
    directory: &std::path::Path,
    id: &str,
    value: &Value,
) -> anyhow::Result<std::path::PathBuf> {
    use anyhow::{Context, ensure};
    use std::{fs, io::Write};
    let id = Uuid::parse_str(id)?;
    let parent = directory.join("temporary-configs");
    let destination = parent.join(id.to_string());
    let files = value["files"]
        .as_array()
        .context("Missing configuration files")?;
    ensure!(
        !files.is_empty() && files.len() <= 2,
        "Invalid configuration files"
    );
    let mut validated = Vec::new();
    for item in files {
        let name = item["filename"].as_str().context("Missing filename")?;
        let filename = match name.rsplit('.').next() {
            Some("vpn") => "AmneziaWG.vpn",
            Some("conf") => "AmneziaWG.conf",
            Some("txt") => "VLESS.txt",
            _ => anyhow::bail!("Unsupported configuration file"),
        };
        ensure!(
            !validated.iter().any(|(name, _)| *name == filename),
            "Duplicate configuration file"
        );
        let content = item["content"].as_str().context("Missing configuration")?;
        ensure!(!content.is_empty(), "Empty configuration file");
        validated.push((filename, content));
    }
    fs::create_dir_all(&parent)?;
    ensure!(
        !fs::symlink_metadata(&parent)?.file_type().is_symlink(),
        "Unsafe configuration directory"
    );
    if let Ok(metadata) = fs::symlink_metadata(&destination) {
        ensure!(
            metadata.is_dir() && !metadata.file_type().is_symlink(),
            "Unsafe configuration directory"
        );
        for (name, content) in &validated {
            let path = destination.join(name);
            ensure!(
                !fs::symlink_metadata(&path)?.file_type().is_symlink(),
                "Unsafe configuration file"
            );
            ensure!(
                fs::read(&path)? == content.as_bytes(),
                "A different configuration is already saved"
            );
        }
        return Ok(destination);
    }
    let staging = tempfile::Builder::new()
        .prefix(".saving-")
        .tempdir_in(&parent)?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        fs::set_permissions(staging.path(), fs::Permissions::from_mode(0o700))?;
    }
    for (filename, content) in validated {
        let mut options = fs::OpenOptions::new();
        options.write(true).create_new(true);
        #[cfg(unix)]
        {
            use std::os::unix::fs::OpenOptionsExt;
            options.mode(0o600);
        }
        let mut file = options
            .open(staging.path().join(filename))
            .context("File already exists or cannot be saved")?;
        file.write_all(content.as_bytes())?;
        file.sync_all()?;
    }
    fs::rename(staging.path(), &destination)?;
    Ok(destination)
}

/// Windows Terminal must actually report Sixel; its presence alone proves nothing.
pub fn supports_images(
    protocol: ratatui_image::picker::ProtocolType,
    capabilities: &[ratatui_image::picker::Capability],
    windows_terminal: bool,
) -> bool {
    use ratatui_image::picker::{Capability, ProtocolType};
    if windows_terminal {
        return protocol == ProtocolType::Sixel && capabilities.contains(&Capability::Sixel);
    }
    match protocol {
        ProtocolType::Kitty => capabilities.contains(&Capability::Kitty),
        ProtocolType::Sixel => capabilities.contains(&Capability::Sixel),
        ProtocolType::Iterm2 => true,
        ProtocolType::Halfblocks => false,
    }
}

/// Generate the same payload that the Telegram/Amnezia import path expects.
pub fn qr_image(uri: &str) -> anyhow::Result<image::DynamicImage> {
    anyhow::ensure!(
        uri.starts_with("vless://") || uri.starts_with("vpn://"),
        "Unsupported configuration link"
    );
    let payload = uri.strip_prefix("vpn://").unwrap_or(uri);
    let code = qrcode::QrCode::with_error_correction_level(payload.as_bytes(), qrcode::EcLevel::L)?;
    let scale = 6usize;
    let width = code.width();
    let side = (width + 8) * scale;
    let image = image::GrayImage::from_fn(side as u32, side as u32, |x, y| {
        let x = x as usize / scale;
        let y = y as usize / scale;
        image::Luma([
            if x >= 4
                && y >= 4
                && x < width + 4
                && y < width + 4
                && code[(x - 4, y - 4)] == qrcode::Color::Dark
            {
                0
            } else {
                255
            },
        ])
    });
    Ok(image::DynamicImage::ImageLuma8(image))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn windows_terminal_requires_confirmed_sixel() {
        use ratatui_image::picker::{Capability, ProtocolType};
        assert!(!supports_images(ProtocolType::Halfblocks, &[], true));
        assert!(!supports_images(ProtocolType::Sixel, &[], true));
        assert!(!supports_images(
            ProtocolType::Kitty,
            &[Capability::Kitty],
            true
        ));
        assert!(supports_images(
            ProtocolType::Sixel,
            &[Capability::Sixel],
            true
        ));
        assert!(supports_images(
            ProtocolType::Kitty,
            &[Capability::Kitty],
            false
        ));
        assert!(!supports_images(ProtocolType::Kitty, &[], false));
    }

    #[test]
    fn qr_has_white_border_and_awg_import_payload() {
        let awg = qr_image("vpn://example").unwrap().to_luma8();
        assert_eq!(awg.get_pixel(0, 0)[0], 255);
        assert_eq!(awg.get_pixel(24, 24)[0], 0);
        assert_eq!(awg.width(), awg.height());
        let expected =
            qrcode::QrCode::with_error_correction_level(b"example", qrcode::EcLevel::L).unwrap();
        assert_eq!(awg.width() as usize, (expected.width() + 8) * 6);
        for y in 0..expected.width() {
            for x in 0..expected.width() {
                assert_eq!(
                    awg.get_pixel(((x + 4) * 6) as u32, ((y + 4) * 6) as u32)[0],
                    if expected[(x, y)] == qrcode::Color::Dark {
                        0
                    } else {
                        255
                    }
                );
            }
        }
        assert!(qr_image("vless://example").is_ok());
        assert!(qr_image("https://example.com").is_err());
    }

    #[test]
    fn save_is_private_repeatable_and_rejects_partial_input() {
        let directory = tempfile::tempdir().unwrap();
        let id = Uuid::new_v4().to_string();
        let invalid =
            json!({"files":[{"filename":"a.vpn","content":"vpn://secret"},{"filename":"a.conf"}]});
        assert!(save_artifact(directory.path(), &id, &invalid).is_err());
        assert!(
            !directory
                .path()
                .join("temporary-configs")
                .join(&id)
                .exists()
        );
        let artifact = json!({"files":[{"filename":"../../a.vpn","content":"vpn://secret"},{"filename":"a.conf","content":"private key"}]});
        let destination = save_artifact(directory.path(), &id, &artifact).unwrap();
        assert_eq!(
            std::fs::read_to_string(destination.join("AmneziaWG.conf")).unwrap(),
            "private key"
        );
        assert_eq!(
            save_artifact(directory.path(), &id, &artifact).unwrap(),
            destination
        );
        assert!(save_artifact(directory.path(), &id, &invalid).is_err());
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            assert_eq!(
                std::fs::metadata(&destination)
                    .unwrap()
                    .permissions()
                    .mode()
                    & 0o777,
                0o700
            );
            assert_eq!(
                std::fs::metadata(destination.join("AmneziaWG.vpn"))
                    .unwrap()
                    .permissions()
                    .mode()
                    & 0o777,
                0o600
            );
        }
    }
}

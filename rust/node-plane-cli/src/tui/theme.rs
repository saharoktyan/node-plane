//! File-backed workstation themes. Missing packaged themes are seeded once.
use crate::connections::{Connections, validate_accent};
use anyhow::{Context, Result, ensure};
use ratatui::{Frame, style::Color};
use serde::Deserialize;
use std::{fs, io::Write, path::Path};

pub(super) const SECTION_BORDER: Color = Color::Indexed(253);
const PACKAGED: [(&str, &str); 6] = [
    ("auto.json", include_str!("themes/auto.json")),
    ("peach.json", include_str!("themes/peach.json")),
    ("ocean.json", include_str!("themes/ocean.json")),
    ("lilac.json", include_str!("themes/lilac.json")),
    ("olive.json", include_str!("themes/olive.json")),
    ("glacer.json", include_str!("themes/glacer.json")),
];

#[derive(Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub(super) struct Theme {
    #[serde(skip)]
    pub id: String,
    pub name: String,
    pub accent: String,
    #[serde(default)]
    pub section_border: Option<String>,
}

impl Theme {
    fn parse(id: &str, text: &str) -> Result<Self> {
        let mut theme: Self = serde_json::from_str(text)?;
        ensure!(
            !theme.name.trim().is_empty()
                && theme.name.chars().count() <= 40
                && !theme.name.chars().any(char::is_control),
            "Use a short theme name"
        );
        validate_accent(&theme.accent)?;
        if let Some(border) = &theme.section_border {
            validate_accent(border)?;
        }
        theme.id = id.into();
        Ok(theme)
    }

    pub fn selected(&self, preferences: &Connections) -> bool {
        preferences.theme.as_ref().map_or_else(
            || self.accent.eq_ignore_ascii_case(&preferences.accent),
            |id| id == &self.id,
        )
    }
}

pub(super) struct Catalog {
    pub themes: Vec<Theme>,
    pub warnings: Vec<String>,
}

pub(super) fn defaults() -> Vec<Theme> {
    PACKAGED
        .iter()
        .map(|(id, text)| Theme::parse(id, text).expect("packaged theme"))
        .collect()
}

pub(super) fn load(state: &Path) -> Result<Catalog> {
    crate::ssh::secure_state_directory(state)?;
    let directory = state.join("themes");
    fs::create_dir_all(&directory).context("Create workstation themes directory")?;
    for (id, text) in PACKAGED {
        match fs::OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(directory.join(id))
        {
            Ok(mut file) => file.write_all(text.as_bytes())?,
            Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => {}
            Err(error) => return Err(error).context("Create packaged workstation theme"),
        }
    }
    let mut themes = Vec::new();
    let mut warnings = Vec::new();
    for entry in fs::read_dir(&directory)? {
        let entry = entry?;
        let path = entry.path();
        if !entry.file_type()?.is_file()
            || path.extension().and_then(|s| s.to_str()) != Some("json")
        {
            continue;
        }
        let Some(id) = path.file_name().and_then(|s| s.to_str()) else {
            continue;
        };
        let result = (|| {
            ensure!(entry.metadata()?.len() <= 16_384, "Theme file is too large");
            Theme::parse(id, &fs::read_to_string(&path)?)
        })();
        match result {
            Ok(theme) => themes.push(theme),
            Err(error) => {
                warnings.push(format!("Skipped theme {id}: {error}"));
                if let Some((_, text)) = PACKAGED.iter().find(|(name, _)| *name == id) {
                    themes.push(Theme::parse(id, text)?);
                }
            }
        }
    }
    themes.sort_by(|a, b| {
        let order = |id: &str| {
            PACKAGED
                .iter()
                .position(|(name, _)| *name == id)
                .unwrap_or(PACKAGED.len())
        };
        order(&a.id)
            .cmp(&order(&b.id))
            .then_with(|| a.name.to_lowercase().cmp(&b.name.to_lowercase()))
            .then_with(|| a.id.cmp(&b.id))
    });
    Ok(Catalog { themes, warnings })
}

pub(super) fn accent_color(value: &str) -> Color {
    if value == "terminal" {
        return Color::Cyan;
    }
    let rgb = value
        .strip_prefix('#')
        .and_then(|rgb| u32::from_str_radix(rgb, 16).ok())
        .unwrap_or(0x74b9ff);
    Color::Rgb((rgb >> 16) as u8, (rgb >> 8) as u8, rgb as u8)
}

pub(super) fn section_border_color(accent: Color) -> Color {
    match accent {
        Color::Rgb(r, g, b) => Color::Rgb(
            (u16::from(r) * 3 / 4) as u8,
            (u16::from(g) * 3 / 4) as u8,
            (u16::from(b) * 3 / 4) as u8,
        ),
        _ => Color::DarkGray,
    }
}

pub(super) fn apply(frame: &mut Frame, saved_accent: &str, selected: Option<&Theme>) {
    let accent = accent_color(selected.map_or(saved_accent, |theme| &theme.accent));
    let border = selected
        .and_then(|theme| theme.section_border.as_deref())
        .map_or_else(|| section_border_color(accent), accent_color);
    for cell in &mut frame.buffer_mut().content {
        if cell.fg == SECTION_BORDER {
            cell.fg = border;
        }
        if cell.fg == Color::Cyan {
            cell.fg = accent;
        }
        if cell.bg == Color::Cyan {
            cell.bg = accent;
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn seeds_loads_custom_themes_and_preserves_edits() {
        let state = tempfile::tempdir().unwrap();
        assert_eq!(load(state.path()).unwrap().themes.len(), 6);
        let directory = state.path().join("themes");
        let custom = r##"{"name":"Midnight","accent":"#123456","section_border":"#334455"}"##;
        fs::write(directory.join("midnight.json"), custom).unwrap();
        fs::write(directory.join("peach.json"), custom).unwrap();
        fs::write(
            directory.join("broken.json"),
            r##"{"name":"Oops","accent":"wrong"}"##,
        )
        .unwrap();
        let catalog = load(state.path()).unwrap();
        assert_eq!(catalog.themes.len(), 7);
        assert_eq!(catalog.warnings.len(), 1);
        assert_eq!(catalog.themes[1].accent, "#123456");
        assert_eq!(
            fs::read_to_string(directory.join("peach.json")).unwrap(),
            custom
        );
        let theme = catalog
            .themes
            .iter()
            .find(|theme| theme.id == "midnight.json")
            .unwrap();
        let mut terminal =
            ratatui::Terminal::new(ratatui::backend::TestBackend::new(2, 1)).unwrap();
        terminal
            .draw(|frame| {
                frame.buffer_mut()[(0, 0)].fg = Color::Cyan;
                frame.buffer_mut()[(1, 0)].fg = SECTION_BORDER;
                apply(frame, "terminal", Some(theme));
            })
            .unwrap();
        assert_eq!(
            terminal.backend().buffer()[(0, 0)].fg,
            Color::Rgb(0x12, 0x34, 0x56)
        );
        assert_eq!(
            terminal.backend().buffer()[(1, 0)].fg,
            Color::Rgb(0x33, 0x44, 0x55)
        );
    }
}

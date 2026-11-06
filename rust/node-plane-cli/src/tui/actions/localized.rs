//! Texts in several languages in Workstation, as the bot writes them: one
//! screen for tariff names and descriptions, announcements and the messages
//! visitors see. Each language is a line with its text or whose text it
//! shows; saving sends the value where its purpose says.
use super::super::*;
use crate::controller::Command;
use crate::localized::{Editor, Purpose, language_name};

impl App {
    /// Opens a text; a value without texts starts with the first one.
    pub(in crate::tui) fn open_text(&mut self, purpose: Purpose, value: &serde_json::Value) {
        self.controller.ltext = Some(Editor::open(purpose, value, "en"));
        self.controller.selected = 0;
        self.error.clear();
        // A stored value names every language; a new one asks for them.
        if value["shown"].is_null() {
            self.controller_command(Command::Languages);
        }
    }

    /// The editor's text and labelled controls.
    pub(in crate::tui) fn localized_view(&self) -> (String, Vec<(String, Control)>) {
        let Some(editor) = &self.controller.ltext else {
            return (String::new(), Vec::new());
        };
        if let Some((language, _)) = &editor.editing {
            let text = format!(
                "{}\nText in {} (up to {} characters).",
                editor.purpose.title(),
                language_name(language),
                editor.purpose.max_length()
            );
            let mut controls = vec![
                ("Text".into(), Control::ControllerTextInput),
                ("Done".into(), Control::ControllerTextDone),
            ];
            // A title may be left out before any is written.
            if editor.purpose == Purpose::AnnouncementTitle && editor.draft.texts.is_empty() {
                controls.push((
                    editor.purpose.clear_label().into(),
                    Control::ControllerTextClear,
                ));
            }
            controls.push(("Back".into(), Control::ControllerBack));
            return (text, controls);
        }
        let mut controls = Vec::new();
        for (index, language) in editor.languages.iter().enumerate() {
            let name = language_name(language);
            if editor.draft.texts.contains_key(language) {
                controls.push((
                    format!("Edit · {name}"),
                    Control::ControllerTextWrite(index),
                ));
                if *language != editor.draft.source {
                    controls.push((
                        format!("Make source · {name}"),
                        Control::ControllerTextSource(index),
                    ));
                    controls.push((
                        format!("Remove · {name}"),
                        Control::ControllerTextRemove(index),
                    ));
                }
            } else {
                controls.push((
                    format!("Write · {name}"),
                    Control::ControllerTextWrite(index),
                ));
                if editor.draft.texts.len() > 1 {
                    controls.push((
                        format!("Show another · {name}"),
                        Control::ControllerTextFallback(index),
                    ));
                }
            }
        }
        controls.push((
            match editor.purpose {
                Purpose::Announcement => "Preview",
                Purpose::AnnouncementTitle if self.controller.announcement_texts.is_none() => {
                    "Next"
                }
                Purpose::AnnouncementTitle => "Preview",
                _ => "Save",
            }
            .into(),
            Control::ControllerTextSave,
        ));
        if editor.purpose.optional() {
            controls.push((
                editor.purpose.clear_label().into(),
                Control::ControllerTextClear,
            ));
        }
        controls.push(("Back".into(), Control::ControllerBack));
        (editor.summary(), controls)
    }

    /// Handles an editor control; false for any other control.
    pub(in crate::tui) fn activate_localized(&mut self, control: Control) -> bool {
        let Some(editor) = &mut self.controller.ltext else {
            return false;
        };
        let languages = editor.languages.clone();
        let language = |index: usize| languages.get(index).cloned().unwrap_or_default();
        match control {
            Control::ControllerTextWrite(index) => {
                editor.write(index);
                self.controller.selected = 0;
            }
            Control::ControllerTextSource(index) => editor.draft.make_source(&language(index)),
            Control::ControllerTextRemove(index) => {
                editor.draft.remove(&language(index));
                self.controller.selected = 0;
            }
            Control::ControllerTextFallback(index) => {
                editor.draft.next_fallback(&language(index), &languages);
            }
            Control::ControllerTextInput => {}
            Control::ControllerTextDone => match editor.done() {
                Ok(()) => {
                    self.error.clear();
                    self.controller.selected = 0;
                }
                Err(error) => self.error = error,
            },
            Control::ControllerTextSave => {
                let (purpose, value) = (editor.purpose, editor.draft.to_value());
                self.save_text(purpose, Some(value));
            }
            Control::ControllerTextClear => {
                let purpose = editor.purpose;
                self.save_text(purpose, None);
            }
            _ => return false,
        }
        true
    }

    fn save_text(&mut self, purpose: Purpose, value: Option<serde_json::Value>) {
        let value = value.unwrap_or(serde_json::Value::Null);
        match purpose {
            Purpose::Announcement => {
                // A changed text is a new announcement, so it gets a new key.
                if self.controller.announcement_texts.as_ref() != Some(&value) {
                    self.controller.announcement_key = Some(uuid::Uuid::new_v4());
                }
                self.controller.announcement_texts = Some(value.clone());
                let titles = self
                    .controller
                    .announcement_titles
                    .clone()
                    .unwrap_or_default();
                self.controller_command(Command::AnnouncementPreview(value, titles));
            }
            Purpose::AnnouncementTitle => {
                if self.controller.announcement_titles.as_ref() != Some(&value) {
                    self.controller.announcement_key = Some(uuid::Uuid::new_v4());
                }
                self.controller.announcement_titles = Some(value.clone());
                // On to the text, or straight to the preview when it is written.
                match self.controller.announcement_texts.clone() {
                    Some(texts) => {
                        self.controller_command(Command::AnnouncementPreview(texts, value))
                    }
                    None => self.open_text(Purpose::Announcement, &serde_json::Value::Null),
                }
            }
            Purpose::GateMessage | Purpose::WelcomeMessage => {
                let field = if purpose == Purpose::GateMessage {
                    "gate_messages"
                } else {
                    "welcome_messages"
                };
                self.controller_command(Command::PolicySave(
                    "access-requests",
                    serde_json::json!({ field: value }),
                ));
            }
            Purpose::TariffNew => self.controller_command(Command::TariffCreate(value)),
            Purpose::TariffTitles | Purpose::TariffDescriptions => {
                let field = if purpose == Purpose::TariffTitles {
                    "titles"
                } else {
                    "descriptions"
                };
                if let Some((id, revision)) = self.open_tariff_id() {
                    self.controller_command(Command::TariffSave(
                        id,
                        serde_json::json!({ field: value }),
                        revision,
                    ));
                }
            }
        }
    }

    /// Back from writing one language to the editor, else out of it.
    pub(in crate::tui) fn localized_back(&mut self) -> bool {
        let Some(editor) = &mut self.controller.ltext else {
            return false;
        };
        if editor.editing.is_some() && !editor.draft.texts.is_empty() {
            editor.editing = None;
        } else {
            self.controller.ltext = None;
        }
        self.controller.selected = 0;
        self.error.clear();
        true
    }
}

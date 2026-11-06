//! Texts written in several languages, as the bot's editor writes them:
//! tariff titles and descriptions, announcements, the messages visitors see.
//!
//! A value is `{"source": "ru", "texts": {…}, "fallback": {"en": "ru"}}`.
//! Only the source language needs a text; a language without its own shows
//! the one chosen for it, else the source's. The controller lists the
//! languages it supports; a value it returns names each in `shown`.
use std::collections::BTreeMap;

use serde_json::{Value, json};

/// The languages when the controller has not said yet.
pub const LANGUAGES: [&str; 2] = ["en", "ru"];

pub fn language_name(code: &str) -> String {
    match code {
        "en" => "English".into(),
        "ru" => "Russian".into(),
        other => other.to_uppercase(),
    }
}

/// What a text is for: its limits and where saving sends it.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Purpose {
    Announcement,
    GateMessage,
    WelcomeMessage,
    TariffNew,
    TariffTitles,
    TariffDescriptions,
}

impl Purpose {
    pub fn title(self) -> &'static str {
        match self {
            Self::Announcement => "Announcement",
            Self::GateMessage => "Stub message for users when access requests are disabled",
            Self::WelcomeMessage => "Welcome message for users requesting access",
            Self::TariffNew => "New tariff · name",
            Self::TariffTitles => "Tariff name",
            Self::TariffDescriptions => "Tariff description",
        }
    }

    pub fn max_length(self) -> usize {
        match self {
            Self::Announcement => 3000,
            Self::GateMessage | Self::WelcomeMessage => 500,
            Self::TariffNew | Self::TariffTitles => 64,
            Self::TariffDescriptions => 1000,
        }
    }

    pub fn multiline(self) -> bool {
        !matches!(self, Self::TariffNew | Self::TariffTitles)
    }

    /// A text that may be removed altogether.
    pub fn optional(self) -> bool {
        self == Self::TariffDescriptions
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Draft {
    pub source: String,
    pub texts: BTreeMap<String, String>,
    pub fallback: BTreeMap<String, String>,
}

impl Draft {
    pub fn new(source: &str) -> Self {
        Self {
            source: source.into(),
            texts: BTreeMap::new(),
            fallback: BTreeMap::new(),
        }
    }

    pub fn from_value(value: &Value) -> Option<Self> {
        let strings = |key: &str| -> BTreeMap<String, String> {
            value[key]
                .as_object()
                .map(|map| {
                    map.iter()
                        .filter_map(|(k, v)| Some((k.clone(), v.as_str()?.to_owned())))
                        .collect()
                })
                .unwrap_or_default()
        };
        Some(Self {
            source: value["source"].as_str()?.into(),
            texts: strings("texts"),
            fallback: strings("fallback"),
        })
    }

    /// The draft as the API takes it: no empty texts, no fallback to the
    /// source or to a language without a text.
    pub fn to_value(&self) -> Value {
        let texts: BTreeMap<_, _> = self
            .texts
            .iter()
            .filter(|(_, text)| !text.trim().is_empty())
            .collect();
        let fallback: BTreeMap<_, _> = self
            .fallback
            .iter()
            .filter(|(name, target)| {
                !texts.contains_key(name) && texts.contains_key(target) && **target != self.source
            })
            .collect();
        json!({"source": self.source, "texts": texts, "fallback": fallback})
    }

    /// Whose text a language shows.
    pub fn shown<'a>(&'a self, language: &'a str) -> &'a str {
        if self.texts.contains_key(language) {
            language
        } else {
            self.fallback.get(language).unwrap_or(&self.source)
        }
    }

    pub fn set(&mut self, language: &str, text: &str) {
        if self.texts.is_empty() {
            self.source = language.into();
        }
        self.texts.insert(language.into(), text.trim().into());
        self.fallback.remove(language);
    }

    pub fn make_source(&mut self, language: &str) {
        if self.texts.contains_key(language) {
            self.source = language.into();
        }
    }

    /// Removes a language's text; the source keeps its own.
    pub fn remove(&mut self, language: &str) {
        if language != self.source && self.texts.remove(language).is_some() {
            self.fallback.retain(|_, target| target != language);
        }
    }

    /// Shows the next written language's text, around the list.
    pub fn next_fallback(&mut self, language: &str, languages: &[String]) {
        if self.texts.contains_key(language) {
            return;
        }
        let written: Vec<_> = languages
            .iter()
            .filter(|name| self.texts.contains_key(*name))
            .collect();
        if written.is_empty() {
            return;
        }
        let current = self.shown(language).to_owned();
        let index = written.iter().position(|name| **name == current);
        let next = written[index.map_or(0, |index| (index + 1) % written.len())].clone();
        self.fallback.insert(language.into(), next);
    }
}

/// The languages a value names, else the ones known without asking.
pub fn languages_of(value: &Value) -> Vec<String> {
    value["shown"]
        .as_object()
        .filter(|shown| !shown.is_empty())
        .map(|shown| shown.keys().cloned().collect())
        .unwrap_or_else(|| LANGUAGES.iter().map(|name| (*name).to_owned()).collect())
}

/// One text being edited in Workstation.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Editor {
    pub purpose: Purpose,
    pub languages: Vec<String>,
    pub draft: Draft,
    /// The language being written and its text so far.
    pub editing: Option<(String, String)>,
}

impl Editor {
    /// Opens a value, or a new text in the given language written first.
    pub fn open(purpose: Purpose, value: &Value, language: &str) -> Self {
        let languages = languages_of(value);
        let draft = Draft::from_value(value).unwrap_or_else(|| {
            Draft::new(if languages.iter().any(|name| name == language) {
                language
            } else {
                &languages[0]
            })
        });
        let editing = draft
            .texts
            .is_empty()
            .then(|| (draft.source.clone(), String::new()));
        Self {
            purpose,
            languages,
            draft,
            editing,
        }
    }

    pub fn write(&mut self, index: usize) {
        if let Some(language) = self.languages.get(index) {
            let text = self.draft.texts.get(language).cloned().unwrap_or_default();
            self.editing = Some((language.clone(), text));
        }
    }

    /// Keeps the written text; an error when it is empty or too long.
    pub fn done(&mut self) -> Result<(), String> {
        let Some((language, text)) = &self.editing else {
            return Ok(());
        };
        let length = text.trim().chars().count();
        if length == 0 || length > self.purpose.max_length() {
            return Err(format!(
                "Enter from 1 to {} characters.",
                self.purpose.max_length()
            ));
        }
        if !self.purpose.multiline() && text.trim().contains('\n') {
            return Err("Use one line.".into());
        }
        let (language, text) = (language.clone(), text.clone());
        self.draft.set(&language, &text);
        self.editing = None;
        Ok(())
    }

    /// The text being typed, its limit and whether it may have several lines.
    pub fn input(&mut self) -> Option<(&mut String, usize, bool)> {
        let purpose = self.purpose;
        let (_, text) = self.editing.as_mut()?;
        Some((text, purpose.max_length(), purpose.multiline()))
    }

    /// The editor's text: each language with its text or whose it shows.
    pub fn summary(&self) -> String {
        let mut text = format!(
            "{}\nOnly the source language needs a text. A language without its own shows the one chosen for it.",
            self.purpose.title()
        );
        for language in &self.languages {
            let name = language_name(language);
            match self.draft.texts.get(language) {
                Some(own) if *language == self.draft.source => {
                    text.push_str(&format!("\n\n{name} · source\n{own}"))
                }
                Some(own) => text.push_str(&format!("\n\n{name}\n{own}")),
                None => text.push_str(&format!(
                    "\n\n{name}\nShows the {} text.",
                    language_name(self.draft.shown(language))
                )),
            }
        }
        text
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn languages(names: &[&str]) -> Vec<String> {
        names.iter().map(|name| (*name).to_owned()).collect()
    }

    #[test]
    fn a_new_text_starts_in_the_admins_language() {
        let mut editor = Editor::open(Purpose::TariffNew, &Value::Null, "ru");
        assert_eq!(editor.editing, Some(("ru".into(), String::new())));
        editor.editing.as_mut().unwrap().1.push_str(" Месяц ");
        editor.done().unwrap();
        assert_eq!(
            editor.draft.to_value(),
            json!({"source":"ru","texts":{"ru":"Месяц"},"fallback":{}})
        );
        assert!(
            editor
                .summary()
                .contains("English\nShows the Russian text.")
        );
    }

    #[test]
    fn titles_stay_on_one_line_and_within_the_limit() {
        let mut editor = Editor::open(Purpose::TariffTitles, &Value::Null, "en");
        editor.editing = Some(("en".into(), "one\ntwo".into()));
        assert!(editor.done().is_err());
        editor.editing = Some(("en".into(), "x".repeat(65)));
        assert!(editor.done().is_err());
        assert!(editor.draft.texts.is_empty());
    }

    #[test]
    fn source_fallback_and_removal() {
        let value = json!({"source":"en","texts":{"en":"Month","ru":"Месяц"},"fallback":{},
                           "shown":{"de":"en","en":"en","ru":"ru"}});
        let mut editor = Editor::open(Purpose::TariffTitles, &value, "en");
        assert_eq!(editor.languages, languages(&["de", "en", "ru"]));
        assert_eq!(editor.editing, None);
        editor.draft.next_fallback("de", &editor.languages.clone());
        assert_eq!(editor.draft.shown("de"), "ru");
        assert_eq!(editor.draft.to_value()["fallback"], json!({"de":"ru"}));
        // The fallback to the source is the default, so it is not sent.
        editor.draft.next_fallback("de", &editor.languages.clone());
        assert_eq!(editor.draft.to_value()["fallback"], json!({}));
        editor.draft.next_fallback("de", &editor.languages.clone());
        editor.draft.make_source("ru");
        editor.draft.remove("ru");
        assert!(editor.draft.texts.contains_key("ru"));
        editor.draft.make_source("en");
        editor.draft.remove("ru");
        assert_eq!(
            editor.draft.to_value(),
            json!({"source":"en","texts":{"en":"Month"},"fallback":{}})
        );
    }
}

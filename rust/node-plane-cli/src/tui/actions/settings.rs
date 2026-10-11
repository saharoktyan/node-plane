use super::super::*;

impl App {
    pub(in crate::tui) fn activate_settings(&mut self, control: Control) -> Option<KeyEvent> {
        match control {
            Control::QuickProfile => {
                self.activate_profile();
                None
            }
            Control::CycleProfile(backwards) => {
                if let Err(error) = self.cycle_profile(backwards) {
                    self.error = error.to_string();
                }
                None
            }
            Control::Settings => {
                self.open_settings();
                None
            }
            Control::SettingsPage(page) => {
                if page == SettingsPage::Appearance {
                    self.accent_input = if self.connections.accent == "terminal" {
                        String::new()
                    } else {
                        self.connections.accent.clone()
                    };
                }
                self.screen = Screen::SettingsPage(page);
                self.settings_selected = 1;
                self.error.clear();
                None
            }
            Control::Accent(index) => {
                self.settings_selected = index + 1;
                let mut next = self.connections.clone();
                let selected = self.themes.get(index)?;
                next.accent = selected.accent.clone();
                next.theme = Some(selected.id.clone());
                match next.save(&self.state_dir) {
                    Ok(()) => {
                        self.connections = next;
                        self.error.clear();
                    }
                    Err(error) => self.error = error.to_string(),
                }
                None
            }
            Control::AccentInput => {
                self.settings_selected = self.themes.len() + 1;
                None
            }
            Control::SaveAccent => {
                self.settings_selected = self.themes.len() + 2;
                let mut next = self.connections.clone();
                next.accent = self.accent_input.trim().to_ascii_lowercase();
                next.theme = None;
                match next.save(&self.state_dir) {
                    Ok(()) => {
                        self.connections = next;
                        self.error.clear();
                    }
                    Err(error) => self.error = error.to_string(),
                }
                None
            }
            Control::DeleteInstallation => {
                if let Some(profile) = self.connections.installations.get(self.settings_profile) {
                    self.screen = Screen::DeleteInstallation(profile.id);
                    self.confirm = false;
                }
                None
            }
            Control::ConfirmDelete(confirmed) => {
                if confirmed {
                    if let Screen::DeleteInstallation(id) = self.screen
                        && let Err(error) = self.delete_profile(id)
                    {
                        self.error = error.to_string();
                    }
                } else {
                    self.screen = Screen::SettingsPage(SettingsPage::Profiles);
                }
                None
            }
            Control::SelfUpdate => {
                self.self_requested = true;
                None
            }
            Control::CheckWorkstationUpdates => {
                self.self_check_requested = true;
                None
            }
            Control::Installation(index) => {
                self.settings_profile = index;
                self.settings_selected = index + 1;
                None
            }
            Control::UseInstallation => {
                if let Err(error) = self.use_selected() {
                    self.error = error.to_string();
                }
                None
            }
            Control::EditInstallation => {
                self.begin_edit(false);
                None
            }
            Control::NewInstallation => {
                self.begin_edit(true);
                None
            }
            Control::RepairMode => {
                if let Some(position) = self.form.visible().iter().position(|&i| i == 11) {
                    self.quick_focus = false;
                    self.form.selected = position + 1;
                    toggle_repair(&mut self.form.fields[11]);
                }
                None
            }
            Control::Channel(editor, backwards) => {
                self.quick_focus = false;
                if editor {
                    if let Some((_, fields, selected)) = &mut self.editor {
                        *selected = 4;
                        cycle_channel(&mut fields[4], backwards);
                    }
                } else if let Some(position) = self.form.visible().iter().position(|&i| i == 3) {
                    self.form.selected = position + 1;
                    cycle_channel(&mut self.form.fields[3], backwards);
                }
                None
            }
            Control::EditField(index) => {
                if let Some(editor) = &mut self.editor {
                    editor.2 = index;
                }
                None
            }
            Control::SaveInstallation => {
                if let Err(error) = self.save_edit() {
                    self.error = error.to_string();
                }
                None
            }
            Control::CancelEdit => {
                self.editor = None;
                self.screen = Screen::SettingsPage(SettingsPage::Profiles);
                self.error.clear();
                None
            }
            _ => None,
        }
    }
    pub(in crate::tui) fn open_settings(&mut self) {
        self.quick_focus = false;
        self.settings_selected = match self.screen {
            Screen::SettingsPage(page) => SettingsPage::ALL
                .iter()
                .position(|entry| *entry == page)
                .map_or(1, |index| index + 1),
            _ => 0,
        };
        self.screen = Screen::Settings;
        self.error.clear();
    }
    pub(in crate::tui) fn profile_controls(&self) -> Vec<Control> {
        let mut controls: Vec<_> = (0..self.connections.installations.len())
            .map(Control::Installation)
            .collect();
        if !controls.is_empty() {
            controls.extend([
                Control::UseInstallation,
                Control::EditInstallation,
                Control::NewInstallation,
                Control::DeleteInstallation,
            ]);
        } else {
            controls.push(Control::NewInstallation);
        }
        controls.push(Control::Settings);
        controls
    }
    pub(in crate::tui) fn delete_profile(&mut self, id: uuid::Uuid) -> Result<()> {
        let mut next = self.connections.clone();
        let removed = next
            .installations
            .iter()
            .find(|profile| profile.id == id)
            .cloned();
        ensure!(
            next.installations.iter().any(|profile| profile.id == id),
            "Saved profile no longer exists."
        );
        next.installations.retain(|profile| profile.id != id);
        if next.selected == Some(id) {
            next.selected = None;
        }
        next.save(&self.state_dir)?;
        if self
            .form
            .saved
            .as_ref()
            .is_some_and(|profile| profile.id == id)
        {
            self.form.saved = None;
            self.form.fields[6].zeroize();
            self.password.zeroize();
            for index in [0, 3, 4, 5, 7, 8] {
                self.form.fields[index].clear();
            }
            self.form.fields[1] = "22".into();
            self.form.fields[2] = "root".into();
            self.form.fields[9] = "22".into();
            self.form.fields[10] = "root".into();
            self.form.selected = 0;
        }
        self.connections = next;
        self.settings_profile = self
            .settings_profile
            .min(self.connections.installations.len().saturating_sub(1));
        self.settings_selected = 1;
        self.screen = Screen::SettingsPage(SettingsPage::Profiles);
        self.error.clear();
        if let Some(removed) = removed
            && !self
                .connections
                .installations
                .iter()
                .any(|p| p.host.eq_ignore_ascii_case(&removed.host) && p.port == removed.port)
        {
            crate::ssh::forget_host(&self.state_dir, &removed.host, removed.port)?;
        }
        Ok(())
    }
    pub(in crate::tui) fn profile_index(&self) -> usize {
        self.form
            .saved
            .as_ref()
            .and_then(|profile| {
                self.connections
                    .installations
                    .iter()
                    .position(|p| p.id == profile.id)
            })
            .unwrap_or(self.connections.installations.len())
    }
    pub(in crate::tui) fn cycle_profile(&mut self, backwards: bool) -> Result<()> {
        let total = self.connections.installations.len() + 1;
        let next = (self.profile_index() + if backwards { total - 1 } else { 1 }) % total;
        let mut preferences = self.connections.clone();
        preferences.selected = preferences.installations.get(next).map(|p| p.id);
        preferences.save(&self.state_dir)?;
        if let Some(profile) = preferences.installations.get(next) {
            self.form.use_installation(profile);
            self.settings_profile = next;
        } else {
            self.form.fields[6].zeroize();
            self.form.saved = None;
            for index in [0, 3, 4, 5, 7, 8] {
                self.form.fields[index].clear();
            }
            self.form.fields[1] = "22".into();
            self.form.fields[2] = "root".into();
            self.form.fields[9] = "22".into();
            self.form.fields[10] = "root".into();
            self.form.selected = 0;
        }
        self.connections = preferences;
        self.quick_focus = true;
        self.screen = Screen::Idle;
        self.hovered = None;
        self.pointer_active = false;
        self.editor = None;
        self.error.clear();
        Ok(())
    }
    pub(in crate::tui) fn activate_profile(&mut self) {
        if let Some(profile) = &self.form.saved {
            if let Some(index) = self
                .connections
                .installations
                .iter()
                .position(|saved| saved.id == profile.id)
            {
                self.settings_profile = index;
                self.begin_edit(false);
            }
        } else {
            self.begin_edit(true);
        }
    }
    pub(in crate::tui) fn begin_edit(&mut self, new: bool) {
        let was_saved = self.form.saved.is_some();
        if new && was_saved {
            let mut preferences = self.connections.clone();
            preferences.selected = None;
            if let Err(error) = preferences.save(&self.state_dir) {
                self.error = error.to_string();
                return;
            }
            self.connections = preferences;
        }
        let profile = if new {
            self.form.installation()
        } else {
            let Some(profile) = self.connections.installations.get(self.settings_profile) else {
                return;
            };
            profile.clone()
        };
        let (id, mut fields) = if new && self.form.saved.is_some() {
            (
                None,
                vec![
                    String::new(),
                    String::new(),
                    "22".into(),
                    "root".into(),
                    String::new(),
                    String::new(),
                    String::new(),
                ],
            )
        } else {
            (
                if new { None } else { Some(profile.id) },
                vec![
                    profile.name,
                    profile.host,
                    profile.port.to_string(),
                    profile.user,
                    profile.branch,
                    profile.admin_ids,
                    profile.account,
                ],
            )
        };
        if new {
            fields[4] = "dev".into();
        }
        self.editor = Some((id, fields, 0));
        if new {
            self.form.saved = None;
            self.form.fields[6].zeroize();
            if was_saved {
                for index in [0, 3, 4, 5, 7, 8] {
                    self.form.fields[index].clear();
                }
                self.form.fields[1] = "22".into();
                self.form.fields[2] = "root".into();
                self.form.fields[9] = "22".into();
                self.form.fields[10] = "root".into();
            }
        }
        self.quick_focus = false;
        self.screen = Screen::EditInstallation;
        self.error.clear();
    }
    pub(in crate::tui) fn save_edit(&mut self) -> Result<()> {
        let (id, fields, _) = self.editor.as_ref().unwrap();
        let profile = Installation {
            installed: id
                .and_then(|id| self.connections.installations.iter().find(|p| p.id == id))
                .is_some_and(|p| {
                    p.installed
                        && p.matches(
                            fields[1].trim(),
                            fields[2].parse().unwrap_or(0),
                            fields[3].trim(),
                        )
                }),
            id: id.unwrap_or_else(uuid::Uuid::new_v4),
            name: fields[0].trim().into(),
            host: fields[1].trim().into(),
            port: fields[2].parse()?,
            user: fields[3].trim().into(),
            branch: fields[4].trim().into(),
            admin_ids: fields[5].trim().into(),
            account: fields[6].trim().into(),
        };
        profile.validate()?;
        let mut next = self.connections.clone();
        if let Some(existing) = next.installations.iter_mut().find(|p| p.id == profile.id) {
            *existing = profile.clone();
        } else {
            next.installations.push(profile.clone());
        }
        next.selected = Some(profile.id);
        next.save(&self.state_dir)?;
        self.connections = next;
        self.form.use_installation(&profile);
        self.quick_focus = false;
        self.editor = None;
        self.screen = Screen::SettingsPage(SettingsPage::Profiles);
        self.settings_selected = self
            .connections
            .installations
            .iter()
            .position(|p| p.id == profile.id)
            .unwrap()
            + 1;
        self.settings_profile = self.settings_selected - 1;
        self.error.clear();
        Ok(())
    }
    pub(in crate::tui) fn use_selected(&mut self) -> Result<()> {
        self.fresh_installation = false;
        let Some(profile) = self
            .connections
            .installations
            .get(self.settings_profile)
            .cloned()
        else {
            return Ok(());
        };
        let mut next = self.connections.clone();
        next.selected = Some(profile.id);
        next.save(&self.state_dir)?;
        self.connections = next;
        self.form.use_installation(&profile);
        self.quick_focus = false;
        self.screen = Screen::Form;
        self.error.clear();
        Ok(())
    }
    pub(in crate::tui) fn remember_form(&mut self) -> Result<()> {
        let mut profile = self.form.installation();
        if self.form.saved.is_none()
            && let Some(existing) = self
                .connections
                .installations
                .iter()
                .find(|p| p.matches(&profile.host, profile.port, &profile.user))
        {
            profile.id = existing.id;
            profile.name = existing.name.clone();
        }
        profile.validate()?;
        let mut next = self.connections.clone();
        if let Some(existing) = next.installations.iter_mut().find(|p| p.id == profile.id) {
            *existing = profile.clone();
        } else {
            next.installations.push(profile.clone());
        }
        next.selected = Some(profile.id);
        next.save(&self.state_dir)?;
        self.connections = next;
        self.form.saved = Some(profile);
        Ok(())
    }
}

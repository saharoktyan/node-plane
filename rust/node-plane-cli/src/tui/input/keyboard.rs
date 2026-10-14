use super::super::*;

impl App {
    pub(in crate::tui) fn normalize_backspace(&self, key: &mut KeyEvent) {
        if key.code != KeyCode::Backspace {
            return;
        }
        let input = if self.exit || self.discard.is_some() {
            false
        } else if let Some((prompt, _)) = &self.prompt {
            matches!(prompt, Prompt::Password { .. })
        } else {
            match self.screen {
                Screen::Controller => matches!(
                    self.controller_controls().get(self.controller.selected),
                    Some(
                        Control::ControllerDefaultsInput
                            | Control::ControllerAnnouncementInput
                            | Control::ControllerPhrase
                    )
                ),
                Screen::Access => matches!(
                    self.access_controls().get(self.access.selected),
                    Some(
                        Control::AccessSearch
                            | Control::AccessField(_)
                            | Control::AccessDeviceName
                            | Control::AccessDurationInput
                            | Control::AccessIdentity
                    )
                ),
                Screen::Nodes => matches!(
                    self.nodes_controls().get(self.nodes_selected),
                    Some(Control::NodesSearch | Control::NodesEditField(_))
                ),
                Screen::NodeWizard => self
                    .node_wizard
                    .as_ref()
                    .is_some_and(|wizard| wizard.field().is_some()),
                Screen::Form => {
                    self.form.selected > 0
                        && self
                            .form
                            .visible()
                            .get(self.form.selected - 1)
                            .is_some_and(|index| !matches!(index, 3 | 11))
                }
                Screen::EditInstallation => {
                    self.editor.as_ref().is_some_and(|(_, fields, selected)| {
                        *selected < fields.len() && *selected != 4
                    })
                }
                Screen::SettingsPage(SettingsPage::Appearance) => {
                    self.settings_selected == self.themes.len() + 1
                }
                _ => false,
            }
        };
        if !input {
            key.code = KeyCode::Esc;
        }
    }

    pub(in crate::tui) fn appearance_key(&mut self, key: KeyCode) {
        self.remember_navigation_focus(None);
        self.appearance_key_inner(key);
        self.restore_navigation_context();
    }

    fn appearance_key_inner(&mut self, key: KeyCode) {
        if self.move_spatial(key) {
            return;
        }
        let count = self.themes.len() + 3;
        match key {
            KeyCode::Esc => self.open_settings(),
            KeyCode::Down | KeyCode::Tab => {
                self.settings_selected = (self.settings_selected + 1).min(count)
            }
            KeyCode::Up | KeyCode::BackTab => {
                self.settings_selected = self.settings_selected.saturating_sub(1).max(1)
            }
            KeyCode::Char(c)
                if self.settings_selected == self.themes.len() + 1
                    && self.accent_input.len() < 7
                    && (c == '#' || c.is_ascii_hexdigit()) =>
            {
                self.accent_input.push(c)
            }
            KeyCode::Backspace if self.settings_selected == self.themes.len() + 1 => {
                self.accent_input.pop();
            }
            KeyCode::Enter => {
                let control = match self.settings_selected {
                    i if i > 0 && i <= self.themes.len() => Control::Accent(i - 1),
                    i if i == self.themes.len() + 1 || i == self.themes.len() + 2 => {
                        Control::SaveAccent
                    }
                    _ => Control::Settings,
                };
                self.activate(control);
            }
            _ => {}
        }
    }
    pub(in crate::tui) fn controller_key(&mut self, key: crossterm::event::KeyEvent) {
        self.remember_navigation_focus(None);
        self.controller_key_inner(key);
        self.restore_navigation_context();
    }

    fn controller_key_inner(&mut self, key: crossterm::event::KeyEvent) {
        if key.code == KeyCode::Char('c') && key.modifiers.contains(KeyModifiers::CONTROL) {
            self.exit = true;
            self.exit_confirm = false;
            return;
        }
        if self.controller_sidebar {
            match key.code {
                KeyCode::Down | KeyCode::Up => self.navigate(key.code == KeyCode::Up),
                KeyCode::Enter | KeyCode::Right | KeyCode::Tab => {
                    self.controller_sidebar = false;
                    self.quick_focus = false;
                }
                KeyCode::Esc => {
                    self.exit = true;
                    self.exit_confirm = false;
                }
                _ => {}
            }
            return;
        }
        if key.code == KeyCode::Esc {
            self.activate(Control::ControllerBack);
            return;
        }
        let controls = self.controller_controls();
        if controls.is_empty() {
            return;
        }
        let current = self.controller.selected.min(controls.len() - 1);
        if self.controller.defaults.is_some()
            && self.controller.defaults_input.is_none()
            && matches!(
                key.code,
                KeyCode::Up | KeyCode::Down | KeyCode::Left | KeyCode::Right
            )
        {
            let width = self.controller_width.get();
            let mut areas = defaults_rects(&controls, Rect::new(0, 0, width, 0));
            let height = defaults_rows(&controls, width).len() as u16 * 3;
            if let Some(index) = controls
                .iter()
                .position(|control| *control == Control::ControllerBack)
            {
                areas[index] = Rect::new(width.saturating_sub(7), height, 7, 3);
            }
            if let Some(next) = directional_index(&areas, current, key.code) {
                self.controller.selected = next;
            }
            return;
        }
        if controls[current] == Control::ControllerAnnouncementInput
            && let Some(value) = &mut self.controller.announcement_draft
            && self
                .text_input
                .borrow_mut()
                .edit("Message", value, key, 3000)
        {
            return;
        }
        match key.code {
            KeyCode::Enter if controls[current] == Control::ControllerAnnouncementInput => {
                if let Some(value) = &mut self.controller.announcement_draft
                    && value.chars().count() < 3000
                {
                    value.push('\n');
                }
            }
            KeyCode::Enter => {
                self.activate(controls[current]);
            }
            KeyCode::Down => self.controller.selected = (current + 1).min(controls.len() - 1),
            KeyCode::Up => self.controller.selected = current.saturating_sub(1),
            KeyCode::Tab => self.controller.selected = (current + 1) % controls.len(),
            KeyCode::BackTab => {
                self.controller.selected = (current + controls.len() - 1) % controls.len()
            }
            KeyCode::Left | KeyCode::Right => {
                let visible: Vec<_> = controls
                    .iter()
                    .enumerate()
                    .filter_map(|(index, c)| {
                        self.hits
                            .iter()
                            .find(|h| h.control == *c)
                            .map(|h| (index, h.area))
                    })
                    .collect();
                if let Some(position) = visible.iter().position(|(index, _)| *index == current) {
                    let rects: Vec<_> = visible.iter().map(|(_, area)| *area).collect();
                    if let Some(next) = directional_index(&rects, position, key.code) {
                        self.controller.selected = visible[next].0;
                    }
                }
            }
            KeyCode::Char(c)
                if controls[current] == Control::ControllerDefaultsInput && !c.is_control() =>
            {
                if let Some((_, value)) = &mut self.controller.defaults_input
                    && value.chars().count() < 500
                {
                    value.push(c);
                }
            }
            KeyCode::Backspace if controls[current] == Control::ControllerDefaultsInput => {
                if let Some((_, value)) = &mut self.controller.defaults_input {
                    value.pop();
                }
            }
            KeyCode::Char(c)
                if controls[current] == Control::ControllerAnnouncementInput && !c.is_control() =>
            {
                if let Some(value) = &mut self.controller.announcement_draft
                    && value.chars().count() < 3000
                {
                    value.push(c);
                }
            }
            KeyCode::Backspace if controls[current] == Control::ControllerAnnouncementInput => {
                if let Some(value) = &mut self.controller.announcement_draft {
                    value.pop();
                }
            }
            KeyCode::Char(c)
                if controls[current] == Control::ControllerPhrase && !c.is_control() =>
            {
                if self.controller.phrase.len() < 64 {
                    self.controller.phrase.push(c);
                }
            }
            KeyCode::Backspace if controls[current] == Control::ControllerPhrase => {
                self.controller.phrase.pop();
            }
            _ => {}
        }
    }
    pub(in crate::tui) fn access_key(&mut self, key: KeyEvent) {
        self.remember_navigation_focus(None);
        self.access_key_inner(key);
        self.restore_navigation_context();
    }

    fn access_key_inner(&mut self, key: KeyEvent) {
        if self.access.result.is_some()
            && matches!(key.code, KeyCode::Esc | KeyCode::Backspace | KeyCode::Enter)
        {
            self.activate(Control::AccessResultClose);
            return;
        }
        if self.access.provision.is_some() {
            if key.code == KeyCode::Esc {
                self.activate(Control::AccessProvisionCancel);
                return;
            }
            if self.access_controls().get(self.access.selected) == Some(&Control::AccessIdentity)
                && let Some(draft) = &mut self.access.provision
            {
                match key.code {
                    KeyCode::Char(c) if !c.is_control() && draft.identity.chars().count() < 64 => {
                        draft.identity.push(c);
                        return;
                    }
                    KeyCode::Backspace => {
                        draft.identity.pop();
                        return;
                    }
                    KeyCode::Enter => {
                        self.activate(Control::AccessProvisionNext);
                        return;
                    }
                    _ => {}
                }
            }
        }

        let controls = self.access_controls();
        if controls.get(self.access.selected) == Some(&Control::AccessDeviceName)
            && let Some(editor) = self.access.devices.as_mut().and_then(|d| d.editor.as_mut())
        {
            match key.code {
                KeyCode::Char(c)
                    if !key.modifiers.contains(KeyModifiers::CONTROL) && !c.is_control() =>
                {
                    if editor.name.chars().count() < 64 {
                        editor.name.push(c);
                        editor.key = uuid::Uuid::new_v4();
                    }
                    return;
                }
                KeyCode::Backspace => {
                    editor.name.pop();
                    editor.key = uuid::Uuid::new_v4();
                    return;
                }
                _ => {}
            }
        }
        if controls.get(self.access.selected) == Some(&Control::AccessDurationInput)
            && let Some(input) = self
                .access
                .editor
                .as_mut()
                .and_then(|editor| editor.duration_input.as_mut())
        {
            match key.code {
                KeyCode::Char(c)
                    if !key.modifiers.contains(KeyModifiers::CONTROL)
                        && c.is_ascii_alphanumeric() =>
                {
                    if input.len() < 32 {
                        input.push(c);
                    }
                    return;
                }
                KeyCode::Backspace => {
                    input.pop();
                    return;
                }
                KeyCode::Enter => {
                    self.activate(Control::AccessDurationApply);
                    return;
                }
                _ => {}
            }
        }
        if let Some(Control::AccessField(index)) = controls.get(self.access.selected).copied()
            && let Some(editor) = &mut self.access.editor
        {
            match key.code {
                KeyCode::Char(c)
                    if !key.modifiers.contains(KeyModifiers::CONTROL) && !c.is_control() =>
                {
                    if editor.fields[index].chars().count() < 128 {
                        editor.fields[index].push(c);
                        editor.key = uuid::Uuid::new_v4();
                    }
                    return;
                }
                KeyCode::Backspace => {
                    editor.fields[index].pop();
                    editor.key = uuid::Uuid::new_v4();
                    return;
                }
                _ => {}
            }
        }
        match key.code {
            KeyCode::Esc => {
                if self.access.devices.is_some() {
                    self.activate(Control::AccessBack);
                    return;
                }
                if let Some(editor) = &mut self.access.editor
                    && editor.expiry_view
                {
                    self.activate(Control::AccessExpiryBack);
                    return;
                }
                if let Some(editor) = &mut self.access.editor
                    && editor.grants_view
                {
                    editor.grants_view = false;
                    self.access.selected = 0;
                    return;
                }
                if self.access.editor.is_some() {
                    self.activate(
                        if self
                            .access
                            .editor
                            .as_ref()
                            .is_some_and(|editor| editor.creation_step.is_some())
                        {
                            Control::AccessWizardBack
                        } else {
                            Control::AccessBack
                        },
                    );
                    return;
                }
                if self.access.profile_action.take().is_some()
                    || self.access.account_action.take().is_some()
                {
                    return;
                }
                if self.access.decision.take().is_some() {
                    return;
                }
                if self.access.card.take().is_some() {
                    self.access_command(crate::access::Command::List(self.access.tab));
                } else {
                    self.screen = Screen::Form;
                }
            }
            KeyCode::Tab => {
                self.access.selected =
                    (self.access.selected + 1).min(controls.len().saturating_sub(1))
            }
            KeyCode::BackTab => self.access.selected = self.access.selected.saturating_sub(1),
            KeyCode::Enter => {
                if let Some(control) = controls.get(self.access.selected) {
                    self.activate(*control);
                }
            }
            KeyCode::Char(c) if key.modifiers.contains(KeyModifiers::CONTROL) && c == 'c' => {
                self.exit = true;
                self.exit_confirm = false;
            }
            KeyCode::Char(c)
                if controls.get(self.access.selected) == Some(&Control::AccessSearch) =>
            {
                if !c.is_control() && self.access.search.len() < 128 {
                    self.access.search.push(c);
                    self.access.page = 0;
                }
            }
            KeyCode::Backspace
                if controls.get(self.access.selected) == Some(&Control::AccessSearch) =>
            {
                self.access.search.pop();
                self.access.page = 0;
            }
            KeyCode::Left | KeyCode::Right | KeyCode::Up | KeyCode::Down => {
                let areas: Vec<_> = controls
                    .iter()
                    .filter_map(|c| self.hits.iter().find(|h| h.control == *c).map(|h| h.area))
                    .collect();
                if let Some(next) = directional_index(&areas, self.access.selected, key.code) {
                    self.access.selected = next;
                }
            }
            _ => {}
        }
    }
    pub(in crate::tui) fn temporary_key(&mut self, key: KeyEvent) {
        self.remember_navigation_focus(None);
        self.temporary_key_inner(key);
        self.restore_navigation_context();
    }

    fn temporary_key_inner(&mut self, key: KeyEvent) {
        if key.code == KeyCode::Char('c') && key.modifiers.contains(KeyModifiers::CONTROL) {
            self.exit = true;
            self.exit_confirm = false;
            return;
        }
        if self.qr_visible {
            if matches!(key.code, KeyCode::Esc | KeyCode::Enter) {
                self.qr_visible = false;
            }
            return;
        }
        if self.temporary_sidebar {
            match key.code {
                KeyCode::Enter | KeyCode::Tab | KeyCode::Right => self.open_temporary(None),
                KeyCode::Up | KeyCode::Down => self.navigate(key.code == KeyCode::Up),
                KeyCode::Esc => {
                    self.exit = true;
                    self.exit_confirm = false;
                }
                _ => {}
            }
            return;
        }
        if self.move_spatial(key.code) {
            return;
        }
        let count = self.temporary_controls().len().max(1);
        match key.code {
            KeyCode::Esc if self.temporary.view == crate::temporary::View::List => {
                self.temporary_sidebar = true
            }
            KeyCode::Esc if self.qr_visible => self.qr_visible = false,
            KeyCode::Esc => {
                self.activate(Control::TemporaryBack);
            }
            KeyCode::Down | KeyCode::Right => {
                self.temporary.selected = (self.temporary.selected + 1).min(count - 1)
            }
            KeyCode::Up | KeyCode::Left => {
                self.temporary.selected = self.temporary.selected.saturating_sub(1)
            }
            KeyCode::Tab => self.temporary.selected = (self.temporary.selected + 1) % count,
            KeyCode::BackTab => {
                self.temporary.selected = (self.temporary.selected + count - 1) % count
            }
            KeyCode::PageDown => self.temporary.scroll = self.temporary.scroll.saturating_add(8),
            KeyCode::PageUp => self.temporary.scroll = self.temporary.scroll.saturating_sub(8),
            KeyCode::Enter => {
                if let Some(control) = self.temporary_controls().get(self.temporary.selected) {
                    self.activate(*control);
                }
            }
            _ => {}
        }
    }
    pub(in crate::tui) fn wizard_key(&mut self, key: KeyEvent) {
        self.remember_navigation_focus(None);
        self.wizard_key_inner(key);
        self.restore_navigation_context();
    }

    fn wizard_key_inner(&mut self, key: KeyEvent) {
        if key.code == KeyCode::Char('c') && key.modifiers.contains(KeyModifiers::CONTROL) {
            self.exit = true;
            self.exit_confirm = false;
            return;
        }
        if self.move_spatial(key.code) {
            return;
        }
        let Some(wizard) = &mut self.node_wizard else {
            self.screen = Screen::Nodes;
            return;
        };
        let controls = wizard_controls(wizard);
        let count = controls.len();
        match key.code {
            KeyCode::Esc => {
                self.activate(Control::WizardBack);
            }
            KeyCode::Down | KeyCode::Right => {
                wizard.selected = (wizard.selected + 1).min(count - 1);
            }
            KeyCode::Up | KeyCode::Left => wizard.selected = wizard.selected.saturating_sub(1),
            KeyCode::Tab => wizard.selected = (wizard.selected + 1) % count,
            KeyCode::BackTab => wizard.selected = (wizard.selected + count - 1) % count,
            KeyCode::Backspace => {
                if let Some(field) = wizard.field_mut() {
                    field.pop();
                }
            }
            KeyCode::Char(c)
                if !key.modifiers.contains(KeyModifiers::CONTROL) && wizard.field().is_some() =>
            {
                wizard.append(&c.to_string());
            }
            KeyCode::Enter | KeyCode::Char(' ') => {
                let control = controls[wizard.selected.min(count - 1)];
                self.activate(control);
            }
            _ => {}
        }
    }
    pub(in crate::tui) fn nodes_key(&mut self, key: KeyEvent) {
        self.remember_navigation_focus(None);
        self.nodes_key_inner(key);
        self.restore_navigation_context();
    }

    fn nodes_key_inner(&mut self, key: KeyEvent) {
        if key.code == KeyCode::Char('c') && key.modifiers.contains(KeyModifiers::CONTROL) {
            self.exit = true;
            self.exit_confirm = false;
            return;
        }
        if self.nodes_sidebar {
            match key.code {
                KeyCode::Enter | KeyCode::Tab | KeyCode::Right => self.open_nodes(),
                KeyCode::Up | KeyCode::Down => self.navigate(key.code == KeyCode::Up),
                KeyCode::Esc => {
                    self.exit = true;
                    self.exit_confirm = false;
                }
                _ => {}
            }
            return;
        }
        let controls = self.nodes_controls();
        let selected = self.nodes_selected.min(controls.len().saturating_sub(1));
        let search = matches!(controls.get(selected), Some(Control::NodesSearch));
        if let Some(Control::NodesEditField(index)) = controls.get(selected)
            && let Some(editor) = &mut self.nodes.editor
        {
            if matches!(editor.fields[*index].key, "notes" | "awg_parameters") {
                let limit = editor.fields[*index].input_limit();
                if self.text_input.borrow_mut().edit(
                    editor.fields[*index].label,
                    &mut editor.fields[*index].value,
                    key,
                    limit,
                ) {
                    editor.changed();
                    return;
                }
            }
            match key.code {
                KeyCode::Enter
                    if matches!(editor.fields[*index].key, "notes" | "awg_parameters") =>
                {
                    if editor.fields[*index].value.chars().count()
                        < editor.fields[*index].input_limit()
                    {
                        editor.fields[*index].value.push('\n');
                        editor.changed();
                    }
                    return;
                }
                KeyCode::Char(c)
                    if !key.modifiers.contains(KeyModifiers::CONTROL) && !c.is_control() =>
                {
                    if editor.fields[*index].value.chars().count()
                        < editor.fields[*index].input_limit()
                    {
                        editor.fields[*index].value.push(c);
                        editor.changed();
                    }
                    return;
                }
                KeyCode::Backspace => {
                    editor.fields[*index].value.pop();
                    editor.changed();
                    return;
                }
                _ => {}
            }
        }
        if key.code == KeyCode::Esc && self.nodes.editor.is_some() {
            self.activate(Control::NodesEditBack);
            return;
        }
        if key.code == KeyCode::Esc
            && (self.nodes.diagnostics.is_some() || self.nodes.maintenance.is_some())
        {
            self.activate(Control::NodesBack);
            return;
        }
        if self.nodes.diagnostics.is_some()
            && self.nodes.diagnostic_item.is_none()
            && self.nodes.recovery_confirm.is_none()
        {
            let current = self.nodes.diagnostic_scroll.get();
            let step = self.nodes.diagnostic_visible.get().max(1);
            let maximum = self.nodes.diagnostic_max_scroll.get();
            let next = match key.code {
                KeyCode::PageUp => Some(current.saturating_sub(step)),
                KeyCode::PageDown => Some(current.saturating_add(step).min(maximum)),
                KeyCode::Home => Some(0),
                KeyCode::End => Some(maximum),
                _ => None,
            };
            if let Some(next) = next {
                self.nodes.diagnostic_scroll.set(next);
                return;
            }
        }
        if self.nodes.diagnostics.is_none()
            && let Some(view) = &mut self.nodes.maintenance
        {
            match key.code {
                KeyCode::PageUp => {
                    view.scroll = view.scroll.saturating_sub(5);
                    return;
                }
                KeyCode::PageDown => {
                    view.scroll = view.scroll.saturating_add(5);
                    return;
                }
                KeyCode::Home => {
                    view.scroll = 0;
                    return;
                }
                KeyCode::End => {
                    view.scroll = usize::MAX;
                    return;
                }
                _ => {}
            }
        }
        if self.nodes.card.is_some()
            && self.nodes.diagnostics.is_none()
            && self.nodes.editor.is_none()
            && self.nodes.maintenance.is_none()
        {
            match key.code {
                KeyCode::PageUp => {
                    self.nodes.card_scroll = self.nodes.card_scroll.saturating_sub(5);
                    return;
                }
                KeyCode::PageDown => {
                    self.nodes.card_scroll = self.nodes.card_scroll.saturating_add(5);
                    return;
                }
                KeyCode::Home => {
                    self.nodes.card_scroll = 0;
                    return;
                }
                KeyCode::End => {
                    self.nodes.card_scroll = usize::MAX;
                    return;
                }
                _ => {}
            }
        }
        match key.code {
            KeyCode::Up | KeyCode::Down | KeyCode::Left | KeyCode::Right => {
                self.nodes_move_direction(key.code);
            }
            KeyCode::Esc if self.nodes.card.is_some() => {
                self.nodes.card = None;
                self.nodes_selected = 0;
            }
            KeyCode::Esc => self.nodes_sidebar = true,
            KeyCode::Char('c') if key.modifiers.contains(KeyModifiers::CONTROL) => {
                self.exit = true;
                self.exit_confirm = false;
            }
            KeyCode::Char('f') if key.modifiers.contains(KeyModifiers::CONTROL) => {
                self.nodes_selected = 0
            }
            KeyCode::Tab => self.nodes_selected = (selected + 1) % controls.len().max(1),
            KeyCode::BackTab => {
                self.nodes_selected = (selected + controls.len().max(1) - 1) % controls.len().max(1)
            }
            KeyCode::Enter if !self.nodes.loaded && self.nodes.card.is_none() => self.open_nodes(),
            KeyCode::Enter if search => {
                self.nodes_selected = 1.min(controls.len().saturating_sub(1))
            }
            KeyCode::Enter => {
                if let Some(control) = controls.get(selected) {
                    self.activate(*control);
                }
            }
            KeyCode::Char(c)
                if search && !key.modifiers.contains(KeyModifiers::CONTROL) && !c.is_control() =>
            {
                if self.nodes.search.chars().count() < 128 {
                    self.nodes.search.push(c);
                    self.nodes.page = 0;
                }
            }
            KeyCode::Backspace if search => {
                self.nodes.search.pop();
                self.nodes.page = 0;
            }
            _ => {}
        }
    }
    pub(in crate::tui) fn idle_key(&mut self, key: KeyEvent) {
        self.remember_navigation_focus(None);
        self.idle_key_inner(key);
        self.restore_navigation_context();
    }

    fn idle_key_inner(&mut self, key: KeyEvent) {
        let controls = self.idle_controls();
        if controls.is_empty() {
            return;
        }
        match key.code {
            KeyCode::Down | KeyCode::Tab => {
                self.idle_selection = (self.idle_selection + 1) % controls.len()
            }
            KeyCode::Up | KeyCode::BackTab => {
                self.idle_selection = (self.idle_selection + controls.len() - 1) % controls.len()
            }
            KeyCode::Left | KeyCode::Right
                if controls[self.idle_selection.min(controls.len() - 1)]
                    == Control::QuickProfile =>
            {
                self.activate(Control::CycleProfile(key.code == KeyCode::Left));
            }
            KeyCode::Enter | KeyCode::Right => {
                self.activate(controls[self.idle_selection.min(controls.len() - 1)]);
            }
            KeyCode::Esc => {
                self.exit = true;
                self.exit_confirm = false;
            }
            KeyCode::Char('c') if key.modifiers.contains(KeyModifiers::CONTROL) => {
                self.exit = true;
                self.exit_confirm = false;
            }
            _ => {}
        }
    }
    pub(in crate::tui) fn installation_editor_key(&mut self, key: KeyEvent) {
        self.remember_navigation_focus(None);
        self.installation_editor_key_inner(key);
        self.restore_navigation_context();
    }

    fn installation_editor_key_inner(&mut self, key: KeyEvent) {
        if key.code == KeyCode::Esc {
            self.activate(Control::CancelEdit);
            return;
        }
        if key.code == KeyCode::Char('c') && key.modifiers.contains(KeyModifiers::CONTROL) {
            self.exit = true;
            self.exit_confirm = false;
            return;
        }
        let Some((_, fields, selected)) = &mut self.editor else {
            return;
        };
        match key.code {
            KeyCode::Down | KeyCode::Tab => *selected = (*selected + 1).min(8),
            KeyCode::Up | KeyCode::BackTab => *selected = selected.saturating_sub(1),
            KeyCode::Left | KeyCode::Right if *selected == 4 => {}
            KeyCode::Right if *selected == 7 => *selected = 8,
            KeyCode::Left if *selected == 8 => *selected = 7,
            KeyCode::Char(c)
                if *selected < 7
                    && *selected != 4
                    && !key.modifiers.contains(KeyModifiers::CONTROL) =>
            {
                if fields[*selected].len() < 4096 {
                    fields[*selected].push(c);
                }
            }
            KeyCode::Backspace if *selected < 7 && *selected != 4 => {
                fields[*selected].pop();
            }
            KeyCode::Enter if *selected < 7 => *selected += 1,
            KeyCode::Enter if *selected == 7 => {
                if let Err(error) = self.save_edit() {
                    self.error = error.to_string();
                }
            }
            KeyCode::Enter => {
                self.activate(Control::CancelEdit);
            }
            _ => {}
        }
    }
    pub(in crate::tui) fn prompt_key(&mut self, key: KeyEvent) {
        self.remember_navigation_focus(None);
        self.prompt_key_inner(key);
        self.restore_navigation_context();
    }

    fn prompt_key_inner(&mut self, key: KeyEvent) {
        let Some((prompt, _)) = &self.prompt else {
            return;
        };
        let answer = match prompt {
            Prompt::SelectAdministrator { choices } => match key.code {
                KeyCode::Up => {
                    self.administrator_selected = self.administrator_selected.saturating_sub(1);
                    None
                }
                KeyCode::Down | KeyCode::Tab => {
                    self.administrator_selected =
                        (self.administrator_selected + 1).min(choices.len().saturating_sub(1));
                    None
                }
                KeyCode::Enter if !choices.is_empty() => {
                    Some(Answer::Selection(self.administrator_selected))
                }
                KeyCode::Esc => Some(Answer::Cancel),
                _ => None,
            },
            Prompt::HostKey { .. } | Prompt::ConfirmAction { .. } => match key.code {
                KeyCode::Char('y' | 'Y') => Some(Answer::Confirm(true)),
                KeyCode::Char('n' | 'N') | KeyCode::Esc => Some(Answer::Confirm(false)),
                KeyCode::Left | KeyCode::Right | KeyCode::Tab => {
                    self.trust = !self.trust;
                    None
                }
                KeyCode::Enter => Some(Answer::Confirm(self.trust)),
                _ => None,
            },
            Prompt::Password { .. } => match key.code {
                KeyCode::Char(c) if !key.modifiers.contains(KeyModifiers::CONTROL) => {
                    if self.password.len() < 4096 {
                        self.password.push(c);
                    }
                    None
                }
                KeyCode::Backspace => {
                    self.password.pop();
                    None
                }
                KeyCode::Enter if self.password.is_empty() => {
                    self.error = "Enter the SSH password before connecting.".into();
                    None
                }
                KeyCode::Enter => Some(Answer::Password(std::mem::replace(
                    &mut self.password,
                    Zeroizing::new(String::new()),
                ))),
                KeyCode::Esc => Some(Answer::Cancel),
                _ => None,
            },
        };
        if let Some(answer) = answer {
            self.error.clear();
            let (_, reply) = self.prompt.take().unwrap();
            let _ = reply.send(answer);
        }
    }
    pub(in crate::tui) fn quick_start_key(&mut self, key: KeyEvent) {
        self.remember_navigation_focus(None);
        self.quick_start_key_inner(key);
        self.restore_navigation_context();
    }

    fn quick_start_key_inner(&mut self, key: KeyEvent) {
        match key.code {
            KeyCode::Esc => {
                self.screen = self.quick_start_return.take().unwrap_or(Screen::Form);
            }
            KeyCode::Tab | KeyCode::BackTab => self.confirm = !self.confirm,
            KeyCode::Down | KeyCode::Right => self.confirm = true,
            KeyCode::Up | KeyCode::Left => self.confirm = false,
            KeyCode::Enter => {
                self.activate(if self.confirm {
                    Control::QuickStartAction
                } else {
                    Control::QuickStartRefresh
                });
            }
            KeyCode::Char('c') if key.modifiers.contains(KeyModifiers::CONTROL) => {
                self.exit = true;
                self.exit_confirm = false;
            }
            _ => {}
        }
    }
}

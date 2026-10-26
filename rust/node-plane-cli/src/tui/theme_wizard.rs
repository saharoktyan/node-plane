use super::*;

pub(super) struct ThemeWizard {
    pub name: String,
    pub primary: String,
    pub secondary: String,
    pub step: usize,
    pub selected: usize,
    pub error: String,
    pub delete: Option<String>,
}
impl Default for ThemeWizard {
    fn default() -> Self {
        Self {
            name: String::new(),
            primary: "#".into(),
            secondary: "#".into(),
            step: 0,
            selected: 0,
            error: String::new(),
            delete: None,
        }
    }
}
impl ThemeWizard {
    pub fn controls(&self) -> Vec<Control> {
        match self.step {
            0..=2 => vec![Control::ThemeField, Control::ThemeBack, Control::ThemeNext],
            _ => vec![Control::ThemeBack, Control::ThemeNext],
        }
    }
    pub fn border(&self) -> String {
        if self.secondary != "#" && crate::connections::validate_accent(&self.secondary).is_ok() {
            return self.secondary.clone();
        }
        match theme::section_border_color(theme::accent_color(&self.primary)) {
            Color::Rgb(r, g, b) => format!("#{r:02x}{g:02x}{b:02x}"),
            _ => "#444444".into(),
        }
    }
    pub fn input(&mut self, text: &str) {
        if self.step > 2 || self.selected != 0 {
            return;
        }
        let field = match self.step {
            0 => &mut self.name,
            1 => &mut self.primary,
            _ => &mut self.secondary,
        };
        for c in text.chars() {
            if self.step == 0 {
                if !c.is_control() && field.chars().count() < 40 {
                    field.push(c);
                }
            } else if c.is_ascii_hexdigit() && field.len() < 7 {
                field.push(c.to_ascii_lowercase());
            }
        }
        self.error.clear();
    }
}
impl App {
    pub(in crate::tui) fn appearance_controls(&self) -> Vec<Control> {
        let mut controls = Vec::new();
        for (index, theme) in self.themes.iter().enumerate() {
            controls.push(Control::Accent(index));
            if !theme.packaged() {
                controls.push(Control::ThemeDelete(index));
            }
        }
        controls.extend([Control::CreateTheme, Control::Settings]);
        controls
    }

    pub(in crate::tui) fn theme_wizard_action(&mut self, control: Control) {
        let Some(wizard) = &mut self.theme_wizard else {
            return;
        };
        match control {
            Control::ThemeField => wizard.selected = 0,
            Control::ThemeBack => {
                if wizard.step == 0 || wizard.delete.is_some() {
                    self.theme_wizard = None;
                } else {
                    wizard.step -= 1;
                    wizard.selected = 0;
                    wizard.error.clear();
                }
            }
            Control::ThemeNext if wizard.delete.is_some() => {
                let id = wizard.delete.clone().unwrap();
                let Some(index) = self.themes.iter().position(|t| t.id == id) else {
                    return;
                };
                let chosen = self.themes[index].selected(&self.connections);
                let mut next = self.connections.clone();
                if chosen {
                    next.theme = Some("auto.json".into());
                    next.accent = "terminal".into();
                }
                // Keep the file when preference persistence fails.
                if chosen && let Err(error) = next.save(&self.state_dir) {
                    wizard.error = error.to_string();
                    return;
                }
                if let Err(error) = theme::remove(&self.state_dir, &self.themes[index]) {
                    wizard.error = error.to_string();
                    if chosen {
                        self.connections = next;
                    }
                    return;
                }
                if chosen {
                    self.connections = next;
                }
                self.themes.remove(index);
                self.settings_selected = self
                    .appearance_controls()
                    .iter()
                    .position(|c| {
                        *c == Control::Accent(index.min(self.themes.len().saturating_sub(1)))
                    })
                    .unwrap_or(0)
                    + 1;
                self.theme_wizard = None;
            }
            Control::ThemeNext if wizard.step == 3 => {
                match theme::create(
                    &self.state_dir,
                    &wizard.name,
                    &wizard.primary,
                    &wizard.border(),
                ) {
                    Ok(created) => {
                        self.themes.push(created);
                        self.settings_selected = self
                            .appearance_controls()
                            .iter()
                            .position(|c| *c == Control::Accent(self.themes.len() - 1))
                            .unwrap_or(0)
                            + 1;
                        self.theme_wizard = None;
                        self.error.clear();
                    }
                    Err(error) => wizard.error = format!("Could not save theme: {error}"),
                }
            }
            Control::ThemeNext => {
                let valid = match wizard.step {
                    0 => !wizard.name.trim().is_empty(),
                    1 => crate::connections::validate_accent(&wizard.primary).is_ok(),
                    _ => {
                        wizard.secondary == "#"
                            || crate::connections::validate_accent(&wizard.secondary).is_ok()
                    }
                };
                if !valid {
                    wizard.error = if wizard.step == 0 {
                        "Enter a theme name."
                    } else {
                        "Enter a color in #RRGGBB format."
                    }
                    .into();
                    return;
                }
                wizard.step += 1;
                wizard.selected = if wizard.step == 3 { 1 } else { 0 };
                wizard.error.clear();
            }
            _ => {}
        }
    }
    pub(in crate::tui) fn theme_wizard_key(&mut self, key: KeyEvent) {
        let Some(wizard) = &mut self.theme_wizard else {
            return;
        };
        match key.code {
            KeyCode::Esc => self.theme_wizard_action(Control::ThemeBack),
            KeyCode::Tab | KeyCode::Down | KeyCode::Right => {
                wizard.selected = (wizard.selected + 1) % wizard.controls().len()
            }
            KeyCode::BackTab | KeyCode::Up | KeyCode::Left => {
                wizard.selected =
                    (wizard.selected + wizard.controls().len() - 1) % wizard.controls().len()
            }
            KeyCode::Enter => {
                let control = wizard.controls()[wizard.selected];
                self.theme_wizard_action(if control == Control::ThemeField {
                    Control::ThemeNext
                } else {
                    control
                });
            }
            KeyCode::Backspace if wizard.step < 3 && wizard.selected == 0 => {
                let field = match wizard.step {
                    0 => &mut wizard.name,
                    1 => &mut wizard.primary,
                    _ => &mut wizard.secondary,
                };
                if wizard.step == 0 || field.len() > 1 {
                    super::text_input::delete_backwards(field, key);
                    if wizard.step != 0 && !field.starts_with('#') {
                        field.insert(0, '#');
                    }
                }
            }
            KeyCode::Backspace => self.theme_wizard_action(Control::ThemeBack),
            KeyCode::Char(c) if !key.modifiers.contains(KeyModifiers::CONTROL) => {
                wizard.input(&c.to_string())
            }
            _ => {}
        }
    }
}

pub(super) fn draw(frame: &mut Frame, app: &App, hits: &mut Vec<Hit>) {
    let Some(wizard) = &app.theme_wizard else {
        return;
    };
    hits.clear();
    app.scrollbar.set(None);
    let deletion = wizard.delete.is_some();
    let popup = centered(frame.area(), 78, if deletion { 12 } else { 20 });
    frame.render_widget(Clear, popup);
    let parts = dialog(
        frame,
        popup,
        if deletion {
            " Delete theme? "
        } else {
            " Create custom theme "
        },
    );
    let body = parts[0];
    let description = if deletion {
        format!("Delete ‘{}’?\nThe theme file will be removed.", wizard.name)
    } else {
        format!(
            "Step {}/4 · {}",
            wizard.step + 1,
            [
                "Theme name",
                "Primary color",
                "Secondary color (optional)",
                "Preview"
            ][wizard.step]
        )
    };
    frame.render_widget(
        Paragraph::new(description).wrap(Wrap { trim: false }),
        Rect::new(body.x, body.y, body.width, 2),
    );
    if wizard.step < 3 {
        let rect = Rect::new(body.x, body.y + 3, body.width, 3);
        let (label, value) = match wizard.step {
            0 => ("Name", &wizard.name),
            1 => ("Primary · #RRGGBB", &wizard.primary),
            _ => ("Secondary · #RRGGBB", &wizard.secondary),
        };
        draw_field(frame, rect, label, value, wizard.selected == 0);
        hits.push(Hit {
            area: rect,
            control: Control::ThemeField,
        });
        if wizard.selected == 0 {
            frame.set_cursor_position((
                rect.x
                    + 1
                    + value
                        .chars()
                        .count()
                        .min(rect.width.saturating_sub(3) as usize) as u16,
                rect.y + 1,
            ));
        }
        if wizard.step == 2 {
            frame.render_widget(
                Paragraph::new("Skip to use a darker primary color."),
                Rect::new(body.x, body.y + 6, body.width, 1),
            );
        }
    }
    if !deletion && wizard.step > 0 && crate::connections::validate_accent(&wizard.primary).is_ok()
    {
        let border = wizard.border();
        let y = body.y + if wizard.step == 3 { 4 } else { 8 };
        if wizard.step == 3 {
            frame.render_widget(
                Paragraph::new(&*wizard.name),
                Rect::new(body.x, body.y + 2, body.width, 1),
            );
        }
        for (row, label, value) in [(0, "Primary", &wizard.primary), (1, "Secondary", &border)] {
            frame.render_widget(
                Paragraph::new(Line::from(vec![
                    Span::styled("████ ", Style::default().fg(theme::accent_color(value))),
                    Span::raw(format!("{label}: {value}")),
                ])),
                Rect::new(body.x, y + row, body.width, 1),
            );
        }
    }
    if !wizard.error.is_empty() {
        frame.render_widget(
            Paragraph::new(&*wizard.error)
                .style(Style::default().fg(Color::Red))
                .wrap(Wrap { trim: false }),
            Rect::new(body.x, body.bottom().saturating_sub(2), body.width, 2),
        );
    }
    let controls = wizard.controls();
    let buttons: Vec<_> = controls
        .iter()
        .copied()
        .filter(|c| *c != Control::ThemeField)
        .collect();
    let columns = Layout::horizontal(vec![
        Constraint::Ratio(1, buttons.len() as u32);
        buttons.len()
    ])
    .spacing(1)
    .split(parts[1]);
    for (i, control) in buttons.into_iter().enumerate() {
        let label = match control {
            Control::ThemeBack if wizard.step == 0 || deletion => "Cancel",
            Control::ThemeBack => "Back",
            Control::ThemeNext if wizard.step == 2 && wizard.secondary == "#" => "Skip",
            _ if deletion => "Delete",
            _ if wizard.step == 3 => "Save theme",
            _ => "Continue",
        };
        draw_button_color(
            frame,
            columns[i],
            label,
            controls.get(wizard.selected) == Some(&control),
            if deletion && control == Control::ThemeNext {
                Color::Red
            } else {
                Color::Cyan
            },
        );
        hits.push(Hit {
            area: columns[i],
            control,
        });
    }
}

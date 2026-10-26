//! Cursor-aware multiline editing shared by notes, announcements and AWG parameters.
use super::*;

pub(super) fn word_start(value: &str, cursor: usize) -> usize {
    let prefix = &value[..cursor];
    let end = prefix.trim_end_matches(char::is_whitespace).len();
    prefix[..end]
        .char_indices()
        .rev()
        .find(|(_, c)| c.is_whitespace())
        .map_or(0, |(index, c)| index + c.len_utf8())
}

pub(super) fn delete_backwards(value: &mut String, key: KeyEvent) {
    if key.modifiers.contains(KeyModifiers::CONTROL) {
        value.truncate(word_start(value, value.len()));
    } else {
        value.pop();
    }
}

#[derive(Default)]
pub(super) struct TextInput {
    key: String,
    value: String,
    cursor: usize,
    top: usize,
    follow: bool,
    area: Rect,
    rows: Vec<String>,
    positions: Vec<(usize, usize, u16)>,
}
impl TextInput {
    pub(super) fn reset_area(&mut self) {
        self.area = Rect::default();
    }
    fn sync(&mut self, key: &str, value: &str) {
        if self.key != key || self.value != value {
            self.key = key.into();
            self.value = value.into();
            self.cursor = value.len();
            self.top = 0;
            self.follow = true;
        }
    }
    fn layout(&mut self, width: u16) {
        let width = width.max(1);
        self.rows = vec![String::new()];
        self.positions = vec![(0, 0, 0)];
        let mut row = 0;
        let mut col = 0;
        for (byte, c) in self.value.char_indices() {
            if c == '\n' {
                row += 1;
                col = 0;
                self.rows.push(String::new());
            } else {
                let cells = Span::raw(c.to_string()).width() as u16;
                if col + cells > width {
                    row += 1;
                    col = 0;
                    self.rows.push(String::new());
                    if let Some(position) = self.positions.last_mut() {
                        *position = (byte, row, col);
                    }
                }
                self.rows[row].push(c);
                col += cells;
                if col >= width {
                    row += 1;
                    col = 0;
                    self.rows.push(String::new());
                }
            }
            self.positions.push((byte + c.len_utf8(), row, col));
        }
    }
    fn position(&self) -> (usize, u16) {
        self.positions
            .iter()
            .find(|(byte, _, _)| *byte == self.cursor)
            .map(|(_, row, col)| (*row, *col))
            .unwrap_or((0, 0))
    }
    fn at(&self, row: usize, col: u16) -> usize {
        self.positions
            .iter()
            .filter(|(_, r, _)| *r == row)
            .min_by_key(|(_, _, c)| c.abs_diff(col))
            .map(|(byte, _, _)| *byte)
            .unwrap_or(self.value.len())
    }
    pub(super) fn edit(
        &mut self,
        key: &str,
        value: &mut String,
        event: KeyEvent,
        limit: usize,
    ) -> bool {
        self.sync(key, value);
        self.layout(self.area.width.max(1));
        let previous = || {
            self.value[..self.cursor]
                .char_indices()
                .last()
                .map_or(0, |(i, _)| i)
        };
        let next = || {
            self.value[self.cursor..]
                .chars()
                .next()
                .map_or(self.cursor, |c| self.cursor + c.len_utf8())
        };
        match event.code {
            KeyCode::Char('w') | KeyCode::Backspace
                if event.modifiers.contains(KeyModifiers::CONTROL) =>
            {
                let start = word_start(value, self.cursor);
                value.replace_range(start..self.cursor, "");
                self.cursor = start;
            }
            KeyCode::Char(c)
                if !event.modifiers.contains(KeyModifiers::CONTROL) && !c.is_control() =>
            {
                if value.chars().count() < limit {
                    value.insert(self.cursor, c);
                    self.cursor += c.len_utf8();
                }
            }
            KeyCode::Enter => {
                if value.chars().count() < limit {
                    value.insert(self.cursor, '\n');
                    self.cursor += 1;
                }
            }
            KeyCode::Backspace => {
                let start = previous();
                value.replace_range(start..self.cursor, "");
                self.cursor = start;
            }
            KeyCode::Delete => {
                value.replace_range(self.cursor..next(), "");
            }
            KeyCode::Left => self.cursor = previous(),
            KeyCode::Right => self.cursor = next(),
            KeyCode::Home if event.modifiers.contains(KeyModifiers::CONTROL) => self.cursor = 0,
            KeyCode::End if event.modifiers.contains(KeyModifiers::CONTROL) => {
                self.cursor = value.len()
            }
            KeyCode::Home => {
                let (row, _) = self.position();
                self.cursor = self.at(row, 0);
            }
            KeyCode::End => {
                let (row, _) = self.position();
                self.cursor = self.at(row, u16::MAX);
            }
            KeyCode::Up | KeyCode::Down | KeyCode::PageUp | KeyCode::PageDown => {
                let (row, col) = self.position();
                let step = if matches!(event.code, KeyCode::PageUp | KeyCode::PageDown) {
                    usize::from(self.area.height.max(1))
                } else {
                    1
                };
                let row = if matches!(event.code, KeyCode::Up | KeyCode::PageUp) {
                    row.saturating_sub(step)
                } else {
                    (row + step).min(self.rows.len().saturating_sub(1))
                };
                self.cursor = self.at(row, col);
            }
            _ => return false,
        }
        self.value = value.clone();
        self.follow = true;
        true
    }
    pub(super) fn paste(&mut self, key: &str, value: &mut String, text: &str, limit: usize) {
        self.sync(key, value);
        let remaining = limit.saturating_sub(value.chars().count());
        let text = text
            .chars()
            .filter(|c| !c.is_control() || *c == '\n')
            .take(remaining)
            .collect::<String>();
        value.insert_str(self.cursor, &text);
        self.cursor += text.len();
        self.value = value.clone();
        self.follow = true;
    }
    pub(super) fn mouse(&mut self, event: MouseEvent) -> bool {
        if self.area.height > 0
            && event.column == self.area.right()
            && event.row >= self.area.y
            && event.row < self.area.bottom()
            && matches!(
                event.kind,
                MouseEventKind::Down(MouseButton::Left) | MouseEventKind::Drag(MouseButton::Left)
            )
        {
            let maximum = self
                .rows
                .len()
                .saturating_sub(usize::from(self.area.height));
            self.top = usize::from(event.row - self.area.y) * maximum
                / usize::from(self.area.height.saturating_sub(1).max(1));
            self.follow = false;
            return true;
        }
        if !self.area.contains((event.column, event.row).into()) {
            return false;
        }
        match event.kind {
            MouseEventKind::ScrollUp => {
                self.top = self.top.saturating_sub(3);
                self.follow = false;
                true
            }
            MouseEventKind::ScrollDown => {
                self.top = (self.top + 3).min(
                    self.rows
                        .len()
                        .saturating_sub(usize::from(self.area.height)),
                );
                self.follow = false;
                true
            }
            MouseEventKind::Down(MouseButton::Left) => {
                self.cursor = self.at(
                    self.top + usize::from(event.row - self.area.y),
                    event.column - self.area.x,
                );
                self.follow = true;
                false
            }
            _ => false,
        }
    }
}
pub(super) fn draw_multiline(
    frame: &mut Frame,
    rect: Rect,
    label: &str,
    value: &str,
    selected: bool,
    app: &App,
) {
    let block = Block::bordered()
        .title(label)
        .title_style(Style::default().fg(Color::Cyan))
        .border_style(Style::default().fg(if selected { Color::Cyan } else { Color::White }));
    let inner = block.inner(rect);
    frame.render_widget(block, rect);
    if inner.width < 2 || inner.height == 0 {
        return;
    }
    let mut input = app.text_input.borrow_mut();
    input.sync(label, value);
    input.area = Rect::new(inner.x, inner.y, inner.width - 1, inner.height);
    let width = input.area.width;
    input.layout(width);
    let (row, col) = input.position();
    let visible = usize::from(inner.height);
    if selected && input.follow {
        if row < input.top {
            input.top = row;
        } else if row >= input.top + visible {
            input.top = row + 1 - visible;
        }
    }
    input.top = input.top.min(input.rows.len().saturating_sub(visible));
    let lines = input
        .rows
        .iter()
        .skip(input.top)
        .take(visible)
        .cloned()
        .map(Line::from)
        .collect::<Vec<_>>();
    frame.render_widget(Paragraph::new(lines), input.area);
    if input.rows.len() > visible {
        let mut state =
            ratatui::widgets::ScrollbarState::new(input.rows.len().saturating_sub(visible) + 1)
                .position(input.top)
                .viewport_content_length(visible);
        frame.render_stateful_widget(
            ratatui::widgets::Scrollbar::new(ratatui::widgets::ScrollbarOrientation::VerticalRight),
            inner,
            &mut state,
        );
    }
    if selected && row >= input.top && row < input.top + visible {
        frame.set_cursor_position((inner.x + col, inner.y + (row - input.top) as u16));
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn word_deletion_preserves_suffix_and_unicode_at_cursor() {
        for code in [KeyCode::Char('w'), KeyCode::Backspace] {
            let mut input = TextInput::default();
            let mut value = "Привет мир   хвост".to_string();
            input.sync("Notes", &value);
            input.cursor = "Привет мир   ".len();
            assert!(input.edit(
                "Notes",
                &mut value,
                KeyEvent::new(code, KeyModifiers::CONTROL),
                100
            ));
            assert_eq!(value, "Привет хвост");
            assert_eq!(input.cursor, "Привет ".len());
            input.cursor = 0;
            input.edit(
                "Notes",
                &mut value,
                KeyEvent::new(code, KeyModifiers::CONTROL),
                100,
            );
            assert_eq!(value, "Привет хвост");
        }
        let mut tail = "hello мир   ".to_string();
        delete_backwards(
            &mut tail,
            KeyEvent::new(KeyCode::Backspace, KeyModifiers::CONTROL),
        );
        assert_eq!(tail, "hello ");
    }
    #[test]
    fn every_multiline_field_uses_the_full_scroll_track_and_proportional_thumb() {
        let app = App::new(&super::super::tests::test_request());
        let mut terminal = Terminal::new(ratatui::backend::TestBackend::new(30, 12)).unwrap();
        for label in ["Notes", "Manual AWG parameters", "Message"] {
            let mut thumb_sizes = Vec::new();
            for count in [12, 100] {
                let value = (0..count)
                    .map(|i| format!("line {i}"))
                    .collect::<Vec<_>>()
                    .join("\n");
                terminal
                    .draw(|frame| {
                        draw_multiline(frame, Rect::new(0, 0, 30, 12), label, &value, true, &app)
                    })
                    .unwrap();
                let buffer = terminal.backend().buffer();
                let thumb: Vec<_> = (2..10)
                    .filter(|y| buffer[(28, *y)].symbol() == "█")
                    .collect();
                assert!(!thumb.is_empty(), "{label} {count}");
                assert_eq!(
                    thumb.last(),
                    Some(&9),
                    "bottom position must reach the bottom of the track"
                );
                thumb_sizes.push(thumb.len());
            }
            assert!(thumb_sizes[0] > thumb_sizes[1]);
        }
    }
    #[test]
    fn cursor_inserts_and_pastes_inside_unicode_text_and_deletes_at_cursor() {
        let mut input = TextInput {
            area: Rect::new(0, 0, 12, 4),
            ..Default::default()
        };
        let mut value = "Привет\nworld".to_string();
        input.edit(
            "Notes",
            &mut value,
            KeyEvent::new(KeyCode::Home, KeyModifiers::CONTROL),
            100,
        );
        input.edit(
            "Notes",
            &mut value,
            KeyEvent::new(KeyCode::Right, KeyModifiers::NONE),
            100,
        );
        input.paste("Notes", &mut value, "X\n", 100);
        assert_eq!(value, "ПX\nривет\nworld");
        input.edit(
            "Notes",
            &mut value,
            KeyEvent::new(KeyCode::Backspace, KeyModifiers::NONE),
            100,
        );
        assert_eq!(value, "ПXривет\nworld");
        assert!(value.is_char_boundary(input.cursor));
    }
    #[test]
    fn wrapping_tracks_cursor_and_scrolling_does_not_move_it() {
        let mut input = TextInput {
            area: Rect::new(0, 0, 4, 2),
            ..Default::default()
        };
        let mut value = "abcdefgh\nijklmnop".to_string();
        input.sync("Notes", &value);
        input.layout(4);
        assert!(input.rows.len() > 2);
        let cursor = input.cursor;
        assert!(input.mouse(MouseEvent {
            kind: MouseEventKind::ScrollDown,
            column: 1,
            row: 1,
            modifiers: KeyModifiers::NONE
        }));
        assert!(input.top > 0);
        assert_eq!(input.cursor, cursor);
        input.edit(
            "Notes",
            &mut value,
            KeyEvent::new(KeyCode::Home, KeyModifiers::CONTROL),
            100,
        );
        assert_eq!(input.cursor, 0);
        assert!(input.follow);
    }
}

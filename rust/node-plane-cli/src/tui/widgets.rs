use super::*;

pub(super) fn section_block<'a>(title: impl Into<Line<'a>>) -> Block<'a> {
    Block::bordered()
        .title(title)
        .title_style(Style::default().fg(Color::Cyan))
        .border_style(Style::default().fg(SECTION_BORDER))
}

pub(super) fn centered(area: Rect, width: u16, height: u16) -> Rect {
    let width = width.min(area.width);
    let height = height.min(area.height);
    Rect::new(
        area.x + (area.width - width) / 2,
        area.y + (area.height - height) / 2,
        width,
        height,
    )
}
pub(super) fn directional_index(
    areas: &[Rect],
    selected: usize,
    direction: KeyCode,
) -> Option<usize> {
    let center = |area: &Rect| {
        (
            i32::from(area.x) * 2 + i32::from(area.width),
            i32::from(area.y) * 2 + i32::from(area.height),
        )
    };
    let current = areas.get(selected)?;
    let (x, y) = center(current);
    areas
        .iter()
        .enumerate()
        .filter_map(|(index, area)| {
            if matches!(direction, KeyCode::Left | KeyCode::Right)
                && (area.y >= current.bottom() || area.bottom() <= current.y)
            {
                return None;
            }
            let (nx, ny) = center(area);
            let (forward, sideways) = match direction {
                KeyCode::Up => (y - ny, nx - x),
                KeyCode::Down => (ny - y, nx - x),
                KeyCode::Left => (x - nx, ny - y),
                KeyCode::Right => (nx - x, ny - y),
                _ => return None,
            };
            // Move to the nearest row before choosing its closest column.
            // Matching centers must not skip wide fields or intermediate rows.
            let aligned = matches!(direction, KeyCode::Up | KeyCode::Down) || sideways == 0;
            (forward > 0).then_some((index, (!aligned, forward, sideways.abs())))
        })
        .min_by_key(|(_, score)| *score)
        .map(|(index, _)| index)
}

pub(super) fn pinned_back_area(area: Rect) -> Rect {
    let rows = Layout::vertical([
        Constraint::Length(3),
        Constraint::Min(4),
        Constraint::Length(3),
    ])
    .split(area);
    Rect::new(
        rows[2].right().saturating_sub(7),
        rows[2].y,
        7.min(rows[2].width),
        rows[2].height.min(3),
    )
}

pub(super) fn is_back_control(control: Control) -> bool {
    matches!(
        control,
        Control::ControllerBack
            | Control::NodesBack
            | Control::NodesEditBack
            | Control::WizardBack
            | Control::AccessBack
            | Control::AccessExpiryBack
            | Control::AccessGrantBack
            | Control::TemporaryBack
            | Control::Settings
    )
}

pub(super) fn action_areas(
    screen: Rect,
    area: Rect,
    controls: impl IntoIterator<Item = Control>,
    max_rows: usize,
) -> Vec<Rect> {
    let controls: Vec<_> = controls.into_iter().collect();
    let count = controls
        .iter()
        .filter(|control| !is_back_control(**control))
        .count();
    let mut grid = button_areas(area, count, max_rows).into_iter();
    controls
        .into_iter()
        .map(|control| {
            if is_back_control(control) {
                pinned_back_area(screen)
            } else {
                grid.next().unwrap()
            }
        })
        .collect()
}

pub(super) fn button_areas(area: Rect, count: usize, max_rows: usize) -> Vec<Rect> {
    if count == 0 {
        return Vec::new();
    }
    let rows = if count > 2 {
        max_rows.min(usize::from(area.height / 3)).max(1)
    } else {
        1
    };
    let columns = count.div_ceil(rows);
    (0..count)
        .map(|index| {
            let row = index / columns;
            let row_count = (count - row * columns).min(columns);
            Layout::horizontal(vec![Constraint::Fill(1); row_count])
                .spacing(1)
                .split(Rect::new(
                    area.x,
                    area.y + row as u16 * 3,
                    area.width,
                    3.min(area.height),
                ))[index % columns]
        })
        .collect()
}

pub(super) fn dialog(frame: &mut Frame, area: Rect, title: &str) -> [Rect; 2] {
    let block = section_block(title);
    let inner = block.inner(area);
    frame.render_widget(Clear, area);
    frame.render_widget(block, area);
    let parts = Layout::vertical([Constraint::Min(0), Constraint::Length(3)]).split(inner);
    [parts[0], parts[1]]
}

pub(super) fn draw_button(frame: &mut Frame, area: Rect, label: &str, selected: bool) {
    draw_button_color(frame, area, label, selected, Color::Cyan);
}
pub(super) fn draw_choice_button(
    frame: &mut Frame,
    area: Rect,
    label: &str,
    focused: bool,
    chosen: bool,
) {
    draw_button(frame, area, label, focused);
    if chosen {
        frame.render_widget(
            Paragraph::new(label)
                .alignment(ratatui::layout::Alignment::Center)
                .style(
                    Style::default()
                        .fg(Color::Cyan)
                        .bg(Color::Reset)
                        .add_modifier(Modifier::BOLD),
                ),
            Block::bordered().inner(area),
        );
    }
}
pub(super) fn draw_button_color(
    frame: &mut Frame,
    area: Rect,
    label: &str,
    selected: bool,
    accent: Color,
) {
    let block = Block::bordered().border_style(Style::default().fg(if selected {
        accent
    } else {
        Color::White
    }));
    let inner = block.inner(area);
    frame.render_widget(block, area);
    frame.render_widget(
        Paragraph::new(label)
            .alignment(ratatui::layout::Alignment::Center)
            .style(
                Style::default()
                    .fg(if selected {
                        accent
                    } else if accent == Color::Red {
                        Color::Red
                    } else {
                        Color::Gray
                    })
                    .bg(Color::Reset),
            ),
        inner,
    );
}

pub(super) fn button_pair(
    frame: &mut Frame,
    area: Rect,
    labels: (&str, &str),
    first: bool,
    controls: (Control, Control),
    hits: &mut Vec<Hit>,
) {
    let strip = centered(area, 36, 3);
    let buttons = Layout::horizontal([
        Constraint::Fill(1),
        Constraint::Length(2),
        Constraint::Fill(1),
    ])
    .split(strip);
    draw_button_color(
        frame,
        buttons[0],
        labels.0,
        first,
        if matches!(
            controls.0,
            Control::ConfirmDelete(true) | Control::Discard(true)
        ) {
            Color::Red
        } else {
            Color::Cyan
        },
    );
    draw_button(frame, buttons[2], labels.1, !first);
    hits.push(Hit {
        area: buttons[0],
        control: controls.0,
    });
    hits.push(Hit {
        area: buttons[2],
        control: controls.1,
    });
}

pub(super) fn cycle_channel(value: &mut String, backwards: bool) {
    *value = match value.as_str() {
        "main" => "dev",
        "dev" => "main",
        _ if backwards => "main",
        _ => "dev",
    }
    .into();
}
pub(super) fn draw_channel(
    frame: &mut Frame,
    area: Rect,
    value: &str,
    selected: bool,
    editor: bool,
    hits: &mut Vec<Hit>,
) {
    let block = Block::bordered()
        .title(" Update channel · ← / → ")
        .border_style(Style::default().fg(if selected { Color::Cyan } else { Color::Gray }));
    let inner = block.inner(area);
    frame.render_widget(block, area);
    let parts = Layout::horizontal([
        Constraint::Length(3),
        Constraint::Min(0),
        Constraint::Length(3),
    ])
    .split(inner);
    frame.render_widget(
        Paragraph::new("←").alignment(ratatui::layout::Alignment::Center),
        parts[0],
    );
    frame.render_widget(
        Paragraph::new(if value.is_empty() { "Installed" } else { value })
            .alignment(ratatui::layout::Alignment::Center),
        parts[1],
    );
    frame.render_widget(
        Paragraph::new("→").alignment(ratatui::layout::Alignment::Center),
        parts[2],
    );
    // Arrow hits follow the field hit so clicks change the choice immediately.
    hits.push(Hit {
        area: parts[0],
        control: Control::Channel(editor, true),
    });
    hits.push(Hit {
        area: parts[2],
        control: Control::Channel(editor, false),
    });
}
pub(super) fn viewport_start(current: usize, selected: usize, capacity: usize) -> usize {
    if selected < current {
        selected
    } else if selected >= current.saturating_add(capacity.max(1)) {
        selected + 1 - capacity.max(1)
    } else {
        current
    }
}
pub(super) fn sidebar_action_label(action: Action, compact: bool) -> &'static str {
    if compact && action == Action::Update {
        "NP Updates"
    } else if compact && action == Action::Install {
        "Install NP"
    } else {
        action.label()
    }
}

pub(super) fn draw_navigation_button(frame: &mut Frame, area: Rect, label: &str, selected: bool) {
    let style = Style::default().fg(if selected { Color::Cyan } else { Color::White });
    let block = Block::bordered().border_style(style);
    frame.render_widget(
        Paragraph::new(if area.height < 3 { "" } else { label })
            .alignment(ratatui::layout::Alignment::Center)
            .style(if selected {
                style.add_modifier(Modifier::BOLD)
            } else {
                Style::default().fg(Color::Gray)
            })
            .block(if area.height < 3 {
                block.title(label).title_style(style)
            } else {
                block
            }),
        area,
    );
}
pub(super) fn draw_field(frame: &mut Frame, rect: Rect, label: &str, value: &str, selected: bool) {
    let style = Style::default().fg(if selected { Color::Cyan } else { Color::White });
    frame.render_widget(
        Paragraph::new(value).block(
            Block::bordered()
                .title(label)
                .title_style(Style::default().fg(Color::Cyan))
                .border_style(style),
        ),
        rect,
    );
}

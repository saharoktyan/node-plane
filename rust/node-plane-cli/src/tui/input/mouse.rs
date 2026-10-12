use super::super::*;

impl App {
    pub(in crate::tui) fn mouse(&mut self, event: MouseEvent) -> Option<KeyEvent> {
        // Releasing a mouse button must not restore an unrelated keyboard focus.
        self.pointer_active = true;
        self.pointer_position = Some((event.column, event.row).into());
        let modal = self.prompt.is_some() || self.exit || self.discard.is_some() || self.qr_visible;
        if !modal && let Some(bar) = self.scrollbar.get() {
            match event.kind {
                MouseEventKind::Down(MouseButton::Left)
                    if bar.track.contains((event.column, event.row).into()) =>
                {
                    let offset = event.row.saturating_sub(bar.track.y);
                    let on_thumb =
                        offset >= bar.thumb_start && offset < bar.thumb_start + bar.thumb_length;
                    let grab = if on_thumb {
                        offset - bar.thumb_start
                    } else {
                        bar.thumb_length / 2
                    };
                    self.scroll_drag = Some(grab);
                    self.hovered = None;
                    if !on_thumb {
                        self.set_scroll_position(bar, bar.position_at(event.row, grab));
                    }
                    return None;
                }
                MouseEventKind::Drag(MouseButton::Left) if self.scroll_drag.is_some() => {
                    self.set_scroll_position(
                        bar,
                        bar.position_at(event.row, self.scroll_drag.unwrap()),
                    );
                    return None;
                }
                MouseEventKind::Up(MouseButton::Left) if self.scroll_drag.take().is_some() => {
                    return None;
                }
                MouseEventKind::Moved if bar.track.contains((event.column, event.row).into()) => {
                    self.hovered = None;
                    return None;
                }
                MouseEventKind::ScrollUp | MouseEventKind::ScrollDown => {
                    self.pointer_active = true;
                    self.pointer_position = Some((event.column, event.row).into());
                    let step = if matches!(bar.target, ScrollTarget::List) {
                        1
                    } else {
                        3
                    };
                    let position = if event.kind == MouseEventKind::ScrollUp {
                        bar.position.saturating_sub(step)
                    } else {
                        bar.position.saturating_add(step)
                    };
                    self.set_scroll_position(bar, position);
                    // Update geometry immediately so a burst of input can accumulate.
                    self.scrollbar.set(Some(Scrollbar::new(
                        bar.track,
                        position,
                        bar.maximum,
                        bar.visible,
                        bar.target,
                    )));
                    return None;
                }
                _ => {}
            }
        }
        if matches!(
            event.kind,
            MouseEventKind::Up(MouseButton::Left) | MouseEventKind::Down(MouseButton::Left)
        ) {
            self.scroll_drag = None;
        }
        if matches!(
            event.kind,
            MouseEventKind::ScrollUp | MouseEventKind::ScrollDown
        ) {
            return None;
        }
        if event.kind == MouseEventKind::Moved {
            self.pointer_position = Some((event.column, event.row).into());
            let control = self
                .hits
                .iter()
                .rev()
                .find(|hit| hit.area.contains((event.column, event.row).into()))
                .map(|hit| hit.control);
            if let Some(control) = control {
                self.focus(control);
            }
            self.hovered = control;
            return None;
        }
        if event.kind != MouseEventKind::Down(MouseButton::Left) {
            return None;
        }
        let control = self
            .hits
            .iter()
            .rev()
            .find(|hit| hit.area.contains((event.column, event.row).into()))?
            .control;
        self.hovered = Some(control);
        self.activate(control)
    }
    pub(in crate::tui) fn refresh_scroll_hover(&mut self) -> bool {
        if !self.pointer_active {
            return false;
        }
        let control = self.pointer_position.and_then(|position| {
            self.hits
                .iter()
                .rev()
                .find(|hit| hit.area.contains(position))
                .map(|hit| hit.control)
        });
        let changed = self.hovered != control;
        self.hovered = control;
        if let Some(control) = control {
            self.focus(control);
        }
        changed
    }
}

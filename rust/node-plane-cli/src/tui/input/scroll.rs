use super::super::*;

impl App {
    pub(in crate::tui) fn scroll_result(&mut self, backwards: bool, amount: usize) {
        self.result_scroll = if backwards {
            self.result_scroll.saturating_sub(amount)
        } else {
            self.result_scroll
                .saturating_add(amount)
                .min(self.result_max_scroll.get())
        };
    }
    pub(in crate::tui) fn viewport(
        &self,
        automatic: usize,
        length: usize,
        capacity: usize,
    ) -> usize {
        let maximum = length.saturating_sub(capacity.max(1));
        let start = self.manual_viewport.get().unwrap_or(automatic).min(maximum);
        self.list_viewport.set(start);
        self.list_max_viewport.set(maximum);
        self.register_scroll(start, maximum, capacity, ScrollTarget::List);
        if self.manual_viewport.get().is_some() {
            self.manual_viewport.set(Some(start));
        }
        self.content_overflow.set(maximum > 0);
        start
    }

    pub(in crate::tui) fn register_scroll(
        &self,
        position: usize,
        maximum: usize,
        visible: usize,
        target: ScrollTarget,
    ) {
        if maximum > 0 && self.scroll_track.get().height > 0 {
            self.scrollbar.set(Some(Scrollbar::new(
                self.scroll_track.get(),
                position,
                maximum,
                visible,
                target,
            )));
        }
    }
    pub(in crate::tui) fn set_scroll_position(&mut self, bar: Scrollbar, position: usize) {
        let next = position.min(bar.maximum);
        self.action_focus = None;
        match bar.target {
            ScrollTarget::Result => self.result_scroll = next,
            ScrollTarget::NodeDiagnostics => self.nodes.diagnostic_scroll.set(next),
            ScrollTarget::NodeCard => self.nodes.card_scroll = next,
            ScrollTarget::Maintenance => {
                if let Some(view) = &mut self.nodes.maintenance {
                    view.scroll = next;
                }
            }
            ScrollTarget::Temporary => self.temporary.scroll = next.min(u16::MAX as usize) as u16,
            ScrollTarget::List => {
                self.manual_viewport.set(Some(next));
                match self.screen {
                    Screen::Nodes if self.nodes.card.is_none() && self.nodes.editor.is_none() => {
                        let pages = self.nodes.pages(self.nodes_capacity.get());
                        let mut start = 0;
                        self.nodes.page = pages
                            .iter()
                            .position(|page| {
                                start += page.len();
                                next < start
                            })
                            .unwrap_or(pages.len().saturating_sub(1));
                    }
                    Screen::Access => {
                        let page = next / self.access_capacity.get().max(1);
                        if let Some(devices) = &mut self.access.devices {
                            devices.page = page;
                        } else if let Some(editor) = &mut self.access.editor {
                            if editor.grants_view {
                                editor.grants_page = page;
                            } else if editor.expiry_view {
                                editor.expiry_page = page;
                            }
                        } else {
                            self.access.page = page;
                        }
                    }
                    Screen::Temporary if self.temporary.view == crate::temporary::View::Server => {
                        self.temporary.page = next / self.temporary.capacity.get().max(1)
                    }
                    _ => {}
                }
            }
        }
    }
}

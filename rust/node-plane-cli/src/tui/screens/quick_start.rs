use super::super::*;

pub(in crate::tui) fn draw_quick_start(
    frame: &mut Frame,
    app: &App,
    area: Rect,
    hits: &mut Vec<Hit>,
) {
    let right = draw_navigation(frame, app, area, hits);
    hits.clear();
    let sections = dialog(frame, right, " Welcome to Node Plane Workstation! ");
    let text = "Quick start guide:\n1. Create a connection profile with your VPS address, SSH port and user.\n2. Select \"Install Node Plane\" and enter your Telegram ID and BotFather token.\n3. Confirm the server fingerprint and enter the SSH password when requested.\n4. Open \"Nodes\" to add a VPN node, install its agent and protocols.\n\nPasswords and bot tokens are not saved on disk.";
    let layout = Layout::vertical([Constraint::Min(8), Constraint::Length(1)]).split(sections[0]);
    frame.render_widget(Paragraph::new(text).wrap(Wrap { trim: false }), layout[0]);
    let checkbox_label = if app.connections.hide_quick_start {
        "[X] Don't show this again"
    } else {
        "[ ] Don't show this again"
    };
    frame.render_widget(
        Paragraph::new(checkbox_label).style(Style::default().fg(Color::Cyan)),
        layout[1],
    );
    hits.push(Hit {
        area: layout[1],
        control: Control::QuickStartToggle,
    });
    button_pair(
        frame,
        sections[1],
        ("Close", "Quick start"),
        app.confirm,
        (Control::QuickStartClose, Control::QuickStartAction),
        hits,
    );
}

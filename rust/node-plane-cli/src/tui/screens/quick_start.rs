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
    let text = if app.controller_ready() {
        if let Some(value) = &app.controller.onboarding {
            let labels = [
                ("defaults", "Confirm installation defaults"),
                ("node", "Create your first VPN node"),
                ("protocols", "Install agent and protocols"),
                ("access", "Grant your profile access"),
                ("config", "Create a device and obtain a configuration"),
            ];
            let mut text = String::from(
                "Progress is checked against controller state. F1 refreshes this guide.\n\n",
            );
            for (key, label) in labels {
                let done = value["steps"].as_array().is_some_and(|steps| {
                    steps
                        .iter()
                        .any(|v| v["key"] == key && v["complete"] == true)
                });
                text.push_str(&format!("{} {label}\n", if done { "✓" } else { "○" }));
            }
            text.push_str(match value["next_step"].as_str().unwrap_or("done") {
                "defaults" => "\nContinue accepts current defaults. To customize them, open Controller → Installation defaults first.",
                "node" => "\nContinue opens the node creation wizard. Choose Local for a node on this controller.",
                "protocols" => "\nContinue opens the node. Set up its agent, then bootstrap protocols. Wait for a successful operation.",
                "access" => "\nContinue opens Access. Select your profile → Grants and add this node. Save and wait for application.",
                "config" => "\nContinue opens Access to create an AWG device. Obtain your configuration in the Telegram bot: Main menu → Configurations. Import it into a VPN client and test connectivity. F1 checks completed issuance.",
                _ => "\nConfiguration issued. Import it into a client and test connectivity from your network.",
            });
            text
        } else {
            "Continue loads progress from the controller.".into()
        }
    } else {
        "1. Create a connection profile with your VPS address, SSH port and user.\n2. Select Install Node Plane and enter your Telegram ID and BotFather token.\n3. Confirm the server fingerprint and enter the SSH password when requested.\n4. After installation press F1 to continue with defaults, your first node, access and configuration.\n\nPasswords and bot tokens are not saved on disk.".into()
    };
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
        (
            "Close",
            if app.controller_ready() {
                "Continue"
            } else {
                "Quick start"
            },
        ),
        app.confirm,
        (Control::QuickStartClose, Control::QuickStartAction),
        hits,
    );
}

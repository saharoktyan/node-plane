use super::super::*;

pub(in crate::tui) fn draw_quick_start(
    frame: &mut Frame,
    app: &App,
    area: Rect,
    hits: &mut Vec<Hit>,
) {
    let right = draw_navigation(frame, app, area, hits);
    let idle = matches!(app.screen, Screen::Idle);
    if !idle {
        hits.clear();
    }
    let sections = dialog(frame, right, " Quick Start ");
    let text = if app.controller_ready() {
        if let Some(value) = &app.controller.onboarding {
            let labels = [
                ("defaults", "Confirm installation defaults"),
                ("node", "Create your first VPN node"),
                ("protocols", "Install agent and protocols"),
                ("access", "Grant your profile access"),
                ("config", "Create a device and obtain a configuration"),
            ];
            let mut text =
                String::from("Progress refreshes every second while this guide is open.\n\n");
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
                "config" => "\nContinue opens Access to create an AWG device. Obtain your configuration in the Telegram bot: Main menu → Configurations. Import it into a VPN client and test connectivity. The guide checks completed issuance.",
                _ => "\nConfiguration issued. Import it into a client and test connectivity from your network.",
            });
            text
        } else {
            "Continue loads progress from the controller.".into()
        }
    } else {
        "1. Create a connection profile with your VPS address, SSH port and user.\n2. Select Install Node Plane and enter your Telegram ID and BotFather token.\n3. Confirm the server fingerprint and enter the SSH password when requested.\n4. After installation press F1 to continue with defaults, your first node, access and configuration.\n\nPasswords and bot tokens are not saved on disk.".into()
    };
    frame.render_widget(Paragraph::new(text).wrap(Wrap { trim: false }), sections[0]);
    let controls = if idle {
        (Control::QuickStartRefresh, Control::QuickStartAction)
    } else {
        (Control::QuickStartClose, Control::QuickStartAction)
    };
    let next_label = if app.controller_ready() {
        "Continue"
    } else {
        "Quick start"
    };
    if idle {
        let strip = centered(sections[1], 36, 3);
        let buttons = Layout::horizontal([
            Constraint::Fill(1),
            Constraint::Length(2),
            Constraint::Fill(1),
        ])
        .split(strip);
        let selected = app.idle_controls().get(app.idle_selection).copied();
        for (area, label, control) in [
            (buttons[0], "Refresh", controls.0),
            (buttons[2], next_label, controls.1),
        ] {
            draw_button(frame, area, label, selected == Some(control));
            hits.push(Hit { area, control });
        }
    } else {
        button_pair(
            frame,
            sections[1],
            ("Close", next_label),
            app.confirm,
            controls,
            hits,
        );
    }
}

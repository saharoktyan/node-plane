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
                ("defaults", "Confirm installation defaults for servers"),
                ("node", "Create your first VPN node"),
                ("protocols", "Install agent and protocols"),
            ];
            let mut text = String::new();
            for (key, label) in labels {
                let done = value["steps"].as_array().is_some_and(|steps| {
                    steps
                        .iter()
                        .any(|v| v["key"] == key && v["complete"] == true)
                });
                text.push_str(&format!("{} {label}\n", if done { "✓" } else { "○" }));
                if key == "defaults" {
                    text.push_str("  Node Plane Settings → Basic → Installation defaults\n");
                }
            }
            text.push_str(match value["next_step"].as_str().unwrap_or("done") {
                "defaults" => "\nContinue accepts current defaults. To customize them, open Node Plane Settings → Basic → Installation defaults first.",
                "node" => "\nContinue opens the node creation wizard. Choose Local for a node on this controller.",
                "protocols" => "\nContinue opens the node. Set up its agent, then bootstrap protocols. Wait for a successful operation.",
                "config" => "\nServer setup is complete.",
                _ => "\nServer setup is complete.",
            });
            text
        } else {
            "Continue loads progress from the controller.".into()
        }
    } else if app.form.saved.is_some() {
        "1. Continue opens Install Node Plane for the selected profile.\n2. Enter your Telegram administrator ID and BotFather token, then review the installation.\n3. Confirm the server fingerprint and enter the SSH password when requested.\n4. After installation return here for server defaults, your first node.".into()
    } else {
        "1. Create a connection profile with your VPS address, SSH port and user.\n2. Select Install Node Plane and enter your Telegram ID and BotFather token.\n3. Confirm the server fingerprint and enter the SSH password when requested.\n4. After installation return here to continue with defaults, your first node.\n\nPasswords and bot tokens are not saved on disk.".into()
    };
    frame.render_widget(Paragraph::new(text).wrap(Wrap { trim: false }), sections[0]);
    let controls = (Control::QuickStartRefresh, Control::QuickStartAction);
    let next_label = if app.controller_ready() {
        if app
            .controller
            .onboarding
            .as_ref()
            .is_some_and(|guide| matches!(guide["next_step"].as_str(), Some("config" | "done")))
        {
            "Done"
        } else {
            "Continue"
        }
    } else {
        "Quick start"
    };
    let complete = next_label == "Done";
    if complete {
        let area = sections[1];
        draw_button(
            frame,
            area,
            next_label,
            !idle
                || app.idle_controls().get(app.idle_selection) == Some(&Control::QuickStartAction),
        );
        hits.push(Hit {
            area,
            control: Control::QuickStartAction,
        });
    } else {
        let strip = sections[1];
        let buttons = Layout::horizontal([
            Constraint::Fill(1),
            Constraint::Length(2),
            Constraint::Fill(1),
        ])
        .split(strip);
        let selected = app.idle_controls().get(app.idle_selection).copied();
        for (area, label, control) in [
            (buttons[0], "Dismiss", controls.0),
            (buttons[2], next_label, controls.1),
        ] {
            draw_button(frame, area, label, selected == Some(control));
            hits.push(Hit { area, control });
        }
    }
}

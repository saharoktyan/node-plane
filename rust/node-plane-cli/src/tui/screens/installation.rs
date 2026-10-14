use super::super::*;

pub(in crate::tui) fn draw_form(frame: &mut Frame, app: &App, area: Rect, hits: &mut Vec<Hit>) {
    if app.actions_enabled()
        && !app.fresh_installation
        && (app.form.action == Action::Install && app.has_installation()
            || app.form.action != Action::Install && !app.controller_ready())
    {
        draw_controller(frame, app, area, hits);
        return;
    }
    let labels = [
        "Controller hostname / IP",
        "SSH port",
        "SSH user",
        "Update channel (main / dev)",
        "Release tag (blank = latest)",
        "Telegram administrator ID(s)",
        "BotFather token (hidden)",
        "Administrator account (UUID / Telegram ID; blank = only admin)",
        "Target node hostname / IP",
        "Target SSH port",
        "Target SSH user",
        "Offer service and operation recovery (confirmation required)",
    ];
    let right = draw_navigation(frame, app, area, hits);
    if !app.actions_enabled() {
        let sections = dialog(frame, right, " New installation profile ");
        frame.render_widget(Paragraph::new("Create or select an installation profile to enable actions.\n\nUse ← / → in the profile switcher above Settings.\nSelect New profile again, or press Enter, to create a profile.").wrap(Wrap { trim: false }), sections[0]);
        return;
    }
    let title = app.form.saved.as_ref().map_or_else(
        || " Connection settings ".to_string(),
        |profile| {
            format!(
                " {} · {} ",
                profile.name,
                if app.form.action == Action::PrepareNode {
                    "Prepare SSH access"
                } else {
                    app.form.action.label()
                }
            )
        },
    );
    let sections = dialog(frame, right, &title);
    if app.form.action == Action::Install
        && sections[0].height >= (app.form.visible().len() as u16 + 1) * 3
    {
        let manage = centered(
            Rect::new(
                sections[0].x,
                sections[0].bottom() - 3,
                sections[0].width,
                3,
            ),
            18,
            3,
        );
        draw_button(frame, manage, "Manage installed", false);
        hits.push(Hit {
            area: manage,
            control: Control::Controller,
        });
    }
    let visible = app.form.visible();
    if visible.is_empty() {
        frame.render_widget(Paragraph::new("This installation is ready. Continue to review the action.\nEdit its connection details in Settings.").wrap(Wrap { trim: false }), sections[0]);
    }
    let capacity = usize::from(sections[0].height / 3).max(1);
    let selected = app
        .form
        .selected
        .saturating_sub(1)
        .min(visible.len().saturating_sub(1));
    app.content_overflow.set(visible.len() > capacity);
    let offset = selected
        .saturating_add(1)
        .saturating_sub(capacity)
        .min(visible.len().saturating_sub(capacity));
    let offset = app.viewport(offset, visible.len(), capacity);
    for (position, &i) in visible.iter().enumerate().skip(offset).take(capacity) {
        let y = sections[0].y + (position - offset) as u16 * 3;
        if y.saturating_add(3) > sections[0].bottom() {
            break;
        }
        let label = if i == 3 && app.form.action == Action::Update {
            "Update channel (blank = installed channel)"
        } else {
            labels[i]
        };
        let value = if i == 6 {
            "*".repeat(app.form.fields[i].len().min(48))
        } else {
            app.form.fields[i].clone()
        };
        let rect = Rect::new(sections[0].x, y, sections[0].width, 3);
        let selected = app.form.selected == position + 1;
        hits.push(Hit {
            area: rect,
            control: Control::Field(position + 1),
        });
        if i == 3 {
            draw_channel(frame, rect, &value, selected, false, hits);
        } else if i == 11 {
            draw_field(
                frame,
                rect,
                labels[i],
                if value == "true" {
                    "← Enabled →"
                } else {
                    "← Disabled · read-only →"
                },
                selected,
            );
            hits.push(Hit {
                area: rect,
                control: Control::RepairMode,
            });
        } else {
            draw_field(frame, rect, label, &value, selected);
        }
    }
    let button_area = if app.form.action == Action::Diagnose {
        let buttons = Layout::horizontal([Constraint::Percentage(50), Constraint::Percentage(50)])
            .spacing(1)
            .split(sections[1]);
        let operations = centered(buttons[0], 24, 3);
        draw_button(
            frame,
            operations,
            "Operations",
            app.form.selected == app.form.count() - 2,
        );
        hits.push(Hit {
            area: operations,
            control: Control::ControllerRecoveryInventory(0),
        });
        buttons[1]
    } else {
        sections[1]
    };
    let button = centered(button_area, 18, 3);
    draw_button(
        frame,
        button,
        "Continue",
        app.form.selected == app.form.count() - 1,
    );
    hits.push(Hit {
        area: button,
        control: Control::Continue,
    });
}

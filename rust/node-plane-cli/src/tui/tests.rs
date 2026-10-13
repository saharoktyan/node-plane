#[test]
fn header_status_tracks_installation_probe_and_connection() {
    let mut app = test_profile_app();
    assert_eq!(app.header_status(), "Status: SSH closed");
    app.probe_requested = true;
    assert_eq!(app.header_status(), "Status: Loading installation…");
    app.ssh_state = crate::nodes::ConnectionState::Connecting;
    assert_eq!(app.header_status(), "Status: SSH connecting");
    app.probe_requested = false;
    app.nodes_busy = true;
    app.nodes_background = true;
    app.loading_message = "Loading installation…".into();
    app.ssh_state = crate::nodes::ConnectionState::Connected;
    assert_eq!(app.header_status(), "Status: Loading installation…");
    app.nodes_busy = false;
    assert_eq!(app.header_status(), "Status: SSH connected");
}

#[test]
fn startup_update_offer_waits_for_connection_then_opens_on_idle_with_its_own_title() {
    let mut app = test_profile_app();
    app.screen = Screen::Idle;
    app.self_release = Some(
        serde_json::from_value(serde_json::json!({
            "tag_name":"v99.0.0", "draft":false, "prerelease":false
        }))
        .unwrap(),
    );
    app.nodes_busy = true;
    assert!(app.offer_workstation_update().is_none());
    assert!(app.prompt.is_none());
    app.nodes_busy = false;
    app.probe_requested = true;
    assert!(app.offer_workstation_update().is_none());
    app.probe_requested = false;
    app.nodes_requested = Some(crate::nodes::Command::Controller(
        app.controller.profile,
        crate::controller::Command::Overview,
    ));
    assert!(app.offer_workstation_update().is_none());
    app.nodes_requested = None;
    app.screen = Screen::Running;
    assert!(app.offer_workstation_update().is_none());
    app.screen = Screen::Idle;
    let answer = app.offer_workstation_update().unwrap();
    assert!(
        matches!(&app.prompt, Some((Prompt::ConfirmAction { title, .. }, _)) if title == "Update Workstation?")
    );
    let mut terminal = Terminal::new(ratatui::backend::TestBackend::new(100, 40)).unwrap();
    terminal.draw(|frame| app.hits = draw(frame, &app)).unwrap();
    let text: String = terminal
        .backend()
        .buffer()
        .content()
        .iter()
        .map(|cell| cell.symbol())
        .collect();
    assert!(text.contains("Update Workstation?"));
    assert!(text.contains("99.0.0"));
    assert!(text.contains(crate::config::workstation_channel()));
    assert!(!text.contains("Connection confirmation"));
    app.prompt
        .take()
        .unwrap()
        .1
        .send(Answer::Confirm(false))
        .unwrap();
    assert!(matches!(answer.recv().unwrap(), Answer::Confirm(false)));
    assert!(matches!(app.screen, Screen::Idle));
}

#[test]
fn header_close_uses_confirmation_and_red_hover() {
    let mut app = test_profile_app();
    app.screen = Screen::Idle;
    let mut terminal = Terminal::new(ratatui::backend::TestBackend::new(100, 40)).unwrap();
    terminal.draw(|frame| app.hits = draw(frame, &app)).unwrap();
    let close = app
        .hits
        .iter()
        .find(|hit| hit.control == Control::WindowClose)
        .unwrap()
        .area;
    let symbol = &terminal.backend().buffer()[(close.right() - 2, close.y)];
    assert_eq!(symbol.fg, Color::Gray);
    assert_eq!(symbol.bg, Color::Reset);
    app.mouse(MouseEvent {
        kind: MouseEventKind::Moved,
        column: close.x,
        row: close.y + 1,
        modifiers: KeyModifiers::NONE,
    });
    assert!(!app.exit);
    terminal.draw(|frame| app.hits = draw(frame, &app)).unwrap();
    let symbol = &terminal.backend().buffer()[(close.right() - 2, close.y)];
    assert_eq!(symbol.symbol(), "×");
    assert_eq!(symbol.fg, Color::Red);
    assert_eq!(symbol.bg, Color::Reset);
    app.activate(Control::WindowClose);
    assert!(app.exit && !app.exit_confirm);
    terminal.draw(|frame| app.hits = draw(frame, &app)).unwrap();
    assert!(
        app.hits
            .iter()
            .any(|hit| hit.control == Control::Exit(true))
    );
    assert!(
        !app.hits
            .iter()
            .any(|hit| hit.control == Control::WindowClose)
    );
    let cancel = app
        .hits
        .iter()
        .find(|hit| hit.control == Control::Exit(false))
        .unwrap()
        .area;
    assert!(
        app.mouse(MouseEvent {
            kind: MouseEventKind::Down(MouseButton::Left),
            column: cancel.x + 1,
            row: cancel.y + 1,
            modifiers: KeyModifiers::NONE
        })
        .is_none()
    );
    app.mouse(MouseEvent {
        kind: MouseEventKind::Up(MouseButton::Left),
        column: cancel.x + 1,
        row: cancel.y + 1,
        modifiers: KeyModifiers::NONE,
    });
    assert!(!app.exit);
    assert!(app.pointer_active);
    assert!(app.hovered.is_none());
    terminal.draw(|frame| app.hits = draw(frame, &app)).unwrap();
    let settings = app
        .hits
        .iter()
        .find(|hit| hit.control == Control::Settings)
        .unwrap()
        .area;
    assert_eq!(
        terminal.backend().buffer()[(settings.x, settings.y)].fg,
        Color::White
    );
}

#[test]
fn connected_section_loading_has_no_preparation_counter() {
    let mut app = test_profile_app();
    app.screen = Screen::Running;
    app.nodes_busy = true;
    app.ssh_state = crate::nodes::ConnectionState::Connected;
    app.loading_message = "Loading nodes…".into();
    let mut terminal = Terminal::new(ratatui::backend::TestBackend::new(100, 40)).unwrap();
    terminal.draw(|frame| app.hits = draw(frame, &app)).unwrap();
    let text = terminal
        .backend()
        .buffer()
        .content
        .iter()
        .map(|cell| cell.symbol())
        .collect::<String>();
    assert!(text.contains("Loading nodes…"));
    assert!(!text.contains("Step "));
    assert!(!text.contains("Authorizing"));
}

#[test]
fn scrollbar_drag_reaches_bottom_without_activating_controls() {
    let mut app = test_profile_app();
    app.screen = Screen::SettingsPage(SettingsPage::Appearance);
    app.settings_selected = 1;
    let mut terminal = Terminal::new(ratatui::backend::TestBackend::new(100, 18)).unwrap();
    terminal
        .draw(|frame| app.hits = draw_content(frame, &app))
        .unwrap();
    let bar = app.scrollbar.get().unwrap();
    let selected = app.settings_selected;
    app.mouse(MouseEvent {
        kind: MouseEventKind::Down(MouseButton::Left),
        column: bar.track.x,
        row: bar.track.y + bar.thumb_start,
        modifiers: KeyModifiers::NONE,
    });
    assert!(app.scroll_drag.is_some());
    app.mouse(MouseEvent {
        kind: MouseEventKind::Drag(MouseButton::Left),
        column: bar.track.x.saturating_sub(10),
        row: bar.track.bottom() + 10,
        modifiers: KeyModifiers::NONE,
    });
    terminal
        .draw(|frame| app.hits = draw_content(frame, &app))
        .unwrap();
    assert_eq!(app.list_viewport.get(), app.list_max_viewport.get());
    assert_eq!(app.settings_selected, selected);
    assert!(app.nodes_requested.is_none());
    app.mouse(MouseEvent {
        kind: MouseEventKind::Up(MouseButton::Left),
        column: bar.track.x,
        row: bar.track.bottom(),
        modifiers: KeyModifiers::NONE,
    });
    assert!(app.scroll_drag.is_none());
    terminal.backend_mut().resize(100, 70);
    terminal.resize(Rect::new(0, 0, 100, 70)).unwrap();
    terminal
        .draw(|frame| app.hits = draw_content(frame, &app))
        .unwrap();
    assert!(app.scrollbar.get().is_none());
}

#[test]
fn diagnostic_result_uses_scrollbar_and_close_without_arrow_buttons() {
    let mut app = test_profile_app();
    app.screen = Screen::Finished;
    app.form.action = Action::Diagnose;
    app.outcome = Some(Ok((0..80).map(|i| format!("Check {i}\n")).collect()));
    let mut terminal = Terminal::new(ratatui::backend::TestBackend::new(100, 25)).unwrap();
    terminal
        .draw(|frame| app.hits = draw_content(frame, &app))
        .unwrap();
    assert_eq!(
        app.hits
            .iter()
            .filter(|hit| hit.control != Control::WindowClose)
            .count(),
        1
    );
    assert!(app.hits[0].control == Control::Close);
    let bar = app.scrollbar.get().unwrap();
    assert!(matches!(bar.target, ScrollTarget::Result));
    app.mouse(MouseEvent {
        kind: MouseEventKind::Down(MouseButton::Left),
        column: bar.track.x,
        row: bar.track.bottom() - 1,
        modifiers: KeyModifiers::NONE,
    });
    assert_eq!(app.result_scroll, app.result_max_scroll.get());
    assert!(matches!(app.screen, Screen::Finished));
}

#[test]
fn compact_sidebar_labels_fit_and_narrow_windows_disable_content() {
    let mut app = test_profile_app();
    app.installed_profile = app.confirmed_installation;
    app.screen = Screen::Idle;
    for (width, short) in [(120, false), (85, true), (70, true)] {
        let mut terminal = Terminal::new(ratatui::backend::TestBackend::new(width, 40)).unwrap();
        terminal
            .draw(|frame| app.hits = draw_content(frame, &app))
            .unwrap();
        let text = terminal
            .backend()
            .buffer()
            .content
            .iter()
            .map(|cell| cell.symbol())
            .collect::<String>();
        for label in if short {
            ["NP Settings", "NP Updates", "Temp. Configs"]
        } else {
            [
                "Node Plane Settings",
                "Node Plane Updates",
                "Temporary configs",
            ]
        } {
            assert!(text.contains(label), "missing {label} at width {width}");
        }
    }
    for (width, height) in [(69, 30), (70, 29), (49, 20)] {
        let mut terminal =
            Terminal::new(ratatui::backend::TestBackend::new(width, height)).unwrap();
        terminal.draw(|frame| app.hits = draw(frame, &app)).unwrap();
        assert!(app.hits.is_empty());
        assert!(
            format!("{:?}", terminal.backend().buffer()).contains("Terminal window is too small")
        );
        app.exit = true;
        terminal.draw(|frame| app.hits = draw(frame, &app)).unwrap();
        assert!(
            app.hits
                .iter()
                .any(|hit| hit.control == Control::Exit(true))
        );
        assert!(
            app.hits
                .iter()
                .any(|hit| hit.control == Control::Exit(false))
        );
        app.exit = false;
    }
    let mut terminal = Terminal::new(ratatui::backend::TestBackend::new(70, 30)).unwrap();
    terminal.draw(|frame| app.hits = draw(frame, &app)).unwrap();
    assert!(!app.hits.is_empty());
}

#[test]
fn profile_management_actions_use_two_columns() {
    let mut app = test_profile_app();
    app.screen = Screen::Access;
    app.access.tab = crate::access::Tab::Profiles;
    app.access.card = Some(serde_json::json!({"display_name":"Member","frozen":false,
            "_account":{"role":"member","status":"approved"}}));
    let mut terminal = Terminal::new(ratatui::backend::TestBackend::new(100, 40)).unwrap();
    terminal
        .draw(|frame| app.hits = draw_content(frame, &app))
        .unwrap();
    let controls: Vec<_> = app
        .hits
        .iter()
        .filter(|hit| {
            hit.area.x >= 26 && !matches!(hit.control, Control::AccessBack | Control::WindowClose)
        })
        .collect();
    assert_eq!(controls.len(), 6);
    for pair in controls.chunks(2) {
        if pair.len() == 2 {
            assert_eq!(pair[0].area.y, pair[1].area.y);
            assert!(pair[0].area.right() <= pair[1].area.x);
        }
    }
    assert!(controls.iter().all(|hit| hit.area.height == 3));
}

#[test]
fn defaults_protocols_and_advanced_options_use_responsive_rows() {
    let mut app = test_profile_app();
    app.screen = Screen::Controller;
    app.controller_sidebar = false;
    let defaults = serde_json::json!({"protocols":["awg","xray"],"xray_transports":["tcp"],
            "settings":{"awg_i1_preset":"quic","awg_port_mode":"auto"}});
    app.controller.defaults = Some(defaults.clone());
    app.controller.defaults_baseline = Some(defaults);
    assert!(!defaults_controls(&app.controller).contains(&Control::ControllerDefaultsPreset));
    app.controller.defaults_advanced = true;
    let controls = defaults_controls(&app.controller);
    for (width, columns) in [(100, 2), (40, 1)] {
        let rows = defaults_rows(&controls, width);
        let advanced = rows
            .iter()
            .position(|row| {
                row.iter()
                    .any(|&index| controls[index] == Control::ControllerDefaultsAdvanced)
            })
            .unwrap();
        assert_eq!(rows[advanced].len(), 1);
        assert_eq!(rows[advanced + 1].len(), columns);
        assert!(
            rows[advanced + 1]
                .iter()
                .any(|&i| controls[i] == Control::ControllerDefaultsPreset)
        );
        assert_eq!(rows[0].len(), columns);
    }
    let mut terminal = Terminal::new(ratatui::backend::TestBackend::new(120, 40)).unwrap();
    terminal
        .draw(|frame| app.hits = draw_content(frame, &app))
        .unwrap();
    let area = |control| {
        app.hits
            .iter()
            .find(|hit| hit.control == control)
            .unwrap()
            .area
    };
    let awg = area(Control::ControllerDefaultsToggle("awg"));
    let vless = area(Control::ControllerDefaultsToggle("xray"));
    let tcp = area(Control::ControllerDefaultsToggle("tcp"));
    let xhttp = area(Control::ControllerDefaultsToggle("xhttp"));
    assert_eq!(awg.y, vless.y);
    assert_eq!(tcp.y, xhttp.y);
    assert_eq!(awg.x, tcp.x);
    assert_eq!(vless.x, xhttp.x);
    let advanced = area(Control::ControllerDefaultsAdvanced);
    assert_eq!(advanced.width, awg.width + vless.width + 1);
    app.controller.selected = 0;
    app.controller_key(KeyCode::Down.into());
    assert!(
        app.controller_controls()[app.controller.selected]
            == Control::ControllerDefaultsToggle("tcp")
    );
    app.controller_key(KeyCode::Right.into());
    assert!(
        app.controller_controls()[app.controller.selected]
            == Control::ControllerDefaultsToggle("xhttp")
    );
    app.controller.defaults.as_mut().unwrap()["protocols"] = serde_json::json!(["awg"]);
    assert!(
        !defaults_controls(&app.controller).contains(&Control::ControllerDefaultsToggle("tcp"))
    );
}

#[test]
fn profile_delete_has_red_text_and_only_focused_red_border() {
    let mut app = test_profile_app();
    app.screen = Screen::Access;
    app.access.tab = crate::access::Tab::Profiles;
    app.access.card = Some(serde_json::json!({"display_name":"Member","frozen":false}));
    let mut terminal = Terminal::new(ratatui::backend::TestBackend::new(100, 35)).unwrap();
    terminal
        .draw(|frame| app.hits = draw_content(frame, &app))
        .unwrap();
    let delete = Control::AccessProfileAction(true);
    app.access.selected = app
        .access_controls()
        .iter()
        .position(|c| *c == Control::AccessEdit)
        .unwrap();
    terminal
        .draw(|frame| app.hits = draw_content(frame, &app))
        .unwrap();
    let rect = app
        .hits
        .iter()
        .find(|hit| hit.control == delete)
        .unwrap()
        .area;
    let buffer = terminal.backend().buffer();
    assert_eq!(buffer[(rect.x, rect.y)].fg, Color::White);
    assert!(
        (rect.x + 1..rect.right() - 1).any(|x| buffer[(x, rect.y + 1)].fg == Color::Red
            && !buffer[(x, rect.y + 1)].symbol().trim().is_empty())
    );
    app.mouse(MouseEvent {
        kind: MouseEventKind::Moved,
        column: rect.x + 1,
        row: rect.y + 1,
        modifiers: KeyModifiers::NONE,
    });
    terminal
        .draw(|frame| app.hits = draw_content(frame, &app))
        .unwrap();
    assert_eq!(terminal.backend().buffer()[(rect.x, rect.y)].fg, Color::Red);
}

#[test]
fn wheel_scroll_survives_hover_and_clamps_after_resize() {
    let mut app = test_profile_app();
    app.screen = Screen::SettingsPage(SettingsPage::Appearance);
    app.settings_selected = 1;
    let mut terminal = Terminal::new(ratatui::backend::TestBackend::new(100, 18)).unwrap();
    terminal
        .draw(|frame| app.hits = draw_content(frame, &app))
        .unwrap();
    app.mouse(MouseEvent {
        kind: MouseEventKind::ScrollDown,
        column: 60,
        row: 8,
        modifiers: KeyModifiers::NONE,
    });
    terminal
        .draw(|frame| app.hits = draw_content(frame, &app))
        .unwrap();
    app.refresh_scroll_hover();
    terminal
        .draw(|frame| app.hits = draw_content(frame, &app))
        .unwrap();
    assert_eq!(app.list_viewport.get(), 1);
    app.mouse(MouseEvent {
        kind: MouseEventKind::Moved,
        column: 60,
        row: 8,
        modifiers: KeyModifiers::NONE,
    });
    terminal
        .draw(|frame| app.hits = draw_content(frame, &app))
        .unwrap();
    assert_eq!(app.list_viewport.get(), 1);
    terminal.backend_mut().resize(100, 70);
    terminal.resize(Rect::new(0, 0, 100, 70)).unwrap();
    terminal
        .draw(|frame| app.hits = draw_content(frame, &app))
        .unwrap();
    assert_eq!(app.list_viewport.get(), 0);
    assert!(app.hits.iter().any(|hit| hit.control == Control::Accent(0)));
}

#[test]
fn detached_back_does_not_reserve_a_grid_slot() {
    let screen = Rect::new(0, 0, 100, 30);
    let area = Rect::new(27, 20, 70, 6);
    let controls = [
        Control::UseInstallation,
        Control::EditInstallation,
        Control::NewInstallation,
        Control::DeleteInstallation,
        Control::Settings,
    ];
    let rects = action_areas(screen, area, controls, 2);
    assert_eq!(rects[..4], button_areas(area, 4, 2));
    assert_eq!(rects[4], pinned_back_area(screen));
    assert_eq!(rects[0].width, rects[2].width);
    assert_eq!(rects[1].width, rects[3].width);
    assert_eq!(rects[0].y, rects[1].y);
    assert_eq!(rects[2].y, rects[3].y);
    for count in 1..=7 {
        let mut controls = vec![Control::NodesRefresh; count];
        controls.insert(count / 2, Control::NodesBack);
        let rects = action_areas(screen, area, controls.clone(), 2);
        let grid: Vec<_> = controls
            .iter()
            .zip(rects)
            .filter_map(|(control, rect)| (!is_back_control(*control)).then_some(rect))
            .collect();
        assert_eq!(grid, button_areas(area, count, 2));
    }
}

#[test]
fn saved_profile_actions_include_channel_copy_without_overlapping_back() {
    let mut app = test_profile_app();
    app.screen = Screen::SettingsPage(SettingsPage::Profiles);
    let mut terminal = Terminal::new(ratatui::backend::TestBackend::new(100, 30)).unwrap();
    terminal
        .draw(|frame| app.hits = draw_content(frame, &app))
        .unwrap();
    let rect = |control| {
        app.hits
            .iter()
            .find(|hit| hit.control == control)
            .unwrap()
            .area
    };
    let areas = [
        Control::UseInstallation,
        Control::EditInstallation,
        Control::NewInstallation,
        Control::TransferInstallation,
        Control::DeleteInstallation,
    ]
    .map(rect);
    for (i, area) in areas.iter().enumerate() {
        assert!(area.width > 0 && area.height > 0);
        for other in &areas[i + 1..] {
            assert!(area.intersection(*other).is_empty());
        }
    }
    assert_eq!(
        app.hits
            .iter()
            .rfind(|hit| hit.control == Control::Settings)
            .unwrap()
            .area,
        pinned_back_area(terminal.backend().buffer().area)
    );
}

#[test]
fn access_arrows_traverse_search_and_profiles_between_tabs_and_actions() {
    use crate::access::Tab;
    let mut app = test_profile_app();
    app.screen = Screen::Access;
    app.access.tab = Tab::Profiles;
    app.access.items = vec![serde_json::json!({"id":"one", "display_name":"Member"})];
    let mut terminal = Terminal::new(ratatui::backend::TestBackend::new(100, 30)).unwrap();
    for tab in Tab::ALL {
        terminal
            .draw(|frame| app.hits = draw_content(frame, &app))
            .unwrap();
        app.access.selected = app
            .access_controls()
            .iter()
            .position(|c| *c == Control::AccessTab(tab))
            .unwrap();
        app.access_key(KeyCode::Down.into());
        assert!(app.access_controls()[app.access.selected] == Control::AccessSearch);
        app.access_key(KeyCode::Down.into());
        assert!(app.access_controls()[app.access.selected] == Control::AccessItem(0));
        app.access_key(KeyCode::Down.into());
        assert!(matches!(
            app.access_controls()[app.access.selected],
            Control::AccessRefresh | Control::AccessNew
        ));
        app.access_key(KeyCode::Up.into());
        assert!(app.access_controls()[app.access.selected] == Control::AccessItem(0));
        app.access_key(KeyCode::Up.into());
        assert!(app.access_controls()[app.access.selected] == Control::AccessSearch);
        app.access_key(KeyCode::Up.into());
        assert!(matches!(
            app.access_controls()[app.access.selected],
            Control::AccessTab(_)
        ));
    }
}

#[test]
fn active_access_tab_retains_colored_label_without_focus_outline() {
    use crate::access::Tab;
    let mut app = test_profile_app();
    app.screen = Screen::Access;
    app.access.tab = Tab::Profiles;
    let mut terminal = Terminal::new(ratatui::backend::TestBackend::new(100, 30)).unwrap();
    terminal
        .draw(|frame| app.hits = draw_content(frame, &app))
        .unwrap();
    app.access.selected = app
        .access_controls()
        .iter()
        .position(|c| *c == Control::AccessSearch)
        .unwrap();
    for pointer_active in [false, true] {
        app.pointer_active = pointer_active;
        app.pointer_position = Some((0, 0).into());
        terminal
            .draw(|frame| app.hits = draw_content(frame, &app))
            .unwrap();
        let tab = app
            .hits
            .iter()
            .find(|h| h.control == Control::AccessTab(Tab::Profiles))
            .unwrap();
        let buffer = terminal.backend().buffer();
        assert_eq!(buffer[(tab.area.x, tab.area.y)].fg, Color::White);
        let inner = Block::bordered().inner(tab.area);
        assert!((inner.x..inner.right()).any(|x| {
            let cell = &buffer[(x, inner.y)];
            !cell.symbol().trim().is_empty() && cell.fg == Color::Cyan
        }));
    }
    app.pointer_active = false;
    app.access.selected = app
        .access_controls()
        .iter()
        .position(|c| *c == Control::AccessTab(Tab::Requests))
        .unwrap();
    terminal
        .draw(|frame| app.hits = draw_content(frame, &app))
        .unwrap();
    let tab = app
        .hits
        .iter()
        .find(|h| h.control == Control::AccessTab(Tab::Requests))
        .unwrap();
    assert_eq!(
        terminal.backend().buffer()[(tab.area.x, tab.area.y)].fg,
        Color::Cyan
    );
}

#[test]
fn section_boundaries_do_not_switch_sidebar_or_wrap_on_arrows() {
    let mut app = test_profile_app();
    let mut terminal = Terminal::new(ratatui::backend::TestBackend::new(100, 20)).unwrap();
    app.screen = Screen::Form;
    app.form.selected = app.form.count() - 1;
    for _ in 0..4 {
        app.mouse(MouseEvent {
            kind: MouseEventKind::ScrollDown,
            column: 60,
            row: 10,
            modifiers: KeyModifiers::NONE,
        });
    }
    assert_eq!(app.form.selected, app.form.count() - 1);
    assert!(matches!(app.screen, Screen::Form));
    app.screen = Screen::Settings;
    app.settings_selected = SettingsPage::ALL.len();
    terminal
        .draw(|frame| app.hits = draw_content(frame, &app))
        .unwrap();
    assert!(app.move_spatial(KeyCode::Down));
    assert_eq!(app.settings_selected, SettingsPage::ALL.len());
    app.screen = Screen::SettingsPage(SettingsPage::Appearance);
    app.settings_selected = app.themes.len() + 3;
    terminal
        .draw(|frame| app.hits = draw_content(frame, &app))
        .unwrap();
    app.appearance_key(KeyCode::Down);
    assert_eq!(app.settings_selected, app.themes.len() + 3);
    app.screen = Screen::Controller;
    app.controller_sidebar = false;
    app.controller_section = Some(ControllerSection::Removal);
    app.controller.selected = app.controller_controls().len() - 1;
    app.controller_key(KeyCode::Down.into());
    assert_eq!(app.controller.selected, app.controller_controls().len() - 1);
    assert!(!app.controller_sidebar);
    assert!(matches!(app.screen, Screen::Controller));
}

#[test]
fn compact_installation_editor_arrows_visit_every_field_and_stop_at_end() {
    let mut app = test_profile_app();
    app.begin_edit(false);
    let mut terminal = Terminal::new(ratatui::backend::TestBackend::new(100, 20)).unwrap();
    for selected in 0..9 {
        terminal
            .draw(|frame| app.hits = draw_content(frame, &app))
            .unwrap();
        assert_eq!(app.editor.as_ref().unwrap().2, selected);
        let control = match selected {
            0..=6 => Control::EditField(selected),
            7 => Control::SaveInstallation,
            _ => Control::CancelEdit,
        };
        assert!(
            app.hits.iter().any(|hit| hit.control == control),
            "missing selected control {selected}"
        );
        if selected < 7 {
            let save = app
                .hits
                .iter()
                .find(|hit| hit.control == Control::SaveInstallation)
                .unwrap();
            assert_eq!(
                terminal.backend().buffer()[(save.area.x, save.area.y)].fg,
                Color::White
            );
        }
        app.installation_editor_key(KeyCode::Down.into());
    }
    for _ in 0..4 {
        app.installation_editor_key(KeyEvent::new(KeyCode::Down, KeyModifiers::NONE));
    }
    assert_eq!(app.editor.as_ref().unwrap().2, 8);
    assert!(matches!(app.screen, Screen::EditInstallation));
    for selected in (0..8).rev() {
        app.installation_editor_key(KeyEvent::new(KeyCode::Up, KeyModifiers::NONE));
        assert_eq!(app.editor.as_ref().unwrap().2, selected);
    }
}

#[test]
fn hidden_temporary_choices_are_selected_before_footer_actions() {
    let mut app = test_profile_app();
    app.screen = Screen::Temporary;
    app.temporary_sidebar = false;
    app.temporary.view = crate::temporary::View::Duration;
    let mut terminal = Terminal::new(ratatui::backend::TestBackend::new(100, 14)).unwrap();
    for selected in 0..3 {
        terminal
            .draw(|frame| app.hits = draw_content(frame, &app))
            .unwrap();
        assert_eq!(app.temporary.selected, selected);
        assert!(
            app.hits
                .iter()
                .any(|h| h.control == Control::TemporaryChoice(selected))
        );
        app.temporary_key(KeyEvent::new(KeyCode::Down, KeyModifiers::NONE));
    }
    terminal
        .draw(|frame| app.hits = draw_content(frame, &app))
        .unwrap();
    assert!(app.temporary_controls()[app.temporary.selected] == Control::TemporaryBack);
    app.temporary_key(KeyEvent::new(KeyCode::Down, KeyModifiers::NONE));
    assert_eq!(app.temporary.selected, 3);
    assert!(matches!(app.screen, Screen::Temporary));
}

#[test]
fn compact_profile_list_reveals_hidden_profiles_before_actions() {
    let mut app = test_profile_app();
    for _ in 0..10 {
        app.connections
            .installations
            .push(saved_installation("other.example"));
    }
    app.screen = Screen::SettingsPage(SettingsPage::Profiles);
    app.settings_selected = 1;
    let mut terminal = Terminal::new(ratatui::backend::TestBackend::new(100, 22)).unwrap();
    for index in 0..app.connections.installations.len() {
        terminal
            .draw(|frame| app.hits = draw_content(frame, &app))
            .unwrap();
        assert_eq!(app.settings_selected, index + 1);
        assert!(
            app.hits
                .iter()
                .any(|h| h.control == Control::Installation(index))
        );
        assert!(app.move_spatial(KeyCode::Down));
    }
    app.settings_selected = app.profile_controls().len();
    terminal
        .draw(|frame| app.hits = draw_content(frame, &app))
        .unwrap();
    assert!(app.move_spatial(KeyCode::Down));
    assert_eq!(app.settings_selected, app.profile_controls().len());
    assert!(matches!(
        app.screen,
        Screen::SettingsPage(SettingsPage::Profiles)
    ));
}

#[test]
fn mouse_wheel_pans_without_changing_keyboard_selection() {
    let mut app = test_profile_app();
    app.screen = Screen::SettingsPage(SettingsPage::Appearance);
    app.settings_selected = 1;
    let mut terminal = Terminal::new(ratatui::backend::TestBackend::new(100, 18)).unwrap();
    terminal
        .draw(|frame| app.hits = draw_content(frame, &app))
        .unwrap();
    assert!(app.hits.iter().any(|h| h.control == Control::Accent(0)));
    app.mouse(MouseEvent {
        kind: MouseEventKind::ScrollDown,
        column: 60,
        row: 8,
        modifiers: KeyModifiers::NONE,
    });
    terminal
        .draw(|frame| app.hits = draw_content(frame, &app))
        .unwrap();
    assert_eq!(app.settings_selected, 1);
    assert!(!app.hits.iter().any(|h| h.control == Control::Accent(0)));
    assert!(app.hits.iter().any(|h| h.control == Control::Accent(1)));
    assert!(app.pointer_active);
    assert!(app.nodes_requested.is_none());
}
#[test]
fn idle_exposes_sidebar_and_quick_start_without_opening_sections_on_arrows() {
    let mut app = test_profile_app();
    app.screen = Screen::Idle;
    let mut terminal = Terminal::new(ratatui::backend::TestBackend::new(100, 40)).unwrap();
    terminal
        .draw(|frame| app.hits = draw_content(frame, &app))
        .unwrap();
    assert!(app.hits.iter().all(|hit| hit.area.x < 26
        || matches!(
            hit.control,
            Control::WindowClose | Control::QuickStartAction | Control::QuickStartRefresh
        )));
    app.idle_key(KeyEvent::new(KeyCode::Down, KeyModifiers::NONE));
    assert!(matches!(app.screen, Screen::Idle));
    assert!(app.nodes_requested.is_none());
    app.idle_selection = app
        .idle_controls()
        .iter()
        .position(|control| *control == Control::Settings)
        .unwrap();
    app.idle_key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
    assert!(matches!(app.screen, Screen::Settings));
    assert!(app.section_navigation(KeyCode::Esc));
    assert!(matches!(app.screen, Screen::Idle));
}
#[test]
fn esc_closes_root_sections_but_keeps_nested_back_navigation() {
    let mut app = test_profile_app();
    app.form.action = Action::Install;
    app.screen = Screen::Controller;
    app.controller.updates = Some(serde_json::json!({}));
    assert!(!app.section_navigation(KeyCode::Esc));
    app.activate(Control::ControllerBack);
    assert!(matches!(app.screen, Screen::Controller));
    assert!(app.section_navigation(KeyCode::Esc));
    assert!(matches!(app.screen, Screen::Idle));
    app.screen = Screen::Access;
    assert!(app.section_navigation(KeyCode::Esc));
    assert!(matches!(app.screen, Screen::Idle));
}
#[test]
fn profile_editor_expiry_is_read_only_and_buttons_are_compact() {
    let mut app = test_profile_app();
    app.screen = Screen::Access;
    app.access.editor = Some(crate::access::Editor::new(
        Some(serde_json::json!({"display_name":"Member", "expires_at":"2026-11-01T00:00:00Z"})),
        None,
    ));
    let mut terminal = Terminal::new(ratatui::backend::TestBackend::new(120, 40)).unwrap();
    terminal
        .draw(|frame| app.hits = draw_content(frame, &app))
        .unwrap();
    assert!(
        !app.hits
            .iter()
            .any(|h| h.control == Control::AccessField(1))
    );
    let field = app
        .hits
        .iter()
        .find(|h| h.control == Control::AccessField(0))
        .unwrap();
    assert!(field.area.y < 7);
    for control in [
        Control::AccessExpiryMenu,
        Control::AccessGrants,
        Control::AccessSave,
        Control::AccessBack,
    ] {
        assert_eq!(
            app.hits
                .iter()
                .find(|h| h.control == control)
                .unwrap()
                .area
                .height,
            3
        );
    }
    let text: String = terminal
        .backend()
        .buffer()
        .content
        .iter()
        .map(|cell| cell.symbol())
        .collect();
    assert!(text.contains("Applied rules: None"));
    assert!(text.contains("Access: 0 servers"));
}
#[test]
fn profile_role_is_not_duplicated_and_actions_clear_old_notice() {
    let mut app = test_profile_app();
    app.screen = Screen::Access;
    app.access.card = Some(
        serde_json::json!({"display_name":"Member", "_account":{"id":"owner","role":"admin","status":"approved","telegram_user_id":101}}),
    );
    app.access.notice = "Profile saved.".into();
    let mut terminal = Terminal::new(ratatui::backend::TestBackend::new(120, 40)).unwrap();
    terminal
        .draw(|frame| app.hits = draw_content(frame, &app))
        .unwrap();
    let text: String = terminal
        .backend()
        .buffer()
        .content
        .iter()
        .map(|cell| cell.symbol())
        .collect();
    assert_eq!(text.matches(" · admin").count(), 1);
    app.activate(Control::AccessAccountAction(false));
    assert!(app.access.notice.is_empty());
    terminal
        .draw(|frame| app.hits = draw_content(frame, &app))
        .unwrap();
    let text: String = terminal
        .backend()
        .buffer()
        .content
        .iter()
        .map(|cell| cell.symbol())
        .collect();
    assert!(text.contains("VPN access is retained"));
}
#[test]
fn enlarging_window_reveals_entire_installed_menu() {
    let mut app = test_profile_app();
    app.screen = Screen::Controller;
    app.controller.selected = 5;
    let mut small = Terminal::new(ratatui::backend::TestBackend::new(100, 22)).unwrap();
    small
        .draw(|frame| app.hits = draw_content(frame, &app))
        .unwrap();
    assert!(app.controller_viewport.get() > 0);
    let mut large = Terminal::new(ratatui::backend::TestBackend::new(100, 80)).unwrap();
    large
        .draw(|frame| app.hits = draw_content(frame, &app))
        .unwrap();
    assert_eq!(app.controller_viewport.get(), 0);
    assert!(
        app.hits
            .iter()
            .any(|h| h.control == Control::ControllerSection(ControllerSection::Removal))
    );
}
#[test]
fn unified_profile_card_has_account_and_vpn_actions_without_tabs() {
    let mut app = test_profile_app();
    app.screen = Screen::Access;
    app.access.card = Some(
        serde_json::json!({"id":"profile", "display_name":"Member", "owner_account_id":"account", "frozen":false, "_account":{"id":"account", "revision":4, "role":"member", "status":"approved"}}),
    );
    let mut terminal = Terminal::new(ratatui::backend::TestBackend::new(120, 40)).unwrap();
    terminal
        .draw(|frame| app.hits = draw_content(frame, &app))
        .unwrap();
    assert!(
        !app.hits
            .iter()
            .any(|hit| matches!(hit.control, Control::AccessTab(_) | Control::AccessNew))
    );
    assert!(
        !terminal
            .backend()
            .buffer()
            .content
            .iter()
            .map(|cell| cell.symbol())
            .collect::<String>()
            .contains("Devices")
    );
    app.activate(Control::AccessAccountAction(true));
    app.activate(Control::AccessAccountConfirm);
    assert!(
        matches!(app.nodes_requested.take(), Some(crate::nodes::Command::Access(crate::access::Command::AccountMutate { id, revision:4, .. })) if id == "account")
    );
}
#[test]
fn grouped_counter_counts_regions_and_servers_separately() {
    let mut app = test_profile_app();
    app.nodes.items = vec![
        browser_node("a", "Europe"),
        browser_node("b", "Europe"),
        browser_node("c", "Asia"),
    ];
    assert_eq!(app.list_position(Control::NodesCard(1), &[]), Some((2, 2)));
    assert_eq!(
        app.list_position(Control::NodesRegion(0), &[]),
        Some((2, 2))
    );
    assert_eq!(app.list_position(Control::NodesSearch, &[]), None);
    assert_eq!(viewport_start(8, 10, 5), 8);
    assert_eq!(viewport_start(8, 7, 5), 7);
    assert_eq!(viewport_start(8, 13, 5), 9);
}
#[test]
fn appearance_back_can_receive_hover_focus() {
    let mut app = test_profile_app();
    app.screen = Screen::SettingsPage(SettingsPage::Appearance);
    app.focus(Control::Settings);
    assert_eq!(app.settings_selected, app.themes.len() + 3);
}
#[test]
fn removal_section_shows_cleanup_without_waiting_for_overview() {
    let mut app = test_profile_app();
    app.screen = Screen::Controller;
    let mut terminal = Terminal::new(ratatui::backend::TestBackend::new(100, 60)).unwrap();
    terminal
        .draw(|frame| app.hits = draw_content(frame, &app))
        .unwrap();
    assert!(
        app.hits
            .iter()
            .any(|hit| hit.control == Control::ControllerSection(ControllerSection::Removal))
    );
    assert!(
        !app.hits
            .iter()
            .any(|hit| hit.control == Control::ControllerPlan(true))
    );
    app.activate(Control::ControllerSection(ControllerSection::Removal));
    terminal
        .draw(|frame| app.hits = draw_content(frame, &app))
        .unwrap();
    for control in [
        Control::ControllerPlan(false),
        Control::ControllerPlan(true),
    ] {
        assert!(app.hits.iter().any(|hit| hit.control == control));
    }
    assert!(
        app.hits
            .iter()
            .any(|hit| hit.control == Control::ControllerBack)
    );
    app.activate(Control::ControllerPlan(true));
    assert!(app.controller.plan_pending);
    terminal
        .draw(|frame| app.hits = draw_content(frame, &app))
        .unwrap();
    assert!(
        !app.hits
            .iter()
            .any(|hit| hit.control == Control::ControllerConfirm)
    );
}

#[test]
fn sidebar_hover_and_overflow_are_visible() {
    let mut app = test_profile_app();
    app.screen = Screen::SettingsPage(SettingsPage::Appearance);
    app.settings_selected = 3;
    let mut terminal = Terminal::new(ratatui::backend::TestBackend::new(100, 22)).unwrap();
    terminal
        .draw(|frame| app.hits = draw_content(frame, &app))
        .unwrap();
    let hit = app
        .hits
        .iter()
        .find(|hit| hit.control == Control::Action(Action::Diagnose))
        .unwrap()
        .area;
    app.mouse(MouseEvent {
        kind: MouseEventKind::Moved,
        column: hit.x + 1,
        row: hit.y + 1,
        modifiers: KeyModifiers::NONE,
    });
    terminal
        .draw(|frame| app.hits = draw_content(frame, &app))
        .unwrap();
    assert_eq!(
        terminal.backend().buffer()[(hit.x, hit.y)].fg,
        accent_color(&app.connections.accent)
    );
    let text: String = terminal
        .backend()
        .buffer()
        .content
        .iter()
        .map(|cell| cell.symbol())
        .collect();
    assert!(text.contains("wheel: scroll"));
    assert!(text.contains(&format!("3/{}", app.themes.len())));
}
#[test]
fn destructive_button_border_only_turns_red_when_focused() {
    let mut terminal = Terminal::new(ratatui::backend::TestBackend::new(20, 3)).unwrap();
    for (selected, color) in [(false, Color::White), (true, Color::Red)] {
        terminal
            .draw(|frame| draw_button_color(frame, frame.area(), "Delete", selected, Color::Red))
            .unwrap();
        assert_eq!(terminal.backend().buffer()[(0, 0)].fg, color);
        assert_eq!(terminal.backend().buffer()[(7, 1)].fg, Color::Red);
    }
}

#[test]
fn same_screen_toggle_restores_focus_by_control() {
    let mut app = test_profile_app();
    app.screen = Screen::Nodes;
    app.nodes_sidebar = false;
    app.nodes.editor = Some(node_settings_editor());
    app.nodes.editor.as_mut().unwrap().tab = crate::node_editor::Tab::Protocols;
    let mut terminal = Terminal::new(ratatui::backend::TestBackend::new(100, 30)).unwrap();
    terminal
        .draw(|frame| app.hits = draw_content(frame, &app))
        .unwrap();
    let control = app
        .hits
        .iter()
        .map(|h| h.control)
        .find(|c| matches!(c, Control::NodesEditToggle(_, _)))
        .unwrap();
    app.activate(control);
    terminal
        .draw(|frame| app.hits = draw_content(frame, &app))
        .unwrap();
    app.restore_action_focus();
    let controls = app.nodes_controls();
    assert!(controls[app.nodes_selected] == control);
}
#[test]
fn update_refresh_controls_are_only_shown_while_an_operation_runs() {
    let mut app = test_profile_app();
    app.screen = Screen::Controller;

    app.controller.update_result = Some(serde_json::json!({"status":"succeeded","items":[]}));
    assert!(
        !app.controller_controls()
            .contains(&Control::ControllerUpdateResult)
    );
    app.controller.update_result = Some(serde_json::json!({"status":"running","items":[]}));
    assert!(
        app.controller_controls()
            .contains(&Control::ControllerUpdateResult)
    );

    app.controller.update_result = None;
    app.controller.components = Some(serde_json::json!({
        "nodes":[], "latest_job":{"status":"succeeded"}
    }));
    assert!(
        !app.controller_controls()
            .contains(&Control::ControllerComponents)
    );
    app.controller.components = Some(serde_json::json!({
        "nodes":[], "latest_job":{"status":"awaiting_executor"}
    }));
    assert!(
        !app.controller_controls()
            .contains(&Control::ControllerComponents)
    );
}

#[test]
fn update_intervals_share_one_row_and_only_the_selected_text_keeps_the_accent() {
    let mut app = test_profile_app();
    app.screen = Screen::Controller;
    app.form.select_action(Action::Update);
    app.controller.updates =
        Some(serde_json::json!({"auto_check_enabled":true,"auto_check_interval_minutes":360}));
    let mut terminal = Terminal::new(ratatui::backend::TestBackend::new(120, 40)).unwrap();
    terminal.draw(|frame| app.hits = draw(frame, &app)).unwrap();
    let choices: Vec<_> = app
        .hits
        .iter()
        .filter(|hit| matches!(hit.control, Control::ControllerUpdateInterval(_)))
        .collect();
    assert_eq!(choices.len(), 3);
    assert!(choices.iter().all(|hit| hit.area.y == choices[0].area.y));
    let chosen = choices
        .iter()
        .find(|hit| hit.control == Control::ControllerUpdateInterval(360))
        .unwrap();
    assert_eq!(
        terminal.backend().buffer()[(chosen.area.x, chosen.area.y)].fg,
        Color::White
    );
    assert!((chosen.area.x + 1..chosen.area.right() - 1).any(|x| {
        let cell = &terminal.backend().buffer()[(x, chosen.area.y + 1)];
        cell.symbol() == "6" && cell.fg == Color::Cyan
    }));
    app.activate(Control::ControllerUpdateInterval(1440));
    assert!(
        matches!(&app.nodes_requested, Some(crate::nodes::Command::Controller(_, crate::controller::Command::UpdatePreferences(v))) if v["auto_check_interval_minutes"] == 1440)
    );
    app.nodes_requested = None;
    app.controller.updates.as_mut().unwrap()["auto_check_enabled"] = false.into();
    assert!(
        !app.controller_controls()
            .iter()
            .any(|control| matches!(control, Control::ControllerUpdateInterval(_)))
    );
    app.activate(Control::ControllerUpdateInterval(60));
    assert!(app.nodes_requested.is_none());
}

#[test]
fn diagnostic_recovery_preference_survives_reopening_workstation() {
    let state = tempfile::tempdir().unwrap();
    let mut app = test_profile_app();
    app.state_dir = state.path().into();
    app.form.fields[11] = "false".into();
    app.toggle_diagnostic_recovery();
    assert!(Connections::load(state.path()).unwrap().diagnostic_recovery);
    let mut request = test_request();
    request.state_dir = state.path().into();
    let mut reopened = App::new(&request);
    reopened.load_connections(&request).unwrap();
    assert_eq!(reopened.form.fields[11], "true");
    reopened.toggle_diagnostic_recovery();
    assert!(!Connections::load(state.path()).unwrap().diagnostic_recovery);
}

#[test]
fn workflow_confirmation_explains_the_selected_action_without_installation_boilerplate() {
    let mut app = test_profile_app();
    let mut titles = std::collections::HashSet::new();
    app.form.fields[6] = "SECRET_TOKEN".into();
    for action in Action::ALL {
        app.form.select_action(action);
        let (title, description) = app.workflow_confirmation();
        titles.insert(title);
        assert!(!description.contains("First connection"));
        assert!(!description.contains("SECRET_TOKEN"));
        assert!(description.contains("controller.example"));
    }
    assert_eq!(titles.len(), 4);
}

#[test]
fn update_branch_change_requires_confirmation_and_can_be_cancelled() {
    let mut app = test_profile_app();
    app.screen = Screen::Controller;
    app.controller.updates = Some(serde_json::json!({"branch":"main"}));

    app.activate(Control::ControllerUpdateBranch);
    assert!(app.controller.branch_confirmation);
    let confirmation_controls = app.controller_controls();
    assert_eq!(confirmation_controls.len(), 2);
    assert!(confirmation_controls.contains(&Control::ControllerUpdateBranchConfirm(true)));
    assert!(confirmation_controls.contains(&Control::ControllerUpdateBranchConfirm(false)));
    app.activate(Control::ControllerUpdateBranchConfirm(false));
    assert!(!app.controller.branch_confirmation);
    assert!(app.nodes_requested.is_none());

    app.activate(Control::ControllerUpdateBranch);
    app.activate(Control::ControllerUpdateBranchConfirm(true));
    assert!(!app.controller.branch_confirmation);
    assert!(matches!(
        app.nodes_requested.take(),
        Some(crate::nodes::Command::Controller(_, crate::controller::Command::UpdatePreferences(value)))
            if value["branch"] == "dev"
    ));
}

#[test]
fn server_removal_requires_live_preview_and_explicit_confirmation() {
    use crate::node_maintenance::{Browser, Mode};
    let mut app = test_profile_app();
    app.screen = Screen::Nodes;
    app.nodes_sidebar = false;
    app.nodes.card = Some(browser_node("lv1", "Europe"));
    let state = serde_json::json!({"node_key":"lv1","status":"active","affected_profiles":4});
    app.event(Event::Nodes(crate::nodes::Update::Maintenance(
        Browser::new("lv1".into(), state.clone(), None).unwrap(),
        false,
    )));
    app.nodes_requested = None;
    app.activate(Control::NodesRemove(Mode::Full));
    assert!(
        matches!(app.nodes_requested.take(),Some(crate::nodes::Command::Maintenance(key,Some(Mode::Full))) if key == "lv1")
    );
    app.event(Event::Nodes(crate::nodes::Update::Maintenance(
        Browser::new("lv1".into(), state.clone(), Some(Mode::Full)).unwrap(),
        false,
    )));
    assert_eq!(app.nodes_selected, 1); // Back is initially focused.
    let mut terminal = Terminal::new(ratatui::backend::TestBackend::new(100, 30)).unwrap();
    terminal
        .draw(|frame| app.hits = draw_content(frame, &app))
        .unwrap();
    assert!(
        app.hits
            .iter()
            .any(|hit| hit.control == Control::NodesRemoveConfirm)
    );
    app.nodes_key(KeyCode::Esc.into());
    assert!(
        app.nodes
            .maintenance
            .as_ref()
            .unwrap()
            .confirmation
            .is_none()
    );
    assert!(app.nodes_requested.is_none());
    app.event(Event::Nodes(crate::nodes::Update::Maintenance(
        Browser::new("lv1".into(), state, Some(Mode::RegistryOnly)).unwrap(),
        false,
    )));
    app.activate(Control::NodesRemoveConfirm);
    assert!(
        matches!(app.nodes_requested.take(),Some(crate::nodes::Command::Remove(key,Mode::RegistryOnly,false)) if key=="lv1")
    );
}
#[test]
fn removal_progress_preserves_focus_and_finishes_with_only_nodes_button() {
    use crate::node_maintenance::Browser;
    let mut app = test_profile_app();
    app.screen = Screen::Nodes;
    app.nodes_sidebar = false;
    app.nodes.card = Some(browser_node("lv1", "Europe"));
    app.nodes.items = vec![browser_node("lv1", "Europe"), browser_node("lv2", "Europe")];
    let running = serde_json::json!({"node_key":"lv1","status":"draining","removal_status":"running","cleanup_phase":"prepared"});
    app.event(Event::Nodes(crate::nodes::Update::Maintenance(
        Browser::new("lv1".into(), running.clone(), None).unwrap(),
        false,
    )));
    app.nodes_selected = 2;
    app.nodes.maintenance.as_mut().unwrap().scroll = 3;
    app.event(Event::Nodes(crate::nodes::Update::Maintenance(
        Browser::new("lv1".into(), running.clone(), None).unwrap(),
        true,
    )));
    assert_eq!(app.nodes_selected, 2);
    assert_eq!(app.nodes.maintenance.as_ref().unwrap().scroll, 3);
    app.event(Event::Nodes(crate::nodes::Update::Maintenance(
        Browser::new(
            "lv1".into(),
            serde_json::json!({"node_key":"lv1","status":"removed"}),
            None,
        )
        .unwrap(),
        true,
    )));
    assert_eq!(app.nodes.items.len(), 1);
    let mut terminal = Terminal::new(ratatui::backend::TestBackend::new(100, 30)).unwrap();
    terminal
        .draw(|frame| app.hits = draw_content(frame, &app))
        .unwrap();
    assert!(app.nodes_controls() == vec![Control::NodesBack]);
    app.nodes_key(KeyCode::Esc.into());
    assert!(app.nodes.card.is_none());
    assert!(matches!(
        app.nodes_requested.take(),
        Some(crate::nodes::Command::List)
    ));
    // A delayed read cannot reopen the maintenance screen after leaving it.
    app.event(Event::Nodes(crate::nodes::Update::Maintenance(
        Browser::new("lv1".into(), running, None).unwrap(),
        true,
    )));
    assert!(app.nodes.maintenance.is_none());
}
#[test]
fn maintenance_diagnostics_back_and_retry_stay_bound_to_the_server() {
    use crate::node_maintenance::{Browser, Mode};
    let mut app = test_profile_app();
    app.screen = Screen::Nodes;
    app.nodes.card = Some(browser_node("lv1", "Europe"));
    let blocked = serde_json::json!({"node_key":"lv1","status":"draining","removal_status":"blocked","error_code":"host_verification_failed"});
    app.event(Event::Nodes(crate::nodes::Update::Maintenance(
        Browser::new("lv1".into(), blocked.clone(), None).unwrap(),
        false,
    )));
    app.event(Event::Nodes(crate::nodes::Update::Diagnostics(
        "lv1".into(),
        serde_json::json!({}),
        serde_json::json!({"items":[]}),
    )));
    app.nodes_requested = None;
    app.nodes_key(KeyCode::Esc.into());
    assert!(app.nodes.diagnostics.is_none());
    assert!(app.nodes.maintenance.is_some());
    app.event(Event::Nodes(crate::nodes::Update::Maintenance(
        Browser::new("lv1".into(), blocked, Some(Mode::Full)).unwrap(),
        false,
    )));
    app.activate(Control::NodesRemoveConfirm);
    assert!(
        matches!(app.nodes_requested.take(),Some(crate::nodes::Command::Remove(key,Mode::Full,true)) if key=="lv1")
    );
}
#[test]
fn node_recovery_requires_confirmation_and_back_preserves_server() {
    let mut app = test_profile_app();
    app.screen = Screen::Nodes;
    app.nodes.card = Some(browser_node("lv1", "Europe"));
    let identity = uuid::Uuid::new_v4().to_string();
    app.event(Event::Nodes(crate::nodes::Update::Diagnostics("lv1".into(),
            serde_json::json!({"error_code":"node_agent_unavailable"}),
            serde_json::json!({"items":[{"id":identity,"node_key":"lv1","kind":"bootstrap","status":"blocked","actions":["recheck"]}],"total":1,"offset":0,"page_size":10}))));
    app.nodes_requested = None;
    app.activate(Control::NodesDiagnosticItem(0));
    app.activate(Control::NodesRecovery(0, true));
    assert!(app.nodes.recovery_confirm.is_none());
    app.activate(Control::NodesRecovery(0, false));
    assert!(app.nodes.recovery_confirm.is_some());
    assert!(app.nodes_requested.is_none());
    app.nodes_key(KeyCode::Esc.into());
    assert!(app.nodes.recovery_confirm.is_none());
    assert!(app.nodes.diagnostic_item.is_some());
    app.activate(Control::NodesRecovery(0, false));
    app.activate(Control::NodesRecoveryConfirm);
    assert!(
        matches!(app.nodes_requested.take(), Some(crate::nodes::Command::Recover(key, item, action)) if key == "lv1" && item["id"] == identity && action == "recheck")
    );
    app.nodes_key(KeyCode::Esc.into());
    assert!(app.nodes.diagnostic_item.is_none());
    app.nodes_key(KeyCode::Esc.into());
    assert!(app.nodes.diagnostics.is_none());
    assert!(app.nodes.card.is_some());
}
#[test]
fn diagnostics_ignore_other_servers_and_paginate_short_terminals() {
    let mut app = test_profile_app();
    app.screen = Screen::Nodes;
    app.nodes_sidebar = false;
    app.nodes.card = Some(browser_node("lv1", "Europe"));
    app.event(Event::Nodes(crate::nodes::Update::Diagnostics(
        "other".into(),
        serde_json::json!({}),
        serde_json::json!({}),
    )));
    assert!(app.nodes.diagnostics.is_none());
    let items: Vec<_> = (0..10).map(|_| serde_json::json!({"id":uuid::Uuid::new_v4(),"node_key":"lv1","kind":"node","status":"blocked","actions":[]})).collect();
    app.event(Event::Nodes(crate::nodes::Update::Diagnostics(
        "lv1".into(),
        serde_json::json!({"docker":"ok"}),
        serde_json::json!({"items":items,"total":10,"offset":0,"page_size":10}),
    )));
    app.nodes_requested = None;
    let mut terminal = Terminal::new(ratatui::backend::TestBackend::new(90, 22)).unwrap();
    terminal
        .draw(|frame| app.hits = draw_content(frame, &app))
        .unwrap();
    let capacity = app.nodes_capacity.get();
    assert!(capacity < 10);
    assert!(
        app.hits
            .iter()
            .any(|hit| hit.control == Control::NodesDiagnosticPage(false))
    );
    app.activate(Control::NodesDiagnosticPage(false));
    assert_eq!(app.nodes.diagnostic_page, 1);
    assert!(app.nodes_requested.is_none());
    terminal
        .draw(|frame| app.hits = draw_content(frame, &app))
        .unwrap();
    assert!(
        app.hits
            .iter()
            .any(|hit| hit.control == Control::NodesDiagnosticItem(capacity))
    );
}
#[test]
fn controller_back_returns_from_nested_updates_then_closes_section() {
    let mut app = test_profile_app();
    app.form.action = Action::Update;
    app.screen = Screen::Controller;
    app.controller.updates = Some(serde_json::json!({}));
    app.controller.versions = Some(serde_json::json!({"items":[]}));
    app.activate(Control::ControllerBack);
    assert!(app.controller.versions.is_none());
    assert!(app.controller.updates.is_some());
    app.activate(Control::ControllerBack);
    assert!(app.controller.updates.is_none());
    assert!(matches!(app.screen, Screen::Idle));
}

#[test]
fn updates_root_has_no_back_button_but_nested_screens_do() {
    let mut app = test_profile_app();
    app.form.action = Action::Update;
    app.screen = Screen::Controller;
    let mut terminal = Terminal::new(ratatui::backend::TestBackend::new(120, 40)).unwrap();
    for overview in [None, Some(serde_json::json!({"update_available": true}))] {
        app.controller.updates = overview;
        assert!(!app.controller_controls().contains(&Control::ControllerBack));
        terminal
            .draw(|frame| app.hits = draw_content(frame, &app))
            .unwrap();
        assert!(
            !app.hits
                .iter()
                .any(|hit| hit.control == Control::ControllerBack)
        );
    }
    app.controller.versions = Some(serde_json::json!({"items": []}));
    assert!(app.controller_controls().contains(&Control::ControllerBack));
    terminal
        .draw(|frame| app.hits = draw_content(frame, &app))
        .unwrap();
    assert!(
        app.hits
            .iter()
            .any(|hit| hit.control == Control::ControllerBack)
    );
}

#[test]
fn latest_update_uses_known_upstream_and_keeps_confirmation_flow() {
    let mut app = test_profile_app();
    app.form.action = Action::Update;
    app.screen = Screen::Controller;
    app.controller.updates = Some(serde_json::json!({
        "update_available": false, "upstream_ref": "v0.4.3-alpha.75"
    }));
    assert!(
        !app.controller_controls()
            .contains(&Control::ControllerUpdateLatest)
    );
    app.activate(Control::ControllerUpdateLatest);
    assert!(matches!(app.screen, Screen::Controller));
    app.controller.updates.as_mut().unwrap()["update_available"] = true.into();
    assert!(
        app.controller_controls()
            .contains(&Control::ControllerUpdateLatest)
    );
    app.activate(Control::ControllerUpdateLatest);
    assert!(matches!(app.screen, Screen::Form));
    assert!(app.form.action == Action::Update);
    assert_eq!(app.form.fields[4], "v0.4.3-alpha.75");
    assert!(!app.nodes_busy);
}

#[test]
fn update_component_card_back_restores_components() {
    let mut app = test_profile_app();
    app.screen = Screen::Controller;
    app.controller.components = Some(serde_json::json!({"nodes":[{"key":"lv1","title":"Latvia"}]}));
    app.activate(Control::ControllerComponentNode(0));
    assert!(app.nodes_updates_origin);
    assert!(
        matches!(app.nodes_requested.take(), Some(crate::nodes::Command::Card(key)) if key == "lv1")
    );
    app.nodes.card = Some(browser_node("lv1", "Europe"));
    app.activate(Control::NodesBack);
    assert!(matches!(app.screen, Screen::Controller));
    assert!(app.controller.components.is_some());
    assert!(!app.nodes_updates_origin);
}

#[test]
fn installation_defaults_require_save_and_warn_before_leaving() {
    let mut app = test_profile_app();
    app.screen = Screen::Controller;
    let value = serde_json::json!({"revision":2,"protocols":["awg"],"xray_transports":[],"settings":{"awg_i1_preset":"quic"}});
    app.controller
        .apply(crate::controller::Command::Defaults, value.clone());
    assert!(
        !app.controller_controls()
            .contains(&Control::ControllerDefaultsSave)
    );
    app.activate(Control::ControllerDefaultsPreset);
    assert_eq!(
        app.controller.defaults.as_ref().unwrap()["settings"]["awg_i1_preset"],
        "dns"
    );
    assert_eq!(app.controller.defaults_baseline, Some(value));
    assert!(
        app.controller_controls()
            .contains(&Control::ControllerDefaultsSave)
    );
    app.activate(Control::ControllerBack);
    assert!(app.discard.is_some());
    app.activate(Control::Discard(false));
    assert!(app.controller.defaults.is_some());
    app.activate(Control::ControllerDefaultsReset);
    assert!(!app.unsaved_changes());
}

#[test]
fn defaults_manual_port_validation_and_input_cancel_preserve_draft() {
    let mut app = test_profile_app();
    app.screen = Screen::Controller;
    app.controller.apply(
        crate::controller::Command::Defaults,
        serde_json::json!({"revision":1,"protocols":["awg"],"settings":{"awg_port_mode":"auto"}}),
    );
    app.activate(Control::ControllerDefaultsMode);
    app.activate(Control::ControllerDefaultsField("awg_port"));
    app.controller.defaults_input.as_mut().unwrap().1 = "0".into();
    app.activate(Control::ControllerDefaultsInputApply);
    assert!(app.controller.defaults_input.is_some());
    assert_eq!(
        app.controller.defaults.as_ref().unwrap()["settings"]["awg_port"],
        443
    );
    app.activate(Control::ControllerBack);
    assert!(app.controller.defaults_input.is_none());
    assert!(app.discard.is_none());
    app.activate(Control::ControllerDefaultsMode);
    assert!(
        app.controller.defaults.as_ref().unwrap()["settings"]
            .get("awg_port")
            .is_none()
    );
}

#[test]
fn request_message_edit_does_not_modify_installation_defaults() {
    let mut app = test_profile_app();
    app.screen = Screen::Controller;
    app.controller.policies = Some(serde_json::json!({"requests":{"gate_message":"Original"}}));
    app.activate(Control::ControllerGateMessage);
    assert!(app.controller_controls()[0] == Control::ControllerDefaultsInput);
    app.controller.defaults_input.as_mut().unwrap().1 = "New text".into();
    app.activate(Control::ControllerDefaultsInputApply);
    assert!(app.controller.text_confirmation);
    assert!(app.nodes_requested.is_none());
    app.activate(Control::ControllerBack);
    assert!(!app.controller.text_confirmation);
    assert_eq!(
        app.controller.defaults_input.as_ref().unwrap().1,
        "New text"
    );
    app.activate(Control::ControllerDefaultsInputApply);
    app.activate(Control::ControllerTextConfirm);
    assert!(
        matches!(app.nodes_requested.take(),Some(crate::nodes::Command::Controller(_,crate::controller::Command::PolicySave("access-requests",body))) if body["gate_message"] == "New text")
    );
    assert!(app.controller.defaults.is_none());
    assert!(app.controller.defaults_input.is_some());
    app.controller.apply(
        crate::controller::Command::PolicySave("access-requests", serde_json::json!({})),
        serde_json::json!({"requests":{"gate_message":"New text"}}),
    );
    assert!(app.controller.defaults_input.is_none());
}

fn node_settings_editor() -> crate::node_editor::Editor {
    crate::node_editor::Editor::new(serde_json::json!({"key":"lv1","title":"Server", "region":"Europe", "flag":"LV", "desired_revision":4,"applied_revision":4,"protocols":["awg"],"xray_transports":[],"settings":{"public_host":"vpn.test","awg_port":443,"awg_i1_preset":"quic","awg_port_mode":"auto"},"transport":"local"}), &serde_json::json!({"settings":{}})).unwrap()
}
#[test]
fn unsaved_server_changes_can_be_kept_or_discarded_without_mutation() {
    let mut app = test_profile_app();
    app.screen = Screen::Nodes;
    app.nodes.card = Some(browser_node("lv1", "Europe"));
    app.nodes.editor = Some(node_settings_editor());
    app.nodes_requested = None;
    app.nodes.editor.as_mut().unwrap().fields[0].value = "New name".into();
    app.nodes_key(KeyCode::Esc.into());
    assert!(app.discard == Some(Control::NodesEditBack));
    assert!(app.nodes_requested.is_none());
    app.activate(Control::Discard(false));
    assert_eq!(
        app.nodes.editor.as_ref().unwrap().fields[0].value,
        "New name"
    );
    app.activate(Control::NodesEditBack);
    app.activate(Control::Discard(true));
    assert!(app.nodes.editor.is_none());
    assert!(app.nodes.card.is_some());
    assert!(app.nodes_requested.is_none());
}
#[test]
fn dirty_profile_exit_and_sidebar_navigation_require_confirmation() {
    let mut app = test_profile_app();
    app.screen = Screen::Access;
    app.access.editor = Some(crate::access::Editor::new(
        Some(serde_json::json!({"id":"p1","display_name":"Old"})),
        None,
    ));
    app.access.editor.as_mut().unwrap().fields[0] = "New".into();
    app.access_key(KeyCode::Esc.into());
    assert!(app.discard == Some(Control::AccessBack));
    app.activate(Control::Discard(false));
    app.activate(Control::Settings);
    assert!(app.discard == Some(Control::Settings));
    assert!(matches!(app.screen, Screen::Access));
    app.activate(Control::Discard(true));
    assert!(app.access.editor.is_none());
    assert!(matches!(app.screen, Screen::Settings));
}
#[test]
fn unmodified_editors_leave_without_warning_and_review_back_preserves_draft() {
    let mut app = test_profile_app();
    app.screen = Screen::Nodes;
    app.nodes.editor = Some(node_settings_editor());
    app.activate(Control::NodesEditBack);
    assert!(app.discard.is_none());
    assert!(app.nodes.editor.is_none());
    let mut editor = node_settings_editor();
    editor.fields[0].value = "New".into();
    editor.changed();
    let draft = editor.draft().unwrap();
    editor.review = Some((draft, serde_json::json!({"affected_profiles":0})));
    app.nodes.editor = Some(editor);
    app.activate(Control::NodesEditBack);
    assert!(app.discard.is_none());
    assert!(app.nodes.editor.as_ref().unwrap().dirty());
}
#[test]
fn node_settings_scroll_small_terminals_and_save_requires_review() {
    let mut app = test_profile_app();
    app.screen = Screen::Nodes;
    app.nodes.editor = Some(node_settings_editor());
    app.nodes_requested = None;
    app.nodes.editor.as_mut().unwrap().fields[1].value = "Asia".into();
    app.nodes.editor.as_mut().unwrap().changed();
    let mut terminal = Terminal::new(ratatui::backend::TestBackend::new(100, 24)).unwrap();
    terminal
        .draw(|frame| app.hits = draw_content(frame, &app))
        .unwrap();
    assert!(app.hits.iter().any(|h| h.control == Control::NodesEditSave));
    app.activate(Control::NodesEditSave);
    assert!(matches!(
        app.nodes_requested.take(),
        Some(crate::nodes::Command::EditReview(_))
    ));
    let draft = app.nodes.editor.as_ref().unwrap().draft().unwrap();
    app.event(Event::Nodes(crate::nodes::Update::EditReview(
        draft,
        serde_json::json!({"affected_profiles":3}),
    )));
    app.activate(Control::NodesEditConfirm);
    let Some(crate::nodes::Command::EditSave(draft)) = app.nodes_requested.take() else {
        panic!()
    };
    assert_eq!(draft.revision, 4);
    assert_eq!(draft.body["confirm_access_change"], true);
}

#[test]
fn device_delete_requires_confirmation_and_back_does_not_leave_profile() {
    let mut app = test_profile_app();
    app.screen = Screen::Access;
    app.access.tab = crate::access::Tab::Profiles;
    app.access.card = Some(serde_json::json!({"id":"profile", "desired_revision":8}));
    app.access.apply(crate::access::Command::Devices("profile".into()), serde_json::json!({"profile":{"id":"profile", "desired_revision":8},"devices":{"items":[{"id":"device", "display_name":"Phone", "revision":3, "status":"active"}]}}));
    app.nodes_requested = None;
    app.activate(Control::AccessDeviceItem(0));
    app.activate(Control::AccessDeviceDelete);
    assert!(app.nodes_requested.is_none());
    app.activate(Control::AccessBack);
    assert!(app.access.devices.as_ref().unwrap().delete_key.is_none());
    assert!(app.access.devices.as_ref().unwrap().card.is_some());
    app.activate(Control::AccessDeviceDelete);
    app.activate(Control::AccessDeviceConfirm);
    let Some(crate::nodes::Command::Access(crate::access::Command::DeviceMutate {
        profile,
        device,
        revision,
        delete,
        ..
    })) = app.nodes_requested.take()
    else {
        panic!()
    };
    assert_eq!(profile, "profile");
    assert_eq!(device.as_deref(), Some("device"));
    assert_eq!(revision, 3);
    assert!(delete);
    app.activate(Control::AccessBack);
    app.activate(Control::AccessBack);
    app.activate(Control::AccessBack);
    assert!(app.access.devices.is_none());
    assert_eq!(app.access.card.as_ref().unwrap()["id"], "profile");
}
#[test]
fn duration_menu_keeps_draft_and_unlimited_does_not_submit_it() {
    let mut app = test_profile_app();
    app.screen = Screen::Access;
    app.access.editor = Some(crate::access::Editor::new(
        Some(serde_json::json!({"display_name":"Name", "expires_at":"2026-10-11T00:00:00Z"})),
        None,
    ));
    app.nodes_requested = None;
    app.activate(Control::AccessExpiryMenu);
    let mut terminal = Terminal::new(ratatui::backend::TestBackend::new(100, 30)).unwrap();
    terminal
        .draw(|frame| app.hits = draw_content(frame, &app))
        .unwrap();
    assert!(
        app.hits
            .iter()
            .any(|hit| hit.control == Control::AccessExpiry(Some(90)))
    );
    app.activate(Control::AccessExpiry(None));
    assert!(app.access.editor.as_ref().unwrap().fields[1].is_empty());
    assert!(app.nodes_requested.is_none());
    app.activate(Control::AccessExpiryMenu);
    app.access_key(KeyCode::Esc.into());
    assert!(app.access.editor.is_some());
    assert!(!app.access.editor.as_ref().unwrap().expiry_view);
}
#[test]
fn unavailable_sidebar_actions_have_a_cross_and_no_click_target() {
    let mut app = test_profile_app();
    app.confirmed_installation = None;
    let mut terminal = Terminal::new(ratatui::backend::TestBackend::new(120, 40)).unwrap();
    terminal
        .draw(|frame| app.hits = draw_content(frame, &app))
        .unwrap();
    let content = format!("{:?}", terminal.backend().buffer());
    assert!(content.contains("× Access"));
    assert!(content.contains("× Temporary configs"));
    assert!(content.contains("× Diagnostic"));
    assert!(!app.hits.iter().any(|hit| matches!(
        hit.control,
        Control::Access | Control::Temporary | Control::Action(Action::Diagnose)
    )));
}

#[test]
fn account_changes_require_confirmation_and_cancel_keeps_card() {
    let mut app = test_profile_app();
    app.screen = Screen::Access;
    app.access.tab = crate::access::Tab::Accounts;
    app.access.card = Some(
        serde_json::json!({"id":"account", "role":"member", "status":"approved", "revision":7}),
    );
    app.nodes_requested = None;
    app.activate(Control::AccessAccountAction(true));
    assert!(app.nodes_requested.is_none());
    assert_eq!(
        app.access.account_action.as_ref().unwrap().0["role"],
        "admin"
    );
    app.activate(Control::AccessBack);
    assert!(app.access.account_action.is_none());
    assert!(app.access.card.is_some());
    assert!(app.nodes_requested.is_none());
    app.activate(Control::AccessAccountAction(false));
    app.activate(Control::AccessAccountConfirm);
    let Some(crate::nodes::Command::Access(crate::access::Command::AccountMutate {
        revision,
        body,
        ..
    })) = app.nodes_requested.take()
    else {
        panic!("missing confirmed account command")
    };
    assert_eq!(revision, 7);
    assert_eq!(body["status"], "disabled");
}

#[test]
fn qr_preserves_black_and_white_modules_and_requires_enough_space() {
    let image = crate::temporary::qr_image("vpn://SECRET").unwrap();
    let mut terminal = Terminal::new(ratatui::backend::TestBackend::new(50, 20)).unwrap();
    terminal
        .draw(|frame| draw_qr(frame, frame.area(), &image))
        .unwrap();
    for cell in terminal.backend().buffer().content() {
        if cell.symbol() == "▀" {
            assert!(matches!(cell.fg, Color::Black | Color::White));
            assert!(matches!(cell.bg, Color::Black | Color::White));
        }
    }
    terminal.backend_mut().resize(20, 10);
    terminal.clear().unwrap();
    terminal
        .draw(|frame| draw_qr(frame, frame.area(), &image))
        .unwrap();
    assert!(format!("{:?}", terminal.backend().buffer()).contains("Enlarge"));
}

#[test]
fn repeated_custom_accents_persist_and_render_each_selected_color() {
    let directory = tempfile::tempdir().unwrap();
    let mut app = App::new(&test_request());
    app.state_dir = directory.path().into();
    app.activate(Control::SettingsPage(SettingsPage::Appearance));
    let mut terminal = Terminal::new(ratatui::backend::TestBackend::new(100, 40)).unwrap();
    for color in [
        "#123abc", "#ffbb74", "#55b1c2", "#123abc", "#000000", "#ffffff",
    ] {
        app.accent_input = color.into();
        app.activate(Control::SaveAccent);
        assert!(app.error.is_empty());
        terminal.clear().unwrap();
        terminal
            .draw(|frame| app.hits = draw_content(frame, &app))
            .unwrap();
        assert_eq!(Connections::load(directory.path()).unwrap().accent, color);
        assert!(
            terminal
                .backend()
                .buffer()
                .content
                .iter()
                .any(|cell| cell.fg == accent_color(color))
        );
    }
}

#[test]
fn quick_start_keyboard_opens_profile_and_escape_closes_guide() {
    let mut app = test_profile_app();
    app.confirmed_installation = None;
    let directory = tempfile::tempdir().unwrap();
    app.state_dir = directory.path().into();
    app.connections.save(&app.state_dir).unwrap();
    app.screen = Screen::QuickStart;
    app.confirm = true;
    app.quick_start_key(KeyCode::Right.into());
    assert!(app.confirm);
    app.quick_start_key(KeyCode::Left.into());
    assert!(!app.confirm);
    app.quick_start_key(KeyCode::Right.into());
    app.quick_start_key(KeyCode::Enter.into());
    assert!(matches!(app.screen, Screen::Form));
    assert!(app.editor.is_none());
    app.screen = Screen::QuickStart;
    app.quick_start_key(KeyCode::Esc.into());
    assert!(matches!(app.screen, Screen::Form));
    assert!(!app.exit);
}

#[test]
fn qr_modal_has_only_close_and_escape_or_enter_restores_artifact() {
    for key in [KeyCode::Esc, KeyCode::Enter] {
        let mut app = test_profile_app();
        app.screen = Screen::Temporary;
        app.temporary.view = crate::temporary::View::Artifact;
        app.qr_visible = true;
        let mut terminal = Terminal::new(ratatui::backend::TestBackend::new(100, 40)).unwrap();
        terminal
            .draw(|frame| app.hits = draw_content(frame, &app))
            .unwrap();
        assert_eq!(app.hits.len(), 1);
        assert!(matches!(app.hits[0].control, Control::TemporaryBack));
        app.temporary_key(KeyEvent::new(key, KeyModifiers::NONE));
        assert!(!app.qr_visible);
        assert!(app.temporary.view == crate::temporary::View::Artifact);
    }
}

#[test]
fn temporary_qr_survives_small_and_large_layouts_without_showing_uri() {
    let mut app = test_profile_app();
    app.screen = Screen::Temporary;
    app.temporary.view = crate::temporary::View::Artifact;
    app.temporary.card = Some(serde_json::json!({
        "node_key":"lv1", "protocol":"awg", "status":"active"
    }));
    app.temporary.artifact = Some(serde_json::json!({"content":"vpn://SECRET"}));
    let mut picker = ratatui_image::picker::Picker::halfblocks();
    picker.set_protocol_type(ratatui_image::picker::ProtocolType::Sixel);
    *app.qr.borrow_mut() = Some(crate::temporary::qr_image("vpn://SECRET").unwrap());
    app.image_picker = Some(picker);
    app.qr_visible = true;
    let mut terminal = Terminal::new(ratatui::backend::TestBackend::new(100, 30)).unwrap();
    for (width, height) in [(100, 30), (60, 18), (140, 45), (50, 12)] {
        terminal.backend_mut().resize(width, height);
        terminal.clear().unwrap();
        terminal
            .draw(|frame| app.hits = draw_content(frame, &app))
            .unwrap();
        assert!(!format!("{:?}", terminal.backend().buffer()).contains("vpn://SECRET"));
        assert!(
            app.hits
                .iter()
                .any(|hit| matches!(hit.control, Control::TemporaryBack))
        );
    }
}

#[test]
fn temporary_artifact_offers_copy_and_hides_qr_without_graphics() {
    let mut app = test_profile_app();
    app.screen = Screen::Temporary;
    app.temporary.view = crate::temporary::View::Artifact;
    app.temporary.card = Some(
        serde_json::json!({"id":uuid::Uuid::new_v4(), "node_key":"lv1", "protocol":"awg", "transport":"vpn", "status":"active"}),
    );
    app.temporary.artifact = Some(serde_json::json!({"content":"vpn://SECRET"}));
    let mut terminal = Terminal::new(ratatui::backend::TestBackend::new(100, 30)).unwrap();
    terminal
        .draw(|frame| app.hits = draw_content(frame, &app))
        .unwrap();
    assert!(!format!("{:?}", terminal.backend().buffer()).contains("SECRET"));
    assert!(
        app.temporary_controls()
            .iter()
            .any(|c| matches!(c, Control::TemporaryCopy))
    );
    assert!(
        app.temporary_controls()
            .iter()
            .any(|c| matches!(c, Control::TemporarySave))
    );
    assert!(
        !app.temporary_controls()
            .iter()
            .any(|c| matches!(c, Control::TemporaryQr))
    );
    app.qr_visible = true;
    app.temporary_key(KeyCode::Esc.into());
    assert!(!app.qr_visible);
    assert!(app.temporary.view == crate::temporary::View::Artifact);
    app.temporary_key(KeyCode::Esc.into());
    assert!(app.temporary.view == crate::temporary::View::Card);
    app.temporary_key(KeyCode::Esc.into());
    assert!(matches!(
        app.nodes_requested.take(),
        Some(crate::nodes::Command::Temporary(
            crate::temporary::Command::List { .. }
        ))
    ));
}

#[test]
fn temporary_creation_skips_only_single_choices_and_keeps_identity() {
    let mut app = test_profile_app();
    let mut node = browser_node("lv1", "Europe");
    node.applied_revision = 1;
    app.open_temporary(Some(node.clone()));
    app.nodes_requested = None;
    app.activate(Control::TemporaryNew);
    assert!(app.temporary.view == crate::temporary::View::Duration);
    app.temporary.seconds = 43200;
    let identity = app.temporary.id;
    let first = app.temporary.create();
    let second = app.temporary.create();
    assert!(
        matches!((first,second), (crate::temporary::Command::Create{id:a,body:x}, crate::temporary::Command::Create{id:b,body:y}) if a==b && x==y)
    );
    assert_eq!(app.temporary.id, identity);
    app.temporary.back();
    assert!(app.temporary.view == crate::temporary::View::List);
    node.protocols = vec!["xray".into()];
    node.xray_transports = vec!["tcp".into(), "xhttp".into()];
    app.temporary.choose_node(node);
    assert!(app.temporary.view == crate::temporary::View::Transport);
    app.temporary.transport = "xhttp".into();
    app.temporary.view = crate::temporary::View::Duration;
    app.temporary.back();
    assert!(app.temporary.view == crate::temporary::View::Transport);
}

#[test]
fn administrator_picker_selects_exact_account_and_cancels_without_answer() {
    let mut app = test_profile_app();
    let choices: Vec<_> = (0..40)
        .map(|i| (format!("account-{i}"), format!("{} · @admin{i}", 100 + i)))
        .collect();
    let (tx, rx) = mpsc::channel();
    app.event(Event::Prompt(
        Prompt::SelectAdministrator {
            choices: choices.clone(),
        },
        tx,
    ));
    for _ in 0..30 {
        app.prompt_key(KeyEvent::new(KeyCode::Down, KeyModifiers::NONE));
    }
    let mut terminal = Terminal::new(ratatui::backend::TestBackend::new(80, 24)).unwrap();
    terminal
        .draw(|frame| app.hits = draw_content(frame, &app))
        .unwrap();
    assert!(
        app.hits
            .iter()
            .any(|hit| matches!(hit.control, Control::Administrator(30)))
    );
    app.prompt_key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
    assert!(matches!(rx.recv().unwrap(), Answer::Selection(30)));
    let (tx, rx) = mpsc::channel();
    app.event(Event::Prompt(Prompt::SelectAdministrator { choices }, tx));
    assert_eq!(app.administrator_selected, 0);
    let key = app.activate(Control::Administrator(2)).unwrap();
    app.prompt_key(key);
    assert!(matches!(rx.recv().unwrap(), Answer::Selection(2)));
    let (tx, rx) = mpsc::channel();
    app.event(Event::Prompt(
        Prompt::SelectAdministrator { choices: vec![] },
        tx,
    ));
    app.prompt_key(KeyEvent::new(KeyCode::Esc, KeyModifiers::NONE));
    assert!(matches!(rx.recv().unwrap(), Answer::Cancel));
}
#[test]
fn background_observation_preserves_screen_and_selection() {
    let mut app = test_profile_app();
    app.screen = Screen::Nodes;
    app.nodes_busy = true;
    app.nodes_background = true;
    app.nodes_selected = 3;
    app.event(Event::Finished(Ok("Registry observed".into())));
    assert!(matches!(app.screen, Screen::Nodes));
    assert_eq!(app.nodes_selected, 3);
    assert!(!app.nodes_busy);
    assert!(!app.nodes_background);
}

#[test]
fn install_token_precedes_release_without_changing_field_identity() {
    let mut form = Form::new(&test_request());
    form.action = Action::Install;
    let fields = form.visible();
    assert!(
        fields.iter().position(|i| *i == 6).unwrap() < fields.iter().position(|i| *i == 4).unwrap()
    );
}

#[test]
fn action_forms_do_not_offer_administrator_selector() {
    let mut form = Form::new(&test_request());
    for action in Action::ALL {
        form.select_action(action);
        assert!(!form.visible().contains(&7));
    }
}
use super::*;
#[test]
fn progress_label_uses_terminal_background_on_filled_accent() {
    let mut app = App::new(&test_request());
    app.form.action = Action::Install;
    app.screen = Screen::Running;
    app.tracker.current = "Prepare PostgreSQL and backend schema".into();
    app.tracker.detail = "auto-provision local postgresql runtime".into();
    app.tracker.completed = 3;
    for accent in ["terminal", "#ffbb74"] {
        app.connections.accent = accent.into();
        for (width, height) in [(100, 24), (60, 16)] {
            let mut terminal =
                Terminal::new(ratatui::backend::TestBackend::new(width, height)).unwrap();
            terminal
                .draw(|frame| {
                    draw_content(frame, &app);
                })
                .unwrap();
            let buffer = terminal.backend().buffer();
            let filled: Vec<_> = buffer
                .content
                .iter()
                .filter(|cell| cell.modifier.contains(Modifier::REVERSED))
                .collect();
            assert!(!filled.is_empty(), "{accent} at {width}x{height}");
            assert!(
                filled
                    .iter()
                    .all(|cell| cell.fg == accent_color(accent) && cell.bg == Color::Reset)
            );
            assert!(
                filled
                    .iter()
                    .any(|cell| cell.symbol().chars().any(char::is_alphabetic))
            );
            assert!(
                buffer
                    .content
                    .iter()
                    .any(|cell| cell.fg == accent_color(accent)
                        && !cell.modifier.contains(Modifier::REVERSED)
                        && cell.symbol() == "e")
            );
        }
    }
}
#[test]
fn appearance_mouse_keyboard_and_custom_color_persist_across_restart() {
    let dir = tempfile::tempdir().unwrap();
    let mut app = App::new(&test_request());
    app.state_dir = dir.path().into();
    app.activate(Control::SettingsPage(SettingsPage::Appearance));
    let mut terminal = Terminal::new(ratatui::backend::TestBackend::new(100, 40)).unwrap();
    terminal
        .draw(|frame| app.hits = draw_content(frame, &app))
        .unwrap();
    let peach = app
        .hits
        .iter()
        .find(|h| matches!(h.control, Control::Accent(1)))
        .unwrap()
        .area;
    click(&mut app, (peach.x + 1, peach.y + 1));
    terminal
        .draw(|frame| app.hits = draw_content(frame, &app))
        .unwrap();
    assert!(
        terminal
            .backend()
            .buffer()
            .content
            .iter()
            .any(|c| c.fg == Color::Rgb(255, 187, 116))
    );
    assert_eq!(Connections::load(dir.path()).unwrap().accent, "#ffbb74");
    app.activate(Control::AccentInput);
    app.accent_input.clear();
    for c in "#123abc".chars() {
        app.appearance_key(KeyCode::Char(c));
    }
    app.appearance_key(KeyCode::Enter);
    assert_eq!(Connections::load(dir.path()).unwrap().accent, "#123abc");
    app.activate(Control::Accent(0));
    assert_eq!(Connections::load(dir.path()).unwrap().accent, "terminal");
    assert_eq!(Action::Diagnose.label(), "Diagnostic");
}
#[test]
fn deleting_one_of_two_profiles_on_same_endpoint_preserves_trust() {
    let dir = tempfile::tempdir().unwrap();
    let mut app = test_profile_app();
    app.state_dir = dir.path().into();
    let profile = app.connections.active().unwrap().clone();
    let mut other = profile.clone();
    other.id = uuid::Uuid::new_v4();
    other.user = "admin".into();
    app.connections.installations.push(other);
    app.connections.save(dir.path()).unwrap();
    let path = dir.path().join("known_hosts.json");
    let trust = serde_json::to_vec(&std::collections::BTreeMap::from([(
        format!("[{}]:{}", profile.host.to_ascii_lowercase(), profile.port),
        "fingerprint",
    )]))
    .unwrap();
    std::fs::write(&path, &trust).unwrap();
    app.delete_profile(profile.id).unwrap();
    assert_eq!(std::fs::read(path).unwrap(), trust);
}
#[test]
fn ssh_password_field_is_visible_masked_and_empty_submit_is_rejected() {
    let mut app = App::new(&test_request());
    let (tx, rx) = mpsc::channel();
    app.event(Event::Prompt(
        Prompt::Password {
            user: "root".into(),
            host: "vps.example".into(),
        },
        tx,
    ));
    for (width, height) in [(120, 40), (80, 24), (50, 16)] {
        let mut terminal =
            Terminal::new(ratatui::backend::TestBackend::new(width, height)).unwrap();
        terminal
            .draw(|frame| app.hits = draw_content(frame, &app))
            .unwrap();
        let content = format!("{:?}", terminal.backend().buffer());
        assert!(
            content.contains("SSH password (hidden)"),
            "{width}x{height}: {content}"
        );
    }
    let connect = app
        .hits
        .iter()
        .find(|h| matches!(h.control, Control::Prompt(true)))
        .unwrap()
        .area;
    let enter = click(&mut app, (connect.x + 1, connect.y + 1)).unwrap();
    app.prompt_key(enter);
    assert!(app.prompt.is_some());
    assert!(rx.try_recv().is_err());
    for c in "secret".chars() {
        app.prompt_key(KeyEvent::new(KeyCode::Char(c), KeyModifiers::NONE));
    }
    let mut terminal = Terminal::new(ratatui::backend::TestBackend::new(80, 24)).unwrap();
    terminal
        .draw(|frame| app.hits = draw_content(frame, &app))
        .unwrap();
    let content = format!("{:?}", terminal.backend().buffer());
    assert!(content.contains("******"));
    assert!(!content.contains("secret"));
    let connect = app
        .hits
        .iter()
        .find(|h| matches!(h.control, Control::Prompt(true)))
        .unwrap()
        .area;
    let enter = click(&mut app, (connect.x + 1, connect.y + 1)).unwrap();
    app.prompt_key(enter);
    let Answer::Password(password) = rx.recv().unwrap() else {
        panic!("Password answer expected")
    };
    assert_eq!(password.as_str(), "secret");
    assert!(app.password.is_empty());
    assert!(app.error.is_empty());
}
#[test]
fn workstation_settings_offer_update_without_starting_server_work() {
    let mut app = App::new(&test_request());
    app.open_settings();
    app.self_status = "Available: v9.0.0".into();
    app.self_release = Some(
        serde_json::from_value(serde_json::json!({
            "tag_name":"v9.0.0", "draft":false, "prerelease":false, "assets":[]
        }))
        .unwrap(),
    );
    let mut terminal = Terminal::new(ratatui::backend::TestBackend::new(120, 40)).unwrap();
    terminal
        .draw(|frame| app.hits = draw_content(frame, &app))
        .unwrap();
    let content = format!("{:?}", terminal.backend().buffer());
    assert!(content.contains(env!("CARGO_PKG_VERSION")));
    assert!(content.contains("v9.0.0"));
    let button = app
        .hits
        .iter()
        .find(|h| matches!(h.control, Control::SettingsPage(SettingsPage::Updates)))
        .unwrap()
        .area;
    assert!(click(&mut app, (button.x + 1, button.y + 1)).is_none());
    assert!(!app.self_requested);
    assert!(matches!(
        app.screen,
        Screen::SettingsPage(SettingsPage::Updates)
    ));
    terminal
        .draw(|frame| app.hits = draw_content(frame, &app))
        .unwrap();
    let update = app
        .hits
        .iter()
        .find(|h| matches!(h.control, Control::SelfUpdate))
        .unwrap()
        .area;
    assert!(click(&mut app, (update.x + 1, update.y + 1)).is_none());
    assert!(app.self_requested);
    assert!(app.outcome.is_none());
}
#[test]
fn settings_directory_opens_sections_without_profile_actions_on_home() {
    let mut app = test_profile_app();
    let mut terminal = Terminal::new(ratatui::backend::TestBackend::new(80, 24)).unwrap();
    for (index, page) in SettingsPage::ALL.into_iter().enumerate() {
        app.open_settings();
        app.settings_selected = index + 1;
        terminal
            .draw(|frame| app.hits = draw_content(frame, &app))
            .unwrap();
        assert!(!app.hits.iter().any(|h| matches!(
            h.control,
            Control::UseInstallation | Control::DeleteInstallation
        )));
        let target = app
            .hits
            .iter()
            .find(|h| matches!(h.control, Control::SettingsPage(p) if p == page))
            .unwrap()
            .area;
        click(&mut app, (target.x + 1, target.y + 1));
        assert!(matches!(app.screen, Screen::SettingsPage(p) if p == page));
        if page == SettingsPage::Appearance {
            app.settings_selected = app.themes.len() + 3;
        }
        terminal
            .draw(|frame| app.hits = draw_content(frame, &app))
            .unwrap();
        assert!(
            app.hits
                .iter()
                .any(|h| matches!(h.control, Control::Settings))
        );
        assert!(app.screen.is_settings());
        assert!(app.outcome.is_none() && !app.self_requested);
    }
    assert_eq!(Action::Update.label(), "Node Plane Updates");
}
#[test]
fn deleting_saved_profiles_preserves_host_trust_and_operation_history() {
    for delete_active in [true, false] {
        let dir = tempfile::tempdir().unwrap();
        let mut app = test_profile_app();
        app.state_dir = dir.path().into();
        let active = app.connections.selected.unwrap();
        let other = saved_installation("other.example");
        app.connections.installations.push(other.clone());
        app.connections.save(&app.state_dir).unwrap();
        for name in ["id_ed25519", "update-operation.json"] {
            std::fs::write(dir.path().join(name), "keep").unwrap();
        }
        let host = app.connections.active().unwrap().host.clone();
        std::fs::write(
            dir.path().join("known_hosts.json"),
            serde_json::to_vec(&std::collections::BTreeMap::from([
                (format!("[{host}]:2222"), "active"),
                ("[other.example]:22".into(), "other"),
                ("[other.example]:2222".into(), "other-port"),
            ]))
            .unwrap(),
        )
        .unwrap();
        app.delete_profile(if delete_active { active } else { other.id })
            .unwrap();
        let restored = Connections::load(dir.path()).unwrap();
        assert_eq!(restored.installations.len(), 1);
        assert_eq!(
            restored.selected,
            if delete_active { None } else { Some(active) }
        );
        assert_eq!(app.actions_enabled(), !delete_active);
        let hosts: std::collections::BTreeMap<String, String> =
            serde_json::from_slice(&std::fs::read(dir.path().join("known_hosts.json")).unwrap())
                .unwrap();
        assert_eq!(
            hosts.contains_key(&format!("[{host}]:2222")),
            !delete_active
        );
        assert_eq!(hosts.contains_key("[other.example]:2222"), delete_active);
        assert!(hosts.contains_key("[other.example]:22"));
        for name in ["id_ed25519", "update-operation.json"] {
            assert_eq!(
                std::fs::read_to_string(dir.path().join(name)).unwrap(),
                "keep"
            );
        }
    }
}
#[test]
fn profile_deletion_requires_confirmation_and_cancel_preserves_save() {
    let dir = tempfile::tempdir().unwrap();
    let mut app = test_profile_app();
    app.state_dir = dir.path().into();
    app.connections.save(&app.state_dir).unwrap();
    let saved = std::fs::read(dir.path().join("installations.json")).unwrap();
    app.activate(Control::DeleteInstallation);
    assert!(matches!(app.screen, Screen::DeleteInstallation(_)));
    assert!(!app.confirm);
    app.activate(Control::ConfirmDelete(false));
    assert_eq!(
        std::fs::read(dir.path().join("installations.json")).unwrap(),
        saved
    );
    app.activate(Control::DeleteInstallation);
    app.activate(Control::ConfirmDelete(true));
    assert!(
        Connections::load(dir.path())
            .unwrap()
            .installations
            .is_empty()
    );
    assert!(!app.actions_enabled());
    assert_eq!(app.profile_controls().len(), 2);
}
#[test]
fn compact_appearance_keeps_apply_between_input_and_back() {
    let mut app = test_profile_app();
    app.screen = Screen::SettingsPage(SettingsPage::Appearance);
    app.settings_selected = app.themes.len() + 1;
    let mut terminal = Terminal::new(ratatui::backend::TestBackend::new(60, 13)).unwrap();
    terminal
        .draw(|frame| app.hits = draw_content(frame, &app))
        .unwrap();
    app.appearance_key(KeyCode::Down);
    assert_eq!(app.settings_selected, app.themes.len() + 2);
    terminal
        .draw(|frame| app.hits = draw_content(frame, &app))
        .unwrap();
    assert!(
        app.hits
            .iter()
            .any(|hit| hit.control == Control::SaveAccent)
    );
    app.mouse(MouseEvent {
        kind: MouseEventKind::ScrollDown,
        column: 50,
        row: 5,
        modifiers: KeyModifiers::NONE,
    });
    assert_eq!(app.settings_selected, app.themes.len() + 2);
    app.mouse(MouseEvent {
        kind: MouseEventKind::ScrollUp,
        column: 50,
        row: 5,
        modifiers: KeyModifiers::NONE,
    });
    assert_eq!(app.settings_selected, app.themes.len() + 2);
    assert!(app.nodes_requested.is_none());
}

#[test]
fn compact_settings_scroll_and_update_checks_keep_server_actions_idle() {
    let mut app = test_profile_app();
    app.open_settings();
    let mut terminal = Terminal::new(ratatui::backend::TestBackend::new(60, 16)).unwrap();
    terminal
        .draw(|frame| app.hits = draw_content(frame, &app))
        .unwrap();
    let selected = app.settings_selected;
    for _ in 0..SettingsPage::ALL.len() {
        app.mouse(MouseEvent {
            kind: MouseEventKind::ScrollDown,
            column: 50,
            row: 5,
            modifiers: KeyModifiers::NONE,
        });
    }
    terminal
        .draw(|frame| app.hits = draw_content(frame, &app))
        .unwrap();
    assert!(
        app.hits
            .iter()
            .any(|hit| matches!(hit.control, Control::SettingsPage(SettingsPage::About)))
    );
    assert_eq!(app.settings_selected, selected);
    app.activate(Control::SettingsPage(SettingsPage::Updates));
    terminal
        .draw(|frame| app.hits = draw_content(frame, &app))
        .unwrap();
    let check = app
        .hits
        .iter()
        .find(|hit| matches!(hit.control, Control::CheckWorkstationUpdates))
        .unwrap()
        .area;
    click(&mut app, (check.x + 1, check.y + 1));
    assert!(app.self_check_requested);
    assert!(!app.self_requested && app.outcome.is_none());
    assert!(matches!(
        app.screen,
        Screen::SettingsPage(SettingsPage::Updates)
    ));
}
#[test]
fn finished_diagnostics_keep_summary_and_buttons_outside_scrollable_output() {
    let mut app = test_profile_app();
    app.form.action = Action::Diagnose;
    let report = format!(
        "{}\nSummary\nMode: simple\nFailures: 0\nWarnings: 0\n[OK] Setup looks healthy\nSuggested Fixes\n/data-last-line",
        (0..80)
            .map(|i| format!("check-{i:03}"))
            .collect::<Vec<_>>()
            .join("\n")
    );
    app.event(Event::Finished(Ok(report)));
    let mut terminal = Terminal::new(ratatui::backend::TestBackend::new(80, 24)).unwrap();
    terminal
        .draw(|frame| app.hits = draw_content(frame, &app))
        .unwrap();
    let initial = format!("{:?}", terminal.backend().buffer());
    assert!(initial.contains("Setup looks healthy") && initial.contains("check-000"));
    assert!(!initial.contains("/data-last-line"));
    app.scroll_result(false, usize::MAX);
    terminal
        .draw(|frame| app.hits = draw_content(frame, &app))
        .unwrap();
    let end = format!("{:?}", terminal.backend().buffer());
    assert!(end.contains("Setup looks healthy") && end.contains("/data-last-line"));
    let close = app
        .hits
        .iter()
        .find(|hit| matches!(hit.control, Control::Close))
        .unwrap()
        .area;
    let label: String = (close.x + 1..close.right() - 1)
        .map(|x| terminal.backend().buffer()[(x, close.y + 1)].symbol())
        .collect();
    assert_eq!(label.trim(), "Close");
    app.mouse(MouseEvent {
        kind: MouseEventKind::ScrollUp,
        column: 30,
        row: 10,
        modifiers: KeyModifiers::NONE,
    });
    assert_eq!(
        app.result_scroll,
        app.result_max_scroll.get().saturating_sub(3)
    );
    app.scroll_result(true, usize::MAX);
    assert_eq!(app.result_scroll, 0);
}
#[test]
fn channel_fields_are_fixed_to_the_matching_workstation_and_look_disabled() {
    let mut app = App::new(&test_request());
    app.begin_edit(true);
    assert_eq!(
        app.editor.as_ref().unwrap().1[4],
        crate::config::default_branch()
    );
    app.editor.as_mut().unwrap().2 = 4;
    app.installation_editor_key(KeyCode::Right.into());
    app.installation_editor_key(KeyCode::Left.into());
    app.installation_editor_key(KeyCode::Char('m').into());
    assert_eq!(
        app.editor.as_ref().unwrap().1[4],
        crate::config::default_branch()
    );
    app.editor.as_mut().unwrap().2 = 0;
    let mut terminal = Terminal::new(ratatui::backend::TestBackend::new(120, 40)).unwrap();
    terminal
        .draw(|frame| app.hits = draw_content(frame, &app))
        .unwrap();
    let field = app
        .hits
        .iter()
        .find(|h| h.control == Control::EditField(4))
        .unwrap()
        .area;
    assert_eq!(
        terminal.backend().buffer()[(field.x + 1, field.y)].fg,
        Color::DarkGray
    );
}

#[test]
fn running_panel_centers_the_stage_and_prints_it_only_once() {
    let r = Request {
        action: Action::Update,
        host: "controller.example".into(),
        port: 22,
        user: "root".into(),
        state_dir: std::path::PathBuf::new(),
        tag: String::new(),
        branch: String::new(),
        admin_ids: String::new(),
        bot_token: Zeroizing::new(String::new()),
        workflow: WorkflowOptions::default(),
    };
    let mut app = App::new(&r);
    app.screen = Screen::Running;
    app.stage = "Checking available releases in the installed update channel".into();
    let mut terminal = Terminal::new(ratatui::backend::TestBackend::new(120, 40)).unwrap();
    terminal
        .draw(|frame| {
            draw_content(frame, &app);
        })
        .unwrap();
    let content = format!("{:?}", terminal.backend().buffer());
    assert_eq!(content.matches(&app.stage).count(), 1);
    assert!(content.contains("Step 3/4"));
    assert!(!content.contains("Diagnostics"));
    let popup = centered(Rect::new(0, 0, 120, 40), 84, 9);
    assert_eq!(popup.x, 18);
    assert_eq!(popup.y, 15);
}
#[test]
fn action_specific_fields_do_not_edit_hidden_credentials() {
    let mut form = Form {
        action: Action::Update,
        fields: vec![String::new(); 11],
        selected: 6,
        saved: None,
    };
    form.append("admin-account");
    assert!(form.fields[7].is_empty());
    assert!(form.fields[5].is_empty());
    form.action = Action::PrepareNode;
    form.selected = 4;
    form.append("target.example");
    assert_eq!(form.fields[8], "target.example");
    assert!(form.fields[6].is_empty());
    form.cycle(false);
    assert!(form.action == Action::Diagnose);
    form.cycle(false);
    assert!(form.action == Action::Install);
    assert_eq!(form.fields[3], crate::config::default_branch());
    form.cycle(false);
    assert!(form.action == Action::Update);
    assert!(form.fields[3].is_empty());
}
#[test]
fn update_node_pages_render_and_resize_without_leaking_other_pages() {
    let snapshot = crate::workstation::UpdateSnapshot {
        target: "dev · 1.0.0-alpha.5".into(),
        id: uuid::Uuid::new_v4(),
        status: "running".into(),
        phase: "agents".into(),
        components: vec![("backend".into(), "succeeded".into())],
        nodes: (0..12)
            .map(|i| crate::workstation::NodeSnapshot {
                key: format!("n{i}"),
                title: format!("Node{i:02}"),
                region: "Europe".into(),
                status: "waiting".into(),
                phase: "agent".into(),
                error: String::new(),
            })
            .collect(),
        error: String::new(),
        rollback: String::new(),
    };
    for (width, height) in [(100, 40), (50, 16), (20, 8)] {
        let mut terminal =
            Terminal::new(ratatui::backend::TestBackend::new(width, height)).unwrap();
        terminal
            .draw(|frame| draw_update(frame, &snapshot, 0, frame.area()))
            .unwrap();
        let content = format!("{:?}", terminal.backend().buffer());
        assert!(!content.contains("Node10"));
    }
    let mut terminal = Terminal::new(ratatui::backend::TestBackend::new(100, 40)).unwrap();
    terminal
        .draw(|frame| draw_update(frame, &snapshot, 1, frame.area()))
        .unwrap();
    let content = format!("{:?}", terminal.backend().buffer());
    assert!(content.contains("Node10"));
    assert!(!content.contains("Node00"));
}
#[test]
fn rendering_resizes_and_hides_secrets() {
    let r = Request {
        action: Action::Install,
        host: "vps.example".into(),
        port: 22,
        user: "root".into(),
        state_dir: std::path::PathBuf::new(),
        tag: String::new(),
        branch: "dev".into(),
        admin_ids: "42".into(),
        bot_token: Zeroizing::new(String::new()),
        workflow: WorkflowOptions::default(),
    };
    let mut app = App::new(&r);
    app.form.fields[6] = "VERY_SECRET_TOKEN".into();
    for (width, height) in [(80, 24), (120, 40), (50, 16), (20, 8)] {
        let mut terminal =
            Terminal::new(ratatui::backend::TestBackend::new(width, height)).unwrap();
        terminal
            .draw(|f| {
                draw(f, &app);
            })
            .unwrap();
        let content = format!("{:?}", terminal.backend().buffer());
        assert!(!content.contains("VERY_SECRET_TOKEN"));
    }
}
#[test]
fn form_navigation_and_prompt_cancel() {
    let r = Request {
        action: Action::Diagnose,
        host: String::new(),
        port: 22,
        user: "root".into(),
        state_dir: std::path::PathBuf::new(),
        tag: String::new(),
        branch: "dev".into(),
        admin_ids: String::new(),
        bot_token: Zeroizing::new(String::new()),
        workflow: WorkflowOptions::default(),
    };
    let mut app = App::new(&r);
    app.form.previous();
    assert_eq!(app.form.selected, 0);
    app.form.next();
    assert_eq!(app.form.selected, 1);
    app.form.previous();
    assert_eq!(app.form.selected, 1);
    app.form.selected = app.form.count() - 1;
    app.form.next();
    assert_eq!(app.form.selected, app.form.count() - 1);
    let (tx, rx) = mpsc::channel();
    app.prompt = Some((
        Prompt::HostKey {
            host: "h".into(),
            fingerprint: "f".into(),
        },
        tx,
    ));
    app.prompt_key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
    assert!(matches!(rx.recv().unwrap(), Answer::Confirm(false)));
}
fn test_request() -> Request {
    Request {
        action: Action::Update,
        host: "controller.example".into(),
        port: 22,
        user: "root".into(),
        state_dir: std::path::PathBuf::new(),
        tag: String::new(),
        branch: String::new(),
        admin_ids: String::new(),
        bot_token: Zeroizing::new(String::new()),
        workflow: WorkflowOptions::default(),
    }
}
fn click(app: &mut App, point: (u16, u16)) -> Option<KeyEvent> {
    app.mouse(MouseEvent {
        kind: MouseEventKind::Down(MouseButton::Left),
        column: point.0,
        row: point.1,
        modifiers: KeyModifiers::NONE,
    })
}
fn test_profile_app() -> App {
    let mut app = App::new(&test_request());
    let profile = saved_installation("controller.example");
    app.connections.installations.push(profile.clone());
    app.connections.selected = Some(profile.id);
    app.form.use_installation(&profile);
    app.form.select_action(Action::Install);
    app.confirmed_installation = Some(profile.id);
    app.quick_focus = false;
    app
}
#[test]
fn controller_settings_groups_keep_removal_last_and_updates_in_sidebar() {
    let mut app = test_profile_app();
    app.installed_profile = app.confirmed_installation;
    app.activate(Control::Action(Action::Install));
    let controls = app.controller_controls();
    assert!(controls.last() == Some(&Control::ControllerSection(ControllerSection::Removal)));
    assert!(!controls.contains(&Control::ControllerUpdates));
    assert!(!controls.contains(&Control::ControllerPlan(true)));
    app.activate(Control::ControllerSection(ControllerSection::Installation));
    assert!(
        app.controller_controls()
            .contains(&Control::ControllerDefaults)
    );
    app.activate(Control::ControllerBack);
    assert!(app.controller_section.is_none());
    app.activate(Control::Action(Action::Update));
    assert!(matches!(
        app.nodes_requested,
        Some(crate::nodes::Command::Controller(
            _,
            crate::controller::Command::Updates
        ))
    ));
    assert!(matches!(app.screen, Screen::Controller));
    app.controller.updates = Some(serde_json::json!({}));
    app.activate(Control::Action(Action::Install));
    assert!(app.controller.updates.is_none());
    assert!(
        app.controller_controls()
            .contains(&Control::ControllerSection(ControllerSection::Installation))
    );
}

#[test]
fn controller_removal_replaces_settings_and_refreshes_to_finish_automatically() {
    let mut app = test_profile_app();
    app.open_controller();
    app.nodes_requested = None;
    let id = uuid::Uuid::new_v4();
    app.controller.job = Some(
        serde_json::json!({"id":id,"action":"remove","status":"running","phase":"nodes","items":[{"node_key":"local","status":"pending"}]}),
    );
    app.controller.backups = Some(serde_json::json!({}));
    app.controller.backup_job = Some(serde_json::json!({"status":"succeeded"}));
    assert!(
        app.controller_controls() == vec![Control::ControllerShutdown, Control::ControllerAbort]
    );
    app.activate(Control::ControllerShutdown);
    assert!(app.nodes_requested.is_none());
    let command = app.controller.poll().unwrap();
    app.event(Event::Nodes(crate::nodes::Update::Controller(app.controller.profile, command,
        serde_json::json!({"id":id,"action":"remove","status":"awaiting_shutdown","phase":"shutdown"}))));
    assert!(app.controller.removal_ready());
    let mut terminal = Terminal::new(ratatui::backend::TestBackend::new(100, 40)).unwrap();
    terminal.draw(|frame| app.hits = draw(frame, &app)).unwrap();
    assert!(
        app.hits
            .iter()
            .any(|hit| hit.control == Control::ControllerShutdown)
    );
    assert!(
        app.hits
            .iter()
            .any(|hit| hit.control == Control::ControllerAbort)
    );
    assert!(!app.hits.iter().any(|hit| matches!(
        hit.control,
        Control::ControllerSection(_) | Control::ControllerBackups | Control::ControllerBack
    )));
    app.activate(Control::ControllerAbort);
    assert!(
        matches!(app.nodes_requested, Some(crate::nodes::Command::Controller(_, crate::controller::Command::Abort(value))) if value == id)
    );
}

#[test]
fn pinned_back_remains_visible_in_small_scrolled_settings() {
    let mut app = test_profile_app();
    app.screen = Screen::SettingsPage(SettingsPage::Appearance);
    let mut terminal = Terminal::new(ratatui::backend::TestBackend::new(80, 20)).unwrap();
    for selected in [1, app.themes.len() + 3] {
        app.settings_selected = selected;
        terminal
            .draw(|frame| app.hits = draw_content(frame, &app))
            .unwrap();
        let back = app
            .hits
            .iter()
            .rfind(|hit| hit.control == Control::Settings)
            .unwrap();
        assert_eq!(back.area, Rect::new(73, 17, 7, 3));
    }
}

#[test]
fn policies_root_has_traffic_and_requests_settings_are_separate() {
    let mut app = test_profile_app();
    app.screen = Screen::Controller;
    app.activate(Control::ControllerSection(ControllerSection::Policies));
    assert!(matches!(
        app.nodes_requested.take(),
        Some(crate::nodes::Command::Controller(
            _,
            crate::controller::Command::Policies
        ))
    ));
    app.controller.apply(crate::controller::Command::Policies, serde_json::json!({"traffic":{"enabled":false},"requests":{"enabled":true,"notify_requests":true}}));
    assert!(
        app.controller_controls()
            .contains(&Control::ControllerPolicyToggle("traffic"))
    );
    assert!(
        !app.controller_controls()
            .contains(&Control::ControllerPolicyToggle("requests"))
    );
    app.activate(Control::ControllerPolicyToggle("traffic"));
    assert!(
        matches!(app.nodes_requested.take(), Some(crate::nodes::Command::Controller(_, crate::controller::Command::PolicySave("traffic", value))) if value["enabled"] == true)
    );
    app.activate(Control::ControllerPolicies);
    assert!(
        app.controller_controls()
            .contains(&Control::ControllerPolicyToggle("requests"))
    );
    assert!(
        !app.controller_controls()
            .contains(&Control::ControllerPolicyToggle("traffic"))
    );
    app.activate(Control::ControllerBack);
    assert!(!app.requests_settings);
    assert!(app.controller_section == Some(ControllerSection::Policies));
    assert!(
        app.controller_controls()
            .contains(&Control::ControllerPolicyToggle("traffic"))
    );
}

#[test]
fn open_sidebar_label_keeps_accent_after_wheel_scroll() {
    let mut app = test_profile_app();
    app.activate(Control::Action(Action::Install));
    let mut terminal = Terminal::new(ratatui::backend::TestBackend::new(100, 30)).unwrap();
    terminal
        .draw(|frame| app.hits = draw_content(frame, &app))
        .unwrap();
    app.mouse(MouseEvent {
        kind: MouseEventKind::ScrollDown,
        column: 50,
        row: 15,
        modifiers: KeyModifiers::NONE,
    });
    terminal
        .draw(|frame| app.hits = draw_content(frame, &app))
        .unwrap();
    let hit = app
        .hits
        .iter()
        .find(|hit| hit.control == Control::Action(Action::Install))
        .unwrap();
    let buffer = terminal.backend().buffer();
    assert_eq!(buffer[(hit.area.x, hit.area.y)].fg, Color::White);
    let mut letters = 0;
    for y in hit.area.y..hit.area.bottom() {
        for x in hit.area.x + 1..hit.area.right() - 1 {
            if !buffer[(x, y)].symbol().trim().is_empty()
                && !matches!(buffer[(x, y)].symbol(), "─" | "│" | "┌" | "┐" | "└" | "┘")
            {
                assert_eq!(buffer[(x, y)].fg, accent_color(&app.connections.accent));
                letters += 1;
            }
        }
    }
    assert!(letters > 0);
}

#[test]
fn quick_profile_opens_selected_installation_editor() {
    let mut app = test_profile_app();
    let mut other = saved_installation("other.example");
    other.name = "Other".into();
    app.connections.installations.push(other);
    app.settings_profile = 1;
    app.activate(Control::QuickProfile);
    assert!(matches!(app.screen, Screen::EditInstallation));
    let (id, fields, _) = app.editor.as_ref().unwrap();
    assert_eq!(*id, app.form.saved.as_ref().map(|profile| profile.id));
    assert_eq!(fields[1], "controller.example");
    assert!(app.nodes_requested.is_none());
}

#[test]
fn settings_back_hover_clears_when_pointer_leaves() {
    for page in SettingsPage::ALL {
        let mut app = test_profile_app();
        app.screen = Screen::SettingsPage(page);
        let mut terminal = Terminal::new(ratatui::backend::TestBackend::new(120, 40)).unwrap();
        terminal
            .draw(|frame| app.hits = draw_content(frame, &app))
            .unwrap();
        let area = app
            .hits
            .iter()
            .rev()
            .find(|hit| hit.control == Control::Settings)
            .unwrap()
            .area;
        app.mouse(MouseEvent {
            kind: MouseEventKind::Moved,
            column: area.x + 1,
            row: area.y + 1,
            modifiers: KeyModifiers::NONE,
        });
        terminal
            .draw(|frame| app.hits = draw_content(frame, &app))
            .unwrap();
        assert_eq!(
            terminal.backend().buffer()[(area.x, area.y)].fg,
            accent_color(&app.connections.accent)
        );
        for other in app
            .hits
            .iter()
            .filter(|hit| hit.control == Control::Settings && hit.area != area)
        {
            assert_eq!(
                terminal.backend().buffer()[(other.area.x, other.area.y)].fg,
                Color::White,
                "sidebar also highlighted in {}",
                page.label()
            );
        }
        app.mouse(MouseEvent {
            kind: MouseEventKind::Moved,
            column: 119,
            row: 0,
            modifiers: KeyModifiers::NONE,
        });
        terminal
            .draw(|frame| app.hits = draw_content(frame, &app))
            .unwrap();
        assert_eq!(
            terminal.backend().buffer()[(area.x, area.y)].fg,
            Color::White,
            "{}",
            page.label()
        );
    }
}

#[test]
fn exit_dialog_hover_changes_focus_over_idle_and_clears() {
    let mut app = test_profile_app();
    app.screen = Screen::Idle;
    app.exit = true;
    let mut terminal = Terminal::new(ratatui::backend::TestBackend::new(120, 40)).unwrap();
    for value in [true, false] {
        terminal
            .draw(|frame| app.hits = draw_content(frame, &app))
            .unwrap();
        let area = app
            .hits
            .iter()
            .find(|hit| hit.control == Control::Exit(value))
            .unwrap()
            .area;
        app.mouse(MouseEvent {
            kind: MouseEventKind::Moved,
            column: area.x + 1,
            row: area.y + 1,
            modifiers: KeyModifiers::NONE,
        });
        assert_eq!(app.exit_confirm, value);
        terminal
            .draw(|frame| app.hits = draw_content(frame, &app))
            .unwrap();
        app.mouse(MouseEvent {
            kind: MouseEventKind::Moved,
            column: 119,
            row: 0,
            modifiers: KeyModifiers::NONE,
        });
        terminal
            .draw(|frame| app.hits = draw_content(frame, &app))
            .unwrap();
        assert_eq!(
            terminal.backend().buffer()[(area.x, area.y)].fg,
            Color::White
        );
    }
}

#[test]
fn hovering_controls_focuses_without_opening_saving_or_submitting() {
    let mut app = test_profile_app();
    app.screen = Screen::SettingsPage(SettingsPage::Appearance);
    let original = app.connections.accent.clone();
    let mut terminal = Terminal::new(ratatui::backend::TestBackend::new(120, 40)).unwrap();
    terminal
        .draw(|frame| app.hits = draw_content(frame, &app))
        .unwrap();
    let area = app
        .hits
        .iter()
        .find(|hit| hit.control == Control::Accent(1))
        .unwrap()
        .area;
    app.mouse(MouseEvent {
        kind: MouseEventKind::Moved,
        column: area.x + 1,
        row: area.y + 1,
        modifiers: KeyModifiers::NONE,
    });
    assert_eq!(app.settings_selected, 2);
    assert_eq!(app.connections.accent, original);
    assert!(app.nodes_requested.is_none());
    assert!(matches!(
        app.screen,
        Screen::SettingsPage(SettingsPage::Appearance)
    ));
    app.screen = Screen::Nodes;
    terminal
        .draw(|frame| app.hits = draw_content(frame, &app))
        .unwrap();
    let area = app
        .hits
        .iter()
        .find(|hit| hit.control == Control::Settings)
        .unwrap()
        .area;
    app.mouse(MouseEvent {
        kind: MouseEventKind::Moved,
        column: area.x + 1,
        row: area.y + 1,
        modifiers: KeyModifiers::NONE,
    });
    assert!(matches!(app.screen, Screen::Nodes));
}

#[test]
fn selected_button_uses_accent_without_a_background() {
    let mut terminal = Terminal::new(ratatui::backend::TestBackend::new(50, 10)).unwrap();
    let area = Rect::new(3, 2, 20, 3);
    terminal
        .draw(|frame| draw_button_color(frame, area, "Save", true, Color::Rgb(255, 187, 116)))
        .unwrap();
    let buffer = terminal.backend().buffer();
    for y in area.top()..area.bottom() {
        for x in area.left()..area.right() {
            assert_eq!(buffer[(x, y)].bg, Color::Reset);
            assert_eq!(buffer[(x, y)].fg, Color::Rgb(255, 187, 116));
        }
    }
}

#[test]
fn access_request_decision_requires_explicit_confirmation() {
    let mut app = test_profile_app();
    app.screen = Screen::Access;
    app.access.tab = crate::access::Tab::Requests;
    app.access.card = Some(
        serde_json::json!({"id": "7aa330f4-264c-47fd-915f-1234a8c4a072",
            "telegram_user_id": 123, "username": "alice", "status": "pending"}),
    );
    let mut terminal = Terminal::new(ratatui::backend::TestBackend::new(100, 35)).unwrap();
    terminal
        .draw(|frame| app.hits = draw_content(frame, &app))
        .unwrap();
    let area = app
        .hits
        .iter()
        .find(|hit| hit.control == Control::AccessDecision(true))
        .unwrap()
        .area;
    app.mouse(MouseEvent {
        kind: MouseEventKind::Moved,
        column: area.x + 1,
        row: area.y + 1,
        modifiers: KeyModifiers::NONE,
    });
    assert!(app.access.decision.is_none() && app.nodes_requested.is_none());
    app.activate(Control::AccessDecision(true));
    assert_eq!(app.access.decision, Some(true));
    assert!(app.nodes_requested.is_none());
    app.activate(Control::AccessConfirm);
    assert!(matches!(
        app.nodes_requested,
        Some(crate::nodes::Command::Access(
            crate::access::Command::Decision { approve: true, .. }
        ))
    ));
}

#[test]
fn result_close_returns_to_actions_for_success_and_failure_and_preserves_profile() {
    for action in Action::ALL {
        for outcome in [Ok("Done".into()), Err("Unconfirmed operation".into())] {
            let mut app = test_profile_app();
            let state = tempfile::tempdir().unwrap();
            app.state_dir = state.path().into();
            app.connections.save(&app.state_dir).unwrap();
            app.form.select_action(action);
            let profile_id = app.connections.selected;
            let saved = app.form.saved.clone();
            app.stage = "Previous step".into();
            app.error = "Previous error".into();
            app.result_scroll = 12;
            app.password.push_str("SECRET_PASSWORD");
            app.form.fields[6] = "SECRET_TOKEN".into();
            let installed = action == Action::Install && outcome.is_ok();
            let updated = action == Action::Update && outcome.is_ok();
            app.event(Event::Finished(outcome));
            let mut terminal = Terminal::new(ratatui::backend::TestBackend::new(120, 40)).unwrap();
            terminal
                .draw(|frame| app.hits = draw_content(frame, &app))
                .unwrap();
            let close = app
                .hits
                .iter()
                .find(|hit| matches!(hit.control, Control::Close))
                .unwrap()
                .area;
            let key = click(&mut app, (close.x + 1, close.y + 1)).unwrap();
            assert!(app.close_result(key));
            assert!(if installed || updated {
                matches!(app.screen, Screen::Controller)
            } else {
                matches!(app.screen, Screen::Form)
            });
            assert!(!app.exit && !app.quick_focus);
            assert_eq!(app.connections.selected, profile_id);
            let mut expected = saved;
            if installed && let Some(profile) = &mut expected {
                profile.installed = true;
            }
            assert_eq!(app.form.saved, expected);
            assert_eq!(app.form.selected, 0);
            assert!(app.outcome.is_none() && app.update.is_none());
            if updated {
                assert!(matches!(
                    app.nodes_requested,
                    Some(crate::nodes::Command::Controller(
                        _,
                        crate::controller::Command::Updates
                    ))
                ));
            }
            if installed {
                assert_eq!(
                    app.controller.profile,
                    app.form.saved.as_ref().map(|p| p.id)
                );
                assert!(matches!(
                    app.nodes_requested,
                    Some(crate::nodes::Command::Controller(
                        _,
                        crate::controller::Command::Overview
                    ))
                ));
            }
            assert!(app.stage.is_empty() && app.error.is_empty());
            assert!(app.password.is_empty() && app.form.fields[6].is_empty());
            assert_eq!(app.result_scroll, 0);
            terminal
                .draw(|frame| app.hits = draw_content(frame, &app))
                .unwrap();
            assert!(
                app.hits
                    .iter()
                    .any(|hit| matches!(hit.control, Control::Action(_)))
            );
        }
    }
}

#[test]
fn result_escape_returns_home_and_ctrl_c_requires_exit_confirmation() {
    let mut app = test_profile_app();
    app.form.action = Action::Update;
    app.event(Event::Finished(Ok("Done".into())));
    assert!(!app.close_result(KeyEvent::new(KeyCode::Char('c'), KeyModifiers::CONTROL)));
    assert!(app.exit && !app.exit_confirm && matches!(app.screen, Screen::Finished));
    app.exit = false;
    assert!(app.close_result(KeyEvent::new(KeyCode::Esc, KeyModifiers::NONE)));
    assert!(matches!(app.screen, Screen::Controller) && !app.exit);
}

#[test]
fn automatic_settings_return_loads_and_displays_controller_statistics() {
    for key in [KeyCode::Enter, KeyCode::Esc] {
        let mut app = test_profile_app();
        let state = tempfile::tempdir().unwrap();
        app.state_dir = state.path().into();
        app.form.select_action(Action::Install);
        app.event(Event::Finished(Ok("Done".into())));
        assert!(app.close_result(KeyEvent::new(key, KeyModifiers::NONE)));
        let command = app.nodes_requested.take().unwrap();
        assert!(matches!(
            command,
            crate::nodes::Command::Controller(_, crate::controller::Command::Overview)
        ));
        app.event(Event::Nodes(crate::nodes::Update::Controller(app.controller.profile,
            crate::controller::Command::Overview,
            serde_json::json!({"supported":true,"counts":{"accounts":2,"profiles":3,"nodes":4},"latest_job":null}))));
        let mut terminal = Terminal::new(ratatui::backend::TestBackend::new(120, 40)).unwrap();
        terminal
            .draw(|frame| app.hits = draw_content(frame, &app))
            .unwrap();
        let text: String = terminal
            .backend()
            .buffer()
            .content()
            .iter()
            .map(|cell| cell.symbol())
            .collect();
        assert!(text.contains("Accounts: 2 · Profiles: 3 · Nodes: 4"));
        assert!(!text.contains("Loading installation…"));
    }
}

#[test]
fn subsequent_actions_keep_state_directory_and_stop_signal_without_reusing_operation() {
    let mut app = test_profile_app();
    let directory = tempfile::tempdir().unwrap();
    app.state_dir = directory.path().into();
    app.event(Event::Finished(Ok("Done".into())));
    app.close_result(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
    app.form.select_action(Action::Diagnose);
    let mut request = app.next_request();
    app.form.apply(&mut request).unwrap();
    assert_eq!(request.state_dir, directory.path());
    assert!(Arc::ptr_eq(&request.workflow.stop, &app.stop));
    assert!(request.workflow.operation.is_none());
    assert!(request.bot_token.is_empty());
    assert_eq!(request.host, "controller.example");
    assert!(!request.workflow.stop.load(Ordering::Relaxed));
}
#[test]
fn mouse_selects_sidebar_and_fields_and_preserves_hidden_credentials() {
    let mut app = test_profile_app();
    let mut terminal = Terminal::new(ratatui::backend::TestBackend::new(120, 40)).unwrap();
    terminal
        .draw(|frame| app.hits = draw_content(frame, &app))
        .unwrap();
    let action = app
        .hits
        .iter()
        .find(|hit| matches!(hit.control, Control::Action(Action::PrepareNode)))
        .unwrap()
        .area;
    assert!(click(&mut app, (action.x + 1, action.y + 1)).is_none());
    assert!(app.form.action == Action::PrepareNode);
    assert!(matches!(app.screen, Screen::Nodes));
    assert!(matches!(
        app.nodes_requested.take(),
        Some(crate::nodes::Command::List)
    ));
    app.screen = Screen::Form;
    app.form.selected = 1;
    terminal
        .draw(|frame| app.hits = draw_content(frame, &app))
        .unwrap();
    let target_position = app
        .form
        .visible()
        .iter()
        .position(|&index| index == 8)
        .unwrap()
        + 1;
    let field = app
        .hits
        .iter()
        .find(|hit| matches!(hit.control, Control::Field(index) if index == target_position))
        .unwrap()
        .area;
    click(&mut app, (field.x + 1, field.y + 1));
    app.form.append("node.example");
    assert_eq!(app.form.fields[8], "node.example");
    assert!(app.form.fields[6].is_empty());
    let button = app
        .hits
        .iter()
        .find(|hit| matches!(hit.control, Control::Continue))
        .unwrap()
        .area;
    assert_eq!(
        click(&mut app, (button.x + 1, button.y + 1)).unwrap().code,
        KeyCode::Enter
    );
    let previous = app.form.fields.clone();
    app.quick_focus = true;
    assert_eq!(
        click(&mut app, (button.x + 1, button.y + 1)).unwrap().code,
        KeyCode::Enter
    );
    assert!(!app.quick_focus);
    app.form.append("not-a-field");
    assert_eq!(app.form.fields, previous);
}
#[test]
fn modal_mouse_controls_cannot_activate_background_or_implicitly_trust_host() {
    let mut app = App::new(&test_request());
    let (tx, rx) = mpsc::channel();
    app.event(Event::Prompt(
        Prompt::HostKey {
            host: "controller.example".into(),
            fingerprint: "SHA256:test".into(),
        },
        tx,
    ));
    let mut terminal = Terminal::new(ratatui::backend::TestBackend::new(120, 40)).unwrap();
    terminal
        .draw(|frame| app.hits = draw_content(frame, &app))
        .unwrap();
    assert!(
        app.hits
            .iter()
            .all(|hit| matches!(hit.control, Control::Prompt(_)))
    );
    assert!(click(&mut app, (2, 5)).is_none());
    assert!(app.prompt.is_some());
    assert!(!app.trust);
    let cancel = app
        .hits
        .iter()
        .find(|hit| matches!(hit.control, Control::Prompt(false)))
        .unwrap()
        .area;
    let key = click(&mut app, (cancel.x + 1, cancel.y + 1)).unwrap();
    app.prompt_key(key);
    assert!(matches!(rx.recv().unwrap(), Answer::Confirm(false)));
}
#[test]
fn selected_fields_remain_clickable_after_resize_and_keyboard_navigation() {
    let mut app = test_profile_app();
    for (width, height) in [(120, 40), (80, 24), (50, 16), (20, 8)] {
        app.form.selected = app.form.visible().len();
        let mut terminal =
            Terminal::new(ratatui::backend::TestBackend::new(width, height)).unwrap();
        terminal
            .draw(|frame| app.hits = draw_content(frame, &app))
            .unwrap();
        if width >= 40 && height >= 16 {
            assert!(
                app.hits
                    .iter()
                    .any(|hit| matches!(hit.control, Control::Field(n) if n == app.form.selected))
            );
        }
        for hit in &app.hits {
            assert!(hit.area.right() <= width && hit.area.bottom() <= height);
        }
    }
}
fn saved_installation(host: &str) -> Installation {
    Installation {
        installed: false,
        id: uuid::Uuid::new_v4(),
        name: "Home installation".into(),
        host: host.into(),
        port: 2222,
        user: "admin".into(),
        branch: "dev".into(),
        admin_ids: "42".into(),
        account: "42".into(),
    }
}
#[test]
fn saved_installations_restore_and_hide_filled_fields_without_saving_secrets() {
    let dir = tempfile::tempdir().unwrap();
    let mut request = test_request();
    request.state_dir = dir.path().into();
    request.host.clear();
    let profile = saved_installation("one.example");
    let mut store = Connections {
        selected: Some(profile.id),
        installations: vec![profile.clone()],
        ..Connections::default()
    };
    store.save(dir.path()).unwrap();
    let mut app = App::new(&request);
    app.load_connections(&request).unwrap();
    assert_eq!(app.form.fields[0], "one.example");
    assert_eq!(app.form.visible(), vec![4]);
    app.form.fields[6] = "TOP_SECRET_TOKEN".into();
    app.form.fields[4] = "v0.4.3-alpha.48".into();
    app.remember_form().unwrap();
    let text = std::fs::read_to_string(dir.path().join("installations.json")).unwrap();
    assert!(!text.contains("TOP_SECRET_TOKEN") && !text.contains("v0.4.3-alpha.48"));
    let mut restarted = App::new(&request);
    restarted.load_connections(&request).unwrap();
    assert_eq!(restarted.form.fields[4], "");
    restarted.form.select_action(Action::Diagnose);
    assert_eq!(restarted.form.visible(), vec![11]);
    restarted.form.select_action(Action::Install);
    assert_eq!(restarted.form.visible(), vec![6, 4]);
}
#[test]
fn switching_installations_drops_target_and_secret_and_does_not_reuse_another_update() {
    let mut request = test_request();
    request.workflow.operation = Some(uuid::Uuid::new_v4());
    let mut form = Form::new(&request);
    form.fields[6] = "TOKEN".into();
    form.fields[8] = "previous-target.example".into();
    let profile = saved_installation("other.example");
    form.use_installation(&profile);
    assert!(form.fields[6].is_empty() && form.fields[8].is_empty());
    form.apply(&mut request).unwrap();
    assert!(request.workflow.operation.is_none());
    assert_eq!(request.host, profile.host);
}
#[test]
fn settings_is_anchored_and_navigation_selection_has_no_fill() {
    let mut app = test_profile_app();
    app.form.action = Action::Update;
    let mut terminal = Terminal::new(ratatui::backend::TestBackend::new(120, 40)).unwrap();
    terminal
        .draw(|frame| app.hits = draw_content(frame, &app))
        .unwrap();
    let settings = app
        .hits
        .iter()
        .find(|hit| matches!(hit.control, Control::Settings))
        .unwrap()
        .area;
    assert_eq!(settings.bottom(), 36);
    let action = app
        .hits
        .iter()
        .find(|hit| matches!(hit.control, Control::Action(Action::Update)))
        .unwrap()
        .area;
    let buffer = terminal.backend().buffer();
    assert_eq!(buffer[(action.x, action.y)].fg, Color::Cyan);
    assert_eq!(buffer[(action.x + 1, action.y + 1)].bg, Color::Reset);
    app.mouse(MouseEvent {
        kind: MouseEventKind::Moved,
        column: settings.x + 1,
        row: settings.y + 1,
        modifiers: KeyModifiers::NONE,
    });
    assert!(matches!(app.screen, Screen::Form));
    assert!(click(&mut app, (settings.x + 1, settings.y + 1)).is_none());
    assert!(matches!(app.screen, Screen::Settings));
    app.navigate(false);
    assert!(matches!(app.screen, Screen::Form) && app.form.action == Action::Install);
}
#[test]
fn installation_editor_can_create_modify_and_select_a_saved_connection() {
    let dir = tempfile::tempdir().unwrap();
    let mut request = test_request();
    request.state_dir = dir.path().into();
    let mut app = App::new(&request);
    app.begin_edit(true);
    app.editor.as_mut().unwrap().1[0] = "Home".into();
    app.save_edit().unwrap();
    let original_id = app.connections.active().unwrap().id;
    app.begin_edit(false);
    app.editor.as_mut().unwrap().1[1] = "changed.example".into();
    app.save_edit().unwrap();
    assert_eq!(app.connections.installations.len(), 1);
    assert_eq!(app.connections.active().unwrap().id, original_id);
    app.use_selected().unwrap();
    assert_eq!(app.form.fields[0], "changed.example");
    assert!(matches!(app.screen, Screen::Form));
    request.host.clear();
    let mut restarted = App::new(&request);
    restarted.load_connections(&request).unwrap();
    assert_eq!(restarted.form.fields[0], "changed.example");
}
#[test]
fn new_profile_disables_actions_and_enter_opens_creation() {
    let mut app = App::new(&test_request());
    let mut terminal = Terminal::new(ratatui::backend::TestBackend::new(120, 40)).unwrap();
    terminal
        .draw(|frame| app.hits = draw_content(frame, &app))
        .unwrap();
    assert!(!app.actions_enabled());
    assert!(app.hits.iter().all(|hit| matches!(
        hit.control,
        Control::QuickProfile | Control::CycleProfile(_) | Control::Settings | Control::WindowClose
    )));
    app.navigate(false);
    assert!(matches!(app.screen, Screen::Settings));
    app.navigate(true);
    assert!(app.quick_focus && matches!(app.screen, Screen::Form));
    app.activate_profile();
    assert!(matches!(app.screen, Screen::EditInstallation));
    assert!(app.editor.as_ref().unwrap().0.is_none());
}
#[test]
fn session_information_is_read_only_and_distinguishes_local_identity() {
    let dir = tempfile::tempdir().unwrap();
    let mut app = test_profile_app();
    app.state_dir = dir.path().into();
    app.form.fields[6] = "SECRET_TOKEN".into();
    app.password = Zeroizing::new("SECRET_PASSWORD".into());
    app.open_settings();
    let mut terminal = Terminal::new(ratatui::backend::TestBackend::new(120, 40)).unwrap();
    terminal
        .draw(|frame| app.hits = draw_content(frame, &app))
        .unwrap();
    let info = app
        .hits
        .iter()
        .find(|hit| matches!(hit.control, Control::SettingsPage(SettingsPage::Session)))
        .unwrap()
        .area;
    click(&mut app, (info.x + 1, info.y + 1));
    assert!(matches!(
        app.screen,
        Screen::SettingsPage(SettingsPage::Session)
    ));
    terminal
        .draw(|frame| app.hits = draw_content(frame, &app))
        .unwrap();
    let text: String = terminal
        .backend()
        .buffer()
        .content
        .iter()
        .map(|c| c.symbol())
        .collect();
    assert!(text.contains(&app.session_id.to_string()));
    assert!(text.contains(&app.form.saved.as_ref().unwrap().id.to_string()));
    assert!(text.contains("Administrator selector"));
    assert!(!text.contains("SECRET_TOKEN") && !text.contains("SECRET_PASSWORD"));
    assert_eq!(std::fs::read_dir(dir.path()).unwrap().count(), 0);
    let back = app
        .hits
        .iter()
        .rev()
        .find(|hit| matches!(hit.control, Control::Settings))
        .unwrap()
        .area;
    click(&mut app, (back.x + 1, back.y + 1));
    assert!(matches!(app.screen, Screen::Settings));
}
#[test]
fn new_profile_defaults_to_dev_and_disabled_actions_are_dimmed() {
    let dir = tempfile::tempdir().unwrap();
    let mut app = test_profile_app();
    app.state_dir = dir.path().into();
    app.form.fields[3] = "main".into();
    app.begin_edit(true);
    assert_eq!(app.editor.as_ref().unwrap().1[4], "dev");
    app.screen = Screen::Form;
    let mut terminal = Terminal::new(ratatui::backend::TestBackend::new(120, 40)).unwrap();
    terminal
        .draw(|frame| app.hits = draw_content(frame, &app))
        .unwrap();
    assert!(
        terminal
            .backend()
            .buffer()
            .content
            .iter()
            .any(|c| c.fg == Color::DarkGray)
    );
    assert!(
        !app.hits
            .iter()
            .any(|hit| matches!(hit.control, Control::Action(_)))
    );
    app.begin_edit(true);
    assert_eq!(app.editor.as_ref().unwrap().1[4], "dev");
}
#[test]
fn quick_switcher_wraps_profiles_and_new_selection_survives_restart() {
    let dir = tempfile::tempdir().unwrap();
    let mut request = test_request();
    request.state_dir = dir.path().into();
    request.host.clear();
    let one = saved_installation("one.example");
    let mut two = saved_installation("two.example");
    two.name = "Second".into();
    let mut store = Connections {
        selected: Some(one.id),
        installations: vec![one.clone(), two.clone()],
        ..Connections::default()
    };
    store.save(dir.path()).unwrap();
    let mut app = App::new(&request);
    app.load_connections(&request).unwrap();
    assert_eq!(app.profile_index(), 0);
    app.form.fields[6] = "SECRET".into();
    app.form.fields[8] = "old-target.example".into();
    app.cycle_profile(false).unwrap();
    assert_eq!(app.form.saved.as_ref().unwrap().id, two.id);
    assert!(matches!(app.screen, Screen::Idle));
    app.event(Event::Nodes(crate::nodes::Update::Controller(
        Some(two.id),
        crate::controller::Command::Probe,
        serde_json::json!({"installed":true}),
    )));
    assert!(matches!(app.screen, Screen::Idle));
    assert!(app.form.fields[6].is_empty() && app.form.fields[8].is_empty());
    app.cycle_profile(false).unwrap();
    assert!(!app.actions_enabled());
    let mut restarted = App::new(&request);
    restarted.load_connections(&request).unwrap();
    assert!(!restarted.actions_enabled());
    assert!(Connections::load(dir.path()).unwrap().selected.is_none());
    app.cycle_profile(false).unwrap();
    assert_eq!(app.form.saved.as_ref().unwrap().id, one.id);
    app.cycle_profile(true).unwrap();
    assert!(!app.actions_enabled());
    app.activate_profile();
    let editor = app.editor.as_mut().unwrap();
    editor.1[0] = "Third".into();
    editor.1[1] = "three.example".into();
    app.save_edit().unwrap();
    assert!(app.actions_enabled());
    assert_eq!(app.connections.installations.len(), 3);
    assert_eq!(app.connections.active().unwrap().host, "three.example");
}
#[test]
fn quick_switcher_arrow_hits_work_and_compact_sidebar_keeps_all_actions() {
    let dir = tempfile::tempdir().unwrap();
    let mut app = test_profile_app();
    app.state_dir = dir.path().into();
    app.connections.save(dir.path()).unwrap();
    let mut terminal = Terminal::new(ratatui::backend::TestBackend::new(80, 24)).unwrap();
    terminal
        .draw(|frame| app.hits = draw_content(frame, &app))
        .unwrap();
    assert_eq!(
        app.hits
            .iter()
            .filter(|hit| matches!(hit.control, Control::Action(_)))
            .count(),
        4
    );
    let right = app
        .hits
        .iter()
        .find(|hit| matches!(hit.control, Control::CycleProfile(false)))
        .unwrap()
        .area;
    assert_eq!(right.bottom(), 17);
    click(&mut app, (right.x + 1, right.y + 1));
    assert!(!app.actions_enabled());
    terminal
        .draw(|frame| app.hits = draw_content(frame, &app))
        .unwrap();
    assert!(
        !app.hits
            .iter()
            .any(|hit| matches!(hit.control, Control::Action(_) | Control::Continue))
    );
    let label = app
        .hits
        .iter()
        .find(|hit| matches!(hit.control, Control::QuickProfile))
        .unwrap()
        .area;
    click(&mut app, (label.x + 1, label.y + 1));
    assert!(matches!(app.screen, Screen::EditInstallation));
}
#[test]
fn new_node_wizard_keyboard_and_mouse_offer_card_without_installing() {
    let mut app = test_profile_app();
    app.open_nodes();
    app.nodes_requested = None;
    let mut terminal = Terminal::new(ratatui::backend::TestBackend::new(100, 30)).unwrap();
    terminal.draw(|f| app.hits = draw(f, &app)).unwrap();
    assert!(
        app.hits
            .iter()
            .any(|h| matches!(h.control, Control::NodesNew))
    );
    assert!(
        !terminal
            .backend()
            .buffer()
            .content
            .iter()
            .map(|c| c.symbol())
            .collect::<String>()
            .contains("Prepare SSH")
    );
    app.activate(Control::NodesNew);
    assert!(matches!(
        app.nodes_requested.take(),
        Some(crate::nodes::Command::CreationOptions)
    ));
    app.nodes_busy = true;
    app.event(Event::Nodes(crate::nodes::Update::CreationOptions(serde_json::from_value(serde_json::json!({
            "local_available":false,"defaults":{"protocols":["awg"],"xray_transports":[],"settings":{"awg_i1_preset":"quic","awg_port_mode":"auto"}},
            "templates":[{"code":"lv","title":"Latvia","region":"Europe","flag":"🇱🇻","draft":{"key":"lv1","title":"Latvia #1"}}]})).unwrap())));
    app.event(Event::Finished(Ok("Options ready".into())));
    assert!(matches!(app.screen, Screen::NodeWizard));
    app.wizard_key(KeyCode::Enter.into());
    app.wizard_key(KeyCode::Enter.into());
    app.node_wizard
        .as_mut()
        .unwrap()
        .append("root@node.example");
    app.wizard_key(KeyCode::Enter.into());
    app.node_wizard.as_mut().unwrap().append("vpn.example");
    app.wizard_key(KeyCode::Enter.into());
    app.wizard_key(KeyCode::Esc.into());
    assert_eq!(app.node_wizard.as_ref().unwrap().host, "vpn.example");
    app.wizard_key(KeyCode::Enter.into());
    app.activate(Control::WizardNext);
    app.activate(Control::WizardNext);
    assert!(matches!(
        app.nodes_requested.take(),
        Some(crate::nodes::Command::Create(_))
    ));
    app.nodes_busy = true;
    let mut item = browser_node("lv1", "Europe");
    item.title = "Latvia #1".into();
    app.event(Event::Nodes(crate::nodes::Update::Created(item)));
    app.event(Event::Finished(Ok("Created".into())));
    terminal.draw(|f| app.hits = draw(f, &app)).unwrap();
    assert!(
        app.hits
            .iter()
            .any(|h| matches!(h.control, Control::WizardOpen))
    );
    assert!(
        !app.hits
            .iter()
            .any(|h| matches!(h.control, Control::NodesSetup))
    );
    app.wizard_key(KeyCode::Enter.into());
    assert!(
        matches!(app.nodes_requested.take(),Some(crate::nodes::Command::Card(key)) if key == "lv1")
    );
    assert!(matches!(app.screen, Screen::Nodes));
}
fn browser_node(key: &str, region: &str) -> crate::nodes::Node {
    serde_json::from_value(serde_json::json!({"key":key, "title":key, "region":region,
            "flag":"", "enabled":true, "protocols":["awg"], "xray_transports":[],
            "desired_revision":1, "applied_revision":0, "transport":"local",
            "overview":{"state":"not_installed", "settings_complete":true}}))
    .unwrap()
}
#[test]
fn nodes_resize_search_and_fold_remain_local_without_new_requests() {
    let mut app = test_profile_app();
    app.activate(Control::Action(Action::PrepareNode));
    assert!(matches!(
        app.nodes_requested.take(),
        Some(crate::nodes::Command::List)
    ));
    app.event(Event::Nodes(crate::nodes::Update::List(
        (0..40)
            .map(|i| browser_node(&format!("n{i:02}"), if i < 20 { "Europe" } else { "Asia" }))
            .collect(),
    )));
    let mut capacities = Vec::new();
    for (width, height) in [(80, 24), (120, 40), (50, 16)] {
        let mut terminal =
            Terminal::new(ratatui::backend::TestBackend::new(width, height)).unwrap();
        terminal
            .draw(|frame| app.hits = draw_content(frame, &app))
            .unwrap();
        capacities.push(app.nodes_capacity.get());
    }
    assert!(capacities[1] > capacities[0]);
    app.nodes_key(KeyEvent::new(KeyCode::Char('n'), KeyModifiers::NONE));
    app.nodes_key(KeyEvent::new(KeyCode::Char('3'), KeyModifiers::NONE));
    assert_eq!(app.nodes.search, "n3");
    assert!(app.nodes_requested.is_none());
    app.activate(Control::NodesRegion(25));
    assert!(app.nodes.collapsed.contains("Asia"));
    assert!(app.nodes_requested.is_none());
}
#[test]
fn agent_only_card_switches_to_protocol_installation_after_protocol_is_added() {
    let mut app = test_profile_app();
    app.activate(Control::Action(Action::PrepareNode));
    app.nodes_requested = None;
    let mut node = browser_node("lv1", "Europe");
    node.protocols.clear();
    node.agent = crate::nodes::AgentState::Missing;
    node.overview.as_mut().unwrap()["state"] = serde_json::json!("agent_only");
    node.overview.as_mut().unwrap()["settings_complete"] = serde_json::json!(false);
    app.nodes.card = Some(node.clone());
    let mut terminal = Terminal::new(ratatui::backend::TestBackend::new(100, 30)).unwrap();
    terminal
        .draw(|frame| app.hits = draw_content(frame, &app))
        .unwrap();
    assert!(app.nodes_controls().contains(&Control::NodesSetup));
    assert!(!app.nodes_controls().contains(&Control::NodesBootstrap));
    node.agent = crate::nodes::AgentState::Ready;
    app.nodes.card = Some(node.clone());
    terminal
        .draw(|frame| app.hits = draw_content(frame, &app))
        .unwrap();
    assert!(!app.nodes_controls().contains(&Control::NodesSetup));
    node.protocols.push("awg".into());
    node.overview.as_mut().unwrap()["state"] = serde_json::json!("not_installed");
    node.overview.as_mut().unwrap()["settings_complete"] = serde_json::json!(true);
    app.nodes.card = Some(node);
    terminal
        .draw(|frame| app.hits = draw_content(frame, &app))
        .unwrap();
    assert!(app.nodes_controls().contains(&Control::NodesBootstrap));
    assert!(!app.nodes_controls().contains(&Control::NodesSetup));
    assert!(
        terminal
            .backend()
            .buffer()
            .content
            .iter()
            .map(|c| c.symbol())
            .collect::<String>()
            .contains("Install protocols")
    );
}

#[test]
fn node_card_bootstrap_checks_prerequisites_in_worker_and_hides_mutations_while_busy() {
    let mut app = test_profile_app();
    app.activate(Control::Action(Action::PrepareNode));
    app.nodes_requested = None;
    let mut node = browser_node("lv1", "Europe");
    app.nodes.card = Some(node.clone());
    let mut terminal = Terminal::new(ratatui::backend::TestBackend::new(100, 30)).unwrap();
    terminal
        .draw(|frame| app.hits = draw_content(frame, &app))
        .unwrap();
    assert!(
        app.nodes_controls()
            .iter()
            .any(|c| matches!(c, Control::NodesBootstrap))
    );
    node.services =
        Some(serde_json::json!({"docker":true,"awg_running":false,"xray_running":false}));
    app.nodes.card = Some(node.clone());
    terminal
        .draw(|frame| app.hits = draw_content(frame, &app))
        .unwrap();
    assert!(
        app.nodes_controls()
            .iter()
            .any(|c| matches!(c, Control::NodesBootstrap))
    );
    app.activate(Control::NodesRefresh);
    assert!(app.nodes.card.as_ref().unwrap().services.is_some());
    assert!(matches!(
        app.nodes_requested.take(),
        Some(crate::nodes::Command::Card(_))
    ));
    app.nodes.card = Some(node.clone());
    app.nodes.operation = Some(crate::nodes::Operation {
        id: uuid::Uuid::new_v4(),
        kind: "node-jobs".into(),
        action: "bootstrap".into(),
        node_key: node.key,
        status: "awaiting_executor".into(),
        error: String::new(),
        command_id: None,
    });
    terminal
        .draw(|frame| app.hits = draw_content(frame, &app))
        .unwrap();
    assert!(
        app.nodes_controls()
            .iter()
            .any(|c| matches!(c, Control::NodesRefresh))
    );
    assert!(
        !app.nodes_controls()
            .iter()
            .any(|c| matches!(c, Control::NodesBootstrap | Control::NodesSetup))
    );
    app.nodes_key(KeyEvent::new(KeyCode::Esc, KeyModifiers::NONE));
    assert!(app.nodes.card.is_none());
    assert!(app.nodes_requested.is_none());
}
#[test]
fn sidebar_right_enters_content_and_settings_escape_returns_to_settings() {
    let mut app = test_profile_app();
    app.screen = Screen::Form;
    app.quick_focus = false;
    app.form.selected = 0;
    assert!(app.section_navigation(KeyCode::Right));
    assert_eq!(app.form.selected, 1);
    assert!(!app.section_navigation(KeyCode::Right));
    app.open_settings();
    assert!(app.section_navigation(KeyCode::Right));
    assert_eq!(app.settings_selected, 1);
    for (index, page) in SettingsPage::ALL.into_iter().enumerate() {
        app.activate(Control::SettingsPage(page));
        assert!(app.section_navigation(KeyCode::Esc));
        assert!(matches!(app.screen, Screen::Settings));
        assert_eq!(app.settings_selected, index + 1);
        assert!(!app.exit);
    }
    assert!(app.section_navigation(KeyCode::Esc));
    assert!(matches!(app.screen, Screen::Idle));
    app.activate(Control::Action(Action::PrepareNode));
    app.nodes_requested = None;
    app.nodes_sidebar = true;
    app.nodes_key(KeyEvent::new(KeyCode::Right, KeyModifiers::NONE));
    assert!(!app.nodes_sidebar);
    assert!(matches!(app.screen, Screen::Nodes));
}
#[test]
fn node_card_arrows_follow_button_geometry_after_resize() {
    let mut app = test_profile_app();
    app.activate(Control::Action(Action::PrepareNode));
    app.nodes_requested = None;
    let mut node = browser_node("lv1", "Europe");
    node.transport = Some("ssh".into());
    node.overview.as_mut().unwrap()["state"] = serde_json::json!("installed");
    node.agent = crate::nodes::AgentState::Missing;
    node.services =
        Some(serde_json::json!({"docker":true,"awg_running":false,"xray_running":false}));
    app.nodes.card = Some(node);
    for (width, height) in [(100, 30), (80, 24)] {
        let mut terminal =
            Terminal::new(ratatui::backend::TestBackend::new(width, height)).unwrap();
        terminal
            .draw(|frame| app.hits = draw_content(frame, &app))
            .unwrap();
        let count = app.nodes_controls().len();
        let areas: Vec<_> = app.nodes_hits().iter().map(|hit| hit.area).collect();
        assert_eq!(count, 6);
        let row_starts: Vec<_> = (0..count - 1)
            .filter(|&i| i == 0 || areas[i].y != areas[i - 1].y)
            .collect();
        for (row, &begin) in row_starts.iter().enumerate() {
            let end = row_starts.get(row + 1).copied().unwrap_or(count - 1);
            assert_eq!(areas[begin].x, areas[0].x);
            assert_eq!(areas[end - 1].right(), areas[row_starts[1] - 1].right());
            app.nodes_selected = begin;
            for next in begin + 1..end {
                app.nodes_key(KeyCode::Right.into());
                assert_eq!(app.nodes_selected, next);
            }
            app.nodes_key(KeyCode::Right.into());
            assert_eq!(app.nodes_selected, end - 1);
        }
        app.nodes_selected = 0;
        app.nodes_key(KeyCode::Down.into());
        assert!(areas[app.nodes_selected].y > areas[0].y);
        app.nodes_key(KeyCode::Up.into());
        assert_eq!(app.nodes_selected, 0);
        app.nodes_selected = count - 1;
        app.nodes_key(KeyCode::Down.into());
        assert_eq!(app.nodes_selected, count - 1);
        app.nodes_key(KeyCode::Tab.into());
        assert_eq!(app.nodes_selected, 0);
        assert!(app.nodes_requested.is_none());
    }
}
#[test]
fn profile_actions_use_spatial_navigation_without_wrapping_rows() {
    let mut app = test_profile_app();
    app.activate(Control::SettingsPage(SettingsPage::Profiles));
    for (width, height) in [(80, 24), (120, 40)] {
        let mut terminal =
            Terminal::new(ratatui::backend::TestBackend::new(width, height)).unwrap();
        app.settings_selected = 2; // Use, after the one saved profile.
        terminal
            .draw(|frame| app.hits = draw_content(frame, &app))
            .unwrap();
        assert!(app.move_spatial(KeyCode::Right));
        assert_eq!(app.settings_selected, 3); // Edit, on the same row.
        app.settings_selected = 2;
        assert!(app.move_spatial(KeyCode::Down));
        assert_eq!(app.settings_selected, 5); // Copy, below Use.
        assert!(app.move_spatial(KeyCode::Down));
        assert_eq!(app.settings_selected, 7); // Pinned Back, below actions.
        assert!(app.move_spatial(KeyCode::Right));
        assert_eq!(app.settings_selected, 7); // No jump to another row.
        assert!(app.move_spatial(KeyCode::Up));
        assert_eq!(app.settings_selected, 6); // Delete, above Back.
        assert!(matches!(
            app.screen,
            Screen::SettingsPage(SettingsPage::Profiles)
        ));
    }
}
#[test]
fn empty_profiles_actions_fill_the_row_and_small_grids_stay_in_bounds() {
    for width in [30, 61, 100] {
        for count in 1..=7 {
            let area = Rect::new(3, 5, width, 6);
            let buttons = button_areas(area, count, 2);
            assert_eq!(buttons.len(), count);
            for row in [5, 8] {
                let cells: Vec<_> = buttons.iter().filter(|cell| cell.y == row).collect();
                if !cells.is_empty() {
                    assert_eq!(cells.first().unwrap().x, area.x);
                    assert_eq!(cells.last().unwrap().right(), area.right());
                    assert!(cells.iter().all(|cell| cell.bottom() <= area.bottom()));
                }
            }
        }
    }
    let mut app = test_profile_app();
    app.connections.installations.clear();
    app.activate(Control::SettingsPage(SettingsPage::Profiles));
    let mut terminal = Terminal::new(ratatui::backend::TestBackend::new(100, 30)).unwrap();
    terminal
        .draw(|frame| app.hits = draw_content(frame, &app))
        .unwrap();
    let new = app
        .hits
        .iter()
        .find(|hit| hit.control == Control::NewInstallation)
        .unwrap()
        .area;
    let back = app
        .hits
        .iter()
        .rfind(|hit| hit.control == Control::Settings)
        .unwrap()
        .area;
    assert!(back.y > new.y);
    assert_eq!(back.width, 7);
    app.settings_selected = 1;
    assert!(app.move_spatial(KeyCode::Down));
    assert_eq!(app.settings_selected, 2);
    assert!(app.move_spatial(KeyCode::Down));
    assert_eq!(app.settings_selected, 2);
}
#[test]
fn agent_setup_requires_confirmed_absence_and_ssh_status_keeps_layout_fixed() {
    let mut app = test_profile_app();
    app.activate(Control::Action(Action::PrepareNode));
    app.nodes_requested = None;
    let mut node = browser_node("lv1", "Europe");
    node.transport = Some("ssh".into());
    node.overview.as_mut().unwrap()["state"] = serde_json::json!("installed");
    let mut terminal = Terminal::new(ratatui::backend::TestBackend::new(100, 30)).unwrap();
    for state in [
        crate::nodes::AgentState::Unknown,
        crate::nodes::AgentState::Ready,
        crate::nodes::AgentState::Unavailable,
        crate::nodes::AgentState::Missing,
    ] {
        node.agent = state;
        app.nodes.card = Some(node.clone());
        terminal
            .draw(|frame| app.hits = draw_content(frame, &app))
            .unwrap();
        assert_eq!(
            app.nodes_controls()
                .iter()
                .any(|c| matches!(c, Control::NodesBootstrap)),
            state == crate::nodes::AgentState::Missing
        );
        assert!(
            !terminal
                .backend()
                .buffer()
                .content
                .iter()
                .map(|c| c.symbol())
                .collect::<String>()
                .contains("Prepare SSH")
        );
        assert!(
            !app.nodes_controls()
                .iter()
                .any(|control| matches!(control, Control::NodesSetup))
        );
    }
    let mut areas = None;
    app.connections.accent = "#ffbb74".into();
    for state in [
        crate::nodes::ConnectionState::Closed,
        crate::nodes::ConnectionState::Connecting,
        crate::nodes::ConnectionState::Connected,
    ] {
        app.ssh_state = state;
        terminal
            .draw(|frame| app.hits = draw_content(frame, &app))
            .unwrap();
        let current: Vec<_> = app.hits.iter().map(|hit| hit.area).collect();
        if let Some(previous) = &areas {
            assert_eq!(previous, &current);
        }
        areas = Some(current);
        let buffer = terminal.backend().buffer();
        let text: String = buffer.content.iter().map(|cell| cell.symbol()).collect();
        let label = app.header_status();
        assert!(text.contains(&label));
        let status: String = (2..2 + label.len() as u16)
            .map(|x| buffer[(x, 1)].symbol())
            .collect();
        assert_eq!(status, label);
        let close = app
            .hits
            .iter()
            .find(|hit| hit.control == Control::WindowClose)
            .unwrap()
            .area;
        for x in 0..buffer.area.width {
            if close.contains((x, 2).into()) {
                continue;
            }
            assert_eq!(buffer[(x, 2)].symbol(), "─");
            assert_eq!(buffer[(x, 2)].fg, Color::Rgb(255, 187, 116));
        }
    }
}
#[test]
fn nodes_worker_returns_to_browser_and_profile_switch_drops_cached_nodes() {
    let mut app = test_profile_app();
    app.activate(Control::Action(Action::PrepareNode));
    app.nodes_requested = None;
    app.nodes_busy = true;
    app.screen = Screen::Running;
    app.event(Event::Nodes(crate::nodes::Update::List(vec![
        browser_node("lv1", "Europe"),
    ])));
    app.event(Event::Finished(Ok("Ready".into())));
    assert!(matches!(app.screen, Screen::Nodes));
    assert!(!app.nodes_busy);
    assert!(app.outcome.is_none());
    app.form.saved.as_mut().unwrap().id = uuid::Uuid::new_v4();
    app.open_nodes();
    assert!(app.nodes_requested.is_none());
    let profile = app.form.saved.as_ref().map(|p| p.id);
    app.event(Event::Nodes(crate::nodes::Update::Controller(
        profile,
        crate::controller::Command::Probe,
        serde_json::json!({"installed":true}),
    )));
    app.open_nodes();
    assert!(app.nodes.items.is_empty());
    assert!(matches!(
        app.nodes_requested,
        Some(crate::nodes::Command::List)
    ));
}
#[test]
fn controller_cleanup_requires_exact_phrase_and_queues_once() {
    let mut app = App::new(&test_request());
    let plan = uuid::Uuid::new_v4();
    app.controller.plan =
        Some(serde_json::json!({"id":plan,"confirmation_phrase":"RESET NODE PLANE"}));
    app.controller.key = Some(uuid::Uuid::new_v4());
    app.controller.phrase = "RESET".into();
    app.activate(Control::ControllerConfirm);
    assert!(app.nodes_requested.is_none());
    app.controller.phrase = "RESET NODE PLANE".into();
    app.activate(Control::ControllerConfirm);
    assert!(matches!(
        app.nodes_requested,
        Some(crate::nodes::Command::Controller(
            _,
            crate::controller::Command::Queue { .. }
        ))
    ));
    app.nodes_requested = None;
    app.nodes_busy = true;
    app.activate(Control::ControllerConfirm);
    assert!(app.nodes_requested.is_none());
}
#[test]
fn controller_menu_scrolls_to_all_controls() {
    let mut app = App::new(&test_request());
    app.form.action = Action::Install;
    app.screen = Screen::Controller;
    app.controller.overview =
        Some(serde_json::json!({"supported":true,"counts":{"accounts":1,"profiles":2,"nodes":3}}));
    let mut terminal = Terminal::new(ratatui::backend::TestBackend::new(80, 16)).unwrap();
    for _ in 1..app.controller_controls().len() {
        terminal
            .draw(|frame| app.hits = draw_content(frame, &app))
            .unwrap();
        app.controller_key(KeyCode::Down.into());
    }
    terminal
        .draw(|frame| app.hits = draw_content(frame, &app))
        .unwrap();
    assert!(app.controller_controls()[app.controller.selected] == Control::ControllerBack);
    assert!(
        app.hits
            .iter()
            .any(|h| h.control == Control::ControllerBack)
    );
}
#[test]
fn new_installation_is_explicit_and_preserves_the_saved_profile_identity() {
    let mut app = test_profile_app();
    app.form.saved.as_mut().unwrap().installed = true;
    let id = app.form.saved.as_ref().unwrap().id;
    app.activate(Control::Action(Action::Install));
    assert!(matches!(app.screen, Screen::Controller));
    app.nodes_requested = None;
    app.activate(Control::ControllerNew);
    assert!(matches!(app.screen, Screen::NewInstallation));
    assert!(!app.confirm && !app.fresh_installation);
    app.activate(Control::NewInstallationConfirm(false));
    assert!(matches!(app.screen, Screen::Controller));
    app.activate(Control::ControllerNew);
    app.activate(Control::NewInstallationConfirm(true));
    assert!(app.fresh_installation && matches!(app.screen, Screen::Form));
    assert_eq!(app.form.saved.as_ref().unwrap().id, id);
    assert!(app.form.fields[7].is_empty());
    app.activate(Control::Action(Action::Install));
    assert!(!app.fresh_installation && matches!(app.screen, Screen::Controller));
}
#[test]
fn red_buttons_keep_red_frames_on_hover_and_confirmation_phrase_is_bold() {
    let mut app = test_profile_app();
    app.screen = Screen::Controller;
    app.controller.plan = Some(
        serde_json::json!({"action":"remove","confirmation_phrase":"REMOVE NODE PLANE","cleanup_nodes":false}),
    );
    let mut terminal = Terminal::new(ratatui::backend::TestBackend::new(100, 30)).unwrap();
    terminal
        .draw(|frame| app.hits = draw_content(frame, &app))
        .unwrap();
    let rect = app
        .hits
        .iter()
        .find(|hit| hit.control == Control::ControllerConfirm)
        .unwrap()
        .area;
    app.mouse(MouseEvent {
        kind: MouseEventKind::Moved,
        column: rect.x + 1,
        row: rect.y + 1,
        modifiers: KeyModifiers::NONE,
    });
    terminal
        .draw(|frame| app.hits = draw_content(frame, &app))
        .unwrap();
    let buffer = terminal.backend().buffer();
    assert_eq!(buffer[(rect.x, rect.y)].fg, Color::Red);
    assert!(
        buffer
            .content
            .iter()
            .any(|c| c.symbol() == "R" && c.modifier.contains(Modifier::BOLD))
    );
    assert!(app.nodes_requested.is_none());
}
#[test]
fn removed_installation_disables_server_actions_and_can_be_checked_manually() {
    let mut app = test_profile_app();
    let profile = app.form.saved.as_ref().map(|p| p.id);
    app.event(Event::Nodes(crate::nodes::Update::Controller(
        profile,
        crate::controller::Command::Probe,
        serde_json::json!({"installed":false}),
    )));
    assert!(!app.controller_ready());
    for action in [Action::Update, Action::Diagnose, Action::PrepareNode] {
        app.activate(Control::Action(action));
        assert!(app.nodes_requested.is_none());
    }
    app.open_access();
    app.open_temporary(None);
    assert!(app.nodes_requested.is_none());
    app.screen = Screen::Controller;
    let mut terminal = Terminal::new(ratatui::backend::TestBackend::new(100, 30)).unwrap();
    terminal
        .draw(|frame| app.hits = draw_content(frame, &app))
        .unwrap();
    assert!(
        app.hits
            .iter()
            .any(|hit| hit.control == Control::ControllerCheck)
    );
    assert!(!app.hits.iter().any(|hit| matches!(
        hit.control,
        Control::Action(Action::Update | Action::Diagnose | Action::PrepareNode)
            | Control::Access
            | Control::Temporary
    )));
    app.activate(Control::ControllerCheck);
    assert!(matches!(
        app.nodes_requested,
        Some(crate::nodes::Command::Controller(
            _,
            crate::controller::Command::Probe
        ))
    ));
    app.nodes_requested = None;
    app.event(Event::Nodes(crate::nodes::Update::Controller(
        profile,
        crate::controller::Command::Probe,
        serde_json::json!({"installed":true}),
    )));
    assert!(app.controller_ready());
    terminal
        .draw(|frame| app.hits = draw_content(frame, &app))
        .unwrap();
    assert!(
        !app.hits
            .iter()
            .any(|hit| hit.control == Control::ControllerCheck)
    );
}

#[test]
fn custom_theme_selection_survives_restart_and_custom_color_clears_it() {
    let state = tempfile::tempdir().unwrap();
    theme::load(state.path()).unwrap();
    std::fs::write(
        state.path().join("themes/midnight.json"),
        r##"{"name":"Midnight","accent":"#123456","section_border":"#334455"}"##,
    )
    .unwrap();
    let mut request = test_request();
    request.state_dir = state.path().into();
    let mut app = App::new(&request);
    app.load_connections(&request).unwrap();
    let index = app
        .themes
        .iter()
        .position(|theme| theme.id == "midnight.json")
        .unwrap();
    app.activate(Control::Accent(index));
    assert_eq!(app.connections.theme.as_deref(), Some("midnight.json"));
    assert_eq!(app.connections.accent, "#123456");
    let mut restarted = App::new(&request);
    restarted.load_connections(&request).unwrap();
    assert_eq!(
        restarted.connections.theme.as_deref(),
        Some("midnight.json")
    );
    restarted.screen = Screen::SettingsPage(SettingsPage::Appearance);
    restarted.settings_selected = index + 1;
    let mut terminal = Terminal::new(ratatui::backend::TestBackend::new(100, 40)).unwrap();
    terminal
        .draw(|frame| restarted.hits = draw(frame, &restarted))
        .unwrap();
    let text = terminal
        .backend()
        .buffer()
        .content
        .iter()
        .map(|cell| cell.symbol())
        .collect::<String>();
    assert!(text.contains("Midnight · Selected"));
    assert!(
        terminal
            .backend()
            .buffer()
            .content
            .iter()
            .any(|cell| cell.fg == Color::Rgb(0x12, 0x34, 0x56))
    );
    restarted.accent_input = "#ffbb74".into();
    restarted.activate(Control::SaveAccent);
    assert!(restarted.connections.theme.is_none());
    assert!(Connections::load(state.path()).unwrap().theme.is_none());
}

#[test]
fn returning_from_controller_section_restores_opener_and_scroll() {
    let mut app = test_profile_app();
    app.installed_profile = app.confirmed_installation;
    app.activate(Control::Action(Action::Install));
    let opener = Control::ControllerSection(ControllerSection::Monitoring);
    let index = app
        .controller_controls()
        .iter()
        .position(|c| *c == opener)
        .unwrap();
    app.controller.selected = index;
    app.manual_viewport.set(Some(2));
    app.activate(opener);
    app.return_focus = None;
    app.controller.selected = 2;
    app.remember_navigation_focus(None);
    app.controller.recovery_history = Some(serde_json::json!({"items": []}));
    app.restore_navigation_context();
    app.activate(Control::ControllerBack);
    assert_eq!(app.controller.selected, 2);
    app.return_focus = None;
    app.activate(Control::ControllerBack);
    assert_eq!(app.controller.selected, index);
    assert_eq!(app.manual_viewport.get(), Some(2));
}

#[test]
fn access_return_and_async_refresh_keep_selected_profile() {
    let mut app = test_profile_app();
    app.screen = Screen::Access;
    app.access.items = vec![
        serde_json::json!({"id":"one"}),
        serde_json::json!({"id":"two"}),
    ];
    let mut terminal = Terminal::new(ratatui::backend::TestBackend::new(100, 30)).unwrap();
    terminal
        .draw(|frame| app.hits = draw_content(frame, &app))
        .unwrap();
    let index = app
        .access_controls()
        .iter()
        .position(|c| *c == Control::AccessItem(1))
        .unwrap();
    app.access.selected = index;
    app.remember_navigation_focus(Some(Control::AccessItem(1)));
    app.event(Event::Nodes(crate::nodes::Update::Access(
        crate::access::Command::Card(crate::access::Tab::Profiles, "two".into()),
        serde_json::json!({"id":"two"}),
    )));
    app.activate(Control::AccessBack);
    assert_eq!(app.access.selected, index);
    app.event(Event::Nodes(crate::nodes::Update::Access(
        crate::access::Command::List(crate::access::Tab::Profiles),
        serde_json::json!({"items":[{"id":"one"},{"id":"two"}]}),
    )));
    assert_eq!(app.access.selected, index);
}

#[test]
fn settings_escape_restores_selected_submenu() {
    let mut app = test_profile_app();
    app.screen = Screen::Settings;
    let index = SettingsPage::ALL
        .iter()
        .position(|p| *p == SettingsPage::Appearance)
        .unwrap()
        + 1;
    app.settings_selected = index;
    app.activate(Control::SettingsPage(SettingsPage::Appearance));
    app.appearance_key(KeyCode::Esc);
    assert!(matches!(app.screen, Screen::Settings));
    assert_eq!(app.settings_selected, index);
}

#[test]
fn profile_switch_restores_selector_focus_after_sidebar_changes() {
    let mut app = test_profile_app();
    app.screen = Screen::Idle;
    let mut terminal = Terminal::new(ratatui::backend::TestBackend::new(100, 30)).unwrap();
    terminal
        .draw(|frame| app.hits = draw_content(frame, &app))
        .unwrap();
    let old_index = app
        .idle_controls()
        .iter()
        .position(|c| *c == Control::QuickProfile)
        .unwrap();
    app.return_focus = Some(Control::QuickProfile);
    app.form.saved = None;
    app.idle_selection = old_index;
    terminal
        .draw(|frame| app.hits = draw_content(frame, &app))
        .unwrap();
    assert!(app.restore_return_focus());
    assert!(app.idle_controls()[app.idle_selection] == Control::QuickProfile);
    assert!(
        !app.idle_controls()
            .iter()
            .any(|c| matches!(c, Control::CycleProfile(_)))
    );
}

#[test]
fn update_choices_keep_theme_text_after_pointer_focus_changes() {
    let mut app = test_profile_app();
    app.screen = Screen::Controller;
    app.form.select_action(Action::Update);
    app.connections.accent = "#ae78cd".into();
    app.controller.updates = Some(serde_json::json!({
        "auto_check_enabled":true,"auto_check_interval_minutes":360
    }));
    app.pointer_active = true;
    app.pointer_position = Some((0, 0).into());
    let accent = Color::Rgb(174, 120, 205);
    let mut terminal = Terminal::new(ratatui::backend::TestBackend::new(120, 40)).unwrap();
    terminal.draw(|frame| app.hits = draw(frame, &app)).unwrap();
    for control in [
        Control::ControllerUpdateAuto,
        Control::ControllerUpdateInterval(360),
    ] {
        let area = app
            .hits
            .iter()
            .find(|hit| hit.control == control)
            .unwrap()
            .area;
        assert_eq!(
            terminal.backend().buffer()[(area.x, area.y)].fg,
            Color::White
        );
        assert!((area.x + 1..area.right() - 1).any(|x| {
            let cell = &terminal.backend().buffer()[(x, area.y + 1)];
            cell.symbol() != " " && cell.fg == accent
        }));
        app.hovered = Some(control);
        app.pointer_position = Some((area.x + 1, area.y + 1).into());
        terminal.draw(|frame| app.hits = draw(frame, &app)).unwrap();
        assert_eq!(terminal.backend().buffer()[(area.x, area.y)].fg, accent);
        assert!((area.x + 1..area.right() - 1).all(|x| {
            let cell = &terminal.backend().buffer()[(x, area.y + 1)];
            cell.symbol() == " " || cell.fg == accent
        }));
        app.pointer_position = Some((0, 0).into());
        terminal.draw(|frame| app.hits = draw(frame, &app)).unwrap();
    }
}

#[test]
fn successful_workstation_update_requires_restart_without_confirming_installation() {
    let mut app = test_profile_app();
    app.form.select_action(Action::Install);
    app.confirmed_installation = None;
    app.restart_required = true;
    app.event(Event::Finished(Ok("Workstation updated".into())));
    assert!(app.confirmed_installation.is_none());
    for size in [(120, 40), (60, 20)] {
        let mut terminal =
            Terminal::new(ratatui::backend::TestBackend::new(size.0, size.1)).unwrap();
        terminal.draw(|frame| app.hits = draw(frame, &app)).unwrap();
        assert!(app.hits.len() == 1 && app.hits[0].control == Control::Restart);
    }
    for control in [Control::WindowClose, Control::Controller, Control::Close] {
        app.activate(control);
        assert!(!app.restart_requested && !app.exit);
        assert!(matches!(app.screen, Screen::Finished));
    }
    app.activate(Control::Restart);
    assert!(app.restart_requested);
}

#[test]
fn diagnostic_status_prefixes_have_semantic_colors_only_on_the_status() {
    let lines =
        diagnostic_lines("[OK] disk ready\n[WARN] low space\n[FAIL] backend down\nPlain text");
    for (line, color) in lines.iter().zip([Color::Green, Color::Yellow, Color::Red]) {
        assert_eq!(line.spans[1].style.fg, Some(color));
        assert!(line.spans[2].style.fg.is_none());
    }
    assert!(lines[3].spans[0].style.fg.is_none());
}

#[test]
fn backup_and_alert_choices_are_hidden_until_enabled_and_submit_exact_values() {
    let mut app = test_profile_app();
    app.screen = Screen::Controller;
    app.controller.backups =
        Some(serde_json::json!({"enabled":false,"interval_hours":12,"keep_count":10}));
    assert!(!app.controller_controls().iter().any(|c| matches!(
        c,
        Control::ControllerBackupInterval(_) | Control::ControllerBackupKeep(_)
    )));
    app.activate(Control::ControllerBackupInterval(24));
    assert!(app.nodes_requested.is_none());
    app.controller.backups.as_mut().unwrap()["enabled"] = true.into();
    let mut terminal = Terminal::new(ratatui::backend::TestBackend::new(120, 40)).unwrap();
    terminal.draw(|frame| app.hits = draw(frame, &app)).unwrap();
    for choices in [
        vec![
            Control::ControllerBackupInterval(6),
            Control::ControllerBackupInterval(12),
            Control::ControllerBackupInterval(24),
        ],
        vec![
            Control::ControllerBackupKeep(5),
            Control::ControllerBackupKeep(10),
            Control::ControllerBackupKeep(20),
        ],
    ] {
        let areas: Vec<_> = choices
            .iter()
            .map(|c| app.hits.iter().find(|h| h.control == *c).unwrap().area)
            .collect();
        assert!(areas.iter().all(|r| r.y == areas[0].y));
        let label_area = Rect::new(27, areas[0].y, areas[0].x - 27, 3);
        assert!(
            !app.hits
                .iter()
                .any(|h| h.area.intersects(label_area) && h.area.x >= 27)
        );
    }
    app.activate(Control::ControllerBackupKeep(20));
    assert!(
        matches!(&app.nodes_requested,Some(crate::nodes::Command::Controller(_,crate::controller::Command::BackupPreferences(v))) if v["keep_count"]==20)
    );
    app.nodes_requested = None;
    app.controller.backups = None;
    app.controller.alerts = Some(serde_json::json!({"enabled":false,"active":[]}));
    assert!(!app.controller_controls().iter().any(|c| matches!(
        c,
        Control::ControllerAlertInterval(_) | Control::ControllerAlertToggle("notify_resolved")
    )));
    app.controller.alerts.as_mut().unwrap()["enabled"] = true.into();
    app.activate(Control::ControllerAlertInterval(15));
    assert!(
        matches!(&app.nodes_requested,Some(crate::nodes::Command::Controller(_,crate::controller::Command::AlertPreferences(v))) if v["interval_minutes"]==15)
    );
    assert_eq!(format_backup_size(2486), "2.4 KiB");
}

#[test]
fn defaults_dialog_has_cancel_and_apply_only_after_a_change() {
    let mut app = test_profile_app();
    app.screen = Screen::Controller;
    app.controller.defaults = Some(serde_json::json!({"settings":{"xray_sni":"example.com"}}));
    app.controller.defaults_baseline = app.controller.defaults.clone();
    app.activate(Control::ControllerDefaultsField("xray_sni"));
    assert!(
        !app.controller_controls()
            .contains(&Control::ControllerDefaultsInputApply)
    );
    app.controller.defaults_input.as_mut().unwrap().1 = "other.example".into();
    assert!(
        app.controller_controls()
            .contains(&Control::ControllerDefaultsInputApply)
    );
    let mut terminal = Terminal::new(ratatui::backend::TestBackend::new(120, 40)).unwrap();
    terminal.draw(|frame| app.hits = draw(frame, &app)).unwrap();
    let apply = app
        .hits
        .iter()
        .find(|h| h.control == Control::ControllerDefaultsInputApply)
        .unwrap()
        .area;
    let cancel = app
        .hits
        .iter()
        .find(|h| h.control == Control::ControllerBack)
        .unwrap()
        .area;
    assert_eq!(apply.y, cancel.y);
    assert_eq!(cancel.height, 3);
    app.activate(Control::ControllerBack);
    assert_eq!(app.controller.defaults, app.controller.defaults_baseline);
}

#[test]
fn defaults_save_reset_have_their_own_row_and_reset_preserves_scroll() {
    let mut app = test_profile_app();
    app.screen = Screen::Controller;
    app.controller.defaults = Some(
        serde_json::json!({"protocols":["awg","xray"],"xray_transports":["tcp","xhttp"],"settings":{"awg_port_mode":"auto","xray_sni":"old"}}),
    );
    app.controller.defaults_baseline = app.controller.defaults.clone();
    app.controller.defaults.as_mut().unwrap()["settings"]["xray_sni"] = "new".into();
    app.controller.defaults_advanced = true;
    let controls = defaults_controls(&app.controller);
    let rows = defaults_rows(&controls, 100);
    let row = rows
        .iter()
        .find(|row| {
            row.iter()
                .any(|&i| controls[i] == Control::ControllerDefaultsSave)
        })
        .unwrap();
    assert_eq!(row.len(), 2);
    assert!(
        row.iter()
            .any(|&i| controls[i] == Control::ControllerDefaultsReset)
    );
    app.manual_viewport.set(Some(3));
    app.controller.selected = controls.len() - 2;
    app.activate(Control::ControllerDefaultsReset);
    assert_eq!(app.manual_viewport.get(), Some(3));
    assert!(app.controller.selected > 0);
}

#[test]
fn list_actions_use_the_bottom_row_when_no_pagination_is_needed() {
    let mut app = test_profile_app();
    let mut terminal = Terminal::new(ratatui::backend::TestBackend::new(120, 40)).unwrap();
    for (screen, control) in [
        (Screen::Temporary, Control::TemporaryNew),
        (Screen::Access, Control::AccessRefresh),
        (Screen::Nodes, Control::NodesNew),
    ] {
        app.screen = screen;
        terminal.draw(|frame| app.hits = draw(frame, &app)).unwrap();
        let area = app.hits.iter().find(|h| h.control == control).unwrap().area;
        // The main panel ends above the hint: only its border remains below the action.
        assert_eq!(area.bottom(), 36);
    }
}

#[test]
fn wizard_footer_is_inline_and_scrolls_after_long_choices() {
    let mut app = test_profile_app();
    let templates: Vec<_> = (0..20).map(|i| serde_json::json!({"code":format!("n{i}"),"title":format!("Node {i}"),"region":"Europe","flag":"","draft":{}})).collect();
    let options = serde_json::from_value(serde_json::json!({"local_available":true,"defaults":{"protocols":[],"xray_transports":[],"settings":{}},"templates":templates})).unwrap();
    app.node_wizard = Some(crate::node_wizard::Wizard::new(options).unwrap());
    app.screen = Screen::NodeWizard;
    let mut terminal = Terminal::new(ratatui::backend::TestBackend::new(100, 30)).unwrap();
    terminal.draw(|frame| app.hits = draw(frame, &app)).unwrap();
    assert!(!app.hits.iter().any(|h| h.control == Control::WizardBack));
    app.node_wizard.as_mut().unwrap().step = crate::node_wizard::Step::Title;
    terminal.draw(|frame| app.hits = draw(frame, &app)).unwrap();
    let back = app
        .hits
        .iter()
        .find(|h| h.control == Control::WizardBack)
        .unwrap()
        .area;
    for control in [Control::WizardNext, Control::WizardCancel] {
        let area = app.hits.iter().find(|h| h.control == control).unwrap().area;
        assert_eq!(area.y, back.y);
        assert!(area.width > back.width);
    }
    assert_eq!(back.width, 5);
    app.node_wizard.as_mut().unwrap().step = crate::node_wizard::Step::Template;
    app.node_wizard.as_mut().unwrap().selected = 0;
    terminal.draw(|frame| app.hits = draw(frame, &app)).unwrap();
    assert!(!app.hits.iter().any(|h| h.control == Control::WizardCancel));
    assert!(app.scrollbar.get().is_some());
    app.manual_viewport.set(Some(100));
    terminal.draw(|frame| app.hits = draw(frame, &app)).unwrap();
    assert!(app.hits.iter().any(|h| h.control == Control::WizardCancel));
}

#[test]
fn temporary_progress_polls_preserve_focus_and_node_return() {
    let mut app = test_profile_app();
    let node = browser_node("lv1", "Europe");
    app.nodes.card = Some(node.clone());
    app.open_temporary(Some(node));
    app.nodes_requested = None;
    app.activate(Control::TemporaryBack);
    assert!(matches!(app.screen, Screen::Nodes));
    assert_eq!(app.nodes.card.as_ref().unwrap().key, "lv1");
    app.screen = Screen::Temporary;
    app.temporary.view = crate::temporary::View::Card;
    app.temporary.card = Some(serde_json::json!({"id":"temp1","status":"issuing"}));
    app.temporary.selected = 2;
    assert!(
        matches!(app.temporary.poll(),Some(crate::temporary::Command::Card(id)) if id=="temp1")
    );
    app.event(Event::Nodes(crate::nodes::Update::Temporary(
        crate::temporary::Command::Card("temp1".into()),
        serde_json::json!({"id":"temp1","status":"active"}),
    )));
    assert_eq!(app.temporary.selected, 2);
    assert!(app.temporary.poll().is_none());
}

#[test]
fn completed_setup_refreshes_card_and_queued_setup_hides_start_button() {
    let mut app = test_profile_app();
    let node = browser_node("lv1", "Europe");
    app.nodes.card = Some(node);
    app.nodes.operation = Some(crate::nodes::Operation {
        kind: "node-bootstraps".into(),
        id: uuid::Uuid::new_v4(),
        node_key: "lv1".into(),
        status: "queued".into(),
        error: String::new(),
        command_id: None,
        action: String::new(),
    });
    let node = app.nodes.card.as_ref().unwrap();
    assert!(node.needs_observation(app.nodes.operation.as_ref()));
    assert!(
        !node_card_actions(node, app.nodes.operation.as_ref())
            .iter()
            .any(|(_, c)| matches!(c, Control::NodesSetup | Control::NodesBootstrap))
    );
    let mut operation = app.nodes.operation.clone().unwrap();
    operation.status = "succeeded".into();
    app.event(Event::Nodes(crate::nodes::Update::Operation(operation)));
    assert!(matches!(&app.nodes_requested,Some(crate::nodes::Command::Card(key)) if key=="lv1"));
}

#[test]
fn mouse_click_release_keeps_hover_with_a_stationary_pointer() {
    let mut app = test_profile_app();
    let state = tempfile::tempdir().unwrap();
    app.state_dir = state.path().into();
    app.themes = theme::defaults();
    app.screen = Screen::SettingsPage(SettingsPage::Appearance);
    let mut terminal = Terminal::new(ratatui::backend::TestBackend::new(120, 40)).unwrap();
    terminal.draw(|frame| app.hits = draw(frame, &app)).unwrap();
    let area = app
        .hits
        .iter()
        .find(|h| h.control == Control::Accent(1))
        .unwrap()
        .area;
    for kind in [
        MouseEventKind::Moved,
        MouseEventKind::Down(MouseButton::Left),
        MouseEventKind::Up(MouseButton::Left),
    ] {
        app.mouse(MouseEvent {
            kind,
            column: area.x + 1,
            row: area.y + 1,
            modifiers: KeyModifiers::NONE,
        });
        terminal.draw(|frame| app.hits = draw(frame, &app)).unwrap();
        app.refresh_scroll_hover();
        terminal.draw(|frame| app.hits = draw(frame, &app)).unwrap();
        assert!(app.hovered == Some(Control::Accent(1)));
        assert_ne!(
            terminal.backend().buffer()[(area.x, area.y)].fg,
            Color::White
        );
    }
    app.hovered = None;
    assert!(app.refresh_scroll_hover());
    assert!(app.hovered == Some(Control::Accent(1)));
}

#[test]
fn traffic_collection_reserves_a_disabled_slot_while_loading() {
    let mut app = test_profile_app();
    app.screen = Screen::Controller;
    app.controller_section = Some(ControllerSection::Policies);
    let controls = app.controller_root_controls(false);
    assert_eq!(controls[0].0, "Please wait");
    let mut terminal = Terminal::new(ratatui::backend::TestBackend::new(120, 40)).unwrap();
    terminal.draw(|frame| app.hits = draw(frame, &app)).unwrap();
    assert!(
        !app.hits
            .iter()
            .any(|h| h.control == Control::ControllerPolicyToggle("traffic"))
    );
    app.activate(Control::ControllerPolicyToggle("traffic"));
    assert!(app.nodes_requested.is_none());
    app.controller.policies = Some(serde_json::json!({"traffic":{"enabled":false}}));
    terminal.draw(|frame| app.hits = draw(frame, &app)).unwrap();
    assert!(
        app.hits
            .iter()
            .any(|h| h.control == Control::ControllerPolicyToggle("traffic"))
    );
    assert_eq!(
        app.controller_root_controls(false)[0].0,
        "Traffic collection: Off"
    );
}

#[test]
fn custom_duration_accepts_units_and_rejects_invalid_or_overflowing_values() {
    let now = std::time::UNIX_EPOCH;
    assert_eq!(
        crate::access::expiry_from_duration("12H", now).unwrap(),
        "1970-01-01T12:00:00Z"
    );
    assert_eq!(
        crate::access::expiry_from_duration("7d", now).unwrap(),
        crate::access::expiry_after(7, now).unwrap()
    );
    assert_eq!(
        crate::access::expiry_from_duration("1M", now).unwrap(),
        crate::access::expiry_after(30, now).unwrap()
    );
    assert_eq!(
        crate::access::expiry_from_duration("1Y", now).unwrap(),
        crate::access::expiry_after(365, now).unwrap()
    );
    for input in ["", "0D", "-1H", "1.5D", "1Q", "é", "18446744073709551615Y"] {
        assert!(crate::access::expiry_from_duration(input, now).is_err());
    }
    let mut app = test_profile_app();
    app.screen = Screen::Access;
    app.access.editor = Some(crate::access::Editor::new(None, None));
    app.access.editor.as_mut().unwrap().expiry_view = true;
    app.activate(Control::AccessDurationCustom);
    let mut terminal = Terminal::new(ratatui::backend::TestBackend::new(100, 30)).unwrap();
    terminal.draw(|frame| app.hits = draw(frame, &app)).unwrap();
    for c in "12H".chars() {
        app.access_key(KeyEvent::new(KeyCode::Char(c), KeyModifiers::NONE));
    }
    app.activate(Control::AccessDurationApply);
    let editor = app.access.editor.as_ref().unwrap();
    assert!(!editor.expiry_view && editor.duration_input.is_none() && !editor.fields[1].is_empty());
}

#[test]
fn maintenance_groups_danger_and_reinstall_and_back_restores_parent() {
    let mut app = test_profile_app();
    app.screen = Screen::Nodes;
    let mut node = browser_node("lv1", "Europe");
    node.agent = crate::nodes::AgentState::Ready;
    node.protocols = vec!["awg".into()];
    app.nodes.card = Some(node);
    app.nodes.maintenance = Some(
        crate::node_maintenance::Browser::new(
            "lv1".into(),
            serde_json::json!({"node_key":"lv1","status":"ready"}),
            None,
        )
        .unwrap(),
    );
    let mut terminal = Terminal::new(ratatui::backend::TestBackend::new(120, 40)).unwrap();
    terminal.draw(|frame| app.hits = draw(frame, &app)).unwrap();
    assert!(app.hits.iter().any(|h| h.control
        == Control::NodesMaintenanceSection(crate::node_maintenance::Section::Removal)));
    assert!(!app.hits.iter().any(|h| matches!(
        h.control,
        Control::NodesRemove(_) | Control::NodesProtocolAction(_)
    )));
    app.activate(Control::NodesMaintenanceSection(
        crate::node_maintenance::Section::Removal,
    ));
    terminal.draw(|frame| app.hits = draw(frame, &app)).unwrap();
    assert!(
        app.hits
            .iter()
            .any(|h| matches!(h.control, Control::NodesRemove(_)))
    );
    app.activate(Control::NodesBack);
    assert_eq!(
        app.nodes.maintenance.as_ref().unwrap().section,
        crate::node_maintenance::Section::Root
    );
    assert!(
        !node_card_actions(app.nodes.card.as_ref().unwrap(), None)
            .iter()
            .any(|(_, control)| *control == Control::NodesDiagnostics)
    );
}

#[test]
fn selected_subsection_keeps_theme_label_with_pointer_elsewhere() {
    let mut app = test_profile_app();
    app.screen = Screen::Access;
    app.access.editor = Some(crate::access::Editor::new(None, None));
    app.access.editor.as_mut().unwrap().grants_view = true;
    app.pointer_active = true;
    app.pointer_position = Some((0, 0).into());
    let mut terminal = Terminal::new(ratatui::backend::TestBackend::new(120, 40)).unwrap();
    terminal.draw(|frame| app.hits = draw(frame, &app)).unwrap();
    let area = app
        .hits
        .iter()
        .find(|h| h.control == Control::AccessGrantMode(false))
        .unwrap()
        .area;
    assert_eq!(
        terminal.backend().buffer()[(area.x, area.y)].fg,
        Color::White
    );
    assert!(
        (area.x + 1..area.right() - 1)
            .any(|x| terminal.backend().buffer()[(x, area.y + 1)].fg == Color::Cyan)
    );
}

#[test]
fn node_settings_grid_scrolls_without_page_buttons_or_reserved_footer() {
    let mut app = test_profile_app();
    app.screen = Screen::Nodes;
    app.nodes_sidebar = false;
    app.nodes.editor = Some(node_settings_editor());
    app.nodes.editor.as_mut().unwrap().protocols = vec!["awg".into(), "xray".into()];
    app.nodes.editor.as_mut().unwrap().source["protocols"] = serde_json::json!(["awg", "xray"]);
    app.nodes.editor.as_mut().unwrap().tab = crate::node_editor::Tab::Advanced;
    let mut wide = Terminal::new(ratatui::backend::TestBackend::new(120, 40)).unwrap();
    wide.draw(|frame| app.hits = draw(frame, &app)).unwrap();
    let fields: Vec<_> = app
        .hits
        .iter()
        .filter(|h| {
            matches!(
                h.control,
                Control::NodesEditField(_) | Control::NodesEditChoice(_)
            )
        })
        .collect();
    assert_eq!(
        fields.len(),
        node_editor_items(app.nodes.editor.as_ref().unwrap()).len()
    );
    assert_eq!(fields[0].area.y, fields[1].area.y);
    assert!(fields[0].area.right() < fields[1].area.x);
    assert!(app.scrollbar.get().is_none());
    let text = wide
        .backend()
        .buffer()
        .content
        .iter()
        .map(|c| c.symbol())
        .collect::<String>();
    assert!(!text.contains("Next page") && !text.contains("Previous page"));
    let mut narrow = Terminal::new(ratatui::backend::TestBackend::new(80, 30)).unwrap();
    narrow.draw(|frame| app.hits = draw(frame, &app)).unwrap();
    assert!(app.scrollbar.get().is_some());
    let visible: Vec<_> = app
        .hits
        .iter()
        .filter(|h| {
            matches!(
                h.control,
                Control::NodesEditField(_) | Control::NodesEditChoice(_)
            )
        })
        .collect();
    assert!(
        visible
            .windows(2)
            .all(|pair| pair[1].area.y > pair[0].area.y)
    );
    let last = visible.last().unwrap().control;
    app.nodes_selected = app
        .nodes_controls()
        .iter()
        .position(|c| *c == last)
        .unwrap();
    app.nodes_key(KeyCode::Down.into());
    narrow.draw(|frame| app.hits = draw(frame, &app)).unwrap();
    assert!(app.list_viewport.get() > 0);
    assert!(matches!(
        app.nodes_controls()[app.nodes_selected],
        Control::NodesEditField(_) | Control::NodesEditChoice(_)
    ));
    app.manual_viewport.set(Some(100));
    narrow.draw(|frame| app.hits = draw(frame, &app)).unwrap();
    assert_eq!(app.list_viewport.get(), app.list_max_viewport.get());
    wide.draw(|frame| app.hits = draw(frame, &app)).unwrap();
    assert_eq!(app.list_viewport.get(), 0);
    assert!(app.scrollbar.get().is_none());
}

#[test]
fn node_settings_save_reset_share_a_single_footer_row() {
    let mut app = test_profile_app();
    app.screen = Screen::Nodes;
    app.nodes.editor = Some(node_settings_editor());
    app.nodes.editor.as_mut().unwrap().fields[0].value = "New name".into();
    app.nodes.editor.as_mut().unwrap().changed();
    let mut terminal = Terminal::new(ratatui::backend::TestBackend::new(120, 40)).unwrap();
    terminal.draw(|frame| app.hits = draw(frame, &app)).unwrap();
    let save = app
        .hits
        .iter()
        .find(|h| h.control == Control::NodesEditSave)
        .unwrap()
        .area;
    let reset = app
        .hits
        .iter()
        .find(|h| h.control == Control::NodesEditReset)
        .unwrap()
        .area;
    assert_eq!(save.y, reset.y);
    assert_eq!(save.height, 3);
    assert_eq!(save.bottom(), 36);
}

#[test]
fn removal_survives_lost_shutdown_response_and_stops_backend_polling_after_verification() {
    let mut app = test_profile_app();
    let state = tempfile::tempdir().unwrap();
    app.state_dir = state.path().into();
    app.remember_installed();
    app.open_controller();
    app.nodes_requested = None;
    app.controller.overview = Some(
        serde_json::json!({"deployment":{"base_dir":"/opt/node-plane","shared_dir":"/opt/node-plane/shared","units":["node-plane-backend.service"]}}),
    );
    app.controller.job = Some(
        serde_json::json!({"id":uuid::Uuid::new_v4(),"action":"remove","status":"awaiting_shutdown","phase":"shutdown"}),
    );
    app.activate(Control::ControllerShutdown);
    assert!(app.controller.verifying);
    assert!(matches!(
        app.nodes_requested.take(),
        Some(crate::nodes::Command::Controller(
            _,
            crate::controller::Command::Shutdown(..)
        ))
    ));
    let profile = app.controller.profile;
    assert!(
        state
            .path()
            .join(format!("controller-removal-{}.json", profile.unwrap()))
            .exists()
    );
    app.nodes_busy = true;
    app.nodes_background = true;
    app.event(Event::Finished(Err("Shutdown response lost".into())));
    assert!(matches!(
        app.controller.poll(),
        Some(crate::controller::Command::Verify(_))
    ));
    let command = app.controller.poll().unwrap();
    app.event(Event::Nodes(crate::nodes::Update::Controller(
        profile,
        command,
        serde_json::json!({"removed":true}),
    )));
    assert!(app.controller.removed);
    assert!(!app.controller.removal_active());
    assert!(app.controller.poll().is_none());
    assert!(!app.form.saved.as_ref().unwrap().installed);
    assert!(
        !app.connections
            .installations
            .iter()
            .find(|p| Some(p.id) == profile)
            .unwrap()
            .installed
    );
    let mut terminal = Terminal::new(ratatui::backend::TestBackend::new(120, 40)).unwrap();
    terminal.draw(|frame| app.hits = draw(frame, &app)).unwrap();
    let text = terminal
        .backend()
        .buffer()
        .content
        .iter()
        .map(|c| c.symbol())
        .collect::<String>();
    assert!(text.contains("Bye-bye!"));
    assert!(
        !app.hits
            .iter()
            .any(|h| h.control == Control::ControllerShutdown)
    );
    app.controller_command(crate::controller::Command::Overview);
    assert!(app.nodes_requested.is_none());
}

#[test]
fn appearance_keeps_scroll_after_theme_click_and_hovering_settings() {
    let mut app = test_profile_app();
    let state = tempfile::tempdir().unwrap();
    app.state_dir = state.path().into();
    app.themes = (0..15)
        .map(|i| {
            let mut t = theme::defaults()[0].clone();
            t.id = format!("theme{i}");
            t.name = format!("Theme {i}");
            t.accent = "#123456".into();
            t
        })
        .collect();
    app.screen = Screen::SettingsPage(SettingsPage::Appearance);
    let mut terminal = Terminal::new(ratatui::backend::TestBackend::new(80, 30)).unwrap();
    terminal.draw(|frame| app.hits = draw(frame, &app)).unwrap();
    app.manual_viewport.set(Some(2));
    terminal.draw(|frame| app.hits = draw(frame, &app)).unwrap();
    app.activate(Control::Accent(5));
    terminal.draw(|frame| app.hits = draw(frame, &app)).unwrap();
    assert_eq!(app.list_viewport.get(), 2);
    app.manual_viewport.set(None);
    app.focus(Control::Settings);
    terminal.draw(|frame| app.hits = draw(frame, &app)).unwrap();
    assert_eq!(app.list_viewport.get(), 2);
}

#[test]
fn node_diagnostics_scrolls_all_checks_without_truncation() {
    let mut app = test_profile_app();
    app.screen = Screen::Nodes;
    app.nodes_sidebar = false;
    app.nodes.card = Some(browser_node("lv1", "Europe"));
    app.nodes.diagnostics = Some((
        serde_json::json!({"checks":(0..50).map(|i|serde_json::json!({"id":format!("check_{i}"),"status":"ok"})).collect::<Vec<_>>()}),
        serde_json::json!({"items":[],"total":0}),
    ));
    let mut terminal = Terminal::new(ratatui::backend::TestBackend::new(80, 30)).unwrap();
    terminal.draw(|frame| app.hits = draw(frame, &app)).unwrap();
    assert!(
        matches!(app.scrollbar.get(),Some(bar) if matches!(bar.target,ScrollTarget::NodeDiagnostics))
    );
    app.nodes_key(KeyCode::End.into());
    terminal.draw(|frame| app.hits = draw(frame, &app)).unwrap();
    let text = terminal
        .backend()
        .buffer()
        .content
        .iter()
        .map(|c| c.symbol())
        .collect::<String>();
    assert!(text.contains("check 49"));
    assert!(!text.contains("more checks"));
    assert!(app.nodes.diagnostic_scroll.get() > 0);
    app.nodes_key(KeyCode::Home.into());
    assert_eq!(app.nodes.diagnostic_scroll.get(), 0);
}

#[test]
fn new_profile_selects_account_then_opens_editor_and_can_cancel() {
    let mut app = test_profile_app();
    app.open_access();
    app.nodes_requested = None;
    app.activate(Control::AccessNew);
    assert!(app.access.creating_profile);
    assert!(matches!(
        app.nodes_requested.take(),
        Some(crate::nodes::Command::Access(crate::access::Command::List(
            crate::access::Tab::Accounts
        )))
    ));
    app.access.items =
        vec![serde_json::json!({"id":"owner", "role":"member", "status":"approved"})];
    app.activate(Control::AccessItem(0));
    assert!(!app.access.creating_profile);
    assert!(
        matches!(app.nodes_requested.take(), Some(crate::nodes::Command::Access(crate::access::Command::EditContext {id:None, owner:Some(owner)})) if owner == "owner")
    );
    app.activate(Control::AccessNew);
    app.activate(Control::AccessBack);
    assert!(!app.access.creating_profile);
    assert_eq!(app.access.tab, crate::access::Tab::Profiles);
}

#[test]
fn diagnostic_counts_color_only_positive_totals() {
    let line = diagnostic_count_line("Errors: 2 · Warnings: 1");
    assert_eq!(line.spans[1].style.fg, Some(Color::Red));
    assert_eq!(line.spans[3].style.fg, Some(Color::Rgb(255, 165, 0)));
    let line = diagnostic_count_line("Errors: 0 · Warnings: 0");
    assert!(line.spans.iter().all(|span| span.style.fg.is_none()));
}

#[test]
fn external_removal_clears_cached_installation_and_stops_polling() {
    let mut app = test_profile_app();
    let directory = tempfile::tempdir().unwrap();
    app.state_dir = directory.path().into();
    let profile = app.form.saved.clone().unwrap();
    app.installed_profile = Some(profile.id);
    app.event(Event::Nodes(crate::nodes::Update::Controller(None, crate::controller::Command::Probe,
        serde_json::json!({"installed":false,"external_removal":true,"host":profile.host,"port":profile.port,"user":profile.user}))));
    assert!(app.controller.removed);
    assert!(app.confirmed_installation.is_none());
    assert!(app.installed_profile.is_none());
    assert!(!app.form.saved.as_ref().unwrap().installed);
    assert!(app.controller.poll().is_none());
    app.activate(Control::ControllerDefaults);
    assert!(app.nodes_requested.is_none());
}

#[test]
fn installation_defaults_count_fields_as_one_group() {
    let mut app = test_profile_app();
    app.screen = Screen::Controller;
    app.controller.defaults = Some(serde_json::json!({"settings":{}}));
    let controls = [
        Control::ControllerDefaultsField("xray_sni"),
        Control::ControllerDefaultsPreset,
        Control::ControllerDefaultsMode,
        Control::ControllerDefaultsAdvanced,
        Control::ControllerDefaultsSave,
    ];
    assert_eq!(
        app.list_position(Control::ControllerDefaultsAdvanced, &controls),
        Some((4, 4))
    );
    assert_eq!(
        app.list_position(Control::ControllerDefaultsSave, &controls),
        None
    );
}

#[test]
fn quick_start_returns_to_previous_screen_on_escape() {
    let mut app = test_profile_app();
    app.screen = Screen::Nodes;
    app.open_quick_start();
    app.open_quick_start();
    app.quick_start_key(KeyCode::Esc.into());
    assert!(matches!(app.screen, Screen::Nodes));
    assert!(app.quick_start_return.is_none());
}

#[test]
fn quick_start_continue_preserves_an_unsaved_node_draft() {
    let mut app = test_profile_app();
    app.screen = Screen::Nodes;
    let mut editor = node_settings_editor();
    editor.fields[0].value = "changed.example.com".into();
    app.nodes.editor = Some(editor);
    assert!(app.unsaved_changes());
    app.open_quick_start();
    app.activate(Control::QuickStartAction);
    assert!(matches!(app.screen, Screen::Nodes));
    assert_eq!(
        app.nodes.editor.as_ref().unwrap().fields[0].value,
        "changed.example.com"
    );
    assert!(app.error.contains("current draft"));
}

#[test]
fn browser_presence_cannot_replace_a_running_update_receiver() {
    let mut app = test_profile_app();
    app.form.action = Action::Update;
    app.screen = Screen::Running;
    app.nodes_busy = false; // The workflow owns the receiver, not a browser command.
    app.nodes_requested = Some(crate::nodes::Command::Controller(
        app.controller.profile,
        crate::controller::Command::Presence,
    ));
    app.event(Event::Stage("Updating components".into()));
    let id = uuid::Uuid::new_v4();
    app.event(Event::Update(crate::workstation::UpdateSnapshot {
        target: "dev · 1.0.0-alpha.5".into(),
        id,
        status: "running".into(),
        phase: "core".into(),
        components: vec![("backend".into(), "succeeded".into())],
        nodes: vec![],
        error: String::new(),
        rollback: String::new(),
    }));
    assert!(app.take_browser_request(true).is_none());
    assert_eq!(app.update.as_ref().unwrap().id, id);
    let mut terminal = Terminal::new(ratatui::backend::TestBackend::new(120, 40)).unwrap();
    terminal.draw(|frame| app.hits = draw(frame, &app)).unwrap();
    let text: String = terminal
        .backend()
        .buffer()
        .content()
        .iter()
        .map(|c| c.symbol())
        .collect();
    assert!(text.contains("Controller stack"));
    assert!(!text.contains("Update preparation"));
    assert!(app.nodes_requested.is_some());
    assert!(matches!(app.screen, Screen::Running));
    app.event(Event::Finished(Ok("Whole-stack update complete".into())));
    assert!(matches!(app.screen, Screen::Finished));
    assert!(app.take_browser_request(false).is_some());
    assert!(app.outcome.as_ref().unwrap().is_ok());
}

#[test]
fn idle_shows_live_quick_start_with_the_sidebar_and_keyboard_actions() {
    let mut app = test_profile_app();
    app.screen = Screen::Idle;
    app.controller.onboarding = Some(serde_json::json!({"steps":[
        {"key":"defaults","complete":true}, {"key":"node","complete":false}],
        "next_step":"node"}));
    let mut terminal = Terminal::new(ratatui::backend::TestBackend::new(120, 40)).unwrap();
    terminal.draw(|frame| app.hits = draw(frame, &app)).unwrap();
    let text: String = terminal
        .backend()
        .buffer()
        .content()
        .iter()
        .map(|c| c.symbol())
        .collect();
    assert!(text.contains("Quick Start"));
    assert!(text.contains("✓ Confirm installation defaults"));
    assert!(text.contains("○ Create your first VPN node"));
    assert!(app.hits.iter().any(|h| h.control == Control::Settings));
    app.idle_selection = app
        .idle_controls()
        .iter()
        .position(|c| *c == Control::QuickStartAction)
        .unwrap();
    app.idle_key(KeyCode::Enter.into());
    assert!(matches!(app.screen, Screen::Nodes));
    assert!(matches!(
        app.nodes_requested,
        Some(crate::nodes::Command::CreationOptions)
    ));
}

#[test]
fn quick_start_polls_each_second_only_while_visible_and_idle() {
    let mut app = test_profile_app();
    let elapsed = std::time::Duration::from_secs(1);
    for screen in [Screen::Idle, Screen::QuickStart] {
        app.screen = screen;
        assert!(app.onboarding_poll_ready(elapsed, false));
        assert!(!app.onboarding_poll_ready(std::time::Duration::from_millis(999), false));
        assert!(!app.onboarding_poll_ready(elapsed, true));
    }
    for screen in [
        Screen::Nodes,
        Screen::Controller,
        Screen::Access,
        Screen::Running,
    ] {
        app.screen = screen;
        assert!(!app.onboarding_poll_ready(elapsed, false));
    }
    app.screen = Screen::Idle;
    app.nodes_busy = true;
    assert!(!app.onboarding_poll_ready(elapsed, false));
}

#[test]
fn onboarding_background_refresh_keeps_idle_screen_and_sidebar_selection() {
    let mut app = test_profile_app();
    app.screen = Screen::Idle;
    app.controller.profile = app.form.saved.as_ref().map(|p| p.id);
    app.idle_selection = 2;
    app.nodes_busy = true;
    app.nodes_background = true;
    app.event(Event::Nodes(crate::nodes::Update::Controller(
        app.controller.profile,
        crate::controller::Command::Onboarding,
        serde_json::json!({"steps":[{"key":"defaults","complete":true}],"next_step":"node"}),
    )));
    app.event(Event::Finished(Ok("".into())));
    assert!(matches!(app.screen, Screen::Idle));
    assert_eq!(app.idle_selection, 2);
    assert_eq!(
        app.controller.onboarding.as_ref().unwrap()["next_step"],
        "node"
    );
    assert!(!app.nodes_busy);
}

#[test]
fn unconfirmed_manage_back_returns_to_install_form_with_equal_button_widths() {
    let mut app = test_profile_app();
    app.confirmed_installation = None;
    app.form.saved.as_mut().unwrap().installed = false;
    app.screen = Screen::Form;
    app.form.fields[6] = "unsaved fixture token".into();
    let mut terminal = Terminal::new(ratatui::backend::TestBackend::new(120, 50)).unwrap();
    terminal
        .draw(|frame| app.hits = draw_content(frame, &app))
        .unwrap();
    let manage = app
        .hits
        .iter()
        .find(|h| h.control == Control::Controller)
        .unwrap()
        .area;
    let next = app
        .hits
        .iter()
        .find(|h| h.control == Control::Continue)
        .unwrap()
        .area;
    assert_eq!(manage.width, next.width);
    app.activate(Control::Controller);
    assert!(matches!(app.screen, Screen::Controller));
    app.activate(Control::ControllerBack);
    assert!(matches!(app.screen, Screen::Form));
    assert_eq!(app.form.fields[6], "unsaved fixture token");
    assert!(app.nodes_requested.is_none());
}

#[test]
fn quick_start_reuses_selected_profile_before_installation_and_creates_only_if_missing() {
    let mut app = test_profile_app();
    let id = app.form.saved.as_ref().unwrap().id;
    app.confirmed_installation = None;
    app.form.saved.as_mut().unwrap().installed = false;
    app.screen = Screen::Idle;
    app.activate(Control::QuickStartAction);
    assert!(matches!(app.screen, Screen::Form));
    assert_eq!(app.form.saved.as_ref().unwrap().id, id);
    assert!(app.editor.is_none());
    assert_eq!(app.connections.installations.len(), 1);
    app.form.saved = None;
    app.activate(Control::QuickStartAction);
    assert!(matches!(app.screen, Screen::EditInstallation));
    assert!(app.editor.as_ref().unwrap().0.is_none());
}

#[test]
fn profile_transfer_requires_confirmation_and_does_not_modify_the_source() {
    let mut app = test_profile_app();
    let source = tempfile::tempdir().unwrap();
    let target = tempfile::tempdir().unwrap();
    app.state_dir = source.path().into();
    app.connections.save(&app.state_dir).unwrap();
    let before = std::fs::read(source.path().join("installations.json")).unwrap();
    app.activate(Control::TransferInstallation);
    assert!(app.prompt.is_some());
    app.profile_copy.as_mut().unwrap().1 = target.path().into();
    app.prompt_key(KeyCode::Char('n').into());
    app.finish_profile_copy();
    assert!(!target.path().join("installations.json").exists());
    app.activate(Control::TransferInstallation);
    app.profile_copy.as_mut().unwrap().1 = target.path().into();
    app.prompt_key(KeyCode::Char('y').into());
    app.finish_profile_copy();
    let destination = Connections::load(target.path()).unwrap();
    assert_eq!(destination.installations.len(), 1);
    assert_eq!(
        destination.installations[0].id,
        app.connections.installations[0].id
    );
    assert_eq!(
        destination.installations[0].branch,
        if crate::config::default_branch() == "dev" {
            "main"
        } else {
            "dev"
        }
    );
    assert_eq!(
        std::fs::read(source.path().join("installations.json")).unwrap(),
        before
    );
}

#[test]
fn workstation_quick_start_finishes_without_device_creation() {
    let mut app = test_profile_app();
    app.screen = Screen::QuickStart;
    app.controller.onboarding = Some(serde_json::json!({"steps":[
        {"key":"defaults","complete":true}, {"key":"node","complete":true},
        {"key":"protocols","complete":true}, {"key":"access","complete":true},
        {"key":"config","complete":false}], "next_step":"config"}));
    let mut terminal = Terminal::new(ratatui::backend::TestBackend::new(120, 40)).unwrap();
    terminal.draw(|frame| app.hits = draw(frame, &app)).unwrap();
    let text: String = terminal
        .backend()
        .buffer()
        .content()
        .iter()
        .map(|c| c.symbol())
        .collect();
    assert!(text.contains("Server setup is complete"));
    assert!(!text.contains("Create a device"));
    assert!(!text.contains("Progress refreshes"));
    app.nodes_requested = None;
    app.activate(Control::QuickStartAction);
    assert!(matches!(
        app.nodes_requested,
        Some(crate::nodes::Command::Controller(
            _,
            crate::controller::Command::OnboardingComplete
        ))
    ));
}

#[test]
fn done_hides_guide_after_backend_ack_and_survives_restart() {
    let state = tempfile::tempdir().unwrap();
    let mut app = test_profile_app();
    app.state_dir = state.path().into();
    app.screen = Screen::QuickStart;
    let profile = app.form.saved.as_ref().unwrap().id;
    app.controller.profile = Some(profile);
    app.event(Event::Nodes(crate::nodes::Update::Controller(
        Some(profile),
        crate::controller::Command::OnboardingComplete,
        serde_json::json!({"workstation_completed":true, "next_step":"config"}),
    )));
    assert!(matches!(app.screen, Screen::Idle));
    assert!(app.quick_start_completed());
    assert!(!app.onboarding_poll_ready(std::time::Duration::from_secs(1), false));
    app.open_quick_start();
    assert!(matches!(app.screen, Screen::Idle));
    let mut terminal = Terminal::new(ratatui::backend::TestBackend::new(120, 40)).unwrap();
    terminal.draw(|frame| app.hits = draw(frame, &app)).unwrap();
    let text: String = terminal
        .backend()
        .buffer()
        .content()
        .iter()
        .map(|c| c.symbol())
        .collect();
    assert!(!text.contains("Quick Start"));
    assert!(
        !app.hits
            .iter()
            .any(|h| h.control == Control::QuickStartAction)
    );
    app.connections = Connections::load(&app.state_dir).unwrap();
    app.controller.onboarding = None;
    assert!(app.quick_start_completed());
    app.open_quick_start();
    assert!(matches!(app.screen, Screen::Idle));
}

#[test]
fn admin_profile_card_hides_freeze_block_and_delete() {
    let mut app = App::new(&test_request());
    app.screen = Screen::Access;
    app.access.tab = crate::access::Tab::Profiles;
    app.access.card = Some(
        serde_json::json!({"id":"admin-profile","display_name":"Admin", "owner_account_id":"admin", "frozen":false, "_account":{"id":"admin","role":"admin","status":"approved","revision":1}}),
    );
    let mut terminal = Terminal::new(ratatui::backend::TestBackend::new(120, 50)).unwrap();
    terminal.draw(|frame| app.hits = draw(frame, &app)).unwrap();
    assert!(!app.hits.iter().any(|h| matches!(
        h.control,
        Control::AccessProfileAction(_) | Control::AccessAccountAction(false)
    )));
    assert!(app.hits.iter().any(|h| h.control == Control::AccessEdit));
}

#[test]
fn completed_guide_survives_empty_registry_and_changed_account_selector() {
    let mut app = test_profile_app();
    let profile = app.form.saved.as_ref().unwrap().id;
    app.connections
        .completed_quick_starts
        .push(format!("{profile}:previous-account"));
    app.form.saved.as_mut().unwrap().account = "new-selector".into();
    app.controller.onboarding =
        Some(serde_json::json!({"workstation_completed":false,"next_step":"node","node_key":null}));
    app.screen = Screen::Idle;
    assert!(app.quick_start_completed());
    app.open_quick_start();
    assert!(matches!(app.screen, Screen::Idle));
    assert!(!app.onboarding_poll_ready(std::time::Duration::from_secs(2), false));
}

#[test]
fn wizard_pairs_only_matching_regions_when_wide_and_pairs_protocols() {
    let mut app = test_profile_app();
    let templates: Vec<_> = [("lv","Europe","🇱🇻"),("de","Europe","🇩🇪"),("sg","Asia","🇸🇬"),("us","North America","🇺🇸")]
        .into_iter().map(|(code, region, flag)| serde_json::json!({"code":code,"title":code,"region":region,"flag":flag,"draft":{}})).collect();
    let options = serde_json::from_value(serde_json::json!({"local_available":true,"defaults":{"protocols":[],"xray_transports":[],"settings":{}},"templates":templates})).unwrap();
    app.node_wizard = Some(crate::node_wizard::Wizard::new(options).unwrap());
    app.node_wizard.as_mut().unwrap().step = crate::node_wizard::Step::Template;
    app.screen = Screen::NodeWizard;
    for width in [140, 70] {
        let mut terminal = Terminal::new(ratatui::backend::TestBackend::new(width, 40)).unwrap();
        terminal.draw(|frame| app.hits = draw(frame, &app)).unwrap();
        let choice = |index| {
            app.hits
                .iter()
                .find(|hit| hit.control == Control::WizardChoice(index))
                .unwrap()
                .area
        };
        if width == 140 {
            assert_eq!(choice(0).y, choice(1).y);
        } else {
            assert_ne!(choice(0).y, choice(1).y);
        }
        assert_ne!(choice(1).y, choice(2).y);
        assert_ne!(choice(2).y, choice(3).y);
    }
    app.node_wizard.as_mut().unwrap().step = crate::node_wizard::Step::Protocols;
    let mut terminal = Terminal::new(ratatui::backend::TestBackend::new(140, 40)).unwrap();
    terminal.draw(|frame| app.hits = draw(frame, &app)).unwrap();
    let choices: Vec<_> = app
        .hits
        .iter()
        .filter(|hit| matches!(hit.control, Control::WizardChoice(_)))
        .collect();
    assert_eq!(choices[0].area.y, choices[1].area.y);
}

#[test]
fn controller_text_dialogs_use_small_back_arrow_before_primary_action() {
    let mut app = test_profile_app();
    app.screen = Screen::Controller;
    app.controller.policies = Some(serde_json::json!({"requests":{"gate_message":"Original"}}));
    app.activate(Control::ControllerGateMessage);
    app.controller.defaults_input.as_mut().unwrap().1 = "New text".into();
    for confirm in [false, true] {
        app.controller.text_confirmation = confirm;
        let mut terminal = Terminal::new(ratatui::backend::TestBackend::new(120, 40)).unwrap();
        terminal.draw(|frame| app.hits = draw(frame, &app)).unwrap();
        let back = app
            .hits
            .iter()
            .find(|hit| hit.control == Control::ControllerBack)
            .unwrap()
            .area;
        let primary = app
            .hits
            .iter()
            .find(|hit| {
                hit.control
                    == if confirm {
                        Control::ControllerTextConfirm
                    } else {
                        Control::ControllerDefaultsInputApply
                    }
            })
            .unwrap()
            .area;
        assert_eq!(back.width, 5);
        assert_eq!(back.y, primary.y);
        assert!(back.right() < primary.x);
        assert!(primary.y.saturating_sub(back.y) < 4);
    }
}

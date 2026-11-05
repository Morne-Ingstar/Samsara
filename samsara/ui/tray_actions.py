"""Core tray actions, available before optional menu sections are built."""


def add_core_actions(tray, menu):
    from samsara.ui.passive_review import open_review
    from samsara.ui.context_help_qt import open_help

    app = tray._app
    show_action = tray._add(menu, "Show Samsara", lambda: app.show_main_window())
    menu.setDefaultAction(show_action)
    settings_action = tray._add(menu, "Settings", lambda: app.open_settings())
    # PLACEHOLDER COPY (1001)
    tray._add(menu, "Review on-screen messages", lambda: open_review(app))
    support_action = tray._add(menu, "Something wrong?", lambda: open_help(app))
    restart_action = tray._add(menu, "Restart Samsara", tray._restart_samsara)
    exit_action = tray._add(menu, "Exit", lambda: app.quit_app())
    return show_action, settings_action, support_action, restart_action, exit_action

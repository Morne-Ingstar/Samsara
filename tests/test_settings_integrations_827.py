"""827: Connect an AI app lives on the real Integrations tool page."""

from samsara.ui.settings import agents_section


def test_agents_controls_and_search_land_on_integrations(window, qapp):
    window._select_tool('Command library')
    commands = window._tool_pages['Command library']
    assert commands.findChild(agents_section.AgentsControls) is None

    # The existing Integrations route is the destination for the 819 alias.
    window.show_tab('Integrations')
    assert window._tool_name == 'Integrations'
    integrations = window._tool_pages['Integrations']
    controls = integrations.findChild(agents_section.AgentsControls)
    assert controls is not None
    assert integrations.isAncestorOf(controls)

    # Both setup deep links must reveal their target after leaving Commands.
    for key, target in (('agents', controls.enabled), ('agent_token', controls.rotate)):
        window.show_tab('Commands')
        controls.help.toggle.setChecked(False)
        window.focus_setting(key)
        qapp.processEvents()
        assert window._tool_name == 'Integrations'
        assert target.isVisibleTo(integrations)
        if key == 'agent_token':
            assert controls.help.toggle.isChecked()

    for term in ('Connect an AI app', 'agent'):
        window._search_edit.setText(term)
        window._apply_search_filter()
        qapp.processEvents()
        assert window._tool_name == 'Integrations'
        assert integrations.findChild(agents_section.AgentsControls) is controls

from tests.test_friendlier_settings_595 import window

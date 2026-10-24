"""858: AI app setup stays compact until enabled or revealed."""
from samsara.ui.settings.agents_section import AgentsControls
from tests.test_friendlier_settings_595 import window


def test_ai_setup_collapses_and_enable_or_deep_link_reveals(window, qapp):
    window._select_tool("Integrations")
    controls = window._tool_pages["Integrations"].findChild(AgentsControls)

    assert controls.details.toggle.isCheckable()
    assert not controls.details.toggle.isChecked()
    assert not controls.details.content.isVisible()
    assert controls.enable_row.isVisibleTo(controls)

    controls.details.toggle.click()
    qapp.processEvents()
    assert controls.details.content.isVisible()
    controls.details.toggle.click()
    assert not controls.details.content.isVisible()

    controls.enabled.setChecked(True)
    qapp.processEvents()
    assert controls.details.toggle.isChecked()
    assert controls.details.content.isVisible()

    controls.enabled.setChecked(False)
    controls.details.toggle.setChecked(False)
    window.focus_setting("agent_token")
    qapp.processEvents()
    assert controls.details.toggle.isChecked()
    assert controls.rotate.isVisibleTo(controls)

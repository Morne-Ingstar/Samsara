"""The AI app settings use the shared row and divider treatment."""
from types import SimpleNamespace

from PySide6.QtWidgets import QCheckBox, QWidget

from samsara.ui.settings import agents_section
from samsara.ui.settings.calm_controls import switch
from samsara.ui.settings.calm_layout import RowDividers, RowLayout
from samsara.ui.settings.presentation import SettingRow


def test_ai_app_settings_rows_keep_controls_with_titles_and_dividers(qapp, monkeypatch):
    server = SimpleNamespace(
        status="off", port=47832, enabled=False, typing_enabled=False,
        sync=lambda: None, connection_details=lambda: None,
        new_token=lambda: False, cancel_typing=lambda: None,
    )
    monkeypatch.setattr(agents_section, "for_app", lambda _app: server)
    owner = SimpleNamespace(app=SimpleNamespace(config={"command_packs": {"agents": True}}))
    panel = agents_section.AgentsControls(owner, QCheckBox())
    switch(panel.enabled)
    panel.resize(1000, 560)
    panel.show()
    qapp.processEvents()

    rows = panel.findChildren(SettingRow)
    assert [row.title for row in rows] == ["Let AI apps use Samsara", "Spoken replies"]
    assert panel.status.text() == ""
    page_divider = panel.findChild(QWidget, "settingsPageDivider")
    assert page_divider is not None
    assert page_divider.findChild(QWidget, "settingsPageHairline").height() == 1
    for row, control in ((panel.enable_row, panel.enabled),
                         (panel.digest_row, panel.digest_controls)):
        RowLayout(row)
        title_center = row.heading.mapTo(row, row.heading.rect().center()).y()
        control_center = control.mapTo(row, control.rect().center()).y()
        assert abs(control_center - title_center) <= 1
        assert abs(control.mapTo(row, control.rect().topLeft()).x()
                   - row.heading.mapTo(row, row.heading.rect().topRight()).x()) < 250

    dividers = RowDividers(panel.layout())
    qapp.processEvents()
    assert dividers.groups == [[panel.enable_row, panel.digest_row]]
    assert len(dividers.dividers) == 1
    hairline = dividers.dividers[0][3]
    assert hairline.objectName() == "settingsRowHairline" and hairline.height() == 1
    panel.close()
    panel.deleteLater()

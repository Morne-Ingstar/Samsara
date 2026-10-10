"""Real-font Settings input geometry; run separately at Qt scales 1, 1.25, 1.5, 2."""
import os
from pathlib import Path
from math import ceil

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QPA_FONTDIR", str(Path(os.environ.get("SystemRoot", "C:/Windows")) / "Fonts"))

import pytest
from PySide6.QtCore import QPoint, QRect, Qt
from PySide6.QtGui import QFontInfo
from PySide6.QtWidgets import QWidget, QAbstractSpinBox, QComboBox, QLineEdit, QPlainTextEdit, QStyle, QStyleOptionButton, QStyleOptionComboBox, QStyleOptionFrame, QStyleOptionSpinBox
from samsara.ui import theme
from tests.test_settings_pages import _StubApp, _schema_defaults


def input_geometry(widget):
    """Locate actual ink, and retain Qt's paint/cursor content geometry as witness."""
    widget.clearFocus()
    # Include the real parent surface beneath transparent hit-target margins.
    ancestor = widget.window()
    capture = ancestor.grab().toImage()
    ratio = capture.devicePixelRatio()
    origin = widget.mapTo(ancestor, QPoint())
    image = capture.copy(QRect(round(origin.x()*ratio), round(origin.y()*ratio),
                              round(widget.width()*ratio), round(widget.height()*ratio)))
    image.setDevicePixelRatio(ratio)
    dpr = image.devicePixelRatio()
    line = widget if isinstance(widget, QLineEdit) else widget.findChild(QLineEdit)
    if isinstance(widget, QAbstractSpinBox):
        option = QStyleOptionSpinBox(); widget.initStyleOption(option)
        box = widget.style().subControlRect(QStyle.CC_SpinBox, option, QStyle.SC_SpinBoxFrame, widget)
    else:
        box = widget.rect()
    if line is not None:
        option = QStyleOptionFrame(); line.initStyleOption(option)
        content = line.style().subElementRect(QStyle.SE_LineEditContents, option, line)
        if line is widget:
            box = QRect(0, content.y()-option.lineWidth, widget.width(),
                        content.height()+2*option.lineWidth)
        margins = line.textMargins()
        content = content.adjusted(margins.left(), margins.top(), -margins.right(), -margins.bottom())
        content.translate(line.mapTo(widget, QPoint()))
        cursor = line.cursorRect()
        tight = line.fontMetrics().tightBoundingRect(line.displayText())
        tight.translate(cursor.x() + cursor.width() // 2 - line.fontMetrics().horizontalAdvance(line.displayText()[:line.cursorPosition()]), cursor.y() + line.fontMetrics().ascent())
        tight.translate(line.mapTo(widget, QPoint()))
    elif isinstance(widget, QComboBox):
        option = QStyleOptionComboBox(); widget.initStyleOption(option)
        box = widget.style().subControlRect(QStyle.CC_ComboBox, option, QStyle.SC_ComboBoxFrame, widget)
        content = widget.style().subControlRect(QStyle.CC_ComboBox, option, QStyle.SC_ComboBoxEditField, widget)
        tight = widget.fontMetrics().tightBoundingRect(widget.currentText())
        aligned = QStyle.alignedRect(widget.layoutDirection(), Qt.AlignLeft | Qt.AlignVCenter,
                                    widget.fontMetrics().size(Qt.TextSingleLine, widget.currentText()), content)
        tight.translate(aligned.x()+1, aligned.y()+widget.fontMetrics().ascent())
    elif isinstance(widget, QPlainTextEdit):
        from PySide6.QtGui import QTextCursor
        content = widget.viewport().geometry()
        tight = QRect()
        block = widget.document().firstBlock()
        while block.isValid():
            layout = block.layout()
            encoded = block.text().encode('utf-16-le')
            for i in range(layout.lineCount()):
                text_line = layout.lineAt(i)
                cursor = QTextCursor(widget.document())
                cursor.setPosition(block.position() + text_line.textStart())
                caret = widget.cursorRect(cursor)
                text = encoded[2*text_line.textStart():2*(text_line.textStart()+text_line.textLength())].decode('utf-16-le')
                rect = widget.fontMetrics().tightBoundingRect(text)
                rect.translate(content.x()+caret.x(), content.y()+caret.y()+round(text_line.ascent()))
                tight = tight.united(rect)
            block = block.next()
    else:
        option = QStyleOptionButton(); widget.initStyleOption(option)
        box = widget.style().subElementRect(QStyle.SE_PushButtonBevel, option, widget)
        content = widget.style().subElementRect(QStyle.SE_PushButtonContents, option, widget)
        tight = widget.fontMetrics().tightBoundingRect(widget.text())
        aligned = QStyle.alignedRect(widget.layoutDirection(), Qt.AlignCenter,
                                    widget.fontMetrics().size(Qt.TextSingleLine, widget.text()), content)
        tight.translate(aligned.x(), aligned.y()+widget.fontMetrics().ascent())
    # Recover the visible border from the widget's own painted input surface.
    # The event rectangle can include transparent vertical hit-target margins.
    background = theme.qcolor(theme.BG2)
    filled = [y for y in range(image.height()) if image.pixelColor(image.width() // 2, y) == background]
    if filled and not isinstance(widget, QAbstractSpinBox):
        edge = round(dpr)
        physical_box = QRect(0, min(filled) - edge, image.width(), max(filled) - min(filled) + 1 + 2*edge)
    else:
        physical_box = QRect(round(box.x()*dpr), round(box.y()*dpr), round(box.width()*dpr), round(box.height()*dpr))
    # At fractional DPR a logical rect is not the raster border's coverage.
    # The transparent hit margin gives the actual row-background witness.
    middle = image.width()//2
    outside = image.pixelColor(middle, 0)
    if filled and min(filled) > ceil(dpr) and outside != background:
        frame_rows = [y for y in range(image.height())
                      if max(abs(image.pixelColor(middle,y).red()-outside.red()),
                             abs(image.pixelColor(middle,y).green()-outside.green()),
                             abs(image.pixelColor(middle,y).blue()-outside.blue())) > 2]
        physical_box = QRect(0,min(frame_rows),image.width(),max(frame_rows)-min(frame_rows)+1)
    # Include antialiased stems; near-solid foreground alone drops thin ascenders.
    foreground = theme.qcolor(theme.TEXT_PRIMARY)
    background = theme.qcolor(theme.BG2)
    delta = (foreground.red()-background.red(), foreground.green()-background.green(), foreground.blue()-background.blue())
    denominator = sum(v*v for v in delta)
    # Scan full height to retain overflow evidence. The line's ink width excludes
    # its caret; combo edit-field width excludes the separately painted arrow.
    horizontal = tight if line is not None else content
    scan = QRect(round(horizontal.x()*dpr), 0, round(horizontal.width()*dpr), image.height()).intersected(image.rect())
    # Exclude the painted frame border, not the content padding. Unclipped
    # Qt tight bounds below independently prove the complete text fits.
    scan = scan.intersected(physical_box.adjusted(ceil(dpr)+1, ceil(dpr)+1, -ceil(dpr)-1, -ceil(dpr)-1))
    ink = []
    for y in range(scan.top(), scan.bottom()+1):
        for x in range(scan.left(), scan.right()+1):
            pixel = image.pixelColor(x, y)
            relative = (pixel.red()-background.red(), pixel.green()-background.green(), pixel.blue()-background.blue())
            alpha = sum(a*b for a,b in zip(relative, delta))/denominator
            residual = max(abs(a-alpha*b) for a,b in zip(relative, delta))
            if .2 < alpha <= 1.05 and residual < 10:
                ink.append((x, y))
    assert ink, (type(widget).__name__, QFontInfo(widget.font()).family(),
                 dict(enabled=widget.isEnabled(), visible=widget.isVisible(),
                      box=box, content=content, tight=tight, scan=scan,
                      foreground=foreground.name(), background=background.name(),
                      palette_text=widget.palette().color(widget.foregroundRole()).name(),
                      stylesheet=widget.styleSheet()))
    glyph = QRect(min(x for x, y in ink), min(y for x, y in ink),
                  max(x for x, y in ink)-min(x for x, y in ink)+1,
                  max(y for x, y in ink)-min(y for x, y in ink)+1)
    above = glyph.top() - physical_box.top()
    below = physical_box.bottom() - glyph.bottom()
    result = dict(box=box, content=content, glyph=glyph, above=above, below=below,
                  dpr=dpr, tight=tight, font=QFontInfo(widget.font()).family(), theme=theme.active_theme(),
                  font_above=tight.top()-box.top(), font_below=box.bottom()-tight.bottom())
    if abs(above-below) > 1:
        print(type(widget).__name__, result, 'font metrics', widget.fontMetrics().height(),
              widget.fontMetrics().ascent(), 'editor', line.geometry() if line else None,
              'margins', line.textMargins() if line else None, 'cursor', line.cursorRect() if line else None)
    return result


@pytest.fixture(scope="module", params=("dark", "light"))
def settings_inputs(request, qapp):
    from samsara import autostart
    from samsara.ui.first_run_qt import ComponentsPage
    from samsara.ui.settings_qt import _SettingsWindow
    with pytest.MonkeyPatch.context() as patches:
        patches.setattr(autostart, "sync_run_entry", lambda *a, **kw: None)
        patches.setattr(ComponentsPage, "refresh_async", lambda self: None)
        theme.set_theme(request.param, refresh=False)
        config = _schema_defaults()
        config['echo_cancellation'] = {'enabled': True, 'latency_ms': 30}
        window = _SettingsWindow(_StubApp(config))
        window._widgets['adv_ducking_level'].setValue(0.10)
        window._widgets['cmd_stop_phrases'].setPlainText('stop\nfinish')
        window.resize(1160, 900)
        window.show()
        for _ in range(12): qapp.processEvents()
        assert theme.active_theme() == request.param
        yield window
        window.hide()
        qapp.processEvents()  # Drain row-hide callbacks before destroying rows.
        window.deleteLater()
        from PySide6.QtCore import QCoreApplication, QEvent
        QCoreApplication.sendPostedEvents(window, QEvent.Type.DeferredDelete)
        qapp.processEvents()
        theme.set_theme("dark", refresh=False)



@pytest.mark.parametrize("kind", ("decimal", "integer", "text", "combo", "shortcut", "multiline"))
def test_input_ink_is_centred_and_inside_content(settings_inputs, qapp, kind):
    window = settings_inputs
    types = dict(decimal=QAbstractSpinBox, integer=QAbstractSpinBox, text=QLineEdit, combo=QComboBox)
    if kind == 'decimal': widget = window._widgets['adv_ducking_level']
    else:
        candidates = [w for w in window._widgets.values() if isinstance(w, QWidget) and not w.isHidden()
                      and any(r.value.isAncestorOf(w) for r in window._destination_rows)]
        if kind == 'multiline': widget = window._widgets['cmd_stop_phrases']
        elif kind == 'shortcut': widget = next(w for w in candidates if type(w).__name__ == '_HotkeyButton')
        elif kind == 'integer': widget = next(w for w in candidates if type(w).__name__ == 'QSpinBox')
        elif kind == 'text': widget = window._widgets['adv_intent_command_prefix']
        else: widget = next(w for w in candidates if isinstance(w, QComboBox) and w.currentText())
    row = next(r for r in window._destination_rows if r.value.isAncestorOf(widget))
    if kind == 'text':
        widget.setText('Fixture glyph')  # Exercise ascenders and descenders.
    window.show_tab(row.page_name)
    if row.disclosure: row.disclosure.reveal()
    for _ in range(12): qapp.processEvents()
    window._pages[row.page_name].ensureWidgetVisible(row)
    for _ in range(12): qapp.processEvents()
    if kind == 'text':
        # Ava's local-model row is intentionally hidden while Ava is Off.
        # Measure a rendered ordinary field, with the same geometry assertions.
        assert widget.isVisible() and widget.isEnabled()
    data = input_geometry(widget)
    if kind == 'text':
        print('ordinary QLineEdit geometry:', data)
    assert data['font'] == 'Segoe UI'
    # The brief specifies Qt tight font bounds at the actual paint position.
    # Widget/style/cursor coordinates are Qt pixels at every display scale.
    # Raster bounds remain separate evidence of antialiasing and clipping.
    assert abs(data['font_above'] - data['font_below']) <= 1, (kind, data)
    content = data['content']; dpr = data['dpr']
    physical = QRect(round(content.x()*dpr), round(content.y()*dpr), round(content.width()*dpr), round(content.height()*dpr))
    assert physical.contains(data['glyph']), (kind, data)
    assert content.contains(data['tight']), (kind, data)


def test_number_column_fits_legal_values_and_shares_right_edge(settings_inputs, qapp):
    window = settings_inputs
    keys = tuple(key for key in ('adv_ducking_level', 'adv_hands_free_ducking_level', 'adv_hands_free_idle_level', 'adv_aec_latency')
                 if key in window._rows_by_widget)
    assert len(keys) >= 3
    window.show_tab('Audio'); window._pages['Audio'].advanced.reveal()
    for _ in range(12): qapp.processEvents()
    right_edges = []
    for key in keys:
        widget = window._widgets[key]
        for value in (widget.minimum(), widget.maximum()):
            text = widget.prefix() + widget.textFromValue(value) + widget.suffix()
            option = QStyleOptionSpinBox(); widget.initStyleOption(option)
            field = widget.style().subControlRect(QStyle.CC_SpinBox, option, QStyle.SC_SpinBoxEditField, widget)
            assert field.width() >= widget.fontMetrics().horizontalAdvance(text) + 4
        assert widget.buttonSymbols() == QAbstractSpinBox.NoButtons, (key, widget.buttonSymbols(), getattr(widget, '_settings_input_symbols', None))
        right_edges.append(widget.mapTo(window, QPoint(widget.width(), 0)).x())
    assert len(set(right_edges)) == 1, right_edges



def test_scalar_inputs_share_inner_side_padding(settings_inputs, qapp):
    window = settings_inputs
    observed = set()
    padding_by_key = {}
    nearest = {s for s in theme.SPACE_SCALE if abs(s - 12) == min(abs(v - 12) for v in theme.SPACE_SCALE)}
    for key, widget in window._widgets.items():
        hotkey = any(c.__name__ == '_HotkeyButton' for c in type(widget).__mro__)
        if not isinstance(widget, (QAbstractSpinBox, QComboBox, QPlainTextEdit, QLineEdit)) and not hotkey:
            continue
        row = next((r for r in window._destination_rows if r.value.isAncestorOf(widget)), None)
        if row is None: continue
        if widget.isHidden(): continue
        window.show_tab(row.page_name)
        if row.disclosure: row.disclosure.reveal()
        for _ in range(3): qapp.processEvents()
        window._pages[row.page_name].ensureWidgetVisible(row)
        for _ in range(3): qapp.processEvents()
        if isinstance(widget, QAbstractSpinBox):
            option = QStyleOptionSpinBox(); widget.initStyleOption(option)
            content = widget.style().subControlRect(QStyle.CC_SpinBox, option, QStyle.SC_SpinBoxEditField, widget)
        elif isinstance(widget, QComboBox):
            option = QStyleOptionComboBox(); widget.initStyleOption(option)
            content = widget.style().subControlRect(QStyle.CC_ComboBox, option, QStyle.SC_ComboBoxEditField, widget)
        elif isinstance(widget, QPlainTextEdit):
            content = widget.viewport().geometry()
        elif isinstance(widget, QLineEdit):
            option = QStyleOptionFrame(); widget.initStyleOption(option)
            content = widget.style().subElementRect(QStyle.SE_LineEditContents, option, widget)
        elif hotkey:
            option = QStyleOptionButton(); widget.initStyleOption(option)
            content = widget.style().subElementRect(QStyle.SE_PushButtonContents, option, widget)
        else:
            continue
        # Border is separate from the token padding; Qt's cursor inset is also separate.
        observed.add(content.x() - 1)
        padding_by_key[key] = content.x() - 1
    if len(observed) != 1 or not observed <= nearest:
        print('padding exceptions', {k: v for k, v in padding_by_key.items() if v not in nearest})
    assert len(observed) == 1 and observed <= nearest, padding_by_key


def test_late_alarm_dialog_inputs_keep_buttons_and_fit(settings_inputs, qapp, monkeypatch):
    from PySide6.QtWidgets import QDialog, QSpinBox
    from samsara.ui.settings.presentation import CleanComboBox
    window = settings_inputs
    observed = []

    def inspect(dialog):
        name = dialog.findChild(QLineEdit)
        name.setText('Fixture')
        dialog.show()
        for _ in range(12): qapp.processEvents()
        spin = dialog.findChild(QSpinBox)
        buttons = spin.buttonSymbols()
        spin.setValue(spin.maximum())
        for widget in (name, spin, dialog.findChild(CleanComboBox)):
            for _ in range(12): qapp.processEvents()
            data = input_geometry(widget)
            assert abs(data['font_above'] - data['font_below']) <= 1, data
            content = data['content']; ratio = data['dpr']
            rect = QRect(round(content.x()*ratio), round(content.y()*ratio),
                         round(content.width()*ratio), round(content.height()*ratio))
            assert rect.contains(data['glyph']), data
            assert content.contains(data['tight']), data
        assert buttons == QAbstractSpinBox.UpDownArrows
        field = spin.findChild(QLineEdit)
        assert field.width() >= field.fontMetrics().horizontalAdvance(spin.text()) + 4
        observed.append(True)
        dialog.hide()
        return QDialog.Rejected

    monkeypatch.setattr(QDialog, 'exec', inspect)
    window._open_alarm_dialog(None, window._widgets['alarms_table'])
    assert observed == [True]

"""Ephemeral, apartment-local activation binding. Never log control contents."""
import hashlib
import json


def revision(nodes, controls, stats):
    if stats.get('budget_hit'):
        return ''
    rows = []
    try:
        for node, control in zip(nodes, controls):
            if hasattr(control, 'live'):
                control = control.live()
            elif hasattr(control, 'control'):
                control = control.control()
            raw = getattr(control, 'element', None)
            identity = (list(raw.GetRuntimeId()) if raw is not None else
                        list(control.GetRuntimeId()) if hasattr(control, 'GetRuntimeId') else node.index)
            value = ''
            if node.control_type in {'EditControl', 'DocumentControl', 'ComboBoxControl'}:
                if getattr(control, 'IsPassword', False):
                    value = '<password>'
                else:
                    pattern = (control.pattern('value') if hasattr(control, 'pattern') else
                               getattr(control, 'GetValuePattern', lambda: None)())
                    if pattern is not None:
                        value = pattern.CurrentValue if raw is not None else pattern.Value
                    else:
                        pattern = (control.pattern('text') if hasattr(control, 'pattern') else
                                   getattr(control, 'GetTextPattern', lambda: None)())
                        if pattern is not None:
                            value = pattern.DocumentRange.GetText(-1)
                        elif node.control_type == 'EditControl':
                            return ''  # An unreadable recipient/body cannot be approved.
            rows.append((identity, node.name, node.control_type, node.rect,
                         node.enabled, node.offscreen, value))
        return hashlib.sha256(json.dumps(rows, ensure_ascii=True).encode()).hexdigest()
    except Exception:
        return ''

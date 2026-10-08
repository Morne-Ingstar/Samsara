"""Pure three-way reconciliation for Settings controls and live configuration."""
import copy


_MISSING = object()


def merge_outside_changes(opened: dict, live: dict, updates: dict) -> tuple[dict, list[str]]:
    """Keep live leaves only when the dialog still has their opening values.

    Only top-level keys in updates are written. Rebuilt nested sections also
    retain outside additions that were absent both at open and in the dialog.
    Missing keys differ from None; lists and container type changes are atomic.
    The result owns its nested values and never mutates any of the inputs.
    """
    kept: list[str] = []

    def merge_value(before, current, dialog, path):
        if (isinstance(dialog, dict)
                and (isinstance(before, dict) or before is _MISSING)
                and (isinstance(current, dict) or current is _MISSING)
                and not (current is _MISSING and dialog == before)):
            old_dict = before if isinstance(before, dict) else {}
            live_dict = current if isinstance(current, dict) else {}
            result = {}
            for key in dict.fromkeys((*dialog, *live_dict)):
                value = merge_value(
                    old_dict.get(key, _MISSING), live_dict.get(key, _MISSING),
                    dialog.get(key, _MISSING), f"{path}.{key}",
                )
                if value is not _MISSING:
                    result[key] = value
            return result
        if dialog == before and current != before:
            kept.append(path)
            return _MISSING if current is _MISSING else copy.deepcopy(current)
        return _MISSING if dialog is _MISSING else copy.deepcopy(dialog)

    merged = {}
    for key, dialog in updates.items():
        value = merge_value(opened.get(key, _MISSING), live.get(key, _MISSING), dialog, str(key))
        if value is not _MISSING:
            merged[key] = value
    return merged, kept

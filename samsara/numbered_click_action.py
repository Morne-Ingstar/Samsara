"""Stale-target handling owned by the numbered overlay."""


def element_action(app, element, modifier, keys_frozen, activate):
    from samsara.click_effect_policy import element_action as effect_action
    from samsara.uia import UIAError

    try:
        return effect_action(app, element, modifier, keys_frozen, activate)
    except UIAError:
        # The caller supplies stale-label feedback and retires its overlay.
        return False

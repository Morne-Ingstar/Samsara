"""Owner, 2026-11-20: "you should absolutely not have to confirm a submit."

Hands-free "submit" into an agent/chat window Samsara does not recognise asked
for a yes/no and then timed out. The user's own spoken submit is immediate;
email clients, text the user did not author, and model/schedule routes still ask.
"""
import pytest

from samsara import effect_policy as effects
from samsara import execution_policy as ep


def _target(kind="unknown", process="warp.exe", **kw):
    return effects.SubmitTarget(kind, 7, process, "ctl", "rev", **kw)


@pytest.mark.parametrize("process", ["warp.exe", "codex.exe", "chrome.exe", "discord.exe", "windowsterminal.exe"])
@pytest.mark.parametrize("route", ["exact", "grammar"])
def test_users_own_submit_never_asks(process, route):
    assert effects.send_handling(_target(process=process), route=route) == effects.IMMEDIATE


@pytest.mark.parametrize("process", sorted(effects.EMAIL_PROCESSES))
def test_email_clients_still_confirm_even_when_unreadable(process):
    assert effects.send_handling(_target(process=process)) == effects.CONFIRM
    assert effects.send_handling(_target("email", process)) == effects.CONFIRM


def test_model_and_schedule_routes_still_ask():
    assert effects.send_handling(_target(), route=ep.Route.MODEL) == effects.CONFIRM
    assert effects.send_handling(_target(), route=ep.Route.SCHEDULE) == effects.CONFIRM


def test_text_the_user_did_not_author_still_asks():
    assert effects.send_handling(_target(user_authored=False)) == effects.CONFIRM

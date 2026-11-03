"""1031c: explicit configured Slack DMs and the current classic Outlook draft."""
from samsara.plugin_commands import command
from samsara import message_delivery as delivery


@command("slack message", pack="core", risk_class="destructive", reversible=False,
         ai_visible=False, ai_composable=False, side_effects=["message_sent"],
         param_schema={"remainder": {"type": "str", "required": True}})
def slack_message(app, remainder):
    """Says slack message contact saying text; sends to that configured individual DM."""
    inv = delivery.take(app, "slack message")
    name, separator, body = remainder.partition(" saying ")
    return delivery.slack(app, inv, name.strip(), body if separator else "")


@command("trust slack conversation", pack="core", risk_class="destructive", reversible=False,
         ai_visible=False, ai_composable=False, side_effects=["security_change"],
         param_schema={"remainder": {"type": "str", "required": True}})
def trust_slack(app, remainder):
    """Approves the exact configured Slack account, individual and conversation once."""
    return delivery.slack(app, delivery.take(app, "trust slack conversation"),
                          remainder.strip(), "", setup=True)


@command("send outlook email", pack="core", risk_class="destructive", reversible=False,
         ai_visible=False, ai_composable=False, side_effects=["email_sent"],
         param_schema={"remainder": {"type": "str", "required": False}})
def send_outlook_email(app, remainder):
    """Previews the current classic Outlook email and asks once before sending it."""
    inv = delivery.take(app, "send outlook email")
    if remainder.strip():
        return delivery._result("REJECTED", "send outlook email", reason="Open the email draft first")
    return delivery.outlook(app, inv)

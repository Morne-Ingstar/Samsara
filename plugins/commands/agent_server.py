"""Service-only plugin: boot the optional agent endpoint, with no commands."""


def start_services(app):
    from samsara.agent_server import for_app
    for_app(app).sync()

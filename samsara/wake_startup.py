"""Complete lazy wake startup without publishing a stopped-listener gap."""


def finish_pending_start(app, requested):
    """Keep startup pending through capture start AND wake detection arming.

    Model readiness alone does not mean listening has started. Home can poll
    during activation, so clear pending only once activation returns. A failed
    activation must still leave the normal stopped/restart notice available.
    """
    try:
        if requested and app._wake_start_pending and not app.wake_word_active:
            app.start_wake_word_mode()
    finally:
        with app._wake_models_lock:
            app._wake_start_pending = False
        app._publish_wake_state()

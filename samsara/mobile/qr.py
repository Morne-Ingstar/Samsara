"""QR code generation for the mobile companion connect URL.

Generated locally via the `qrcode` package -- the connect URL never leaves
this process, unlike sending it to a third-party QR-rendering API (the
approach the quarantined mobile_companion.py.disabled used).
"""


def generate_png(url):
    """Return PNG bytes encoding `url`, or None if qrcode isn't installed."""
    try:
        import qrcode
    except ImportError:
        return None
    import io
    image = qrcode.make(url)
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue()

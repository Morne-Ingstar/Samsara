"""Hyperion LED strip control plugin.

Control your Hyperion ambient lighting by voice.

Commands:
  "Jarvis, lights red"           - solid color
  "Jarvis, light effect rainbow" - named effect
  "Jarvis, lights off"           - turn off
  "Jarvis, lights on"            - restore default

Setup: Set hyperion_host in samsara_config.json to your Pi's IP.
"""

import json
import socket
from samsara.plugin_commands import command


def _send(app, payload):
    host = app.config.get('hyperion_host', '')
    port = int(app.config.get('hyperion_port', 19444))
    if not host:
        print("[LIGHTS] No hyperion_host in config")
        return None
    try:
        addrinfo = socket.getaddrinfo(host, port, socket.AF_UNSPEC, socket.SOCK_STREAM)
        if not addrinfo:
            print(f"[LIGHTS] Cannot resolve {host}")
            return None
        family, socktype, proto, _, addr = addrinfo[0]
        sock = socket.socket(family, socktype, proto)
        sock.settimeout(3)
        sock.connect(addr)
        sock.sendall(json.dumps(payload).encode() + b'\n')  # <-- this was missing
        resp = b''  # <-- this was missing
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                break
            resp += chunk
            if b'\n' in chunk:
                break
        sock.close()
        return json.loads(resp.decode().strip())
    except Exception as e:
        print(f"[LIGHTS] {e}")
    return None

def _effects(app):
    r = _send(app, {"command": "serverinfo"})
    if r and 'info' in r:
        return [e['name'] for e in r['info'].get('effects', [])]
    return []


COLORS = {
    'red': [255,0,0], 'green': [0,255,0], 'blue': [0,0,255],
    'white': [255,255,255], 'yellow': [255,255,0],
    'purple': [128,0,255], 'pink': [255,50,150],
    'orange': [255,100,0], 'cyan': [0,255,255],
    'warm': [255,180,100], 'cool': [150,200,255],
}

ALIASES = {
    'rainbow': 'Rainbow swirl', 'police': 'Police Lights Single',
    'strobe': 'Strobe white', 'fire': 'Fire flicker',
    'candle': 'Candle', 'mood': 'Full color mood blobs',
    'breathe': 'Breath', 'knight rider': 'Knight rider',
    'snake': 'Snake', 'cinema': 'Cinema brighten lights',
    'rain': 'Rain', 'sparks': 'Sparks Color',
    'waves': 'Waves with Color', 'plasma': 'Plasma',
}


def _match(name, avail):
    n = name.lower().strip()
    if n in ALIASES:
        for e in avail:
            if e.lower() == ALIASES[n].lower():
                return e
    for e in avail:
        if e.lower() == n:
            return e
    for e in avail:
        if n in e.lower():
            return e
    return None


@command("lights", aliases=[
    "set lights to", "set the lights to", "change lights to",
    "make the lights", "light color", "set lights", "lights to"
])
def handle_color(app, remainder):
    if not remainder:
        return True
    c = remainder.strip().lower()
    if c in COLORS:
        r = _send(app, {"command":"color","color":COLORS[c],"priority":1,"origin":"Samsara"})
        print(f"[LIGHTS] {'Set to '+c if r and r.get('success') else 'Failed'}")
    else:
        print(f"[LIGHTS] Unknown: {c}. Try: {', '.join(COLORS)}")
    return True


@command("light effect", aliases=[
    "lights effect", "run effect", "start effect", "play effect", "effect"
])
def handle_effect(app, remainder):
    if not remainder:
        return True
    name = remainder.strip()
    avail = _effects(app)
    matched = _match(name, avail) if avail else ALIASES.get(name.lower(), name)
    r = _send(app, {"command":"effect","effect":{"name":matched},"priority":1,"origin":"Samsara"})
    print(f"[LIGHTS] {'Effect: '+matched if r and r.get('success') else 'Failed: '+matched}")
    return True


@command("lights off", aliases=["turn off the lights","turn lights off","kill the lights","lights out"])
def handle_off(app, remainder):
    _send(app, {"command":"color","color":[0,0,0],"priority":1,"origin":"Samsara"})
    print("[LIGHTS] Off")
    return True


@command("lights on", aliases=["turn on the lights","turn lights on"])
def handle_on(app, remainder):
    _send(app, {"command":"clear","priority":1})
    print("[LIGHTS] Restored")
    return True


@command("list effects", aliases=["what effects are there","show effects"])
def handle_list(app, remainder):
    fx = _effects(app)
    if fx:
        for e in sorted(fx):
            print(f"  - {e}")
    else:
        print("[LIGHTS] Cannot retrieve effects")
    return True
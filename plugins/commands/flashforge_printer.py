"""FlashForge AD5X 3D printer control plugin.

Control your FlashForge AD5X via voice over TCP (port 8899).

Commands:
  "Jarvis, print me a gun"        - start the demo print file
  "Jarvis, start print benchy"    - start a named file
  "Jarvis, printer status"        - temperatures, state, progress
  "Jarvis, pause print"           - pause current print
  "Jarvis, resume print"          - resume paused print
  "Jarvis, cancel print"          - cancel current print
  "Jarvis, printer light"         - toggle chamber light
  "Jarvis, list print files"      - show files on printer

Setup in samsara_config.json:
  "flashforge_ip": "192.168.50.86",
  "flashforge_serial": "SNMQRE9417620",
  "flashforge_check_code": "0e35a229",
  "flashforge_print_file": "toy_gun.gcode"

Protocol: FlashForge TCP M-code on port 8899.
Tested on AD5X firmware v1.2.3.
"""

import socket
import subprocess
import os

from samsara.plugin_commands import command

TCP_PORT = 8899
ORCA_PATH = r"C:\Program Files\Flashforge\Orca-Flashforge\orca-flashforge.exe"


def _send(app, mcode):
    """Send an M-code to the printer and return the response."""
    host = app.config.get('flashforge_ip', '')
    if not host:
        print("[3DP] No flashforge_ip configured")
        return None
    try:
        addrinfo = socket.getaddrinfo(host, TCP_PORT,
                                       socket.AF_UNSPEC, socket.SOCK_STREAM)
        if not addrinfo:
            print(f"[3DP] Cannot resolve {host}")
            return None
        family, socktype, proto, _, addr = addrinfo[0]
        sock = socket.socket(family, socktype, proto)
        sock.settimeout(5)
        sock.connect(addr)
        sock.sendall(f"~{mcode}\r\n".encode())
        resp = b''
        while True:
            try:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                resp += chunk
                if b'ok' in resp:
                    break
            except socket.timeout:
                break
        sock.close()
        return resp.decode('utf-8', errors='replace').strip()
    except Exception as e:
        print(f"[3DP] {e}")
    return None


def _parse_status(resp):
    """Parse M119 response into a dict."""
    info = {}
    if not resp:
        return info
    for line in resp.split('\n'):
        line = line.strip()
        if ':' in line and not line.startswith('CMD'):
            key, _, val = line.partition(':')
            info[key.strip()] = val.strip()
    return info


def _parse_temps(resp):
    """Parse M105 response: T0:24.5/0.0 B:23.8/0.0"""
    if not resp:
        return {}
    temps = {}
    for line in resp.split('\n'):
        if line.startswith('T0:'):
            parts = line.split()
            for p in parts:
                if p.startswith('T0:'):
                    cur, _, target = p[3:].partition('/')
                    temps['extruder'] = f"{cur}/{target}"
                elif p.startswith('B:'):
                    cur, _, target = p[2:].partition('/')
                    temps['bed'] = f"{cur}/{target}"
    return temps


# --- Voice commands ---

@command("print me a gun", aliases=[
    "print a gun", "3d print a gun",
    "three d print a gun", "print me a weapon"
])
def handle_print_gun(app, remainder):
    """Open the toy gun model in Orca FlashForge slicer."""
    model_file = app.config.get('flashforge_model_file',
                                os.path.expanduser(r'~\Downloads\funnygun.3mf'))
    orca = app.config.get('orca_path', ORCA_PATH)

    if os.path.exists(model_file):
        print(f"[3DP] Opening model in Orca: {os.path.basename(model_file)}")
        try:
            subprocess.Popen([orca, model_file])
        except FileNotFoundError:
            # Try opening with default association
            os.startfile(model_file)
        if hasattr(app, 'play_sound'):
            try:
                app.play_sound("start")
            except Exception:
                pass
    else:
        print(f"[3DP] Model file not found: {model_file}")
        print("[3DP] Set flashforge_model_file in config or place funnygun.3mf in Downloads")
    return True


@command("start print", aliases=[
    "start printing", "begin print", "begin printing", "print file"
])
def handle_start_print(app, remainder):
    """Start printing a named file. Usage: 'Jarvis, start print benchy'"""
    if not remainder:
        filename = app.config.get('flashforge_print_file', '')
        if not filename:
            print("[3DP] No file specified")
            return True
    else:
        filename = remainder.strip()
        if not filename.endswith(('.gcode', '.3mf', '.gx')):
            filename += '.gcode'

    resp = _send(app, f"M23 0:/user/{filename}")
    if resp and 'ok' in resp.lower():
        start = _send(app, "M24")
        print(f"[3DP] {'Printing: ' + filename if start and 'ok' in start.lower() else 'Start failed'}")
    else:
        print(f"[3DP] File not found: {filename}")
    return True


@command("printer status", aliases=[
    "print status", "printer state", "how is the print",
    "print progress", "is the printer done", "check the printer"
])
def handle_status(app, remainder):
    """Show printer status, temperatures, and progress."""
    status = _parse_status(_send(app, "M119"))
    temps = _parse_temps(_send(app, "M105"))
    progress = _send(app, "M27")

    state = status.get('MachineStatus', 'Unknown')
    print(f"[3DP] Status: {state}")

    if temps:
        ext = temps.get('extruder', '?')
        bed = temps.get('bed', '?')
        print(f"[3DP] Extruder: {ext}°C | Bed: {bed}°C")

    if progress:
        for line in progress.split('\n'):
            line = line.strip()
            if 'byte' in line.lower() or 'layer' in line.lower():
                print(f"[3DP] {line}")

    cur_file = status.get('CurrentFile', '')
    if cur_file:
        print(f"[3DP] File: {cur_file}")

    led = status.get('LED', '')
    if led:
        print(f"[3DP] Light: {'on' if led == '1' else 'off'}")

    return True


@command("pause print", aliases=[
    "pause printing", "pause the print", "hold the print"
])
def handle_pause(app, remainder):
    """Pause the current print."""
    resp = _send(app, "M25")
    print(f"[3DP] {'Paused' if resp and 'ok' in resp.lower() else 'Pause failed'}")
    return True


@command("resume print", aliases=[
    "resume printing", "continue print", "continue printing"
])
def handle_resume(app, remainder):
    """Resume a paused print."""
    resp = _send(app, "M24")
    print(f"[3DP] {'Resumed' if resp and 'ok' in resp.lower() else 'Resume failed'}")
    return True


@command("cancel print", aliases=[
    "cancel printing", "stop print", "stop printing", "abort print"
])
def handle_cancel(app, remainder):
    """Cancel the current print."""
    resp = _send(app, "M26")
    print(f"[3DP] {'Cancelled' if resp and 'ok' in resp.lower() else 'Cancel failed'}")
    return True


@command("printer light", aliases=[
    "toggle printer light", "printer lamp",
    "chamber light", "turn on printer light"
])
def handle_light(app, remainder):
    """Toggle the chamber light."""
    resp = _send(app, "M146 r255 g255 b255 F0")
    if not resp or 'ok' not in resp.lower():
        # Try alternate command
        resp = _send(app, "M6033")
    print(f"[3DP] {'Light toggled' if resp and 'ok' in resp.lower() else 'Light command failed'}")
    return True


@command("list print files", aliases=[
    "what can i print", "show print files",
    "available prints", "printer files"
])
def handle_list_files(app, remainder):
    """List files on the printer."""
    resp = _send(app, "M20")
    if resp:
        files = []
        for line in resp.split('\n'):
            line = line.strip()
            if (line and not line.startswith('CMD') and line != 'ok'
                    and 'Received' not in line and 'Begin' not in line
                    and 'End' not in line):
                files.append(line)
        if files:
            print(f"[3DP] Files on printer:")
            for f in files:
                print(f"  - {f}")
        else:
            print("[3DP] No files on printer. Upload via Orca-FlashForge first.")
    else:
        print("[3DP] Cannot reach printer")
    return True

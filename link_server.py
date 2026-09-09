#!/usr/bin/env python3
"""Minimal link-state server for the Omarchy Link plugin.

The phone (OhmLauncher) scans the plugin's `omarchy://<pc-ip>:8753?id=<name>`
QR. After showing its "connected" snackbar, OhmLauncher POSTs back to this
server so the PC plugin can reflect the live connection state.

Endpoints:
  POST /omarchy/link        body: {"ip": "<phone-ip>", "name": "<phone-name>"}
                             -> mark connected, persist to STATE_FILE.
  POST /omarchy/link/bye    -> mark disconnected.
  GET  /omarchy/link        -> return current state JSON.

State is written to /tmp/omarchy-link-state.json which the QML panel watches
via a FileView, so the bar icon turns green and the panel shows "Conectado".
"""

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

STATE_FILE = "/tmp/omarchy-link-state.json"
SCREEN_FILE = "/tmp/omarchy-screen.png"
HOST = "0.0.0.0"
# Port is configurable so the emulator lab can run this server on a different
# port than the adb-forwarded phone API (both default to 8753 otherwise).
PORT = int(os.environ.get("OMARCHY_LINK_PORT", "8753"))
# Optional "ip:port" rewrite of the phone address stored in the state file.
# Lab use: the emulator's NAT IP is unreachable from the PC, which instead
# reaches the phone through `adb forward` on 127.0.0.1. On a real LAN this
# stays unset and the phone's self-reported IP is used as-is.
PEER_OVERRIDE = os.environ.get("OMARCHY_LINK_PEER", "")
# RLock (not Lock): write_screen_frame() holds it and calls log() at every
# 30th frame; a plain Lock self-deadlocks there and wedges the whole server.
_lock = threading.RLock()


SCREEN_STATE_FILE = "/tmp/omarchy-screen-state.json"
_frame_count = 0


def _screen_path_for(raw: bytes) -> str:
    # JPEG starts with FFD8; otherwise assume PNG.
    if raw[:2] == b"\xff\xd8":
        return "/tmp/omarchy-screen.jpg"
    return "/tmp/omarchy-screen.png"


def write_screen_frame(raw: bytes, w: int = 0, h: int = 0) -> None:
    global _frame_count
    try:
        path = _screen_path_for(raw)
        with _lock:
            with open(path, "wb") as f:
                f.write(raw)
            _frame_count += 1
            with open(SCREEN_STATE_FILE, "w", encoding="utf-8") as s:
                import time
                json.dump({"frames": _frame_count, "last": time.time(),
                           "w": w, "h": h}, s)
            if _frame_count % 30 == 0:
                log("frame %d received (%d bytes)" % (_frame_count, len(raw)))
    except OSError:
        pass


def read_screen_state() -> dict:
    try:
        with _lock:
            with open(SCREEN_STATE_FILE, "r", encoding="utf-8") as s:
                return json.load(s)
    except (OSError, ValueError):
        return {"frames": 0, "last": 0}


def log(msg: str) -> None:
    try:
        with _lock, open("/tmp/ls.log", "a", encoding="utf-8") as f:
            import datetime
            f.write(datetime.datetime.now().strftime("%H:%M:%S") + " " + msg + "\n")
    except OSError:
        pass


def _wl_set_clipboard(text: str) -> bool:
    """Copy text into the desktop clipboard (wl-copy on Wayland, xclip on X11)."""
    import subprocess
    for cmd in (["wl-copy"], ["xclip", "-selection", "clipboard"]):
        try:
            subprocess.run(cmd, input=text.encode("utf-8"), timeout=3,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
        except (OSError, subprocess.SubprocessError):
            continue
    return False


def _wl_get_clipboard() -> str:
    """Read the desktop clipboard (wl-paste on Wayland, xclip on X11)."""
    import subprocess
    for cmd in (["wl-paste", "-n"], ["xclip", "-selection", "clipboard", "-o"]):
        try:
            out = subprocess.run(cmd, capture_output=True, timeout=3)
            if out.returncode == 0:
                return out.stdout.decode("utf-8", "replace")
        except (OSError, subprocess.SubprocessError):
            continue
    return ""


def write_state(state: dict) -> None:
    try:
        with _lock:
            with open(STATE_FILE, "w", encoding="utf-8") as f:
                json.dump(state, f)
        log("state -> connected=%s peer=%s" % (state.get("connected"), state.get("peerIp")))
    except OSError:
        pass


def read_state() -> dict:
    try:
        with _lock:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except (OSError, ValueError):
        return {"connected": False, "peerIp": "", "peerPort": 8753,
                "peerName": "", "linkPort": PORT}


class Handler(BaseHTTPRequestHandler):
    def _read_body(self) -> bytes:
        """Read the request body, decoding Transfer-Encoding: chunked when
        present (dart:io's HttpClient sends chunked unless contentLength is
        set, and http.server does not decode it by itself)."""
        te = (self.headers.get("Transfer-Encoding") or "").lower()
        if "chunked" in te:
            out = bytearray()
            while True:
                size_line = self.rfile.readline(65536).strip()
                try:
                    size = int(size_line.split(b";")[0], 16)
                except ValueError:
                    break
                if size == 0:
                    # Consume trailers up to the blank line.
                    while self.rfile.readline(65536) not in (b"\r\n", b"\n", b""):
                        pass
                    break
                out += self.rfile.read(size)
                self.rfile.read(2)  # trailing CRLF
            return bytes(out)
        length = int(self.headers.get("Content-Length", "0"))
        return self.rfile.read(length) if length else b""

    def _cors(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _json(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self._cors()
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):
        if self.path == "/omarchy/link":
            self._json(200, read_state())
        elif self.path == "/omarchy/screen/status":
            self._json(200, read_screen_state())
        elif self.path == "/omarchy/clipboard":
            self._json(200, {"text": _wl_get_clipboard()})
        else:
            self._json(404, {"error": "not found"})

    @staticmethod
    def _peer_from(data: dict) -> tuple:
        """Resolve the phone address the panel should call. PEER_OVERRIDE wins
        (emulator lab); otherwise the phone's self-reported ip (+api port)."""
        if PEER_OVERRIDE:
            ip, _, port = PEER_OVERRIDE.partition(":")
            return ip, int(port or "8753")
        ip = str(data.get("ip", ""))
        try:
            port = int(data.get("port", 8753))
        except (TypeError, ValueError):
            port = 8753
        return ip, port

    def do_POST(self):
        if self.path == "/omarchy/link":
            try:
                raw = self._read_body()
                data = json.loads(raw.decode("utf-8") or "{}")
            except (ValueError, OSError):
                raw, data = b"", {}
            log("link POST from %s body=%s" % (self.client_address[0], raw[:200]))
            ip, port = self._peer_from(data)
            state = {
                "connected": True,
                "peerIp": ip,
                "peerPort": port,
                "peerName": str(data.get("name", "phone")),
                "linkPort": PORT,
            }
            write_state(state)
            self._json(200, state)
        elif self.path == "/omarchy/link/bye":
            write_state({"connected": False, "peerIp": "", "peerPort": 8753,
                         "peerName": "", "linkPort": PORT})
            self._json(200, {"connected": False})
        elif self.path.startswith("/omarchy/screen/frame"):
            try:
                raw = self._read_body()
                # Phone pixel size arrives as ?w=&h= so the panel can map
                # remote-control taps back onto the phone screen.
                from urllib.parse import urlparse, parse_qs
                q = parse_qs(urlparse(self.path).query)
                w = int(q.get("w", ["0"])[0] or 0)
                h = int(q.get("h", ["0"])[0] or 0)
                write_screen_frame(raw, w, h)
                self._json(200, {"ok": True, "bytes": len(raw)})
            except (ValueError, OSError):
                self._json(500, {"error": "frame_write_failed"})
        else:
            self._json(404, {"error": "not found"})

    def do_PUT(self):
        # Phone (ClipboardMonitorService) pushes copied text here; mirror it
        # into the Wayland clipboard so it is paste-able on the desktop.
        if self.path == "/omarchy/clipboard":
            try:
                raw = self._read_body()
                data = json.loads(raw.decode("utf-8") or "{}")
            except (ValueError, OSError):
                data = {}
            text = str(data.get("text", ""))
            ok = _wl_set_clipboard(text)
            log("clipboard PUT (%d chars) wl-copy=%s" % (len(text), ok))
            self._json(200 if ok else 500, {"ok": ok, "chars": len(text)})
        else:
            self._json(404, {"error": "not found"})

    def log_message(self, *args):  # silence default logging
        pass


def main() -> None:
    # Bind BEFORE writing the clean state: if another instance already owns the
    # port (e.g. one started manually or by a previous panel open), this process
    # must fail here without resetting the shared state file to "disconnected".
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    # Start from a clean (disconnected) state.
    write_state({"connected": False, "peerIp": "", "peerPort": 8753,
                 "peerName": "", "linkPort": PORT})
    log("link server listening on %s:%d" % (HOST, PORT))
    srv.serve_forever()


if __name__ == "__main__":
    main()

"""Lokale Webseite (nur Standardbibliothek: http.server, im Hintergrund-Thread)."""
import json
import logging
import mimetypes
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from . import autostart
from .config import CORE_METRICS, DYNAMIC_GROUPS, resource_dir
from .mqtt_pub import build_messages, test_connection
from .sensors import IS_WINDOWS
from .version import APP_TITLE, VERSION

log = logging.getLogger(__name__)
CONTENT_TYPES = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
                 ".js": "application/javascript; charset=utf-8", ".png": "image/png",
                 ".ico": "image/x-icon", ".svg": "image/svg+xml"}


class Api:
    """Alle Endpunkte. GET/POST -> (status, objekt)."""

    def __init__(self, ctrl):
        self.ctrl = ctrl

    def get(self, path):
        c = self.ctrl
        if path == "/api/meta":
            snap, _ = c.hub.get()
            return 200, {
                "app": APP_TITLE, "version": VERSION, "windows": IS_WINDOWS, "service": c.service,
                "metrics": [{"key": k, "group": g, "label": l, "unit": u} for k, g, l, u in CORE_METRICS],
                "dynamic": [{"key": k, "label": l, "topic": t} for k, l, t in DYNAMIC_GROUPS],
                "gpus": [g.get("name") for g in (snap.get("gpus") or [])],
                "config_path": c.config.path,
            }
        if path == "/api/state":
            snap, raw = c.hub.get()
            return 200, {"snapshot": snap, "history": c.hub.get_history(), "sensors": c.hub.status(),
                         "mqtt": c.publisher.status(), "version": VERSION, "raw_count": len(raw)}
        if path == "/api/sensors":
            return 200, c.hub.get()[1]
        if path == "/api/preview":
            snap, raw = c.hub.get()
            base, msgs, _ = build_messages(snap, raw, c.config.get(), c.hub.hostname)
            return 200, {"base": base, "messages": msgs[:400], "total": len(msgs)}
        if path == "/api/config":
            return 200, c.config.get()
        if path == "/api/autostart":
            try:
                return 200, autostart.status()
            except Exception as e:
                return 200, {"available": False, "mode": "off", "error": str(e)}
        return 404, {"error": "nicht gefunden"}

    def post(self, path, data):
        c = self.ctrl
        if path == "/api/config":
            old = c.config.get()
            new = c.config.update(data)
            c.publisher.apply_config()
            restart_web = old["web"]["port"] != new["web"]["port"] or old["web"]["lan"] != new["web"]["lan"]
            if restart_web:
                if old["web"]["port"] != new["web"]["port"]:
                    autostart.openwrt_firewall_port(new["web"]["port"])
                threading.Timer(0.6, c.restart_web, args=(old["web"],)).start()
            return 200, {"ok": True, "config": new, "web_restart": restart_web, "port": new["web"]["port"]}
        if path == "/api/mqtt/test":
            mc = c.config.get()["mqtt"]
            mc.update({k: v for k, v in data.items() if k in mc})
            ok, msg = test_connection(mc)
            return 200, {"ok": ok, "message": msg}
        if path == "/api/mqtt/send":
            n = c.publisher.publish_now()
            return 200, {"ok": n > 0, "count": n}
        if path == "/api/pause":
            c.set_paused(bool(data.get("paused")))
            return 200, {"ok": True, "paused": c.publisher.paused}
        if path == "/api/autostart":
            try:
                return 200, dict(ok=True, **autostart.set_mode(data.get("mode", "off")))
            except Exception as e:
                return 400, {"ok": False, "error": str(e)}
        if path == "/api/restart_admin":
            ok, msg = c.restart_as_admin(delayed=True)
            return 200, {"ok": ok, "message": msg}
        if path == "/api/quit":
            threading.Timer(0.5, c.quit).start()
            return 200, {"ok": True}
        return 404, {"error": "nicht gefunden"}


def make_handler(api, web_dir, asset_dir):
    class Handler(BaseHTTPRequestHandler):
        server_version = "SystemMonitor/" + VERSION
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):  # kein Zugriffsprotokoll
            pass

        def _send(self, status, body, ctype):
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, status, obj):
            self._send(status, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")

        def _file(self, folder, name):
            path = os.path.realpath(os.path.join(folder, name))
            if not path.startswith(os.path.realpath(folder) + os.sep) or not os.path.isfile(path):
                return self._json(404, {"error": "nicht gefunden"})
            ext = os.path.splitext(path)[1].lower()
            ctype = CONTENT_TYPES.get(ext) or mimetypes.guess_type(path)[0] or "application/octet-stream"
            with open(path, "rb") as f:
                self._send(200, f.read(), ctype)

        def do_GET(self):
            path = urlparse(self.path).path
            try:
                if path == "/":
                    return self._file(web_dir, "index.html")
                if path.startswith("/static/"):
                    return self._file(web_dir, path[len("/static/"):])
                if path == "/favicon.ico":
                    return self._file(asset_dir, "icon.ico")
                if path.startswith("/api/"):
                    return self._json(*api.get(path))
                self._json(404, {"error": "nicht gefunden"})
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception as e:
                log.exception("Fehler bei GET %s", path)
                self._json(500, {"error": str(e)})

        do_HEAD = do_GET

        def do_POST(self):
            path = urlparse(self.path).path
            try:
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n) if n else b""
                try:
                    data = json.loads(raw.decode("utf-8")) if raw else {}
                except ValueError:
                    data = {}
                if not isinstance(data, dict):
                    data = {}
                self._json(*api.post(path, data))
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception as e:
                log.exception("Fehler bei POST %s", path)
                self._json(500, {"error": str(e)})

    return Handler


class WebServer:
    def __init__(self, ctrl):
        self.ctrl = ctrl
        self.handler = make_handler(Api(ctrl), os.path.join(resource_dir(), "web"),
                                    os.path.join(resource_dir(), "assets"))
        self.server = None
        self.thread = None
        self.error = ""

    def start(self):
        cfg = self.ctrl.config.get()["web"]
        host = "0.0.0.0" if cfg["lan"] else "127.0.0.1"
        port = int(cfg["port"])
        try:
            ThreadingHTTPServer.allow_reuse_address = not IS_WINDOWS
            self.server = ThreadingHTTPServer((host, port), self.handler)
            self.server.daemon_threads = True
        except OSError as e:
            self.error = "Port %d nicht verfügbar: %s" % (port, e)
            log.error(self.error)
            self.server = None
            return False
        self.error = ""
        self.thread = threading.Thread(target=self.server.serve_forever, name="web", daemon=True)
        self.thread.start()
        log.info("Webseite läuft auf http://%s:%d", host, port)
        return True

    def stop(self):
        if self.server:
            try:
                self.server.shutdown()
                self.server.server_close()
            except Exception:
                pass
            self.server = None

    def restart(self):
        self.stop()
        return self.start()

"""Lokale Webseite (Flask, im Hintergrund-Thread)."""
import logging
import os
import threading

from flask import Flask, jsonify, request, send_from_directory
from werkzeug.serving import make_server

from . import autostart
from .config import CORE_METRICS, DYNAMIC_GROUPS, resource_dir
from .mqtt_pub import build_messages, test_connection
from .version import APP_TITLE, VERSION

log = logging.getLogger(__name__)


def create_app(ctrl):
    web_dir = os.path.join(resource_dir(), "web")
    asset_dir = os.path.join(resource_dir(), "assets")
    app = Flask(__name__, static_folder=None)

    @app.after_request
    def no_cache(resp):
        resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.get("/")
    def index():
        return send_from_directory(web_dir, "index.html")

    @app.get("/static/<path:name>")
    def static_files(name):
        return send_from_directory(web_dir, name)

    @app.get("/favicon.ico")
    def favicon():
        return send_from_directory(asset_dir, "icon.ico")

    @app.get("/api/meta")
    def meta():
        snap, _ = ctrl.hub.get()
        return jsonify({
            "app": APP_TITLE, "version": VERSION,
            "metrics": [{"key": k, "group": g, "label": l, "unit": u} for k, g, l, u in CORE_METRICS],
            "dynamic": [{"key": k, "label": l, "topic": t} for k, l, t in DYNAMIC_GROUPS],
            "gpus": [g.get("name") for g in (snap.get("gpus") or [])],
            "config_path": ctrl.config.path,
        })

    @app.get("/api/state")
    def state():
        snap, raw = ctrl.hub.get()
        return jsonify({
            "snapshot": snap,
            "history": ctrl.hub.get_history(),
            "sensors": ctrl.hub.status(),
            "mqtt": ctrl.publisher.status(),
            "version": VERSION,
            "raw_count": len(raw),
        })

    @app.get("/api/sensors")
    def sensors():
        _, raw = ctrl.hub.get()
        return jsonify(raw)

    @app.get("/api/preview")
    def preview():
        snap, raw = ctrl.hub.get()
        cfg = ctrl.config.get()
        base, msgs, js = build_messages(snap, raw, cfg, ctrl.hub.hostname)
        return jsonify({"base": base, "messages": msgs[:400], "total": len(msgs), "json": js})

    @app.get("/api/config")
    def get_config():
        return jsonify(ctrl.config.get())

    @app.post("/api/config")
    def set_config():
        data = request.get_json(force=True, silent=True) or {}
        old = ctrl.config.get()
        new = ctrl.config.update(data)
        ctrl.publisher.apply_config()
        restart_web = (old["web"]["port"] != new["web"]["port"] or old["web"]["lan"] != new["web"]["lan"])
        if restart_web:
            threading.Timer(0.6, ctrl.restart_web, args=(old["web"],)).start()
        return jsonify({"ok": True, "config": new, "web_restart": restart_web,
                        "port": new["web"]["port"]})

    @app.post("/api/mqtt/test")
    def mqtt_test():
        data = request.get_json(force=True, silent=True) or {}
        mc = ctrl.config.get()["mqtt"]
        mc.update({k: v for k, v in data.items() if k in mc})
        ok, msg = test_connection(mc)
        return jsonify({"ok": ok, "message": msg})

    @app.post("/api/mqtt/send")
    def mqtt_send():
        n = ctrl.publisher.publish_now()
        return jsonify({"ok": n > 0, "count": n})

    @app.post("/api/pause")
    def pause():
        data = request.get_json(force=True, silent=True) or {}
        ctrl.set_paused(bool(data.get("paused")))
        return jsonify({"ok": True, "paused": ctrl.publisher.paused})

    @app.get("/api/autostart")
    def autostart_get():
        try:
            return jsonify(autostart.status())
        except Exception as e:
            return jsonify({"available": False, "mode": "off", "error": str(e)})

    @app.post("/api/autostart")
    def autostart_set():
        data = request.get_json(force=True, silent=True) or {}
        try:
            return jsonify({"ok": True, **autostart.set_mode(data.get("mode", "off"))})
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 400

    @app.post("/api/restart_admin")
    def restart_admin():
        ok, msg = ctrl.restart_as_admin(delayed=True)
        return jsonify({"ok": ok, "message": msg})

    @app.post("/api/quit")
    def quit_app():
        threading.Timer(0.5, ctrl.quit).start()
        return jsonify({"ok": True})

    return app


class WebServer:
    def __init__(self, ctrl):
        self.ctrl = ctrl
        self.app = create_app(ctrl)
        self.server = None
        self.thread = None
        self.error = ""

    def start(self):
        cfg = self.ctrl.config.get()["web"]
        host = "0.0.0.0" if cfg["lan"] else "127.0.0.1"
        port = int(cfg["port"])
        try:
            self.server = make_server(host, port, self.app, threaded=True)
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

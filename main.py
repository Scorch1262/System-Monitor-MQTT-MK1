"""System-Monitor-MQTT-MK1 - Einstiegspunkt.

Läuft im Hintergrund (Infobereich/Tray), stellt eine lokale Webseite bereit und sendet
Systemwerte (CPU, GPU, RAM, Temperaturen, Lüfter ...) an einen MQTT-Broker.
"""
import argparse
import logging
import logging.handlers
import os
import sys
import threading
import time
import webbrowser

from app.config import Config, data_dir, resource_dir
from app.sensors import IS_WINDOWS, SensorHub
from app.mqtt_pub import MqttPublisher
from app.version import APP_NAME, APP_TITLE, VERSION
from app.webserver import WebServer

log = logging.getLogger(APP_NAME)


def setup_logging():
    path = os.path.join(data_dir(), APP_NAME + ".log")
    handler = logging.handlers.RotatingFileHandler(path, maxBytes=1_000_000, backupCount=2,
                                                   encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    if sys.stderr is not None and not getattr(sys, "frozen", False):
        root.addHandler(logging.StreamHandler())
    logging.getLogger("werkzeug").setLevel(logging.WARNING)


# --------------------------------------------------------------------------- Einzelinstanz
class SingleInstance:
    def __init__(self):
        self.handle = None

    def acquire(self, wait_seconds=0.0):
        if not IS_WINDOWS:
            return True
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateMutexW.restype = wintypes.HANDLE
        k32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        self._k32 = k32
        deadline = time.time() + wait_seconds
        while True:
            h = k32.CreateMutexW(None, False, "Local\\" + APP_NAME)
            if ctypes.get_last_error() != 183:  # ERROR_ALREADY_EXISTS
                self.handle = h
                return True
            k32.CloseHandle(h)
            if time.time() >= deadline:
                return False
            time.sleep(0.3)

    def release(self):
        if self.handle:
            self._k32.CloseHandle(self.handle)
            self.handle = None


# --------------------------------------------------------------------------- Steuerung
class Controller:
    def __init__(self, instance):
        self.instance = instance
        self.config = Config()
        self.hub = SensorHub(os.path.join(resource_dir(), "lhm"), self.config.get)
        self.publisher = MqttPublisher(self.hub, self.config.get)
        self.web = WebServer(self)
        self.tray = None
        self._quit = threading.Event()

    def url(self):
        return "http://127.0.0.1:%d/" % self.config.get()["web"]["port"]

    def open_dashboard(self, *_):
        webbrowser.open(self.url())

    def set_paused(self, paused):
        self.publisher.paused = paused
        if self.tray:
            self.tray.update_menu()

    def restart_web(self, old_web=None):
        if self.web.restart():
            return
        if old_web:  # neuer Port nicht nutzbar -> alter Port
            log.warning("Zurück auf alten Webport %s", old_web.get("port"))
            self.config.update({"web": old_web})
            self.web.restart()

    def restart_as_admin(self, delayed=False):
        if not IS_WINDOWS:
            return False, "Nur unter Windows möglich"
        import ctypes
        if getattr(sys, "frozen", False):
            exe, params = sys.executable, "--elevated"
        else:
            exe, params = sys.executable, '"%s" --elevated' % os.path.abspath(sys.argv[0])
        shell_execute = ctypes.windll.shell32.ShellExecuteW
        shell_execute.restype = ctypes.c_void_p
        rc = shell_execute(None, "runas", exe, params, os.getcwd(), 1)
        if (rc or 0) <= 32:
            return False, "Start mit Adminrechten abgebrochen"
        if delayed:
            threading.Timer(0.3, self.quit).start()
        else:
            self.quit()
        return True, "Programm startet mit Adminrechten neu"

    def quit(self, *_):
        if self._quit.is_set():
            return
        self._quit.set()
        log.info("Programm wird beendet")
        try:
            self.publisher.stop()
        except Exception:
            pass
        try:
            self.web.stop()
        except Exception:
            pass
        try:
            self.hub.stop()
        except Exception:
            pass
        self.instance.release()
        if self.tray:
            try:
                self.tray.stop()
            except Exception:
                pass
        threading.Timer(1.5, lambda: os._exit(0)).start()

    def run(self, open_browser):
        self.hub.start()
        self.publisher.start()
        if not self.web.start():
            # Port belegt -> nächsten freien probieren, damit die Seite erreichbar bleibt
            port = self.config.get()["web"]["port"]
            for p in range(port + 1, port + 20):
                self.config.update({"web": {"port": p}})
                if self.web.start():
                    break
        if open_browser:
            threading.Timer(1.0, self.open_dashboard).start()
        self.tray = Tray(self)
        if not self.tray.run():  # kein Tray verfügbar -> einfach weiterlaufen
            try:
                while not self._quit.is_set():
                    self._quit.wait(1)
            except KeyboardInterrupt:
                self.quit()
        time.sleep(0.5)


# --------------------------------------------------------------------------- Tray
class Tray:
    def __init__(self, ctrl):
        self.ctrl = ctrl
        self.icon = None

    def run(self):
        try:
            import pystray
            from PIL import Image
        except Exception as e:
            log.info("Kein Tray verfügbar: %s", e)
            return False
        try:
            image = Image.open(os.path.join(resource_dir(), "assets", "icon.png"))
        except Exception:
            image = Image.new("RGB", (64, 64), (245, 165, 36))
        c = self.ctrl
        menu = pystray.Menu(
            pystray.MenuItem("Dashboard öffnen", c.open_dashboard, default=True),
            pystray.MenuItem("Senden pausieren", lambda *_: c.set_paused(not c.publisher.paused),
                             checked=lambda item: c.publisher.paused),
            pystray.MenuItem("Jetzt senden", lambda *_: c.publisher.publish_now()),
            pystray.MenuItem("Als Administrator neu starten", lambda *_: c.restart_as_admin(),
                             visible=lambda item: IS_WINDOWS and not c.hub.admin),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Beenden", c.quit),
        )
        try:
            self.icon = pystray.Icon(APP_NAME, image, APP_TITLE, menu)
            threading.Thread(target=self._title_loop, daemon=True).start()
            self.icon.run()
            return True
        except Exception as e:
            log.warning("Tray konnte nicht gestartet werden: %s", e)
            self.icon = None
            return False

    def _title_loop(self):
        while self.icon is not None:
            try:
                st = self.ctrl.publisher.status()
                text = "%s %s\nMQTT: %s" % (APP_TITLE, VERSION, st["state"])
                self.icon.title = text[:120]
            except Exception:
                pass
            time.sleep(3)

    def update_menu(self):
        try:
            if self.icon:
                self.icon.update_menu()
        except Exception:
            pass

    def stop(self):
        icon, self.icon = self.icon, None
        if icon:
            icon.stop()


def main():
    parser = argparse.ArgumentParser(prog=APP_NAME)
    parser.add_argument("--autostart", action="store_true", help="Start durch Autostart (kein Browser)")
    parser.add_argument("--elevated", action="store_true", help="Neustart mit Adminrechten")
    parser.add_argument("--no-browser", action="store_true")
    args, _ = parser.parse_known_args()

    setup_logging()
    log.info("%s %s startet", APP_TITLE, VERSION)

    instance = SingleInstance()
    if not instance.acquire(wait_seconds=15 if args.elevated else 0):
        # läuft bereits -> nur Dashboard öffnen
        cfg = Config().get()
        webbrowser.open("http://127.0.0.1:%d/" % cfg["web"]["port"])
        return

    ctrl = Controller(instance)
    cfg = ctrl.config.get()
    open_browser = (not args.autostart and not args.no_browser and
                    not args.elevated and (ctrl.config.is_new or cfg["start"]["open_browser"]))
    ctrl.run(open_browser)


if __name__ == "__main__":
    main()

"""Update-Funktion über GitHub-Releases (Scorch1262/System-Monitor-MQTT-MK1).

Windows (exe):    lädt das Release-Asset "...-windows.zip", benennt die laufende exe in
                  ".old" um (unter Windows erlaubt), legt die neue exe an ihre Stelle und startet sie.
Linux / Quellcode: lädt den Quellcode des Release-Tags, ersetzt app/, web/, assets/, main.py,
                  install.sh, uninstall.sh und startet das Programm neu (os.execv). Die
                  Einstellungen (JSON) bleiben erhalten.
"""
import io
import json
import logging
import os
import re
import shutil
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import zipfile

from .config import app_dir
from .version import APP_NAME, VERSION

log = logging.getLogger(__name__)
IS_WINDOWS = sys.platform.startswith("win")
FROZEN = getattr(sys, "frozen", False)
DEFAULT_REPO = "Scorch1262/System-Monitor-MQTT-MK1"
SOURCE_ITEMS = ("app", "web", "assets", "main.py", "install.sh", "uninstall.sh", "README.md")


def parse_version(text):
    nums = [int(x) for x in re.findall(r"\d+", str(text or ""))[:4]]
    return tuple(nums + [0] * (4 - len(nums)))


CA_BUNDLES = ("/etc/ssl/certs/ca-certificates.crt", "/etc/ssl/cert.pem", "/etc/pki/tls/certs/ca-bundle.crt")


def _ssl_context():
    ctx = ssl.create_default_context()
    try:
        if not ctx.cert_store_stats().get("x509_ca"):
            for path in CA_BUNDLES:  # z. B. OpenWrt: Pfad nicht im Standard-Suchpfad
                if os.path.isfile(path):
                    ctx.load_verify_locations(path)
                    break
    except Exception:
        pass
    return ctx


def _explain(e):
    msg = str(e)
    if "CERTIFICATE_VERIFY_FAILED" in msg:
        return msg + " – Stammzertifikate fehlen (OpenWrt: opkg install ca-bundle)"
    return msg


def _http_get(url, timeout=30, accept=None):
    req = urllib.request.Request(url, headers={
        "User-Agent": "%s/%s" % (APP_NAME, VERSION),
        "Accept": accept or "application/octet-stream",
    })
    ctx = _ssl_context() if url.startswith("https:") else None
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
        return r.read()


class Updater:
    CHECK_EVERY = 6 * 3600

    def __init__(self, ctrl):
        self.ctrl = ctrl
        self.latest = None        # Release-Daten (dict)
        self.checked_at = None
        self.error = ""
        self.busy = False
        self.progress = ""
        self._lock = threading.Lock()
        self._thread = None
        self._stop = threading.Event()

    # ------------------------------------------------------------ Infos
    @property
    def repo(self):
        return (self.ctrl.config.get().get("update", {}).get("repo") or DEFAULT_REPO).strip()

    def mode(self):
        if IS_WINDOWS and FROZEN:
            return "exe"
        if FROZEN:
            return None  # gefrorene Linux-Builds gibt es nicht
        return "source"

    def status(self):
        rel = self.latest or {}
        latest = rel.get("tag_name")
        available = bool(latest) and parse_version(latest) > parse_version(VERSION)
        return {
            "current": VERSION,
            "latest": latest,
            "available": available,
            "name": rel.get("name"),
            "notes": (rel.get("body") or "")[:4000],
            "published": rel.get("published_at"),
            "url": rel.get("html_url"),
            "checked_at": self.checked_at,
            "error": self.error,
            "busy": self.busy,
            "progress": self.progress,
            "supported": self.mode() is not None,
            "mode": self.mode(),
            "repo": self.repo,
            "auto_check": self.ctrl.config.get().get("update", {}).get("check", True),
        }

    # ------------------------------------------------------------ Prüfen
    def start(self):
        self._thread = threading.Thread(target=self._loop, name="update", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _loop(self):
        if self._stop.wait(20):  # nach dem Start kurz warten
            return
        while not self._stop.is_set():
            if self.ctrl.config.get().get("update", {}).get("check", True):
                self.check()
            if self._stop.wait(self.CHECK_EVERY):
                return

    def check(self):
        api_base = os.environ.get("SYSMON_UPDATE_API", "https://api.github.com").rstrip("/")  # Test/Mirror
        url = "%s/repos/%s/releases/latest" % (api_base, self.repo)
        try:
            data = json.loads(_http_get(url, timeout=15, accept="application/vnd.github+json").decode("utf-8"))
            self.latest = data
            self.error = ""
            log.info("Update-Prüfung: aktuell %s, neuestes Release %s", VERSION, data.get("tag_name"))
        except Exception as e:
            msg = _explain(e)
            if "404" in msg:
                msg = "Kein Release im Repo %s gefunden" % self.repo
            self.error = "Prüfung fehlgeschlagen: %s" % msg
            log.warning(self.error)
        self.checked_at = time.time()
        return self.status()

    # ------------------------------------------------------------ Installieren
    def install(self):
        with self._lock:
            if self.busy:
                return False, "Update läuft bereits"
            if self.mode() is None:
                return False, "Update wird in dieser Version nicht unterstützt"
            if not self.latest:
                self.check()
            st = self.status()
            if not st["available"]:
                return False, self.error or "Bereits auf dem neuesten Stand (v%s)" % VERSION
            self.busy = True
            self.error = ""
            self.progress = "starte"
        threading.Thread(target=self._install_worker, name="update-install", daemon=True).start()
        return True, "Update auf %s wird installiert" % st["latest"]

    def _install_worker(self):
        try:
            if self.mode() == "exe":
                self._install_exe()
            else:
                self._install_source()
        except Exception as e:
            log.exception("Update fehlgeschlagen")
            self.error = "Update fehlgeschlagen: %s" % _explain(e)
            self.progress = ""
            self.busy = False

    # ---- Windows-exe
    def _install_exe(self):
        rel = self.latest
        asset = next((a for a in rel.get("assets", []) if a.get("name", "").lower().endswith("-windows.zip")), None)
        if not asset:
            raise RuntimeError("Release %s enthält keine Windows-Zip" % rel.get("tag_name"))
        exe = os.path.abspath(sys.executable)
        folder = os.path.dirname(exe)
        if not os.access(folder, os.W_OK):
            raise RuntimeError("Kein Schreibrecht im Programmordner %s – als Administrator starten" % folder)

        self.progress = "lade %s" % asset["name"]
        data = _http_get(asset["browser_download_url"], timeout=300)
        self.progress = "entpacke"
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            name = next((n for n in z.namelist() if n.lower().endswith(".exe")), None)
            if not name:
                raise RuntimeError("Keine exe in der Zip gefunden")
            new_bytes = z.read(name)
        if len(new_bytes) < 1_000_000 or new_bytes[:2] != b"MZ":
            raise RuntimeError("Heruntergeladene exe ist ungültig")

        new_path = exe + ".new"
        old_path = exe + ".old"
        with open(new_path, "wb") as f:
            f.write(new_bytes)
        self.progress = "tausche exe"
        if os.path.exists(old_path):
            try:
                os.remove(old_path)
            except OSError:
                old_path = exe + ".%d.old" % int(time.time())
        os.replace(exe, old_path)           # laufende exe darf umbenannt werden
        try:
            os.replace(new_path, exe)
        except Exception:
            os.replace(old_path, exe)       # zurückrollen
            raise

        self.progress = "starte neue Version"
        env = {k: v for k, v in os.environ.items()
               if not k.startswith("_PYI_") and not k.startswith("_MEIPASS")}
        env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
        flags = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        try:
            subprocess.Popen([exe, "--updated"], env=env, cwd=folder, close_fds=True, creationflags=flags)
        except Exception:
            os.replace(exe, new_path)
            os.replace(old_path, exe)
            raise
        log.info("Update auf %s installiert - Neustart", self.latest.get("tag_name"))
        threading.Timer(0.5, self.ctrl.quit).start()

    # ---- Quellcode (Linux, auch Windows ohne exe)
    def _install_source(self):
        rel = self.latest
        tag = rel.get("tag_name")
        url = rel.get("tarball_url") or "https://codeload.github.com/%s/tar.gz/refs/tags/%s" % (self.repo, tag)
        target = app_dir()
        if not os.access(target, os.W_OK):
            raise RuntimeError("Kein Schreibrecht in %s" % target)

        self.progress = "lade Quellcode %s" % tag
        data = _http_get(url, timeout=300)
        tmp = tempfile.mkdtemp(prefix=APP_NAME + "-update-")
        try:
            self.progress = "entpacke"
            archive = os.path.join(tmp, "src.tar.gz")
            with open(archive, "wb") as f:
                f.write(data)
            out = os.path.join(tmp, "src")
            os.makedirs(out)
            self._extract(archive, out)
            tops = [d for d in os.listdir(out) if os.path.isdir(os.path.join(out, d))]
            if len(tops) != 1:
                raise RuntimeError("Unerwarteter Aufbau des Archivs")
            src = os.path.join(out, tops[0])
            if not os.path.isfile(os.path.join(src, "main.py")) or not os.path.isfile(os.path.join(src, "app", "version.py")):
                raise RuntimeError("Archiv enthält kein Programm")

            self.progress = "installiere Dateien"
            for item in SOURCE_ITEMS:
                s = os.path.join(src, item)
                d = os.path.join(target, item)
                if not os.path.exists(s):
                    continue
                if os.path.isdir(s):
                    staging = d + ".neu"
                    shutil.rmtree(staging, ignore_errors=True)
                    shutil.copytree(s, staging, ignore=shutil.ignore_patterns("__pycache__"))
                    backup = d + ".alt"
                    shutil.rmtree(backup, ignore_errors=True)
                    if os.path.exists(d):
                        os.rename(d, backup)
                    os.rename(staging, d)
                    shutil.rmtree(backup, ignore_errors=True)
                else:
                    shutil.copy2(s, d + ".neu")
                    os.replace(d + ".neu", d)
                    if item.endswith(".sh"):
                        os.chmod(d, 0o755)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

        log.info("Update auf %s installiert - Neustart", tag)
        self.progress = "starte neu"
        threading.Timer(0.5, self.ctrl.restart_process).start()

    @staticmethod
    def _extract(archive, out):
        try:
            import tarfile
            with tarfile.open(archive, "r:gz") as t:
                base = os.path.realpath(out)
                for m in t.getmembers():
                    p = os.path.realpath(os.path.join(out, m.name))
                    if not (p == base or p.startswith(base + os.sep)) or m.issym() or m.islnk():
                        raise RuntimeError("Unsicherer Pfad im Archiv: %s" % m.name)
                t.extractall(out)
        except ImportError:
            # OpenWrt mit python3-light: System-tar verwenden
            subprocess.run(["tar", "-xzf", archive, "-C", out], check=True)


def cleanup_old_exe():
    """Nach einem Windows-Update die alte exe löschen (sobald sie nicht mehr läuft)."""
    if not (IS_WINDOWS and FROZEN):
        return
    folder = os.path.dirname(os.path.abspath(sys.executable))
    base = os.path.basename(sys.executable)

    def work():
        for _ in range(30):
            left = 0
            for n in os.listdir(folder):
                if n.startswith(base) and n.endswith(".old"):
                    try:
                        os.remove(os.path.join(folder, n))
                    except OSError:
                        left += 1
            if not left:
                return
            time.sleep(2)

    threading.Thread(target=work, daemon=True).start()

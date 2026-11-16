"""Update-Funktion über GitHub-Releases (Scorch1262/System-Monitor-MQTT-MK1).

Ohne GitHub-API (die erlaubt ohne Anmeldung nur 60 Abfragen/Stunde je IP):
  neueste Version  -> Weiterleitung von github.com/<repo>/releases/latest auf .../tag/vX.Y.Z
  Notizen          -> Abschnitt "### X.Y.Z" aus README.md des Tags (raw.githubusercontent.com)
  Windows          -> github.com/<repo>/releases/download/vX.Y.Z/System-Monitor-MQTT-MK1-vX.Y.Z-windows-x64.zip
  Quellcode        -> codeload.github.com/<repo>/tar.gz/refs/tags/vX.Y.Z

Windows (Ordner): lädt "...-windows-x64.zip" (exe + _internal), entpackt es nach <Programmordner>/_update,
                  startet die neue exe dort mit "--apply-update"; diese wartet, bis das alte Programm
                  beendet ist, tauscht exe und _internal im Programmordner aus und startet die neue Version.
Linux / Quellcode: lädt den Quellcode des Release-Tags, ersetzt app/, web/, assets/, main.py,
                  install.sh, uninstall.sh und startet das Programm neu (os.execv). Die
                  Einstellungen (JSON) bleiben erhalten.
"""
import http.client
import io
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
import urllib.parse
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


def _bases():
    """Basis-Adressen; SYSMON_UPDATE_BASE ersetzt alle drei (Test/Spiegelserver)."""
    b = os.environ.get("SYSMON_UPDATE_BASE", "").rstrip("/")
    if b:
        return b, b + "/raw", b + "/codeload"
    return "https://github.com", "https://raw.githubusercontent.com", "https://codeload.github.com"


def _redirect_target(url, timeout=15):
    """Fragt eine Adresse ab, ohne der Weiterleitung zu folgen, und liefert das Ziel (Location)."""
    u = urllib.parse.urlsplit(url)
    if u.scheme == "https":
        conn = http.client.HTTPSConnection(u.hostname, u.port or 443, timeout=timeout, context=_ssl_context())
    else:
        conn = http.client.HTTPConnection(u.hostname, u.port or 80, timeout=timeout)
    try:
        conn.request("HEAD", u.path or "/", headers={"User-Agent": "%s/%s" % (APP_NAME, VERSION)})
        r = conn.getresponse()
        r.read()
        if r.status in (301, 302, 303, 307, 308):
            return urllib.parse.urljoin(url, r.getheader("Location") or "")
        if r.status == 404:
            raise RuntimeError("Repo nicht gefunden (HTTP 404)")
        raise RuntimeError("Unerwartete Antwort von GitHub: HTTP %d" % r.status)
    finally:
        conn.close()


def _release_notes(readme, version):
    """Abschnitt "### <version>" aus dem Änderungsteil der README."""
    v = version.lstrip("vV")
    m = re.search(r"^###\s+v?%s\s*$(.*?)(?=^###\s|^##\s|\Z)" % re.escape(v), readme, re.M | re.S)
    return m.group(1).strip() if m else ""


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
        web, raw, codeload = _bases()
        repo = self.repo
        try:
            target = _redirect_target("%s/%s/releases/latest" % (web, repo))
            m = re.search(r"/releases/tag/([^/?#]+)$", target)
            if not m:
                raise RuntimeError("Kein Release im Repo %s gefunden" % repo)
            tag = urllib.parse.unquote(m.group(1))
            ver = tag.lstrip("vV")
            asset = "%s-v%s-windows-x64.zip" % (APP_NAME, ver)
            notes = ""
            try:
                readme = _http_get("%s/%s/%s/README.md" % (raw, repo, tag), timeout=15).decode("utf-8", "replace")
                notes = _release_notes(readme, ver)
            except Exception as e:
                log.info("Release-Notizen nicht geladen: %s", e)
            self.latest = {
                "tag_name": tag,
                "name": "%s %s" % (APP_NAME, tag),
                "body": notes,
                "html_url": "%s/%s/releases/tag/%s" % (web, repo, tag),
                "published_at": None,
                "tarball_url": "%s/%s/tar.gz/refs/tags/%s" % (codeload, repo, tag),
                "assets": [{"name": asset,
                            "browser_download_url": "%s/%s/releases/download/%s/%s" % (web, repo, tag, asset)}],
            }
            self.error = ""
            log.info("Update-Prüfung: aktuell %s, neuestes Release %s", VERSION, tag)
        except Exception as e:
            self.error = "Prüfung fehlgeschlagen: %s" % _explain(e)
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
        asset = next((a for a in rel.get("assets", []) if a.get("name", "").lower().endswith("-windows-x64.zip")), None)
        if not asset:
            raise RuntimeError("Release %s enthält kein Windows-Paket" % rel.get("tag_name"))
        exe = os.path.abspath(sys.executable)
        folder = os.path.dirname(exe)
        if not os.access(folder, os.W_OK):
            raise RuntimeError("Kein Schreibrecht im Programmordner %s – als Administrator starten" % folder)

        self.progress = "lade %s" % asset["name"]
        try:
            data = _http_get(asset["browser_download_url"], timeout=300)
        except Exception as e:
            if "404" in str(e):
                raise RuntimeError("%s ist (noch) nicht im Release – läuft der Build noch?" % asset["name"])
            raise
        self.progress = "entpacke"
        stage = os.path.join(folder, UPDATE_DIR)
        shutil.rmtree(stage, ignore_errors=True)
        os.makedirs(stage)
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            base = os.path.realpath(stage)
            for n in z.namelist():
                p = os.path.realpath(os.path.join(stage, n))
                if not (p == base or p.startswith(base + os.sep)):
                    raise RuntimeError("Unsicherer Pfad im Paket: %s" % n)
            z.extractall(stage)
        new_exe = _find_app_exe(stage)
        if not new_exe:
            raise RuntimeError("Paket enthält keine %s.exe mit _internal-Ordner" % APP_NAME)
        with open(new_exe, "rb") as f:
            if f.read(2) != b"MZ":
                raise RuntimeError("Heruntergeladene exe ist ungültig")

        self.progress = "starte Austausch"
        subprocess.Popen([new_exe, "--apply-update", folder, "--wait-pid", str(os.getpid())],
                         env=_clean_env(), cwd=os.path.dirname(new_exe), close_fds=True,
                         creationflags=DETACHED)
        log.info("Update auf %s vorbereitet - Programm beendet sich für den Austausch", rel.get("tag_name"))
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


UPDATE_DIR = "_update"
DETACHED = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP


def _clean_env():
    """Umgebung ohne PyInstaller-Variablen des laufenden Programms."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("_PYI_") and not k.startswith("_MEIPASS")}
    env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    return env


def _find_app_exe(root):
    for dirpath, dirnames, filenames in os.walk(root):
        if APP_NAME + ".exe" in filenames and "_internal" in dirnames:
            return os.path.join(dirpath, APP_NAME + ".exe")
    return None


def _retry(fn, tries=20, delay=0.5):
    for i in range(tries):
        try:
            return fn()
        except OSError:
            if i == tries - 1:
                raise
            time.sleep(delay)


def apply_update(target, wait_pid):
    """Läuft in der NEUEN exe (aus <target>/_update/...): Dateien in den Programmordner kopieren."""
    import logging.handlers
    log_path = os.path.join(target, APP_NAME + ".log")
    try:
        h = logging.handlers.RotatingFileHandler(log_path, maxBytes=1_000_000, backupCount=2, encoding="utf-8")
        h.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s update: %(message)s"))
        logging.getLogger().addHandler(h)
        logging.getLogger().setLevel(logging.INFO)
    except OSError:
        pass
    src = os.path.dirname(os.path.abspath(sys.executable))
    log.info("Update-Austausch: %s -> %s (warte auf Prozess %s)", src, target, wait_pid)

    # 1. Warten, bis die alte Version beendet ist
    try:
        import psutil
        try:
            psutil.Process(int(wait_pid)).wait(timeout=60)
        except psutil.NoSuchProcess:
            pass
    except Exception as e:
        log.warning("Warten auf alten Prozess: %s", e)
        time.sleep(5)
    time.sleep(1.0)

    # 2. Austausch: _internal komplett, exe und Begleitdateien
    t_int, t_old = os.path.join(target, "_internal"), os.path.join(target, "_internal.old")
    t_exe = os.path.join(target, APP_NAME + ".exe")
    ok = False
    try:
        shutil.rmtree(t_old, ignore_errors=True)
        if os.path.isdir(t_int):
            _retry(lambda: os.rename(t_int, t_old))
        shutil.copytree(os.path.join(src, "_internal"), t_int)
        _retry(lambda: shutil.copy2(os.path.join(src, APP_NAME + ".exe"), t_exe))
        for name in os.listdir(src):
            p = os.path.join(src, name)
            if os.path.isfile(p) and not name.lower().endswith((".exe", ".json", ".log")):
                shutil.copy2(p, os.path.join(target, name))
        ok = True
        log.info("Update-Austausch erfolgreich")
    except Exception:
        log.exception("Update-Austausch fehlgeschlagen - stelle alte Version wieder her")
        try:
            if os.path.isdir(t_old):
                shutil.rmtree(t_int, ignore_errors=True)
                os.rename(t_old, t_int)
        except OSError:
            log.exception("Wiederherstellen fehlgeschlagen")

    # 3. Programm im Programmordner starten (neue oder wiederhergestellte Version)
    subprocess.Popen([t_exe, "--updated"] if ok else [t_exe], env=_clean_env(), cwd=target,
                     close_fds=True, creationflags=DETACHED)


def cleanup_old_exe():
    """Nach einem Windows-Update Reste löschen (_update-Ordner, _internal.old, alte .old-exe)."""
    if not (IS_WINDOWS and FROZEN):
        return
    folder = os.path.dirname(os.path.abspath(sys.executable))
    base = os.path.basename(sys.executable)

    def work():
        for _ in range(30):
            left = []
            for n in os.listdir(folder):
                p = os.path.join(folder, n)
                if n in (UPDATE_DIR, "_internal.old") or (n.startswith(base) and n.endswith(".old")):
                    try:
                        if os.path.isdir(p):
                            shutil.rmtree(p)
                        else:
                            os.remove(p)
                    except OSError:
                        left.append(n)
            if not left:
                return
            time.sleep(2)

    threading.Thread(target=work, daemon=True).start()

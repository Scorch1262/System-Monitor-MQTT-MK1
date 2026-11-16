"""PawnIO-Treiber (für CPU-Temperatur / Mainboard-Lüfter über LibreHardwareMonitor).

Das offizielle PawnIO-Setup ist in der exe enthalten (Ordner drivers/) und wird beim ersten Start
mit Adminrechten still installiert ("PawnIO_setup.exe -install -silent") - keine Handarbeit nötig.
"""
import logging
import os
import subprocess

from .config import resource_dir
from .sensors import IS_WINDOWS, pawnio_installed

log = logging.getLogger(__name__)
CREATE_NO_WINDOW = 0x08000000


def bundled_setup():
    path = os.path.join(resource_dir(), "drivers", "PawnIO_setup.exe")
    return path if os.path.isfile(path) else None


def ensure_pawnio(admin):
    """Installiert PawnIO, falls nötig und möglich. Liefert einen Statustext."""
    if not IS_WINDOWS:
        return ""
    if pawnio_installed():
        return "installiert"
    if not admin:
        return "fehlt - wird beim Start mit Adminrechten automatisch installiert"
    setup = bundled_setup()
    if not setup:
        return "fehlt - Setup nicht in der exe enthalten"
    log.info("Installiere PawnIO-Treiber: %s", setup)
    try:
        r = subprocess.run([setup, "-install", "-silent"], capture_output=True, text=True,
                           timeout=180, creationflags=CREATE_NO_WINDOW)
        log.info("PawnIO-Setup beendet (Code %s) %s", r.returncode, (r.stdout or r.stderr or "").strip()[:300])
    except Exception as e:
        log.exception("PawnIO-Installation fehlgeschlagen")
        return "Installation fehlgeschlagen: %s" % e
    if pawnio_installed():
        return "automatisch installiert"
    return "Installation fehlgeschlagen (Code %s)" % r.returncode

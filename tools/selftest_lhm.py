"""Prüft im Build, ob LibreHardwareMonitorLib über pythonnet geladen werden kann (ohne Adminrechte:
GPU/Laufwerke/RAM liefern Werte, CPU-Temperatur/Lüfter i. d. R. nicht)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.sensors import LhmProvider  # noqa: E402

folder = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else "lhm_out")
p = LhmProvider(folder)
p.start()
print("LHM-Status:", p.status)
if not p.ok:
    sys.exit(1)
raw = p.read()
print("Sensoren gelesen:", len(raw))
for s in raw[:25]:
    print("  %-28s %-24s %-12s %s" % (s["hw"][:28], s["name"][:24], s["type"], s["value"]))
p.close()

# -*- mode: python ; coding: utf-8 -*-
# PyInstaller-Spezifikation für System-Monitor-MQTT-MK1 (eine einzelne exe ohne Konsolenfenster)
import os
from PyInstaller.utils.hooks import collect_all

datas = [("web", "web"), ("assets", "assets")]
binaries = []
hiddenimports = ["pystray._win32", "pynvml", "clr"]

if os.path.isdir("lhm_out"):
    datas.append(("lhm_out", "lhm"))
else:
    print("WARNUNG: Ordner lhm_out fehlt - exe läuft ohne LibreHardwareMonitor (keine CPU-Temperatur/Lüfter)")

for pkg in ("pythonnet", "clr_loader"):
    try:
        d, b, h = collect_all(pkg)
        datas += d
        binaries += b
        hiddenimports += h
    except Exception as e:
        print("Hinweis: %s nicht gesammelt (%s)" % (pkg, e))

version_file = os.path.join("build", "version_info.txt")

a = Analysis(
    ["main.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=["tkinter"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="System-Monitor-MQTT-MK1",
    console=False,
    icon="assets/icon.ico",
    version=version_file if os.path.exists(version_file) else None,
    upx=False,
    debug=False,
    strip=False,
)

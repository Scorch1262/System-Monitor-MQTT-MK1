"""Konfiguration: menschenlesbare JSON-Datei neben der exe (Fallback: %APPDATA%)."""
import copy
import json
import logging
import os
import sys
import threading

from .version import APP_NAME

log = logging.getLogger(__name__)

# Feste Messwerte (Schlüssel -> Gruppe, Bezeichnung, Einheit). Topic = Schlüssel mit "/" statt "."
CORE_METRICS = [
    ("cpu.usage", "CPU", "Auslastung", "%"),
    ("cpu.temperature", "CPU", "Temperatur", "°C"),
    ("cpu.clock", "CPU", "Takt", "MHz"),
    ("cpu.power", "CPU", "Leistung", "W"),
    ("gpu.usage", "GPU", "Auslastung", "%"),
    ("gpu.temperature", "GPU", "Temperatur", "°C"),
    ("gpu.hotspot", "GPU", "Hotspot-Temperatur", "°C"),
    ("gpu.memory_used", "GPU", "Speicher belegt", "MB"),
    ("gpu.memory_total", "GPU", "Speicher gesamt", "MB"),
    ("gpu.memory_usage", "GPU", "Speicher-Auslastung", "%"),
    ("gpu.fan_rpm", "GPU", "Lüfter-Drehzahl", "U/min"),
    ("gpu.fan_percent", "GPU", "Lüfter-Ansteuerung", "%"),
    ("gpu.power", "GPU", "Leistung", "W"),
    ("gpu.clock", "GPU", "Takt", "MHz"),
    ("ram.usage", "Arbeitsspeicher", "Auslastung", "%"),
    ("ram.used", "Arbeitsspeicher", "Belegt", "GB"),
    ("ram.total", "Arbeitsspeicher", "Gesamt", "GB"),
    ("ram.available", "Arbeitsspeicher", "Verfügbar", "GB"),
    ("swap.usage", "Arbeitsspeicher", "Auslagerung", "%"),
    ("disk_io.read", "Datenträger", "Lesen", "MB/s"),
    ("disk_io.write", "Datenträger", "Schreiben", "MB/s"),
    ("net.up", "Netzwerk", "Upload", "kB/s"),
    ("net.down", "Netzwerk", "Download", "kB/s"),
    ("system.uptime", "System", "Laufzeit", "s"),
    ("system.processes", "System", "Prozesse", ""),
    ("system.battery", "System", "Akku", "%"),
]

# Dynamische Gruppen (Anzahl der Werte hängt von der Hardware ab)
DYNAMIC_GROUPS = [
    ("cores", "CPU-Kerne einzeln", "cpu/cores/<nr>"),
    ("fans", "Lüfter (Mainboard, Gehäuse, GPU)", "fans/<name>"),
    ("temps_board", "Mainboard-Temperaturen", "temps/<name>"),
    ("temps_drives", "Laufwerks-Temperaturen", "drives/<name>/temperature"),
    ("disks", "Laufwerks-Belegung", "disks/<laufwerk>/usage, /free_gb"),
    ("raw", "Alle Rohsensoren (LibreHardwareMonitor / Linux-hwmon)", "sensors/<hardware>/<typ>/<sensor>"),
]

DEFAULTS = {
    "mqtt": {
        "host": "",
        "port": 1883,
        "username": "",
        "password": "",
        "tls": False,
        "tls_insecure": False,
        "client_id": "",
        "keepalive": 60,
        "qos": 0,
        "retain": False,
    },
    "publish": {
        "enabled": True,
        "base_topic": "pc/{hostname}",
        "interval": 5,
        "json": True,
        "json_subtopic": "state",
        "single": True,
        "status_topic": "status",
    },
    "metrics": {key: True for key, *_ in CORE_METRICS},
    "dynamic": {"cores": False, "fans": True, "temps_board": True, "temps_drives": True,
                "disks": True, "raw": False},
    "gpu_select": "auto",
    # Unter Linux (Brume 2, Raspberry) läuft das Programm ohne Bildschirm -> Webseite im LAN
    "web": {"port": 8095, "lan": not sys.platform.startswith("win")},
    "start": {"open_browser": False},
    "windows": {"driver_auto": True},
    "update": {"check": True, "repo": "Scorch1262/System-Monitor-MQTT-MK1"},
}


def app_dir():
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def is_openwrt():
    return os.path.exists("/etc/openwrt_release")


def log_dir():
    """Auf OpenWrt ins RAM (/tmp), um den Flash-Speicher zu schonen."""
    if is_openwrt():
        return "/tmp"
    return data_dir()


def resource_dir():
    """Ordner mit gebündelten Dateien (web/, assets/, lhm/)."""
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return sys._MEIPASS
    return app_dir()


def _writable(folder):
    try:
        os.makedirs(folder, exist_ok=True)
        test = os.path.join(folder, ".schreibtest")
        with open(test, "w") as f:
            f.write("x")
        os.remove(test)
        return True
    except OSError:
        return False


def data_dir():
    folder = app_dir()
    if _writable(folder):
        return folder
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    folder = os.path.join(base, APP_NAME)
    os.makedirs(folder, exist_ok=True)
    return folder


def _merge(defaults, loaded):
    out = copy.deepcopy(defaults)
    if not isinstance(loaded, dict):
        return out
    for k, v in loaded.items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


class Config:
    def __init__(self):
        self.path = os.path.join(data_dir(), APP_NAME + ".json")
        self._lock = threading.Lock()
        self.is_new = not os.path.exists(self.path)
        self.data = copy.deepcopy(DEFAULTS)
        if not self.is_new:
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    self.data = _merge(DEFAULTS, json.load(f))
            except Exception as e:  # defekte Datei -> Sicherung anlegen, Standardwerte nutzen
                log.error("Konfiguration nicht lesbar (%s) - Standardwerte werden genutzt", e)
                try:
                    os.replace(self.path, self.path + ".defekt")
                except OSError:
                    pass
        self.save()

    def get(self):
        with self._lock:
            return copy.deepcopy(self.data)

    def update(self, new_data):
        with self._lock:
            self.data = _merge(DEFAULTS, _merge(self.data, new_data))
            self._sanitize()
        self.save()
        return self.get()

    def _sanitize(self):
        d = self.data
        def to_int(v, lo, hi, default):
            try:
                return max(lo, min(hi, int(float(v))))
            except (TypeError, ValueError):
                return default
        d["mqtt"]["port"] = to_int(d["mqtt"]["port"], 1, 65535, 1883)
        d["mqtt"]["qos"] = to_int(d["mqtt"]["qos"], 0, 2, 0)
        d["mqtt"]["keepalive"] = to_int(d["mqtt"]["keepalive"], 10, 3600, 60)
        d["mqtt"]["host"] = str(d["mqtt"]["host"]).strip()
        try:
            d["publish"]["interval"] = max(1.0, min(3600.0, float(d["publish"]["interval"])))
        except (TypeError, ValueError):
            d["publish"]["interval"] = 5
        topic = str(d["publish"]["base_topic"]).strip().strip("/")
        d["publish"]["base_topic"] = topic or "pc/{hostname}"
        d["publish"]["json_subtopic"] = str(d["publish"]["json_subtopic"]).strip().strip("/")
        d["publish"]["status_topic"] = str(d["publish"]["status_topic"]).strip().strip("/")
        d["web"]["port"] = to_int(d["web"]["port"], 1, 65535, 8095)

    def save(self):
        with self._lock:
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f, indent=2, ensure_ascii=False)
            os.replace(tmp, self.path)

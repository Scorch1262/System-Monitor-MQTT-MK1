"""Sensor-Erfassung.

Windows:
  1. LibreHardwareMonitorLib (über pythonnet) - CPU-Temperatur, Lüfter, GPU, Mainboard ...
     Treiber PawnIO wird bei Adminrechten automatisch aus der exe installiert (driver.py)
  2. NVIDIA NVML (nvidia-ml-py) - GPU-Werte ohne Adminrechte als Ergänzung/Ersatz
  3. psutil - CPU-Last, RAM, Laufwerke, Netzwerk, Akku
Linux (Raspberry Pi, GL.iNet Brume 2 ...):
  /proc und /sys (linux_sys.py) - keine Zusatzpakete nötig; NVML falls vorhanden
"""
import collections
import logging
import os
import platform
import re
import socket
import sys
import threading
import time

log = logging.getLogger(__name__)
IS_WINDOWS = sys.platform.startswith("win")


def slug(text):
    text = str(text).strip().lower()
    for a, b in (("ä", "ae"), ("ö", "oe"), ("ü", "ue"), ("ß", "ss"), ("#", "")):
        text = text.replace(a, b)
    text = re.sub(r"[^a-z0-9]+", "_", text).strip("_")
    return text or "x"


def _rnd(v, digits=1):
    if v is None:
        return None
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    if v != v:  # NaN
        return None
    return round(v, digits) if digits else int(round(v))


def _enum_name(e):
    try:
        return e.ToString()
    except Exception:
        return str(e)


def is_admin():
    if not IS_WINDOWS:
        try:
            return os.geteuid() == 0
        except AttributeError:
            return False
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def pawnio_installed():
    if not IS_WINDOWS:
        return None
    try:
        import winreg
        winreg.CloseKey(winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                                       r"SYSTEM\CurrentControlSet\Services\PawnIO"))
        return True
    except OSError:
        return False


def cpu_name_fallback():
    if IS_WINDOWS:
        try:
            import winreg
            k = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                               r"HARDWARE\DESCRIPTION\System\CentralProcessor\0")
            name, _ = winreg.QueryValueEx(k, "ProcessorNameString")
            return str(name).strip()
        except OSError:
            pass
    try:
        with open("/proc/cpuinfo", encoding="utf-8") as f:
            for line in f:
                if line.lower().startswith("model name"):
                    return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return platform.processor() or "CPU"


# --------------------------------------------------------------------------- LHM
class LhmProvider:
    def __init__(self, folder):
        self.folder = folder
        self.computer = None
        self.ok = False
        self.status = "nicht geladen"
        self._resolver = None
        self._errors = 0

    def start(self):
        if not IS_WINDOWS:
            self.status = "nur unter Windows"
            return
        dll = os.path.join(self.folder, "LibreHardwareMonitorLib.dll")
        if not os.path.exists(dll):
            self.status = "LibreHardwareMonitorLib.dll nicht gefunden"
            log.warning(self.status + " (%s)", dll)
            return
        try:
            try:
                import pythonnet
                pythonnet.load("netfx")
            except Exception:
                pass  # bereits geladen oder Standard-Runtime
            import clr  # noqa: F401
            from System import AppDomain, ResolveEventHandler
            from System.Reflection import Assembly, AssemblyName

            folder = self.folder

            def _resolve(sender, args):
                # Ersetzt fehlende Binding-Redirects (python.exe hat keine app.config)
                try:
                    name = AssemblyName(args.Name).Name
                    path = os.path.join(folder, name + ".dll")
                    if os.path.exists(path):
                        return Assembly.LoadFrom(path)
                except Exception:
                    pass
                return None

            self._resolver = ResolveEventHandler(_resolve)
            AppDomain.CurrentDomain.AssemblyResolve += self._resolver
            clr.AddReference(dll)
            from LibreHardwareMonitor.Hardware import Computer

            c = Computer()
            for prop in ("IsCpuEnabled", "IsGpuEnabled", "IsMemoryEnabled", "IsMotherboardEnabled",
                         "IsControllerEnabled", "IsStorageEnabled", "IsPsuEnabled", "IsBatteryEnabled"):
                try:
                    setattr(c, prop, True)
                except Exception:
                    pass
            try:
                c.IsNetworkEnabled = False  # Netzwerk liefert psutil
            except Exception:
                pass
            c.Open()
            self.computer = c
            self.ok = True
            self.status = "aktiv"
            log.info("LibreHardwareMonitor geladen")
        except Exception as e:
            self.status = "Fehler: %s" % (str(e).splitlines()[0][:200] if str(e) else type(e).__name__)
            log.exception("LibreHardwareMonitor konnte nicht geladen werden")

    def _collect(self, hw, parent_type, out):
        try:
            hw.Update()
        except Exception:
            pass
        hw_type = _enum_name(hw.HardwareType)
        group = parent_type or hw_type
        for s in hw.Sensors:
            try:
                val = s.Value
                out.append({
                    "hw": str(hw.Name),
                    "hw_type": hw_type,
                    "group": group,
                    "name": str(s.Name),
                    "type": _enum_name(s.SensorType),
                    "value": None if val is None else float(val),
                    "id": str(s.Identifier),
                })
            except Exception:
                continue
        for sub in hw.SubHardware:
            self._collect(sub, group, out)

    def read(self):
        if not self.ok:
            return []
        out = []
        try:
            for hw in self.computer.Hardware:
                self._collect(hw, None, out)
            self._errors = 0
        except Exception as e:
            self._errors += 1
            if self._errors in (1, 10, 100):
                log.warning("LHM-Lesefehler: %s", e)
        return out

    def close(self):
        try:
            if self.computer is not None:
                self.computer.Close()
        except Exception:
            pass


# --------------------------------------------------------------------------- NVML
class NvmlProvider:
    def __init__(self):
        self.ok = False
        self.nv = None

    def start(self):
        try:
            import pynvml
            pynvml.nvmlInit()
            self.nv = pynvml
            self.ok = True
            log.info("NVML aktiv (%d GPU)", pynvml.nvmlDeviceGetCount())
        except Exception as e:
            log.info("NVML nicht verfügbar: %s", e)

    def read(self):
        if not self.ok:
            return []
        nv = self.nv
        gpus = []
        try:
            count = nv.nvmlDeviceGetCount()
        except Exception:
            return []
        for i in range(count):
            g = {"source": "NVML"}
            try:
                h = nv.nvmlDeviceGetHandleByIndex(i)
                name = nv.nvmlDeviceGetName(h)
                g["name"] = name.decode() if isinstance(name, bytes) else str(name)
            except Exception:
                continue

            def tryget(key, fn):
                try:
                    g[key] = fn()
                except Exception:
                    pass
            tryget("usage", lambda: float(nv.nvmlDeviceGetUtilizationRates(h).gpu))
            tryget("temperature", lambda: float(nv.nvmlDeviceGetTemperature(h, nv.NVML_TEMPERATURE_GPU)))
            tryget("fan_percent", lambda: float(nv.nvmlDeviceGetFanSpeed(h)))
            tryget("power", lambda: nv.nvmlDeviceGetPowerUsage(h) / 1000.0)
            tryget("clock", lambda: float(nv.nvmlDeviceGetClockInfo(h, nv.NVML_CLOCK_GRAPHICS)))
            try:
                m = nv.nvmlDeviceGetMemoryInfo(h)
                g["memory_used"] = m.used / 1048576.0
                g["memory_total"] = m.total / 1048576.0
            except Exception:
                pass
            gpus.append(g)
        return gpus

    def close(self):
        try:
            if self.ok:
                self.nv.nvmlShutdown()
        except Exception:
            pass


# --------------------------------------------------------------------------- Auswertung LHM
def _find(sensors, stype, names=None, contains=None):
    for s in sensors:
        if s["type"] != stype or s["value"] is None:
            continue
        if names and s["name"] not in names:
            continue
        if contains and not any(c.lower() in s["name"].lower() for c in contains):
            continue
        return s["value"]
    return None


def _first_by_names(sensors, stype, names):
    for n in names:
        v = _find(sensors, stype, names=[n])
        if v is not None:
            return v
    return None


def lhm_cpu(raw):
    hw_names = [s["hw"] for s in raw if s["hw_type"] == "Cpu"]
    if not hw_names:
        return {}
    first = hw_names[0]
    ss = [s for s in raw if s["hw_type"] == "Cpu" and s["hw"] == first]
    cpu = {"name": first}
    cpu["usage"] = _find(ss, "Load", names=["CPU Total"])
    temp = _first_by_names(ss, "Temperature", [
        "CPU Package", "Core (Tctl/Tdie)", "Core (Tdie)", "Core (Tctl)", "Tctl/Tdie",
        "Package", "Core Average", "Core Max"])
    if temp is None:
        temps = [s["value"] for s in ss if s["type"] == "Temperature" and s["value"] is not None
                 and (s["name"].startswith("Core") or s["name"].startswith("CCD"))]
        temp = max(temps) if temps else None
    cpu["temperature"] = temp
    power = _first_by_names(ss, "Power", ["CPU Package", "Package"])
    if power is None:
        power = _find(ss, "Power")
    cpu["power"] = power
    clocks = [s["value"] for s in ss if s["type"] == "Clock" and s["value"]
              and s["name"].lower().startswith(("core", "cpu core"))]
    cpu["clock"] = sum(clocks) / len(clocks) if clocks else None
    return cpu


def lhm_gpus(raw):
    order = {"GpuNvidia": 0, "GpuAmd": 1, "GpuIntel": 2}
    names = []
    for s in raw:
        if s["hw_type"].startswith("Gpu") and (s["hw_type"], s["hw"]) not in names:
            names.append((s["hw_type"], s["hw"]))
    names.sort(key=lambda t: order.get(t[0], 9))
    gpus = []
    for hw_type, hw in names:
        ss = [s for s in raw if s["hw_type"] == hw_type and s["hw"] == hw]
        g = {"name": hw, "source": "LHM", "vendor": hw_type[3:]}
        g["usage"] = _find(ss, "Load", names=["GPU Core"]) or _find(ss, "Load", names=["D3D 3D"])
        g["temperature"] = _find(ss, "Temperature", names=["GPU Core"])
        if g["temperature"] is None:
            g["temperature"] = next((s["value"] for s in ss if s["type"] == "Temperature"
                                     and s["value"] is not None
                                     and "hot" not in s["name"].lower()
                                     and "memory" not in s["name"].lower()), None)
        g["hotspot"] = _find(ss, "Temperature", contains=["Hot Spot", "Hotspot"])
        g["memory_used"] = _first_by_names(ss, "SmallData", ["GPU Memory Used", "D3D Dedicated Memory Used"])
        g["memory_total"] = _first_by_names(ss, "SmallData", ["GPU Memory Total", "D3D Dedicated Memory Total"])
        g["fan_rpm"] = _find(ss, "Fan")
        g["fan_percent"] = _find(ss, "Control")
        g["power"] = _first_by_names(ss, "Power", ["GPU Package", "GPU Power", "GPU Board Power"])
        if g["power"] is None:
            g["power"] = _find(ss, "Power")
        g["clock"] = _find(ss, "Clock", names=["GPU Core"])
        gpus.append(g)
    return gpus


BOARD_TYPES = ("Motherboard", "SuperIO", "EmbeddedController", "Cooler", "Psu")


def lhm_fans(raw):
    fans = []
    for s in raw:
        if s["type"] != "Fan" or s["value"] is None:
            continue
        if s["group"] in BOARD_TYPES or s["hw_type"] in BOARD_TYPES:
            name = s["name"]
        elif s["hw_type"].startswith("Gpu"):
            name = s["name"] if s["name"].lower().startswith("gpu") else "GPU " + s["name"]
        else:
            name = "%s %s" % (s["hw"], s["name"])
        fans.append({"name": name, "rpm": s["value"]})
    return _dedupe(fans)


def lhm_board_temps(raw):
    out = []
    for s in raw:
        if s["type"] == "Temperature" and s["value"] is not None and \
                (s["group"] in BOARD_TYPES or s["hw_type"] in BOARD_TYPES):
            out.append({"name": s["name"], "value": s["value"]})
    return _dedupe(out)


def lhm_drive_temps(raw):
    per_hw = collections.OrderedDict()
    for s in raw:
        if s["hw_type"] == "Storage" and s["type"] == "Temperature" and s["value"] is not None:
            per_hw.setdefault(s["hw"], []).append(s)
    out = []
    for hw, ss in per_hw.items():
        # Erster Temperaturwert = Haupttemperatur des Laufwerks
        out.append({"name": hw, "value": ss[0]["value"]})
    return _dedupe(out)


def _dedupe(items):
    seen = {}
    for it in items:
        base = it["name"]
        n = seen.get(base, 0) + 1
        seen[base] = n
        if n > 1:
            it["name"] = "%s %d" % (base, n)
    return items


# --------------------------------------------------------------------------- Hub
class SensorHub:
    HISTORY = 120

    def __init__(self, lhm_folder, get_config):
        self.get_config = get_config
        self.lhm = LhmProvider(lhm_folder)
        self.nvml = NvmlProvider()
        self.snapshot = {}
        self.raw = []
        self.lock = threading.Lock()
        self.history = {k: collections.deque(maxlen=self.HISTORY) for k in
                        ("cpu", "cpu_temp", "gpu", "gpu_temp", "ram", "net_up", "net_down")}
        self._stop = threading.Event()
        self._thread = None
        self._last_io = None
        self.admin = is_admin()
        self.pawnio = pawnio_installed()
        self.driver_status = ""
        self.hostname = socket.gethostname()
        if IS_WINDOWS:
            self.linux = None
            self._cpu_name = cpu_name_fallback()
        else:
            from .linux_sys import LinuxSys
            self.linux = LinuxSys()
            self._cpu_name = self.linux.cpu_name()
            self.lhm.status = "nur unter Windows"

    def start(self):
        self._thread = threading.Thread(target=self._run, name="sensoren", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)

    def _run(self):
        if IS_WINDOWS:
            if self.get_config().get("windows", {}).get("driver_auto", True):
                from .driver import ensure_pawnio
                self.driver_status = ensure_pawnio(self.admin)
                self.pawnio = pawnio_installed()
            self.lhm.start()
            import psutil
            psutil.cpu_percent(percpu=True)
        else:
            self.linux.cpu_usage()
        self.nvml.start()
        while not self._stop.is_set():
            t0 = time.time()
            try:
                self._sample()
            except Exception:
                log.exception("Fehler bei der Sensorerfassung")
            self._stop.wait(max(0.2, 1.0 - (time.time() - t0)))
        self.lhm.close()
        self.nvml.close()

    def status(self):
        return {
            "lhm": self.lhm.status,
            "lhm_ok": self.lhm.ok,
            "nvml": self.nvml.ok,
            "admin": self.admin,
            "pawnio": self.pawnio,
            "driver": self.driver_status,
            "windows": IS_WINDOWS,
            "platform": "Windows" if IS_WINDOWS else (self.linux.device_model() or "Linux"),
            "hostname": self.hostname,
        }

    def get(self):
        with self.lock:
            return self.snapshot, list(self.raw)

    def get_history(self):
        with self.lock:
            return {k: list(v) for k, v in self.history.items()}

    # ---------------------------------------------------------------
    def _sample(self):
        now = time.time()
        if IS_WINDOWS:
            parts = self._sample_windows()
        else:
            parts = self._sample_linux()
        raw, cpu, ram, swap, fans, board_temps, drive_temps, disks, io, system = parts

        # --- GPU (LHM unter Windows, NVML auf beiden Systemen)
        gpus = lhm_gpus(raw) if IS_WINDOWS else []
        for ng in self.nvml.read():
            match = next((g for g in gpus if g.get("vendor") == "Nvidia" and
                          (ng["name"] in g["name"] or g["name"] in ng["name"])), None)
            if match:
                for k, v in ng.items():
                    if k not in ("name", "source") and match.get(k) is None:
                        match[k] = v
            else:
                gpus.insert(0, ng)
        for g in gpus:
            if g.get("memory_used") is not None and g.get("memory_total"):
                g["memory_usage"] = g["memory_used"] / g["memory_total"] * 100.0
        gpu = self._select_gpu(gpus, self.get_config().get("gpu_select", "auto"))

        # --- I/O-Raten
        disk_io = {"read": None, "write": None}
        net = {"up": None, "down": None}
        dio, nio = io
        if self._last_io:
            t_old, dio_old, nio_old = self._last_io
            dt = max(0.001, now - t_old)
            if dio and dio_old:
                disk_io["read"] = max(0, dio[0] - dio_old[0]) / dt / 1048576.0
                disk_io["write"] = max(0, dio[1] - dio_old[1]) / dt / 1048576.0
            if nio and nio_old:
                net["up"] = max(0, nio[0] - nio_old[0]) / dt / 1024.0
                net["down"] = max(0, nio[1] - nio_old[1]) / dt / 1024.0
        self._last_io = (now, dio, nio)

        system["hostname"] = self.hostname
        snap = {"ts": now, "cpu": cpu, "gpu": gpu, "gpus": gpus, "ram": ram, "swap": swap,
                "fans": fans, "temps_board": board_temps, "temps_drives": drive_temps,
                "disks": disks, "disk_io": disk_io, "net": net, "system": system}
        with self.lock:
            self.snapshot = snap
            self.raw = raw
            h = self.history
            h["cpu"].append(_rnd(cpu["usage"]))
            h["cpu_temp"].append(_rnd(cpu["temperature"]))
            h["gpu"].append(_rnd(gpu.get("usage")) if gpu else None)
            h["gpu_temp"].append(_rnd(gpu.get("temperature")) if gpu else None)
            h["ram"].append(_rnd(ram["usage"]))
            h["net_up"].append(_rnd(net["up"]))
            h["net_down"].append(_rnd(net["down"]))

    def _sample_windows(self):
        import psutil
        raw = self.lhm.read()
        gb = 1024 ** 3
        cores = psutil.cpu_percent(percpu=True)
        cpu = {"name": self._cpu_name, "usage": sum(cores) / len(cores) if cores else None,
               "temperature": None, "clock": None, "power": None, "cores": [_rnd(c) for c in cores]}
        try:
            f = psutil.cpu_freq()
            if f and f.current:
                cpu["clock"] = f.current
        except Exception:
            pass
        for k, v in lhm_cpu(raw).items():
            if v is not None:
                cpu[k] = v
        vm = psutil.virtual_memory()
        sw = psutil.swap_memory()
        ram = {"usage": vm.percent, "used": (vm.total - vm.available) / gb,
               "total": vm.total / gb, "available": vm.available / gb}
        swap = {"usage": sw.percent, "used": sw.used / gb, "total": sw.total / gb}
        disks = []
        for p in psutil.disk_partitions(all=False):
            if not p.fstype or "cdrom" in p.opts:
                continue
            try:
                u = psutil.disk_usage(p.mountpoint)
            except Exception:
                continue
            disks.append({"name": p.mountpoint.rstrip("\\/:") or p.mountpoint, "usage": u.percent,
                          "used_gb": u.used / gb, "total_gb": u.total / gb, "free_gb": u.free / gb})
        try:
            d = psutil.disk_io_counters()
            dio = (d.read_bytes, d.write_bytes) if d else None
        except Exception:
            dio = None
        nio = None
        try:
            sent = recv = 0
            for name, c in psutil.net_io_counters(pernic=True).items():
                if "loopback" in name.lower():
                    continue
                sent += c.bytes_sent
                recv += c.bytes_recv
            nio = (sent, recv)
        except Exception:
            pass
        system = {"uptime": int(time.time() - psutil.boot_time()), "processes": len(psutil.pids()),
                  "battery": None, "battery_plugged": None, "os": "%s %s" % (platform.system(), platform.release())}
        try:
            b = psutil.sensors_battery()
            if b:
                system["battery"] = b.percent
                system["battery_plugged"] = b.power_plugged
        except Exception:
            pass
        return (raw, cpu, ram, swap, lhm_fans(raw), lhm_board_temps(raw), lhm_drive_temps(raw),
                disks, (dio, nio), system)

    def _sample_linux(self):
        from .linux_sys import split_sensors
        L = self.linux
        gb = 1024 ** 3
        raw = L.sensors()
        total, cores = L.cpu_usage()
        cpu_temp, board, drives, fans = split_sensors(raw)
        cpu = {"name": self._cpu_name, "usage": total, "temperature": cpu_temp, "clock": L.cpu_freq(),
               "power": None, "cores": [_rnd(c) for c in cores]}
        mt, ma, st, su = L.memory()
        ram = {"usage": (mt - ma) / mt * 100.0 if mt else None, "used": (mt - ma) / gb,
               "total": mt / gb, "available": ma / gb}
        swap = {"usage": su / st * 100.0 if st else 0.0, "used": su / gb, "total": st / gb}
        up = L.uptime()
        bat, plugged = L.battery()
        system = {"uptime": int(up) if up is not None else None, "processes": L.processes(),
                  "battery": bat, "battery_plugged": plugged,
                  "os": "%s %s" % (platform.system(), platform.release())}
        try:
            dio = L.disk_io_bytes()
        except Exception:
            dio = None
        try:
            nio = L.net_bytes()
        except Exception:
            nio = None
        return (raw, cpu, ram, swap, _dedupe(fans), _dedupe(board), _dedupe(drives),
                L.disks(), (dio, nio), system)

    @staticmethod
    def _select_gpu(gpus, sel):
        if not gpus:
            return {}
        if sel and sel != "auto":
            for g in gpus:
                if g.get("name") == sel:
                    return g
        return gpus[0]

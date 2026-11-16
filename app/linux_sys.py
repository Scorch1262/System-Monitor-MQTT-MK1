"""Linux-Systemwerte direkt aus /proc und /sys (ohne psutil).

Funktioniert auf Raspberry Pi OS, Debian/Ubuntu und OpenWrt (GL.iNet Brume 2).
"""
import glob
import os
import re


def _read(path, default=None):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read().strip()
    except OSError:
        return default


def _read_num(path):
    v = _read(path)
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


REAL_FS = {"ext2", "ext3", "ext4", "xfs", "btrfs", "vfat", "exfat", "ntfs", "ntfs3", "fuseblk",
           "f2fs", "ubifs", "jffs2", "zfs", "hfsplus"}
SKIP_MOUNTS = ("/proc", "/sys", "/dev", "/run", "/snap", "/boot", "/rom", "/var/lib/docker")

HWMON_KINDS = {
    "temp": ("Temperature", 1000.0),
    "fan": ("Fan", 1.0),
    "in": ("Voltage", 1000.0),
    "curr": ("Current", 1000.0),
    "power": ("Power", 1000000.0),
}

CPU_CHIPS = ("coretemp", "k10temp", "zenpower", "cpu_thermal", "cpu-thermal", "soc_thermal",
             "soc-thermal", "cpu", "x86_pkg_temp", "mtktscpu", "cpu_thermal_zone")
DRIVE_CHIPS = ("nvme", "drivetemp")
HWMON_GLOB = "/sys/class/hwmon/hwmon*"
THERMAL_GLOB = "/sys/class/thermal/thermal_zone*"


class LinuxSys:
    def __init__(self):
        self._prev_cpu = None

    # ------------------------------------------------------------ CPU
    def cpu_usage(self):
        stats = []
        for line in (_read("/proc/stat", "") or "").splitlines():
            if not line.startswith("cpu"):
                break
            parts = line.split()
            vals = [float(x) for x in parts[1:]]
            idle = vals[3] + (vals[4] if len(vals) > 4 else 0)  # idle + iowait
            total = sum(vals[:8])  # guest-Zeiten sind in user enthalten
            stats.append((idle, total))
        prev, self._prev_cpu = self._prev_cpu, stats
        if not prev or len(prev) != len(stats):
            return None, [0.0] * max(0, len(stats) - 1)
        res = []
        for (i0, t0), (i1, t1) in zip(prev, stats):
            dt = t1 - t0
            res.append(max(0.0, min(100.0, (1 - (i1 - i0) / dt) * 100.0)) if dt > 0 else 0.0)
        return res[0], res[1:]

    @staticmethod
    def cpu_freq():
        vals = [_read_num(p) for p in glob.glob("/sys/devices/system/cpu/cpu[0-9]*/cpufreq/scaling_cur_freq")]
        vals = [v for v in vals if v]
        if vals:
            return sum(vals) / len(vals) / 1000.0
        mhz = re.findall(r"^cpu MHz\s*:\s*([\d.]+)", _read("/proc/cpuinfo", "") or "", re.M)
        if mhz:
            return sum(float(x) for x in mhz) / len(mhz)
        return None

    @staticmethod
    def device_model():
        for p in ("/proc/device-tree/model", "/tmp/sysinfo/model", "/sys/firmware/devicetree/base/model"):
            v = _read(p)
            if v:
                return v.replace("\x00", "").strip()
        return None

    @classmethod
    def cpu_name(cls):
        info = _read("/proc/cpuinfo", "") or ""
        cores = len(re.findall(r"^processor\s*:", info, re.M)) or os.cpu_count() or 1
        m = re.search(r"^model name\s*:\s*(.+)$", info, re.M)
        if m and not m.group(1).strip().lower().startswith("arm"):
            return m.group(1).strip()
        model = cls.device_model()
        if model:
            return "%s (%d Kerne)" % (model, cores)
        m = re.search(r"^(?:Hardware|cpu model|system type)\s*:\s*(.+)$", info, re.M)
        return ("%s (%d Kerne)" % (m.group(1).strip(), cores)) if m else "CPU (%d Kerne)" % cores

    # ------------------------------------------------------------ Speicher
    @staticmethod
    def memory():
        mi = {}
        for line in (_read("/proc/meminfo", "") or "").splitlines():
            k, _, rest = line.partition(":")
            try:
                mi[k] = float(rest.split()[0]) * 1024
            except (IndexError, ValueError):
                pass
        total = mi.get("MemTotal", 0)
        avail = mi.get("MemAvailable")
        if avail is None:
            avail = mi.get("MemFree", 0) + mi.get("Buffers", 0) + mi.get("Cached", 0)
        st, sf = mi.get("SwapTotal", 0), mi.get("SwapFree", 0)
        return total, avail, st, st - sf

    # ------------------------------------------------------------ System
    @staticmethod
    def uptime():
        v = _read("/proc/uptime")
        return float(v.split()[0]) if v else None

    @staticmethod
    def processes():
        try:
            return sum(1 for d in os.listdir("/proc") if d.isdigit())
        except OSError:
            return None

    @staticmethod
    def battery():
        for d in glob.glob("/sys/class/power_supply/*"):
            if _read(os.path.join(d, "type")) == "Battery":
                cap = _read_num(os.path.join(d, "capacity"))
                status = _read(os.path.join(d, "status"), "")
                return cap, status in ("Charging", "Full", "Not charging")
        return None, None

    # ------------------------------------------------------------ Laufwerke
    @staticmethod
    def disks():
        out, seen = [], set()
        for line in (_read("/proc/mounts", "") or "").splitlines():
            parts = line.split()
            if len(parts) < 3:
                continue
            dev, mnt, fs = parts[0], parts[1].replace("\\040", " "), parts[2]
            if fs not in REAL_FS or mnt.startswith(SKIP_MOUNTS) or dev in seen:
                continue
            try:
                st = os.statvfs(mnt)
            except OSError:
                continue
            total = st.f_blocks * st.f_frsize
            if not total:
                continue
            seen.add(dev)
            free = st.f_bavail * st.f_frsize
            used = (st.f_blocks - st.f_bfree) * st.f_frsize
            usage = used / (used + free) * 100.0 if used + free else 0.0
            out.append({"name": mnt, "usage": usage, "used_gb": used / 1024 ** 3,
                        "total_gb": total / 1024 ** 3, "free_gb": free / 1024 ** 3})
        return out

    @staticmethod
    def disk_io_bytes():
        """Summe gelesener/geschriebener Bytes über alle physischen Laufwerke."""
        try:
            whole = set(os.listdir("/sys/block"))
        except OSError:
            whole = set()
        rd = wr = 0
        for line in (_read("/proc/diskstats", "") or "").splitlines():
            p = line.split()
            if len(p) < 10:
                continue
            name = p[2]
            if whole and name not in whole:
                continue  # Partition
            if name.startswith(("loop", "ram", "zram", "dm-", "md", "sr", "mtdblock")):
                continue
            rd += int(p[5]) * 512
            wr += int(p[9]) * 512
        return rd, wr

    @staticmethod
    def net_bytes():
        sent = recv = 0
        for line in (_read("/proc/net/dev", "") or "").splitlines()[2:]:
            name, _, rest = line.partition(":")
            name = name.strip()
            if name == "lo" or not rest:
                continue
            f = rest.split()
            recv += int(f[0])
            sent += int(f[8])
        return sent, recv

    # ------------------------------------------------------------ Sensoren
    @staticmethod
    def sensors():
        """Rohsensoren im gleichen Format wie LibreHardwareMonitor."""
        out = []
        chips = set()
        for d in sorted(glob.glob(HWMON_GLOB)):
            chip = _read(os.path.join(d, "name")) or os.path.basename(d)
            chips.add(chip)
            base = d if glob.glob(os.path.join(d, "*_input")) else os.path.join(d, "device")
            files = glob.glob(os.path.join(base, "*_input"))
            for f in sorted(files, key=lambda x: [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", x)]):
                m = re.match(r"(temp|fan|in|curr|power)(\d+)_input$", os.path.basename(f))
                if not m:
                    continue
                kind, idx = m.groups()
                stype, div = HWMON_KINDS[kind]
                raw = _read_num(f)
                if raw is None:
                    continue
                label = _read(os.path.join(base, "%s%s_label" % (kind, idx)))
                if not label:
                    label = {"temp": "Temperatur", "fan": "Lüfter", "in": "Spannung",
                             "curr": "Strom", "power": "Leistung"}[kind] + " " + idx
                out.append({"hw": chip, "hw_type": "Hwmon", "group": "Hwmon", "name": label,
                            "type": stype, "value": raw / div, "id": f})
        for z in sorted(glob.glob(THERMAL_GLOB)):
            ztype = _read(os.path.join(z, "type")) or os.path.basename(z)
            if ztype in chips or ztype.replace("-", "_") in chips:
                continue  # schon über hwmon erfasst
            v = _read_num(os.path.join(z, "temp"))
            if v is None:
                continue
            out.append({"hw": ztype, "hw_type": "Thermal", "group": "Thermal", "name": ztype,
                        "type": "Temperature", "value": v / 1000.0, "id": z})
        return out


def split_sensors(raw):
    """Teilt Linux-Rohsensoren auf: CPU-Temperatur, Mainboard-/Sonstige-Temperaturen, Laufwerke, Lüfter."""
    temps = [s for s in raw if s["type"] == "Temperature" and s["value"] is not None
             and -40 < s["value"] < 150]
    cpu_temp, cpu_ids = None, set()
    for chip in CPU_CHIPS:
        cand = [s for s in temps if s["hw"].lower() == chip]
        if not cand:
            continue
        pref = next((s for s in cand if re.search(r"package|tctl|tdie", s["name"], re.I)), cand[0])
        cpu_temp = pref["value"]
        cpu_ids = {s["id"] for s in cand}
        break
    if cpu_temp is None:
        cand = [s for s in temps if re.search(r"cpu|soc|core", s["hw"], re.I)]
        if cand:
            cpu_temp = cand[0]["value"]
            cpu_ids = {cand[0]["id"]}
    drives = [s for s in temps if s["hw"].lower().startswith(DRIVE_CHIPS)]
    drive_ids = {s["id"] for s in drives}
    board = []
    for s in temps:
        if s["id"] in cpu_ids or s["id"] in drive_ids:
            continue
        name = s["name"] if s["hw_type"] == "Thermal" else "%s %s" % (s["hw"], s["name"])
        board.append({"name": name, "value": s["value"]})
    drive_temps = []
    for s in drives:
        drive_temps.append({"name": s["hw"] if s["name"].lower() in ("composite", "temperatur 1") else
                            "%s %s" % (s["hw"], s["name"]), "value": s["value"]})
    fans = [{"name": "%s %s" % (s["hw"], s["name"]), "rpm": s["value"]}
            for s in raw if s["type"] == "Fan" and s["value"] is not None]
    return cpu_temp, board, drive_temps, fans

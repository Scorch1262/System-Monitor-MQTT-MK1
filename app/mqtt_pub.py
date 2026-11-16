"""MQTT-Versand der Messwerte."""
import json
import logging
import threading
import time
from datetime import datetime, timezone

from .config import CORE_METRICS
from .mqtt_client import MiniMqtt, random_suffix
from .sensors import slug
from .version import APP_NAME, VERSION

log = logging.getLogger(__name__)

INT_UNITS = ("MHz", "MB", "U/min", "", "s")


def _fmt(v, unit):
    if v is None:
        return None
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    if v != v:
        return None
    return int(round(v)) if unit in INT_UNITS else round(v, 1)


def resolve_base(cfg, hostname):
    base = cfg["publish"]["base_topic"].replace("{hostname}", hostname.replace(" ", "_"))
    return base.strip("/")


def build_messages(snap, raw, cfg, hostname):
    """Liefert (Basis-Topic, [(topic, payload)], json_dict) für den aktuellen Messstand."""
    base = resolve_base(cfg, hostname)
    if not snap:
        return base, [], {}
    pub = cfg["publish"]
    metrics = cfg["metrics"]
    dyn = cfg["dynamic"]
    singles = []
    js = {"timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
          "hostname": hostname}

    def put(path, value):
        node = js
        parts = path.split(".")
        for p in parts[:-1]:
            node = node.setdefault(p, {})
        node[parts[-1]] = value

    any_cpu = any_gpu = False
    for key, group, _label, unit in CORE_METRICS:
        if not metrics.get(key, False):
            continue
        section, field = key.split(".", 1)
        src = snap.get(section) or {}
        v = _fmt(src.get(field), unit)
        if section == "cpu":
            any_cpu = True
        if section == "gpu":
            any_gpu = True
        if v is None:
            continue
        put(key, v)
        singles.append((key.replace(".", "/"), v))

    if any_cpu and snap.get("cpu", {}).get("name"):
        put("cpu.name", snap["cpu"]["name"])
        singles.append(("cpu/name", snap["cpu"]["name"]))
    if any_gpu and snap.get("gpu", {}).get("name"):
        put("gpu.name", snap["gpu"]["name"])
        singles.append(("gpu/name", snap["gpu"]["name"]))

    if dyn.get("cores"):
        cores = snap.get("cpu", {}).get("cores") or []
        put("cpu.cores", cores)
        for i, c in enumerate(cores):
            singles.append(("cpu/cores/%d" % (i + 1), c))
    if dyn.get("fans"):
        d = {}
        for f in snap.get("fans", []):
            v = _fmt(f["rpm"], "U/min")
            if v is None:
                continue
            k = slug(f["name"])
            d[k] = v
            singles.append(("fans/" + k, v))
        js["fans"] = d
    if dyn.get("temps_board"):
        d = {}
        for t in snap.get("temps_board", []):
            v = _fmt(t["value"], "°C")
            if v is None:
                continue
            k = slug(t["name"])
            d[k] = v
            singles.append(("temps/" + k, v))
        js["temps"] = d
    if dyn.get("temps_drives"):
        d = {}
        for t in snap.get("temps_drives", []):
            v = _fmt(t["value"], "°C")
            if v is None:
                continue
            k = slug(t["name"])
            d[k] = {"temperature": v}
            singles.append(("drives/%s/temperature" % k, v))
        js["drives"] = d
    if dyn.get("disks"):
        d = {}
        for disk in snap.get("disks", []):
            k = slug(disk["name"]) if disk["name"] not in ("/",) else "root"
            entry = {"usage": _fmt(disk["usage"], "%"), "free_gb": _fmt(disk["free_gb"], "GB"),
                     "total_gb": _fmt(disk["total_gb"], "GB")}
            d[k] = entry
            singles.append(("disks/%s/usage" % k, entry["usage"]))
            singles.append(("disks/%s/free_gb" % k, entry["free_gb"]))
        js["disks"] = d
    if dyn.get("raw"):
        seen = set()
        for s in raw:
            if s["value"] is None:
                continue
            t = "sensors/%s/%s/%s" % (slug(s["hw"]), slug(s["type"]), slug(s["name"]))
            n, tt = 1, t
            while tt in seen:
                n += 1
                tt = "%s_%d" % (t, n)
            seen.add(tt)
            singles.append((tt, round(s["value"], 2)))

    msgs = []
    if pub.get("json"):
        sub = pub.get("json_subtopic", "")
        msgs.append((base + "/" + sub if sub else base,
                     json.dumps(js, ensure_ascii=False, separators=(",", ":"))))
    if pub.get("single"):
        for t, v in singles:
            msgs.append((base + "/" + t, str(v)))
    return base, msgs, js


def _make_client(mc, client_id, will=None):
    return MiniMqtt(client_id, mc.get("username", ""), mc.get("password", ""),
                    keepalive=int(mc.get("keepalive", 60)), tls=bool(mc.get("tls")),
                    tls_insecure=bool(mc.get("tls_insecure")), will=will)


def test_connection(mc, timeout=6.0):
    if not mc.get("host"):
        return False, "Kein Broker angegeben"
    client = _make_client(mc, "%s-test-%s" % (APP_NAME, random_suffix()))
    try:
        client.connect(mc["host"], int(mc["port"]), timeout=timeout)
    except Exception as e:
        return False, "Verbindung fehlgeschlagen: %s" % e
    client.disconnect()
    return True, "Verbindung erfolgreich"


class MqttPublisher:
    """Hält die Verbindung zum Broker (mit automatischem Wiederverbinden) und sendet im Intervall."""

    def __init__(self, hub, get_config):
        self.hub = hub
        self.get_config = get_config
        self.client = None
        self.state = "aus"
        self.last_error = ""
        self.last_publish = None
        self.sent_total = 0
        self.last_count = 0
        self.paused = False
        self._conn_key = None
        self._target = None  # (mqtt-config, status_topic)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread = None
        self._retry_at = 0.0
        self._retry_delay = 2.0

    def start(self):
        self.apply_config()
        self._thread = threading.Thread(target=self._run, name="mqtt", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._wake.set()
        self._disconnect(graceful=True)

    def apply_config(self):
        cfg = self.get_config()
        mc = cfg["mqtt"]
        base = resolve_base(cfg, self.hub.hostname)
        st = cfg["publish"].get("status_topic")
        status_topic = base + "/" + st if st else None
        key = json.dumps([mc, status_topic], sort_keys=True)
        with self._lock:
            changed = key != self._conn_key
            self._conn_key = key
            if changed:
                self._target = (dict(mc), status_topic) if mc.get("host") else None
                self._retry_at = 0.0
                self._retry_delay = 2.0
        if changed:
            self._disconnect(graceful=True)
            self.state = "verbinde" if self._target else "aus"
            self.last_error = ""
            self.last_publish = None
        self._wake.set()

    def _connect(self):
        mc, status_topic = self._target
        cid = mc.get("client_id") or "%s-%s" % (slug(self.hub.hostname), random_suffix())
        will = (status_topic, "offline", 1, True) if status_topic else None
        client = _make_client(mc, cid, will)
        client.on_disconnect = self._on_disconnect
        self.state = "verbinde"
        try:
            client.connect(mc["host"], int(mc["port"]))
        except Exception as e:
            self.state = "fehler"
            self.last_error = str(e)
            self._retry_at = time.time() + self._retry_delay
            log.warning("MQTT-Verbindung fehlgeschlagen: %s (neuer Versuch in %ds)", e, self._retry_delay)
            self._retry_delay = min(60.0, self._retry_delay * 2)
            return
        self.client = client
        self.state = "verbunden"
        self.last_error = ""
        self._retry_delay = 2.0
        log.info("MQTT verbunden mit %s:%s", mc["host"], mc["port"])
        if status_topic:
            client.publish(status_topic, "online", qos=1, retain=True)

    def _on_disconnect(self, reason):
        self.state = "getrennt"
        self.last_error = "Verbindung verloren: %s" % reason
        self._retry_at = time.time() + 2
        log.info("MQTT getrennt (%s)", reason)
        self._wake.set()

    def _disconnect(self, graceful=False):
        client, self.client = self.client, None
        if not client:
            return
        try:
            if graceful and client.connected and self._target and self._target[1]:
                client.publish(self._target[1], "offline", qos=1, retain=True)
        except Exception:
            pass
        client.disconnect()

    def _run(self):
        next_due = 0.0
        while not self._stop.is_set():
            now = time.time()
            if self._target and (self.client is None or not self.client.connected) and now >= self._retry_at:
                self.client = None
                self._connect()
                if self.state == "verbunden":
                    next_due = 0.0
            cfg = self.get_config()
            if time.time() >= next_due:
                if self.state == "verbunden" and cfg["publish"]["enabled"] and not self.paused:
                    self.publish_now(cfg)
                next_due = time.time() + float(cfg["publish"]["interval"])
            self._wake.wait(min(1.0, max(0.05, next_due - time.time())))
            self._wake.clear()

    def publish_now(self, cfg=None):
        cfg = cfg or self.get_config()
        client = self.client
        if not client or not client.connected:
            return 0
        snap, raw = self.hub.get()
        _, msgs, _ = build_messages(snap, raw, cfg, self.hub.hostname)
        qos = int(cfg["mqtt"].get("qos", 0))
        retain = bool(cfg["mqtt"].get("retain", False))
        n = 0
        for topic, payload in msgs:
            if client.publish(topic, payload, qos=qos, retain=retain):
                n += 1
        self.sent_total += n
        self.last_count = n
        self.last_publish = time.time()
        return n

    def status(self):
        cfg = self.get_config()
        mc = cfg["mqtt"]
        return {
            "state": "pausiert" if (self.paused and self.state == "verbunden") else self.state,
            "connected": self.state == "verbunden",
            "paused": self.paused,
            "broker": "%s:%s" % (mc["host"], mc["port"]) if mc["host"] else "",
            "base_topic": resolve_base(cfg, self.hub.hostname),
            "last_error": self.last_error,
            "last_publish": self.last_publish,
            "last_count": self.last_count,
            "sent_total": self.sent_total,
            "interval": cfg["publish"]["interval"],
            "enabled": cfg["publish"]["enabled"],
        }

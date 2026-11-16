"""Netzwerkzugriff: eigene IP-Adressen, Prüfung "Zugriff vom PC selbst", Windows-Firewall-Regel."""
import ipaddress
import logging
import socket
import subprocess
import sys

from .version import APP_NAME

log = logging.getLogger(__name__)
IS_WINDOWS = sys.platform.startswith("win")
CREATE_NO_WINDOW = 0x08000000
RULE_NAME = APP_NAME + " Webseite"


def local_ips():
    """IPv4-Adressen dieses Geräts (ohne Loopback), Hauptadresse zuerst."""
    ips = []
    try:  # Adresse der Standardroute (es wird nichts gesendet)
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("192.0.2.1", 9))
        ips.append(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.append(info[4][0])
    except OSError:
        pass
    out = []
    for ip in ips:
        try:
            a = ipaddress.ip_address(ip)
        except ValueError:
            continue
        if a.is_loopback or a.is_link_local or ip in out:
            continue
        out.append(ip)
    return out


def is_local_client(addr):
    """True, wenn die Anfrage vom Gerät selbst kommt."""
    if not addr:
        return False
    if addr.startswith("::ffff:"):
        addr = addr[7:]
    try:
        if ipaddress.ip_address(addr).is_loopback:
            return True
    except ValueError:
        return False
    return addr in local_ips()


# --------------------------------------------------------------------------- Windows-Firewall
def _netsh(args):
    return subprocess.run(["netsh", "advfirewall", "firewall"] + args, capture_output=True,
                          text=True, creationflags=CREATE_NO_WINDOW)


def firewall_status(port):
    """{'available', 'exists', 'port_ok'} - Abfrage geht auch ohne Adminrechte."""
    if not IS_WINDOWS:
        return {"available": False}
    try:
        r = _netsh(["show", "rule", "name=%s" % RULE_NAME])
    except Exception as e:
        return {"available": True, "exists": False, "port_ok": False, "error": str(e)}
    exists = r.returncode == 0
    port_ok = exists and any(line.strip().endswith(str(port)) for line in r.stdout.splitlines()
                             if ":" in line)
    return {"available": True, "exists": exists, "port_ok": bool(port_ok)}


def firewall_ensure(port):
    """Legt die Eingangsregel (TCP, nur lokales Subnetz) an bzw. passt den Port an. Braucht Adminrechte."""
    if not IS_WINDOWS:
        return True, ""
    st = firewall_status(port)
    if st.get("port_ok"):
        return True, "Firewall-Regel vorhanden"
    if st.get("exists"):
        _netsh(["delete", "rule", "name=%s" % RULE_NAME])
    r = _netsh(["add", "rule", "name=%s" % RULE_NAME, "dir=in", "action=allow", "protocol=TCP",
                "localport=%d" % int(port), "remoteip=localsubnet", "profile=any",
                "description=%s - Zugriff auf die Webseite aus dem lokalen Netz" % APP_NAME])
    if r.returncode != 0:
        msg = (r.stdout or r.stderr or "").strip()
        log.warning("Firewall-Regel nicht angelegt: %s", msg)
        return False, "Firewall-Regel nicht angelegt (Adminrechte nötig): %s" % msg[:200]
    log.info("Firewall-Regel für Port %d angelegt", int(port))
    return True, "Firewall-Regel für Port %d angelegt" % int(port)


def firewall_remove():
    if IS_WINDOWS:
        _netsh(["delete", "rule", "name=%s" % RULE_NAME])

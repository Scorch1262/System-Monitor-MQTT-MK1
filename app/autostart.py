"""Autostart unter Windows: normal (Registry, HKCU\\...\\Run) oder mit Adminrechten (Aufgabenplanung)."""
import logging
import os
import subprocess
import sys
import tempfile
from xml.sax.saxutils import escape

from .sensors import IS_WINDOWS, is_admin
from .version import APP_NAME

log = logging.getLogger(__name__)
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
CREATE_NO_WINDOW = 0x08000000


def _command():
    """(exe, argumente) für den Programmstart."""
    if getattr(sys, "frozen", False):
        return sys.executable, "--autostart"
    script = os.path.abspath(sys.argv[0])
    pyw = sys.executable.replace("python.exe", "pythonw.exe")
    return pyw, '"%s" --autostart' % script


def _run(args):
    return subprocess.run(args, capture_output=True, text=True, creationflags=CREATE_NO_WINDOW)


def _reg_get():
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.QueryValueEx(k, APP_NAME)
            return True
    except OSError:
        return False


def _reg_set(enable):
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if enable:
            exe, args = _command()
            winreg.SetValueEx(k, APP_NAME, 0, winreg.REG_SZ, '"%s" %s' % (exe, args))
        else:
            try:
                winreg.DeleteValue(k, APP_NAME)
            except OSError:
                pass


def _task_exists():
    return _run(["schtasks", "/Query", "/TN", APP_NAME]).returncode == 0


def _task_create():
    exe, args = _command()
    user = "%s\\%s" % (os.environ.get("USERDOMAIN", ""), os.environ.get("USERNAME", ""))
    xml = """<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>{name} - Systemwerte per MQTT senden</Description></RegistrationInfo>
  <Triggers>
    <LogonTrigger><Enabled>true</Enabled><UserId>{user}</UserId><Delay>PT10S</Delay></LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author"><UserId>{user}</UserId><LogonType>InteractiveToken</LogonType><RunLevel>HighestAvailable</RunLevel></Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>false</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings><StopOnIdleEnd>false</StopOnIdleEnd><RestartOnIdle>false</RestartOnIdle></IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Priority>7</Priority>
  </Settings>
  <Actions Context="Author">
    <Exec><Command>{exe}</Command><Arguments>{args}</Arguments><WorkingDirectory>{wd}</WorkingDirectory></Exec>
  </Actions>
</Task>""".format(name=escape(APP_NAME), user=escape(user), exe=escape(exe), args=escape(args),
                  wd=escape(os.path.dirname(exe) if getattr(sys, "frozen", False) else os.getcwd()))
    fd, path = tempfile.mkstemp(suffix=".xml")
    os.close(fd)
    try:
        with open(path, "w", encoding="utf-16") as f:
            f.write(xml)
        r = _run(["schtasks", "/Create", "/TN", APP_NAME, "/XML", path, "/F"])
        if r.returncode != 0:
            raise RuntimeError((r.stderr or r.stdout or "schtasks-Fehler").strip())
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def _task_delete():
    r = _run(["schtasks", "/Delete", "/TN", APP_NAME, "/F"])
    if r.returncode != 0 and _task_exists():
        raise RuntimeError("Aufgabe konnte nicht gelöscht werden (Adminrechte nötig?): "
                           + (r.stderr or r.stdout).strip())


SERVICE = "system-monitor-mqtt"


def linux_service_status():
    """Autostart unter Linux richtet install.sh als Dienst ein (procd bzw. systemd)."""
    if os.path.exists("/etc/init.d/" + SERVICE):
        enabled = any(n.endswith(SERVICE) for n in os.listdir("/etc/rc.d")) if os.path.isdir("/etc/rc.d") else False
        return "Dienst (procd) " + ("aktiv beim Systemstart" if enabled else "eingerichtet, aber deaktiviert")
    if os.path.exists("/etc/systemd/system/%s.service" % SERVICE):
        try:
            r = subprocess.run(["systemctl", "is-enabled", SERVICE], capture_output=True, text=True)
            on = r.stdout.strip() == "enabled"
        except OSError:
            on = False
        return "Dienst (systemd) " + ("aktiv beim Systemstart" if on else "eingerichtet, aber deaktiviert")
    return "kein Dienst eingerichtet - install.sh ausführen"


def status():
    if not IS_WINDOWS:
        return {"available": False, "mode": "off", "admin": is_admin(), "linux": linux_service_status()}
    task = _task_exists()
    reg = _reg_get()
    mode = "admin" if task else ("user" if reg else "off")
    return {"available": True, "mode": mode, "admin": is_admin()}


def set_mode(mode):
    if not IS_WINDOWS:
        raise RuntimeError("Autostart wird nur unter Windows unterstützt")
    if mode not in ("off", "user", "admin"):
        raise ValueError("Unbekannter Modus")
    if mode == "admin":
        if not is_admin():
            raise RuntimeError("Für den Autostart mit Adminrechten bitte das Programm zuerst "
                               "als Administrator neu starten.")
        _task_create()
        _reg_set(False)
    elif mode == "user":
        if _task_exists():
            _task_delete()
        _reg_set(True)
    else:
        if _task_exists():
            _task_delete()
        _reg_set(False)
    return status()


def openwrt_firewall_port(port):
    """Passt die von install.sh angelegte Firewall-Regel (OpenWrt) an einen neuen Webport an."""
    if not os.path.exists("/etc/openwrt_release"):
        return
    try:
        if subprocess.run(["uci", "-q", "get", "firewall.system_monitor_mqtt"],
                          capture_output=True).returncode != 0:
            return
        subprocess.run(["uci", "set", "firewall.system_monitor_mqtt.dest_port=%d" % int(port)], check=True)
        subprocess.run(["uci", "commit", "firewall"], check=True)
        subprocess.run(["/etc/init.d/firewall", "reload"], capture_output=True)
        log.info("Firewall-Regel auf Port %d angepasst", int(port))
    except Exception as e:
        log.warning("Firewall-Regel nicht angepasst: %s", e)

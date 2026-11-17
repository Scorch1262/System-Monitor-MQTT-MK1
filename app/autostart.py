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


APPROVED_KEY = r"Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run"


def _reg_value():
    """Befehl aus HKCU/.../Run oder None."""
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            return str(winreg.QueryValueEx(k, APP_NAME)[0])
    except OSError:
        return None


def _reg_get():
    return _reg_value() is not None


def _startup_disabled():
    """True, wenn der Eintrag im Task-Manager (Reiter Autostart) deaktiviert wurde."""
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, APPROVED_KEY) as k:
            data = winreg.QueryValueEx(k, APP_NAME)[0]
            return bool(data) and (data[0] & 1) == 1
    except OSError:
        return False


def _startup_enable():
    import winreg
    try:
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, APPROVED_KEY) as k:
            winreg.SetValueEx(k, APP_NAME, 0, winreg.REG_BINARY, b"\x02" + b"\x00" * 11)
    except OSError as e:
        log.warning("StartupApproved nicht gesetzt: %s", e)


def _reg_set(enable):
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if enable:
            exe, args = _command()
            winreg.SetValueEx(k, APP_NAME, 0, winreg.REG_SZ, '"%s" %s' % (exe, args))
            _startup_enable()
        else:
            try:
                winreg.DeleteValue(k, APP_NAME)
            except OSError:
                pass


def _current_user():
    """DOMÄNE\\Benutzer des angemeldeten Benutzers (für Trigger und Principal der Aufgabe)."""
    try:
        r = _run(["whoami"])
        if r.returncode == 0 and "\\" in r.stdout.strip():
            return r.stdout.strip()
    except OSError:
        pass
    dom = os.environ.get("USERDOMAIN") or os.environ.get("COMPUTERNAME", "")
    return "%s\\%s" % (dom, os.environ.get("USERNAME", ""))


def _task_info():
    """Details der Aufgabe über PowerShell (Pfad, Zustand, letzter Start, Ergebnis) oder None."""
    ps = ("$ErrorActionPreference='Stop';"
          "$t=Get-ScheduledTask -TaskName '%s';$i=$t|Get-ScheduledTaskInfo;"
          "[pscustomobject]@{Execute=$t.Actions[0].Execute;Arguments=$t.Actions[0].Arguments;"
          "State=[string]$t.State;LastRun=if($i.LastRunTime){$i.LastRunTime.ToString('s')}else{''};"
          "Result=[int64]$i.LastTaskResult}|ConvertTo-Json -Compress") % APP_NAME
    try:
        r = _run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps])
        if r.returncode != 0 or not r.stdout.strip():
            return None
        import json
        return json.loads(r.stdout.strip())
    except Exception:
        return None


TASK_RESULTS = {0: "erfolgreich", 0x41301: "läuft gerade", 0x41303: "noch nie gestartet",
                0x41306: "abgebrochen", 0x80070002: "exe nicht gefunden",
                0x80070005: "Zugriff verweigert", 0xC000013A: "beim Abmelden beendet"}


def _same_exe(cmd):
    exe, _ = _command()
    return bool(cmd) and os.path.normcase(exe) in os.path.normcase(cmd.replace('"', ""))


def _task_exists():
    return _run(["schtasks", "/Query", "/TN", APP_NAME]).returncode == 0


def _task_create():
    exe, args = _command()
    user = _current_user()
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


def status(details=True):
    if not IS_WINDOWS:
        return {"available": False, "mode": "off", "admin": is_admin(), "linux": linux_service_status()}
    task = _task_exists()
    reg = _reg_value()
    mode = "admin" if task else ("user" if reg else "off")
    st = {"available": True, "mode": mode, "admin": is_admin(), "problems": [], "info": ""}
    exe, _ = _command()
    if mode == "user":
        if not _same_exe(reg):
            st["problems"].append("Eintrag zeigt auf eine andere exe (%s) – „Neu einrichten“ klicken" % reg)
        if _startup_disabled():
            st["problems"].append("Im Task-Manager (Reiter Autostart) deaktiviert – „Neu einrichten“ aktiviert ihn wieder")
        st["info"] = "Registry-Eintrag (HKCU\\…\\Run)"
    elif mode == "admin" and details:
        ti = _task_info()
        if ti:
            if not _same_exe(ti.get("Execute")):
                st["problems"].append("Aufgabe startet eine andere exe (%s) – „Neu einrichten“ klicken" % ti.get("Execute"))
            if ti.get("State") == "Disabled":
                st["problems"].append("Aufgabe ist in der Aufgabenplanung deaktiviert")
            res = int(ti.get("Result") or 0) & 0xFFFFFFFF
            text = TASK_RESULTS.get(res, "Code 0x%08X" % res)
            last = (ti.get("LastRun") or "").replace("T", " ")
            if last.startswith("1999"):
                last = ""
            st["info"] = "Aufgabenplanung · letzter Start: %s · Ergebnis: %s" % (last or "–", text)
            if res not in (0, 0x41301, 0x41303):
                st["problems"].append("Letzter Autostart fehlgeschlagen: %s" % text)
        else:
            st["info"] = "Aufgabenplanung (Details nicht lesbar)"
    if mode == "admin" and not st["admin"] and any("andere exe" in p for p in st["problems"]):
        st["problems"].append("Zum Reparieren der Aufgabe werden Adminrechte benötigt")
    return st


def repair():
    """Beim Programmstart: Autostart-Eintrag auf den aktuellen exe-Pfad korrigieren (z. B. nach Umzug)."""
    if not IS_WINDOWS or not getattr(sys, "frozen", False):
        return
    try:
        reg = _reg_value()
        if reg is not None and not _same_exe(reg):
            _reg_set(True)
            log.info("Autostart-Eintrag (Registry) auf %s korrigiert", sys.executable)
        if _task_exists() and is_admin():
            ti = _task_info()
            if ti and not _same_exe(ti.get("Execute")):
                _task_create()
                log.info("Autostart-Aufgabe auf %s korrigiert", sys.executable)
    except Exception as e:
        log.warning("Autostart-Prüfung fehlgeschlagen: %s", e)


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

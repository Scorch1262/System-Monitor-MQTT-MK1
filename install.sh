#!/bin/sh
# System-Monitor-MQTT-MK1 – Installation unter Linux
#   GL.iNet Brume 2 / OpenWrt  -> /root/System-Monitor-MQTT-MK1, Dienst über procd
#   Raspberry Pi / Debian      -> /opt/System-Monitor-MQTT-MK1,  Dienst über systemd
#
# Aufruf aus dem Repo / der Zip:   sh install.sh        (Raspberry: sudo sh install.sh)
# Direkt aus GitHub (ohne git):    wget -qO- https://raw.githubusercontent.com/Scorch1262/System-Monitor-MQTT-MK1/main/install.sh | sh
#   Liegt kein main.py neben install.sh, wird der aktuelle Stand des Repos heruntergeladen.
# Ein erneuter Aufruf aktualisiert das Programm, die Einstellungen bleiben erhalten.

NAME=System-Monitor-MQTT-MK1
SERVICE=system-monitor-mqtt
REPO=${REPO:-Scorch1262/System-Monitor-MQTT-MK1}
BRANCH=${BRANCH:-main}
SRC=$(cd "$(dirname "$0")" 2>/dev/null && pwd)
case "$0" in
    sh|-sh|ash|-ash|bash|-bash|*/sh|*/ash|*/bash) SRC="" ;;  # per Pipe gestartet -> immer aus GitHub laden
esac

say() { echo "[$NAME] $*"; }
fail() { echo "[$NAME] FEHLER: $*" >&2; exit 1; }

[ "$(id -u)" = "0" ] || fail "Bitte als root ausführen (Raspberry: sudo sh install.sh)"

if [ -f /etc/openwrt_release ]; then
    SYS=openwrt
    TARGET=/root/$NAME
else
    SYS=linux
    TARGET=/opt/$NAME
fi
say "System: $SYS, Zielordner: $TARGET"

# ---------------------------------------------------------------- Quellcode aus GitHub holen
if [ ! -f "$SRC/main.py" ] || [ ! -d "$SRC/app" ]; then
    URL="https://codeload.github.com/$REPO/tar.gz/refs/heads/$BRANCH"
    TMP=/tmp/$NAME-download
    rm -rf "$TMP" && mkdir -p "$TMP"
    say "Lade $REPO ($BRANCH) von GitHub ..."
    if command -v wget >/dev/null 2>&1; then
        wget -qO "$TMP/src.tar.gz" "$URL" || fail "Download fehlgeschlagen ($URL)"
    elif command -v curl >/dev/null 2>&1; then
        curl -fsSL -o "$TMP/src.tar.gz" "$URL" || fail "Download fehlgeschlagen ($URL)"
    else
        fail "weder wget noch curl vorhanden"
    fi
    tar -xzf "$TMP/src.tar.gz" -C "$TMP" || fail "Entpacken fehlgeschlagen"
    SRC=$(ls -d "$TMP"/*/ | head -n 1)
    SRC=${SRC%/}
    [ -f "$SRC/main.py" ] || fail "main.py nicht im Download gefunden"
fi

# ---------------------------------------------------------------- Python 3
py_ok() {
    python3 -c "import json, ssl, socket, select, struct, glob, logging.handlers, http.server, urllib.parse, argparse, platform" >/dev/null 2>&1
}
if ! py_ok; then
    say "Python 3 (bzw. Module) fehlen - werden installiert ..."
    if [ $SYS = openwrt ]; then
        opkg update && opkg install python3 || fail "opkg install python3 fehlgeschlagen (Internet?)"
    elif command -v apt-get >/dev/null 2>&1; then
        apt-get update && apt-get install -y python3 || fail "apt-get install python3 fehlgeschlagen"
    else
        fail "Python 3 nicht gefunden - bitte manuell installieren"
    fi
fi
py_ok || fail "Python 3 ist unvollständig"
PY=$(command -v python3)
say "Python: $($PY --version 2>&1)"

# ---------------------------------------------------------------- Dienst stoppen
if [ $SYS = openwrt ] && [ -x /etc/init.d/$SERVICE ]; then
    /etc/init.d/$SERVICE stop >/dev/null 2>&1
elif command -v systemctl >/dev/null 2>&1; then
    systemctl stop $SERVICE >/dev/null 2>&1
fi

# ---------------------------------------------------------------- Dateien kopieren
if [ "$SRC" != "$TARGET" ]; then
    mkdir -p "$TARGET"
    for d in app web assets; do
        rm -rf "${TARGET:?}/$d"
        cp -r "$SRC/$d" "$TARGET/" || fail "Kopieren von $d fehlgeschlagen"
    done
    for f in main.py install.sh uninstall.sh README.md; do
        [ -f "$SRC/$f" ] && cp "$SRC/$f" "$TARGET/"
    done
    say "Dateien nach $TARGET kopiert (Einstellungen bleiben erhalten)"
fi
for d in "$TARGET"/__pycache__ "$TARGET"/app/__pycache__; do rm -rf "$d"; done

# Port aus vorhandener Konfiguration lesen
PORT=$($PY -c "import json;print(json.load(open('$TARGET/$NAME.json'))['web']['port'])" 2>/dev/null)
[ -n "$PORT" ] || PORT=8095

# ---------------------------------------------------------------- Dienst einrichten
if [ $SYS = openwrt ]; then
    cat > /etc/init.d/$SERVICE <<INIT
#!/bin/sh /etc/rc.common
# $NAME - Systemwerte per MQTT senden
START=99
STOP=10
USE_PROCD=1

start_service() {
    procd_open_instance
    procd_set_param command $PY $TARGET/main.py --service
    procd_set_param respawn 3600 5 0
    procd_set_param stdout 0
    procd_set_param stderr 0
    procd_close_instance
}
INIT
    chmod +x /etc/init.d/$SERVICE
    /etc/init.d/$SERVICE enable
    /etc/init.d/$SERVICE start

    # Webseite auch über den WAN-Port erreichbar machen (Brume 2 als Netzwerkteilnehmer)
    if ! uci -q get firewall.system_monitor_mqtt >/dev/null; then
        uci set firewall.system_monitor_mqtt=rule
        uci set firewall.system_monitor_mqtt.name="Allow-$NAME"
        uci set firewall.system_monitor_mqtt.src=wan
        uci set firewall.system_monitor_mqtt.proto=tcp
        uci set firewall.system_monitor_mqtt.target=ACCEPT
    fi
    uci set firewall.system_monitor_mqtt.dest_port="$PORT"
    uci commit firewall
    /etc/init.d/firewall reload >/dev/null 2>&1
    say "Dienst (procd) eingerichtet und gestartet, Firewall-Regel für Port $PORT gesetzt"
else
    cat > /etc/systemd/system/$SERVICE.service <<UNIT
[Unit]
Description=$NAME - Systemwerte per MQTT senden
After=network-online.target
Wants=network-online.target

[Service]
ExecStart=$PY $TARGET/main.py --service
WorkingDirectory=$TARGET
Restart=always
RestartSec=5
User=root

[Install]
WantedBy=multi-user.target
UNIT
    systemctl daemon-reload
    systemctl enable $SERVICE >/dev/null 2>&1
    systemctl restart $SERVICE
    say "Dienst (systemd) eingerichtet und gestartet"
fi

IP=$(ip -4 addr show scope global 2>/dev/null | sed -n 's/.*inet \([0-9.]*\).*/\1/p' | head -n 1)
[ -n "$IP" ] || IP=$(hostname -I 2>/dev/null | awk '{print $1}')
rm -rf /tmp/$NAME-download
sleep 2
say "Fertig. Webseite: http://${IP:-<IP-Adresse>}:$PORT/"

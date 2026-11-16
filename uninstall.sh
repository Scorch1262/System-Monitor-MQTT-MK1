#!/bin/sh
# System-Monitor-MQTT-MK1 – Deinstallation unter Linux (Dienst, Firewall-Regel und Programmordner)
NAME=System-Monitor-MQTT-MK1
SERVICE=system-monitor-mqtt
[ "$(id -u)" = "0" ] || { echo "Bitte als root ausführen"; exit 1; }

if [ -f /etc/openwrt_release ]; then
    TARGET=/root/$NAME
    [ -x /etc/init.d/$SERVICE ] && { /etc/init.d/$SERVICE stop; /etc/init.d/$SERVICE disable; }
    rm -f /etc/init.d/$SERVICE
    if uci -q get firewall.system_monitor_mqtt >/dev/null; then
        uci delete firewall.system_monitor_mqtt
        uci commit firewall
        /etc/init.d/firewall reload >/dev/null 2>&1
    fi
else
    TARGET=/opt/$NAME
    systemctl disable --now $SERVICE >/dev/null 2>&1
    rm -f /etc/systemd/system/$SERVICE.service
    systemctl daemon-reload
fi
rm -rf "${TARGET:?}"
rm -f /tmp/$NAME.log*
echo "[$NAME] entfernt."

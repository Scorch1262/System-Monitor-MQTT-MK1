"""Schlanker MQTT-3.1.1-Client (nur Standardbibliothek) zum Senden von Werten.

Unterstützt: Benutzer/Passwort, TLS, Last Will, Keepalive (PINGREQ), PUBLISH mit QoS 0/1.
Damit läuft das Programm auch auf OpenWrt (GL.iNet Brume 2) ohne zusätzliche Pakete.
"""
import os
import select
import socket
import ssl
import struct
import threading
import time

CONNACK_TEXT = {
    1: "Protokollversion nicht unterstützt",
    2: "Client-ID abgelehnt",
    3: "Broker nicht verfügbar",
    4: "Benutzername oder Passwort falsch",
    5: "nicht berechtigt",
}


class MqttError(Exception):
    pass


def _str(s):
    b = s.encode("utf-8") if isinstance(s, str) else bytes(s)
    return struct.pack("!H", len(b)) + b


def _remaining_length(n):
    out = bytearray()
    while True:
        byte = n % 128
        n //= 128
        if n:
            byte |= 0x80
        out.append(byte)
        if not n:
            return bytes(out)


def _packet(header, body):
    return bytes([header]) + _remaining_length(len(body)) + body


class MiniMqtt:
    def __init__(self, client_id, username="", password="", keepalive=60,
                 tls=False, tls_insecure=False, will=None):
        self.client_id = client_id
        self.username = username or ""
        self.password = password or ""
        self.keepalive = int(keepalive)
        self.tls = tls
        self.tls_insecure = tls_insecure
        self.will = will  # (topic, payload, qos, retain)
        self.sock = None
        self.connected = False
        self.on_disconnect = None  # callback(grund)
        self._wlock = threading.Lock()
        self._pid = 0
        self._last_tx = 0.0
        self._last_rx = 0.0
        self._ping_sent = 0.0
        self._thread = None
        self._closing = False

    # ------------------------------------------------------------ Verbindung
    def connect(self, host, port, timeout=8.0):
        raw = socket.create_connection((host, int(port)), timeout=timeout)
        try:
            if self.tls:
                ctx = ssl.create_default_context()
                if self.tls_insecure:
                    ctx.check_hostname = False
                    ctx.verify_mode = ssl.CERT_NONE
                raw = ctx.wrap_socket(raw, server_hostname=host)
            self.sock = raw
            self._send(self._connect_packet())
            hdr, body = self._read_packet(timeout)
            if hdr >> 4 != 2 or len(body) < 2:
                raise MqttError("Unerwartete Antwort vom Broker")
            rc = body[1]
            if rc != 0:
                raise MqttError("Broker lehnt ab: %s" % CONNACK_TEXT.get(rc, "Code %d" % rc))
        except Exception:
            try:
                raw.close()
            except OSError:
                pass
            self.sock = None
            raise
        self.sock.settimeout(None)
        self.connected = True
        self._closing = False
        self._last_rx = self._last_tx = time.time()
        self._ping_sent = 0.0
        self._thread = threading.Thread(target=self._loop, name="mqtt-rx", daemon=True)
        self._thread.start()

    def _connect_packet(self):
        flags = 0x02  # clean session
        payload = _str(self.client_id)
        if self.will:
            topic, msg, qos, retain = self.will
            flags |= 0x04 | ((qos & 3) << 3) | (0x20 if retain else 0)
            payload += _str(topic) + _str(msg)
        if self.username:
            flags |= 0x80
            payload += _str(self.username)
            if self.password:
                flags |= 0x40
                payload += _str(self.password)
        var = _str("MQTT") + bytes([4, flags]) + struct.pack("!H", self.keepalive)
        return _packet(0x10, var + payload)

    def disconnect(self):
        self._closing = True
        if self.sock and self.connected:
            try:
                self._send(b"\xe0\x00")
            except OSError:
                pass
        self._drop(None)

    # ------------------------------------------------------------ Senden
    def publish(self, topic, payload, qos=0, retain=False):
        if not self.connected:
            return False
        if isinstance(payload, str):
            payload = payload.encode("utf-8")
        qos = 1 if qos else 0  # QoS 2 wird als QoS 1 gesendet
        body = _str(topic)
        if qos:
            self._pid = self._pid % 65535 + 1
            body += struct.pack("!H", self._pid)
        body += payload
        try:
            self._send(_packet(0x30 | (qos << 1) | (1 if retain else 0), body))
            return True
        except OSError as e:
            self._drop("Sendefehler: %s" % e)
            return False

    def _send(self, data):
        with self._wlock:
            self.sock.sendall(data)
            self._last_tx = time.time()

    # ------------------------------------------------------------ Empfangen
    def _read_exact(self, n, timeout=None):
        buf = b""
        while len(buf) < n:
            if timeout is not None:
                self.sock.settimeout(timeout)
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise MqttError("Verbindung vom Broker geschlossen")
            buf += chunk
        return buf

    def _read_packet(self, timeout=None):
        hdr = self._read_exact(1, timeout)[0]
        mult, length = 1, 0
        for _ in range(4):
            b = self._read_exact(1, timeout)[0]
            length += (b & 0x7F) * mult
            if not b & 0x80:
                break
            mult *= 128
        body = self._read_exact(length, timeout) if length else b""
        return hdr, body

    def _loop(self):
        while self.connected:
            try:
                r, _, _ = select.select([self.sock], [], [], 1.0)
                now = time.time()
                if r or (hasattr(self.sock, "pending") and self.sock.pending()):
                    hdr, _body = self._read_packet()
                    self._last_rx = now
                    if hdr >> 4 == 13:  # PINGRESP
                        self._ping_sent = 0.0
                    continue
                if self.keepalive:
                    if self._ping_sent and now - self._ping_sent > max(10, self.keepalive / 2):
                        raise MqttError("Keine Antwort vom Broker (Keepalive)")
                    if not self._ping_sent and now - max(self._last_tx, self._last_rx) > self.keepalive * 0.5:
                        self._send(b"\xc0\x00")
                        self._ping_sent = now
            except (OSError, ValueError, MqttError) as e:
                if self.connected:
                    self._drop(None if self._closing else str(e) or "Verbindung verloren")
                return

    def _drop(self, reason):
        was = self.connected
        self.connected = False
        sock, self.sock = self.sock, None
        if sock:
            try:
                sock.close()
            except OSError:
                pass
        if was and reason and self.on_disconnect:
            try:
                self.on_disconnect(reason)
            except Exception:
                pass


def random_suffix():
    return os.urandom(3).hex()

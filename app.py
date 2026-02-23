#!/usr/bin/env python3
"""
GNSS Dashboard – Flask Backend
================================
Reads NMEA data from the source defined in [frontend.nmea_source] in the TOML config.
Supported sources: tcpcli | tcpsvr | serial | udp | ntrip | file

Provides:
  - WebSocket (Socket.IO):  Live GNSS data (1 Hz)
  - REST /api/gnss:         Current GNSS state
  - REST /api/config:       TOML read / write + syntax check
  - REST /api/service/*:    str2str-manager systemd control
  - REST /api/reader/*:     NMEA reader restart / status
  - REST /api/processes:    Status of all managed processes
  - REST /api/logs:         List raw log / RINEX files
  - REST /api/logs/download/<file>:         Download a log file
  - REST /api/logs/convert/<file>:          Convert RTCM3 → RINEX via convbin (async)
  - REST /api/logs/convert/status/<job>:    Poll conversion job status
"""

import io
import os
import socket
import subprocess
import threading
import time
from abc import ABC, abstractmethod
from datetime import datetime
from pathlib import Path

import serial
from flask import Flask, jsonify, render_template, render_template_string, make_response, request, send_file
from jinja2.exceptions import TemplateNotFound
from flask_socketio import SocketIO

# ──────────────────────────────────────────────────────────────────────────────
# TOML
# ──────────────────────────────────────────────────────────────────────────────
try:
    import tomllib
except ImportError:
    try:
        import tomli as tomllib
    except ImportError:
        tomllib = None

# ──────────────────────────────────────────────────────────────────────────────
# Flask
# ──────────────────────────────────────────────────────────────────────────────
CONFIG_PATH  = os.environ.get("GNSS_CONFIG", "/etc/gnss/str2str.toml")
SERVICE_NAME = "gnss-stack"          # systemd unit name

# Embedded TOML template – used when the config file does not yet exist.
# Allows the user to start editing immediately in the dashboard.
_DEFAULT_TOML = """# GNSS Stack Configuration
# Please adjust all settings to match your hardware.

[general]
log_level     = "INFO"
log_file      = "/var/log/gnss/manager.log"
restart_delay = 5
max_restarts  = 0

[str2str.input_bridge]
enabled   = true
device    = "/dev/ttyUSB0"
baudrate  = 115200
port      = 4001
bind_addr = "127.0.0.1"

[str2str]
binary = "/usr/local/bin/str2str"

[str2str.input]
type = "tcpcli"
host = "127.0.0.1"
port = 4001

# rtcm_messages applies to every output type (tcpsvr, udpsvr, file, ntrip, ntripsvr).
# "" = forward everything; or comma-separated RTCM3 IDs to filter.
[[str2str.outputs]]
name          = "rtcm3_tcp"
enabled       = true
type          = "tcpsvr"
port          = 9001
bind_addr     = "0.0.0.0"
rtcm_messages = "1005,1074,1077,1084,1087,1094,1097,1124,1127,1230"
reconnect     = true

[[str2str.outputs]]
name          = "file_logger"
enabled       = false
type          = "file"
path          = "/var/log/gnss/rtcm3_%Y%m%d_%H%M%S.rtcm3"
swap_interval = 86400
rtcm_messages = ""
reconnect     = false

[[str2str.outputs]]
name          = "ntrip_caster"
enabled       = false
type          = "ntrip"
host          = "ntrip.example.com"
port          = 2101
mountpoint    = "BASE01"
user          = "user"
password      = "secret"
rtcm_messages = "1004,1005,1006,1008,1012,1033,1230"
reconnect     = true

[rinex]
binary     = "/usr/local/bin/convbin"
format     = "rtcm3"
version    = "3.03"
output_dir = "/var/log/gnss/rinex"

[rinex.marker]
name   = ""
number = ""
type   = "GEODETIC"

[rinex.observer]
name   = ""
agency = ""

[rinex.receiver]
serial  = ""
type    = ""
version = ""

[rinex.antenna]
serial  = ""
type    = ""
delta_h = 0.0
delta_e = 0.0
delta_n = 0.0

[rinex.position]
x = 0.0
y = 0.0
z = 0.0

[rinex.header]
comment = ""

[frontend.nmea_source]
type = "tcpcli"
host = "127.0.0.1"
port = 4001

[frontend.server]
host       = "0.0.0.0"
port       = 5000
secret_key = "change-me-in-production"
"""

# Flask looks for templates in <app_dir>/templates/ by default.
# We set the path explicitly so it works even when app.py is started
# from a different working directory.
_APP_DIR       = Path(__file__).parent
_TEMPLATE_FILE = _APP_DIR / "templates" / "index.html"

app = Flask(__name__, template_folder=str(_APP_DIR / "templates"))
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")


def _load_toml(path: str) -> dict:
    if tomllib is None:
        return {}
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except Exception:
        return {}


def _apply_flask_config():
    cfg = _load_toml(CONFIG_PATH)
    srv = cfg.get("frontend", {}).get("server", {})
    app.config["SECRET_KEY"] = srv.get("secret_key",
                                       os.environ.get("FLASK_SECRET", "gnss-secret"))


_apply_flask_config()


# ──────────────────────────────────────────────────────────────────────────────
# GNSS State
# ──────────────────────────────────────────────────────────────────────────────
class GNSSState:
    def __init__(self):
        self.lock        = threading.Lock()
        self.lat = self.lon = self.alt = None
        self.fix         = 0
        self.hdop = self.pdop = None
        self.speed_knots = self.track = None
        self.utc         = None
        self.satellites  = {}
        self.used_prns   = set()
        self.faulty_prns = set()   # PRNs flagged by GBS (RAIM) or with SNR=0
        self.gsv_seen    = {}      # {system:freq → set of sat keys} for stale removal
        self.last_update = None
        self.source_type = "none"
        self.source_info = ""
        self.source_ok   = False

    def reset(self):
        """Clears all position/satellite state – called on connection loss so the
        frontend does not display stale data."""
        with self.lock:
            self.lat = self.lon = self.alt = None
            self.fix         = 0
            self.hdop = self.pdop = None
            self.satellites  = {}
            self.used_prns   = set()
            self.faulty_prns = set()
            self.gsv_seen    = {}
            self.speed_knots = None
            self.track       = None
            self.source_ok   = False

    def to_dict(self) -> dict:
        with self.lock:
            return dict(
                lat=self.lat, lon=self.lon, alt=self.alt, fix=self.fix,
                hdop=self.hdop, pdop=self.pdop,
                speed_knots=self.speed_knots, track=self.track, utc=self.utc,
                satellites=dict(self.satellites),
                used_prns=list(self.used_prns),
                faulty_prns=list(self.faulty_prns),
                last_update=self.last_update,
                source_type=self.source_type,
                source_info=self.source_info,
                source_ok=self.source_ok,
            )


gnss = GNSSState()

# ──────────────────────────────────────────────────────────────────────────────
# NMEA Parser
# ──────────────────────────────────────────────────────────────────────────────
# NMEA 4.11 System-ID field at end of GSA sentences → constellation name.
# Required when talker is "GN" (multi-constellation).
GSA_SYSTEM_ID = {
    1:"GPS", 2:"GLONASS", 3:"Galileo", 4:"BeiDou", 5:"QZSS", 6:"NavIC",
}
CONSTELLATION_MAP = {
    "GP":"GPS","GN":"Multi","GL":"GLONASS",
    "GA":"Galileo","GB":"BeiDou","GQ":"QZSS","BD":"BeiDou",
}
SYSTEM_COLORS = {
    "GPS":"#00d4ff","GLONASS":"#ff6b35","Galileo":"#7cff6b",
    "BeiDou":"#ffd700","QZSS":"#ff69b4","Multi":"#aaaaaa",
}

# Signal-ID → signal name per constellation.
# Source: Quectel LG29xP/LGx80P Protocol Specification Table 27
# (verified against Unicore UM980 – both receivers use the same numbering).
# BDS B2I uses Signal-ID 0xB (hex), so int conversion with base=0 is needed.
FREQ_MAP = {
    "GPS":     {1:"L1C/A", 4:"L1C",  6:"L2C",  8:"L5I", 9:"L5Q"},
    "GLONASS": {1:"G1",    3:"G2"},
    "Galileo": {1:"E5a",   2:"E5b",  5:"E6",   7:"E1"},
    "BeiDou":  {1:"B1I",   3:"B1C",  5:"B2a",  6:"B2b",  8:"B3I", 11:"B2I"},
    "QZSS":    {1:"L1C/A", 6:"L2C",  8:"L5"},
    "NavIC":   {1:"L5"},
}


def _signal_name(system, signal_id):
    """Returns signal name for a constellation + signal ID. Unknown IDs → 'sig<n>'."""
    return FREQ_MAP.get(system, {}).get(signal_id, "sig{}".format(signal_id))


def _chk(s: str) -> bool:
    if "$" not in s or "*" not in s:
        return False
    try:
        body = s[s.index("$")+1:s.index("*")]
        exp  = int(s[s.index("*")+1:].strip()[:2], 16)
        c = 0
        for ch in body:
            c ^= ord(ch)
        return c == exp
    except Exception:
        return False


def _ll(v: str, h: str):
    if not v:
        return None
    try:
        deg = int(float(v)/100)
        dd  = deg + (float(v) - deg*100)/60.0
        return round(-dd if h in ("S","W") else dd, 8)
    except Exception:
        return None


def process_nmea(sentence: str):
    """Parses one NMEA sentence and updates the global GNSSState."""
    s = sentence.strip()
    # Process ASCII NMEA sentences only; ignore RTCM3 binary data
    if not s.startswith("$") or not _chk(s):
        return
    parts    = s.split(",")
    tag      = parts[0][1:]
    system   = CONSTELLATION_MAP.get(tag[:2], tag[:2])
    msg_type = tag[2:]
    now      = time.time()

    with gnss.lock:
        if msg_type == "GGA" and len(parts) >= 10:
            # New epoch: clear per-epoch sets
            gnss.used_prns   = set()
            gnss.faulty_prns = set()
            gnss.lat = _ll(parts[2], parts[3])
            gnss.lon = _ll(parts[4], parts[5])
            try:
                gnss.fix  = int(parts[6]) if parts[6] else 0
                gnss.hdop = float(parts[8]) if parts[8] else None
                gnss.alt  = float(parts[9]) if parts[9] else None
            except (ValueError, IndexError):
                pass
            gnss.utc = parts[1]
            gnss.last_update = datetime.utcnow().isoformat()

        elif msg_type == "RMC" and len(parts) >= 9 and parts[2] == "A":
            gnss.lat = _ll(parts[3], parts[4])
            gnss.lon = _ll(parts[5], parts[6])
            try:
                gnss.speed_knots = float(parts[7]) if parts[7] else None
                gnss.track       = float(parts[8]) if parts[8] else None
            except (ValueError, IndexError):
                pass

        elif msg_type == "GSA" and len(parts) >= 18:
            # Store PRNs with system prefix so GPS:1 ≠ BeiDou:1.
            # Multiple GSA sentences per epoch (one per constellation) →
            # accumulate rather than overwrite. Reset happens on GGA.
            #
            # NMEA 4.11: optional System-ID field after VDOP (parts[18]).
            # Mandatory when talker is "GN" (multi-constellation) to
            # identify which constellation's PRNs are listed.
            gsa_system = system
            try:
                sys_field = parts[18].split("*")[0].strip()
                if sys_field:
                    gsa_system = GSA_SYSTEM_ID.get(int(sys_field), system)
            except (IndexError, ValueError):
                pass
            for p in parts[3:15]:
                try:
                    if p.strip():
                        gnss.used_prns.add("{}:{}".format(gsa_system, int(p.strip())))
                except ValueError:
                    pass
            try:
                gnss.pdop = float(parts[15]) if parts[15] else None
            except (ValueError, IndexError):
                pass

        elif msg_type == "GBS" and len(parts) >= 7:
            # GNSS Satellite Fault Detection (RAIM) – field 5 = suspect PRN.
            try:
                prn_s = parts[5].strip()
                if prn_s:
                    gnss.faulty_prns.add("{}:{}".format(system, int(prn_s)))
            except (ValueError, IndexError):
                pass

        elif msg_type == "GSV" and len(parts) >= 4:
            # GSV structure (NMEA 4.10/4.11):
            #   $xxGSV, total_msgs, msg_nr, total_sats,
            #   [prn, el, az, snr] × 1..4,  [signal_id]*checksum  ← 4.11 only
            #
            # Stale removal: on the last message of a sequence (msg_nr ==
            # total_msgs) delete all entries for this system:freq that were
            # NOT reported in this sequence.
            try:
                total_msgs = int(parts[1])
                msg_nr     = int(parts[2])
            except (ValueError, IndexError):
                total_msgs = msg_nr = 1

            clean_parts = parts[:-1] + [parts[-1].split("*")[0]]
            n_extra = (len(clean_parts) - 4) % 4
            freq = "L1C/A"
            if n_extra == 1:
                # NMEA 4.11: Signal-ID field at the end.
                # BDS B2I has signal ID 0xB (single hex digit without 0x prefix).
                try:
                    raw = clean_parts[-1].strip()
                    sig_id = int(raw, 16) if (len(raw) == 1 and raw.upper() in "ABCDEF") \
                             else int(raw, 0)
                    freq = _signal_name(system, sig_id)
                except (ValueError, IndexError):
                    pass
            # n_extra == 0: NMEA 4.10 → no signal-ID field, freq stays "L1C/A"

            seq_key = "{}:{}".format(system, freq)
            if msg_nr == 1:
                gnss.gsv_seen[seq_key] = set()

            idx = 4
            while idx + 3 <= len(clean_parts):
                try:
                    prn_s = clean_parts[idx].strip()
                    if prn_s:
                        prn   = int(prn_s)
                        snr_s = clean_parts[idx+3].strip()
                        snr   = int(snr_s) if snr_s else 0
                        key   = "{}:{}:{}".format(system, prn, freq)
                        gnss.satellites[key] = dict(
                            prn    = prn,
                            el     = int(clean_parts[idx+1]) if clean_parts[idx+1].strip() else 0,
                            az     = int(clean_parts[idx+2]) if clean_parts[idx+2].strip() else 0,
                            snr    = snr,
                            system = system,
                            color  = SYSTEM_COLORS.get(system, "#fff"),
                            freq   = freq,
                            ts     = now,
                            healthy= snr > 0,
                        )
                        gnss.gsv_seen.setdefault(seq_key, set()).add(key)
                except (ValueError, IndexError):
                    pass
                idx += 4

            if msg_nr == total_msgs:
                # Last message in sequence: remove entries for this system+freq
                # that were not reported this round.
                seq_sys, seq_freq = seq_key.split(":", 1)
                seen  = gnss.gsv_seen.get(seq_key, set())
                stale = [k for k, v in gnss.satellites.items()
                         if v.get("system") == seq_sys
                         and v.get("freq")   == seq_freq
                         and k not in seen]
                for k in stale:
                    del gnss.satellites[k]
                gnss.gsv_seen.pop(seq_key, None)


# ──────────────────────────────────────────────────────────────────────────────
# NMEA Reader (source configured via TOML [frontend.nmea_source])
# ──────────────────────────────────────────────────────────────────────────────
class NMEAReader(ABC):
    RECONNECT_DELAY = 5

    def __init__(self):
        self._running = False
        self._thread  = None

    def start(self):
        self._running = True
        self._thread  = threading.Thread(
            target=self._loop, daemon=True, name=f"nmea-{self.source_type}")
        self._thread.start()

    def stop(self):
        self._running = False

    @property
    @abstractmethod
    def source_type(self) -> str: ...

    @property
    @abstractmethod
    def source_info(self) -> str: ...

    def _set_ok(self, ok: bool, info: str = ""):
        with gnss.lock:
            gnss.source_type = self.source_type
            gnss.source_info = info or self.source_info
            gnss.source_ok   = ok

    def _loop(self):
        while self._running:
            self._set_ok(False)
            try:
                self._connect_and_read()
            except Exception as e:
                app.logger.warning("[%s] %s – retrying in %ds",
                                   self.source_type, e, self.RECONNECT_DELAY)
            # Connection lost or errored – clear state so the frontend does
            # not continue showing stale satellite / position data.
            gnss.reset()
            if self._running:
                time.sleep(self.RECONNECT_DELAY)

    @abstractmethod
    def _connect_and_read(self): ...

    def _iter_lines(self, fh):
        """Reads a byte/str stream and calls process_nmea() for each line.
        RTCM3 binary data is automatically ignored."""
        buf = ""
        while self._running:
            chunk = fh.read(256)
            if not chunk:
                raise ConnectionResetError("stream ended")
            if isinstance(chunk, (bytes, bytearray)):
                chunk = chunk.decode("ascii", errors="ignore")
            buf += chunk
            while "\n" in buf:
                line, buf = buf.split("\n", 1)
                process_nmea(line)


class SerialReader(NMEAReader):
    def __init__(self, device="/dev/ttyUSB0", baud=115200):
        super().__init__(); self.device=device; self.baud=baud
    source_type = property(lambda self: "serial")
    source_info = property(lambda self: f"{self.device} @ {self.baud}")
    def _connect_and_read(self):
        with serial.Serial(self.device, self.baud, timeout=1) as ser:
            self._set_ok(True)
            while self._running:
                line = ser.readline().decode("ascii", errors="ignore")
                if line: process_nmea(line)


class TCPClientReader(NMEAReader):
    def __init__(self, host="127.0.0.1", port=4001):
        super().__init__(); self.host=host; self.port=port
    source_type = property(lambda self: "tcpcli")
    source_info = property(lambda self: f"{self.host}:{self.port}")
    def _connect_and_read(self):
        with socket.create_connection((self.host, self.port), timeout=10) as sock:
            self._set_ok(True)
            self._iter_lines(sock.makefile("rb"))


class TCPServerReader(NMEAReader):
    def __init__(self, port=4002, bind_host="0.0.0.0"):
        super().__init__(); self.port=port; self.bind_host=bind_host
    source_type = property(lambda self: "tcpsvr")
    source_info = property(lambda self: f"listen :{self.port}")
    def _connect_and_read(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as srv:
            srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            srv.bind((self.bind_host, self.port)); srv.listen(1); srv.settimeout(2)
            self._set_ok(False, f"waiting :{self.port}")
            while self._running:
                try:
                    conn, addr = srv.accept()
                except socket.timeout:
                    continue
                with conn:
                    self._set_ok(True, f"client {addr[0]}:{addr[1]}")
                    self._iter_lines(conn.makefile("rb"))


class UDPReader(NMEAReader):
    def __init__(self, port=4003, bind_host="0.0.0.0"):
        super().__init__(); self.port=port; self.bind_host=bind_host
    source_type = property(lambda self: "udp")
    source_info = property(lambda self: f"udp :{self.port}")
    def _connect_and_read(self):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.bind((self.bind_host, self.port)); sock.settimeout(2)
            self._set_ok(True)
            buf = ""
            while self._running:
                try:
                    data, _ = sock.recvfrom(4096)
                    buf += data.decode("ascii", errors="ignore")
                    while "\n" in buf:
                        line, buf = buf.split("\n", 1)
                        process_nmea(line)
                except socket.timeout:
                    continue


class NTRIPClientReader(NMEAReader):
    def __init__(self, host="", port=2101, mountpoint="", user="", password=""):
        super().__init__()
        self.host=host; self.port=port; self.mountpoint=mountpoint
        self.user=user; self.password=password
    source_type = property(lambda self: "ntrip")
    source_info = property(lambda self: f"{self.host}:{self.port}/{self.mountpoint}")
    def _connect_and_read(self):
        import base64
        auth = ""
        if self.user:
            cred = base64.b64encode(f"{self.user}:{self.password}".encode()).decode()
            auth = f"Authorization: Basic {cred}\r\n"
        req = (f"GET /{self.mountpoint} HTTP/1.0\r\nUser-Agent: GNSSDashboard/1.0\r\n"
               f"Accept: */*\r\nConnection: close\r\n{auth}\r\n")
        with socket.create_connection((self.host, self.port), timeout=10) as sock:
            sock.sendall(req.encode())
            hdr = b""
            while b"\r\n\r\n" not in hdr:
                chunk = sock.recv(256)
                if not chunk: raise ConnectionResetError("NTRIP header incomplete")
                hdr += chunk
            if b"200 OK" not in hdr and b"ICY 200 OK" not in hdr:
                raise ConnectionRefusedError(f"NTRIP: {hdr[:80]}")
            self._set_ok(True)
            buf = hdr.split(b"\r\n\r\n",1)[1].decode("ascii", errors="ignore")
            while self._running:
                chunk = sock.recv(1024)
                if not chunk: raise ConnectionResetError("NTRIP closed")
                buf += chunk.decode("ascii", errors="ignore")
                while "\n" in buf:
                    line, buf = buf.split("\n", 1)
                    process_nmea(line)


class FileReader(NMEAReader):
    def __init__(self, path="/dev/stdin", loop=False):
        super().__init__(); self.path=path; self.loop=loop
    source_type = property(lambda self: "file")
    source_info = property(lambda self: self.path)
    def _connect_and_read(self):
        while self._running:
            with open(self.path, "r", errors="ignore") as f:
                self._set_ok(True)
                for line in f:
                    if not self._running: return
                    process_nmea(line); time.sleep(0.01)
            if not self.loop: break
            time.sleep(1)


# ──────────────────────────────────────────────────────────────────────────────
# Reader factory – reads [frontend.nmea_source] from TOML
# ──────────────────────────────────────────────────────────────────────────────
def reader_from_config(config_path: str) -> NMEAReader:
    cfg = _load_toml(config_path)
    # [frontend.nmea_source] takes precedence
    src = cfg.get("frontend", {}).get("nmea_source", {})

    # Fallback: environment variables
    if not src:
        return TCPClientReader(
            host=os.environ.get("NMEA_HOST", "127.0.0.1"),
            port=int(os.environ.get("NMEA_PORT", "4001")),
        )

    t = src.get("type", "tcpcli").lower()
    if t == "tcpcli":
        return TCPClientReader(host=src.get("host","127.0.0.1"), port=src.get("port",4001))
    if t == "tcpsvr":
        return TCPServerReader(port=src.get("port",4002))
    if t == "serial":
        return SerialReader(
            device=src.get("device","/dev/ttyUSB0"),
            baud=src.get("baudrate",115200))
    if t == "udp":
        return UDPReader(port=src.get("port",4003))
    if t == "ntrip":
        return NTRIPClientReader(
            host=src.get("host",""), port=src.get("port",2101),
            mountpoint=src.get("mountpoint",""),
            user=src.get("user",""), password=src.get("password",""))
    if t == "file":
        return FileReader(path=src.get("path","/dev/stdin"), loop=src.get("loop",False))

    app.logger.warning("Unknown NMEA source type '%s' – falling back to tcpcli", t)
    return TCPClientReader()


# ──────────────────────────────────────────────────────────────────────────────
# Reader-Manager
# ──────────────────────────────────────────────────────────────────────────────
class ReaderManager:
    def __init__(self):
        self._reader = None
        self._lock   = threading.Lock()

    def start(self):
        with self._lock:
            self._reader = reader_from_config(CONFIG_PATH)
            self._reader.start()
            app.logger.info("NMEA source: %s  %s",
                            self._reader.source_type, self._reader.source_info)

    def restart(self):
        with self._lock:
            if self._reader: self._reader.stop(); time.sleep(0.5)
            self._reader = reader_from_config(CONFIG_PATH)
            self._reader.start()
            app.logger.info("NMEA source restarted: %s  %s",
                            self._reader.source_type, self._reader.source_info)

    def info(self) -> dict:
        if self._reader:
            return {"type": self._reader.source_type, "info": self._reader.source_info}
        return {"type": "none", "info": "–"}


reader_manager = ReaderManager()


# ──────────────────────────────────────────────────────────────────────────────
# WebSocket Broadcast
# ──────────────────────────────────────────────────────────────────────────────
def _broadcast():
    while True:
        time.sleep(1)
        socketio.emit("gnss_update", gnss.to_dict())

threading.Thread(target=_broadcast, daemon=True, name="gnss-broadcast").start()


# ──────────────────────────────────────────────────────────────────────────────
# systemd / process status
# ──────────────────────────────────────────────────────────────────────────────
def _systemctl(action: str, service: str = SERVICE_NAME) -> dict:
    # systemctl must be called via sudo – permission is granted in
    # /etc/sudoers.d/gnss-systemctl.
    cmd = ["sudo", "systemctl", action, service]
    app.logger.info("systemctl: %s", " ".join(cmd))
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        if r.returncode == 0:
            app.logger.info("systemctl %s %s: OK", action, service)
        else:
            app.logger.error(
                "systemctl %s %s failed (rc=%d):\n"
                "  stdout: %s\n  stderr: %s",
                action, service, r.returncode,
                r.stdout.strip() or "(empty)",
                r.stderr.strip() or "(empty)")
        return {"ok": r.returncode == 0, "out": r.stdout, "err": r.stderr}
    except FileNotFoundError:
        msg = "sudo or systemctl not found"
        app.logger.error("systemctl %s %s: %s", action, service, msg)
        return {"ok": False, "out": "", "err": msg}
    except subprocess.TimeoutExpired:
        msg = f"Timeout after 15s"
        app.logger.error("systemctl %s %s: %s", action, service, msg)
        return {"ok": False, "out": "", "err": msg}
    except Exception as e:
        app.logger.error("systemctl %s %s: %s", action, service, str(e))
        return {"ok": False, "out": "", "err": str(e)}


def _svc_status() -> dict:
    try:
        r  = subprocess.run(["systemctl","is-active",SERVICE_NAME],
                            capture_output=True, text=True, timeout=5)
        r2 = subprocess.run(
            ["systemctl","show",SERVICE_NAME,
             "--property=ActiveState,SubState,MainPID,ActiveEnterTimestamp"],
            capture_output=True, text=True, timeout=5)
        props = {}
        for line in r2.stdout.splitlines():
            if "=" in line:
                k, v = line.split("=",1); props[k]=v
        return {"active": r.stdout.strip(), "props": props}
    except Exception as e:
        return {"active":"unknown","props":{},"error":str(e)}


# ──────────────────────────────────────────────────────────────────────────────
# REST API
# ──────────────────────────────────────────────────────────────────────────────
@app.route("/api/config", methods=["GET"])
def api_config_get():
    p = Path(CONFIG_PATH)
    if p.exists():
        try:
            content = p.read_text()
            return jsonify({"ok": True, "content": content,
                            "path": CONFIG_PATH, "is_default": False})
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 500
    else:
        # File does not yet exist – return the default template so the user
        # can start editing immediately in the dashboard.
        app.logger.warning("Config not found: %s – serving template.",
                           CONFIG_PATH)
        return jsonify({"ok": True, "content": _DEFAULT_TOML,
                        "path": CONFIG_PATH, "is_default": True})


@app.route("/api/config", methods=["POST"])
def api_config_post():
    data    = request.get_json()
    content = data.get("content","")
    do_restart = data.get("restart_reader", False)

    if tomllib:
        try: tomllib.loads(content)
        except Exception as e:
            return jsonify({"ok":False,"error":f"TOML error: {e}"}), 400

    try:
        p = Path(CONFIG_PATH)
        p.parent.mkdir(parents=True, exist_ok=True)
        backup = None
        if p.exists():
            backup = p.with_suffix(f".toml.bak.{int(time.time())}")
            p.rename(backup)
        p.write_text(content)
        app.logger.info("Config saved: %s", CONFIG_PATH)
    except PermissionError as e:
        app.logger.error("No write permission on %s: %s", CONFIG_PATH, e)
        return jsonify({"ok": False,
                        "error": f"No write permission on {CONFIG_PATH}: {e}\n"
                                  "Check ReadWritePaths in gnss-dashboard.service."}), 500
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

    if do_restart:
        threading.Thread(target=reader_manager.restart, daemon=True).start()

    return jsonify({"ok": True, "backup": str(backup) if backup else None})


@app.route("/api/service/<action>", methods=["POST"])
def api_service(action):
    if action not in ("start", "stop", "restart"):
        return jsonify({"ok": False, "error": "invalid action"}), 400
    data    = request.get_json(silent=True) or {}
    service = data.get("service", SERVICE_NAME)
    if service not in (SERVICE_NAME, "gnss-dashboard"):
        return jsonify({"ok": False, "error": f"unknown service: {service}"}), 400
    app.logger.info("API: service %s → %s (von %s)", service, action, request.remote_addr)
    return jsonify(_systemctl(action, service))


@app.route("/api/service/status", methods=["GET"])
def api_service_status():
    return jsonify(_svc_status())


@app.route("/api/gnss", methods=["GET"])
def api_gnss():
    return jsonify(gnss.to_dict())


@app.route("/api/reader/restart", methods=["POST"])
def api_reader_restart():
    threading.Thread(target=reader_manager.restart, daemon=True).start()
    return jsonify({"ok":True, **reader_manager.info()})


@app.route("/api/reader/status", methods=["GET"])
def api_reader_status():
    return jsonify({**reader_manager.info(), "source_ok": gnss.source_ok})


@app.route("/api/processes", methods=["GET"])
def api_processes():
    """
    Returns the status of processes managed by str2str_manager.
    Reads from an optional status file (if the manager writes one),
    or queries systemd for process information.
    """
    try:
        units = ["gnss-stack", "gnss-dashboard"]
        result = []
        for unit in units:
            r = subprocess.run(
                ["systemctl","show",unit,
                 "--property=ActiveState,SubState,MainPID,Description"],
                capture_output=True, text=True, timeout=3)
            if r.returncode == 0:
                props = {}
                for line in r.stdout.splitlines():
                    if "=" in line:
                        k,v = line.split("=",1); props[k]=v
                if props.get("ActiveState","") != "":
                    result.append({"unit":unit,**props})
        return jsonify({"ok":True,"processes":result})
    except Exception as e:
        return jsonify({"ok":False,"error":str(e)})


# ──────────────────────────────────────────────────────────────────────────────
# Logs & RINEX conversion
# ──────────────────────────────────────────────────────────────────────────────
_ALLOWED_EXT = {".rtcm3", ".ubx", ".obs", ".nav", ".rnx", ".21o", ".22o", ".23o",
                ".21n", ".22n", ".23n"}

_conv_jobs = {}   # job_key → {done, ok, returncode, stdout, stderr, src, out_dir}


def _log_dir():
    """Returns the directory where raw log files are written.
    Reads the path from the file_logger output in [[str2str.outputs]],
    falling back to /var/log/gnss."""
    try:
        cfg = _load_toml(CONFIG_PATH)
        for out in cfg.get("str2str", {}).get("outputs", []):
            if out.get("name") == "file_logger" and out.get("path"):
                return Path(out["path"]).parent
    except Exception:
        pass
    return Path("/var/log/gnss")


def _rinex_dir():
    """Returns the RINEX output directory from [rinex].output_dir."""
    try:
        cfg = _load_toml(CONFIG_PATH)
        d = cfg.get("rinex", {}).get("output_dir")
        if d:
            return Path(d)
    except Exception:
        pass
    return Path("/var/log/gnss/rinex")


def _safe_log_path(filename):
    """Returns the absolute path if the file lives in the log or RINEX dir
    and has an allowed extension; None otherwise."""
    try:
        for d in (_log_dir(), _rinex_dir()):
            p = (d / filename).resolve()
            if p.parent == d.resolve() and p.suffix in _ALLOWED_EXT:
                return p
    except Exception:
        pass
    return None


@app.route("/api/logs", methods=["GET"])
def api_logs_list():
    """List raw log and RINEX files available for download."""
    files = []
    for d in (_log_dir(), _rinex_dir()):
        if not d.exists():
            continue
        for p in sorted(d.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
            if p.suffix in _ALLOWED_EXT and p.is_file():
                st = p.stat()
                files.append({
                    "name":  p.name,
                    "size":  st.st_size,
                    "mtime": st.st_mtime,
                    "dir":   str(d),
                })
    return jsonify({"ok": True, "files": files})


@app.route("/api/logs/download/<path:filename>", methods=["GET"])
def api_logs_download(filename):
    """Download a log file."""
    p = _safe_log_path(filename)
    if not p or not p.exists():
        return jsonify({"ok": False, "error": "file not found"}), 404
    return send_file(p, as_attachment=True, download_name=p.name)


@app.route("/api/logs/convert/<path:filename>", methods=["POST"])
def api_logs_convert(filename):
    """Convert a raw RTCM3 file to RINEX using convbin.

    Optional JSON body:
      rinex_ver  – override RINEX version (e.g. "2.11"); default from [rinex].version
      obs_types  – convbin -y output obs types (default: "ong")

    Returns a job key; poll GET /api/logs/convert/status/<key> for the result.
    # TEST: POST to this endpoint with a .rtcm3 file in the log dir, then poll
    #       /api/logs/convert/status/<key> and check stdout/stderr from convbin.
    """
    p = _safe_log_path(filename)
    if not p or not p.exists():
        return jsonify({"ok": False, "error": "source file not found"}), 404

    data      = request.get_json(silent=True) or {}
    job_key   = "{}_{}".format(filename, int(time.time()))
    _conv_jobs[job_key] = {"done": False}

    def run_conversion(src_path, key):
        cfg       = _load_toml(CONFIG_PATH)
        rcfg      = cfg.get("rinex", {})

        binary    = rcfg.get("binary",     "/usr/local/bin/convbin")
        fmt       = rcfg.get("format",     "rtcm3")
        ver       = data.get("rinex_ver") or rcfg.get("version", "3.03")
        out_dir   = Path(rcfg.get("output_dir", str(src_path.parent)))
        obs_types = data.get("obs_types", "ong")   # o=obs, n=GPS-nav, g=GLO-nav

        try:
            out_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            _conv_jobs[key] = {"done": True, "ok": False,
                               "returncode": -1, "stdout": "", "stderr": str(e),
                               "src": src_path.name, "out_dir": str(out_dir)}
            return

        cmd = [binary, "-r", fmt, "-v", ver, "-os", "-od",
               "-f", obs_types, "-d", str(out_dir)]

        # RINEX header fields from [rinex.*] config
        marker = rcfg.get("marker", {})
        if marker.get("name"):
            cmd += ["-hm", marker["name"]]
        if marker.get("number"):
            cmd += ["-hn", marker["number"]]
        if marker.get("type"):
            cmd += ["-ht", marker["type"]]

        obs = rcfg.get("observer", {})
        if obs.get("name") or obs.get("agency"):
            cmd += ["-ho", "{}/{}".format(obs.get("name", ""), obs.get("agency", ""))]

        rec = rcfg.get("receiver", {})
        if any(rec.get(k) for k in ("serial", "type", "version")):
            cmd += ["-hr", "{}/{}/{}".format(
                rec.get("serial", ""), rec.get("type", ""), rec.get("version", ""))]

        ant = rcfg.get("antenna", {})
        if ant.get("serial") or ant.get("type"):
            cmd += ["-ha", "{}/{}".format(ant.get("serial", ""), ant.get("type", ""))]
        dh = ant.get("delta_h", 0.0)
        de = ant.get("delta_e", 0.0)
        dn = ant.get("delta_n", 0.0)
        if dh or de or dn:
            cmd += ["-hd", "{}/{}/{}".format(dh, de, dn)]

        pos = rcfg.get("position", {})
        x, y, z = pos.get("x", 0.0), pos.get("y", 0.0), pos.get("z", 0.0)
        if x or y or z:
            cmd += ["-hp", "{}/{}/{}".format(x, y, z)]

        comment = rcfg.get("header", {}).get("comment", "")
        if comment:
            cmd += ["-hc", comment]

        cmd.append(str(src_path))

        app.logger.info("convbin: %s", " ".join(cmd))
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
            _conv_jobs[key] = {
                "done":       True,
                "ok":         result.returncode == 0,
                "returncode": result.returncode,
                "stdout":     result.stdout[-4000:],
                "stderr":     result.stderr[-4000:],
                "src":        src_path.name,
                "out_dir":    str(out_dir),
            }
            app.logger.info("convbin rc=%d for %s", result.returncode, src_path.name)
        except subprocess.TimeoutExpired:
            _conv_jobs[key] = {"done": True, "ok": False, "returncode": -1,
                               "stdout": "", "stderr": "timeout after 300s",
                               "src": src_path.name, "out_dir": str(out_dir)}
        except Exception as e:
            _conv_jobs[key] = {"done": True, "ok": False, "returncode": -1,
                               "stdout": "", "stderr": str(e),
                               "src": src_path.name, "out_dir": str(out_dir)}

    threading.Thread(target=run_conversion, args=(p, job_key), daemon=True).start()
    return jsonify({"ok": True, "job": job_key})


@app.route("/api/logs/convert/status/<path:job_key>", methods=["GET"])
def api_logs_convert_status(job_key):
    """Poll the status of a convbin conversion job."""
    job = _conv_jobs.get(job_key)
    if job is None:
        return jsonify({"ok": False, "error": "job not found"}), 404
    return jsonify({"ok": True, **job})


# ──────────────────────────────────────────────────────────────────────────────
# Fallback HTML – shown when templates/index.html is missing
# ──────────────────────────────────────────────────────────────────────────────
_FALLBACK_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>GNSS Dashboard – Setup</title>
  <style>
    *{box-sizing:border-box;margin:0;padding:0}
    body{background:#0a0c10;color:#c8d0db;font-family:'Courier New',monospace;
         display:flex;align-items:center;justify-content:center;min-height:100vh;padding:24px}
    .card{background:#0f1318;border:1px solid #1e2733;border-radius:6px;
          max-width:680px;width:100%;padding:32px}
    h1{color:#f0a500;font-size:18px;letter-spacing:.15em;text-transform:uppercase;
       margin-bottom:24px}
    .badge{display:inline-block;padding:3px 10px;border-radius:3px;
           font-size:12px;margin-bottom:20px}
    .warn{background:#ff475718;border:1px solid #ff475755;color:#ff4757}
    .ok  {background:#7eff6b18;border:1px solid #7eff6b55;color:#7eff6b}
    p{color:#586474;font-size:13px;line-height:1.7;margin-bottom:16px}
    code{color:#00e5ff;background:#161b22;padding:2px 6px;border-radius:2px;font-size:12px}
    .steps{border-top:1px solid #1e2733;margin-top:24px;padding-top:20px}
    .step{display:flex;gap:12px;margin-bottom:14px;align-items:flex-start}
    .num{background:#f0a50022;border:1px solid #f0a50055;color:#f0a500;
         border-radius:50%;width:24px;height:24px;display:flex;align-items:center;
         justify-content:center;font-size:11px;flex-shrink:0}
    a{color:#00e5ff;text-decoration:none}
    a:hover{text-decoration:underline}
    .api{margin-top:20px;background:#161b22;border:1px solid #1e2733;
         border-radius:4px;padding:14px}
    .api h2{font-size:11px;color:#586474;text-transform:uppercase;
            letter-spacing:.1em;margin-bottom:10px}
    .api a{display:block;color:#7eff6b;font-size:12px;margin-bottom:4px}
  </style>
</head>
<body>
<div class="card">
  <h1>&#x1F6F0; GNSS Dashboard</h1>
  <span class="badge warn">&#x26A0; Template not found</span>
  <p>
    The file <code>templates/index.html</code> is missing from
    <code>{{ config_path | replace('/str2str.toml','') | replace('/etc/gnss','') }}/opt/gnss/templates/</code>.
    The full dashboard cannot be loaded.
  </p>
  <div class="steps">
    <p style="color:#c8d0db;margin-bottom:14px">What to do:</p>
    <div class="step">
      <div class="num">1</div>
      <div>Copy the template file to <code>/opt/gnss/templates/index.html</code>
           and restart the service:<br>
           <code>systemctl restart gnss-dashboard</code></div>
    </div>
    <div class="step">
      <div class="num">2</div>
      <div>In the meantime the configuration is accessible via the REST API:</div>
    </div>
  </div>
  <div class="api">
    <h2>Available API endpoints</h2>
    <a href="/api/config">GET  /api/config         – read configuration</a>
    <a href="/api/gnss">GET  /api/gnss           – current GNSS state</a>
    <a href="/api/service/status">GET  /api/service/status – service status</a>
    <a href="/api/reader/status">GET  /api/reader/status  – NMEA reader status</a>
  </div>
  <p style="margin-top:20px;font-size:11px;color:#586474">
    Config file: <code>{{ config_path }}</code><br>
    Service: <code>{{ service_name }}</code>
  </p>
</div>
</body>
</html>"""


@app.route("/")
def index():
    cfg = _load_toml(CONFIG_PATH)
    ctx = dict(
        config_path  = CONFIG_PATH,
        service_name = SERVICE_NAME,
    )
    try:
        return render_template("index.html", **ctx)
    except TemplateNotFound:
        app.logger.warning(
            "templates/index.html not found in %s – serving fallback page.",
            _APP_DIR)
        return make_response(render_template_string(_FALLBACK_HTML, **ctx), 200)


# ──────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    cfg = _load_toml(CONFIG_PATH)
    srv = cfg.get("frontend",{}).get("server",{})
    host = srv.get("host", os.environ.get("FLASK_HOST","0.0.0.0"))
    port = srv.get("port", int(os.environ.get("FLASK_PORT","5000")))

    reader_manager.start()
    socketio.run(app, host=host, port=port,
                 debug=False, allow_unsafe_werkzeug=True)

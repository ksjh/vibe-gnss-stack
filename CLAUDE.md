# CLAUDE.md — vibe-gnss-stack

AI assistant guide for the **vibe-gnss-stack** codebase. Read this before making any changes.

---

## Project Overview

A Python-based GNSS (Global Navigation Satellite System) data processing stack with a real-time web dashboard. Built on top of RTKLIB's `str2str` utility and inspired by [rtkbase](https://github.com/Stefal/rtkbase).

**Status**: Experimental / "vibe coding" evaluation project. No automated tests or CI/CD.

**Core idea**: A GNSS receiver (serial) feeds raw NMEA+RTCM3 data. The stack bridges it to the network, filters and distributes it to multiple outputs, and visualises live position/satellite data in a browser.

---

## Architecture

Three independent layers:

```
Serial GNSS receiver (/dev/ttyUSB0)
        │
        ▼
┌─────────────────────────────────────────────────────┐
│ Layer 1: str2str input bridge                       │
│   Reads serial port, exposes as TCP server          │
│   TCP port: 4001 (default)                          │
└───────────────────┬─────────────────────────────────┘
                    │ TCP (NMEA + RTCM3)
        ┌───────────┴─────────────┐
        ▼                         ▼
┌───────────────┐       ┌─────────────────────────────┐
│ Layer 2:      │       │ Layer 3: Web Dashboard      │
│ str2str       │       │   Flask + Socket.IO (5000)  │
│ (per output)  │       │   Reads NMEA only           │
│               │       │   Config editor, controls,  │
│ → tcpsvr:9001 │       │   live map, satellite view  │
│ → udpsvr:9002 │       └─────────────────────────────┘
│ → file log    │
│ → NTRIP push  │
└───────────────┘
```

**Two systemd services** — intentionally decoupled:
- `gnss-stack.service` — manages str2str input bridge + output processes (Layers 1 & 2)
- `gnss-dashboard.service` — runs the Flask web app (Layer 3)

The dashboard survives `gnss-stack` failures. The stack runs without the dashboard.

**Single source of truth**: `/etc/gnss/str2str.toml` (TOML)

---

## Repository Layout

```
vibe-gnss-stack/
├── app.py                  # Flask backend + NMEA readers (Layer 3)
├── str2str_manager.py      # Process manager: str2str input bridge + outputs (Layers 1 & 2)
├── str2str.toml            # Reference/template configuration
├── requirements.txt        # Python dependencies
├── templates/
│   └── index.html          # Single-page web UI (~800 lines, no build step)
├── gnss-stack.service      # systemd unit for Layer 1 & 2
├── gnss-dashboard.service  # systemd unit for Layer 3
├── install.sh              # Full system installation (requires sudo)
├── uninstall.sh            # Removes all installed files
└── README.md               # User-facing documentation
```

---

## Key Modules

### `app.py` — Flask Backend

**Config path**: resolved from env var `GNSS_CONFIG`, default `/etc/gnss/str2str.toml`.

**Important globals**:
- `gnss: GNSSState` — singleton holding all current GNSS data; protected by `gnss.lock` (threading.Lock)
- `reader_manager: ReaderManager` — manages the active `NMEAReader` instance

**Classes**:

| Class | Purpose |
|---|---|
| `GNSSState` | Thread-safe container for position, fix quality, satellite data. Call `gnss.to_dict()` to serialise. |
| `NMEAReader` (ABC) | Abstract base for all NMEA data sources. Subclass and implement `_connect_and_read()`. |
| `SerialReader` | Reads from a serial port. |
| `TCPClientReader` | Connects to a TCP server (e.g. str2str input bridge port 4001). |
| `TCPServerReader` | Listens as a TCP server, accepts one client at a time. |
| `UDPReader` | Listens on a UDP port. |
| `NTRIPClientReader` | Connects to an NTRIP caster via HTTP 1.0. |
| `FileReader` | Replays an NMEA log file, optionally looping. |
| `ReaderManager` | Starts/stops/restarts the active reader, reads config to pick the right type. |

**NMEA parsing**:
- `process_nmea(sentence)` — parses one NMEA sentence, updates global `gnss` state
- `_chk(s)` — NMEA checksum validation
- `_ll(v, h)` — converts NMEA lat/lon format (DDMM.MMMM) to decimal degrees
- Sentences handled: `GGA`, `RMC`, `GSA`, `GSV`
- RTCM3 binary data is silently ignored (non-ASCII filtered out)
- Satellite entries expire after 30 seconds

**REST API routes**:

| Method | Path | Description |
|---|---|---|
| `GET` | `/` | Serves `templates/index.html`; falls back to embedded HTML if missing |
| `GET` | `/api/gnss` | Returns current `GNSSState` as JSON |
| `GET` | `/api/config` | Returns TOML config file content (or embedded default if missing) |
| `POST` | `/api/config` | Saves TOML (validates syntax first, creates timestamped `.bak` backup); pass `restart_reader: true` to reload reader |
| `POST` | `/api/service/<action>` | `start`/`stop`/`restart` a systemd service via `sudo systemctl` |
| `GET` | `/api/service/status` | Returns `gnss-stack` service state from systemd |
| `POST` | `/api/reader/restart` | Restarts the active NMEA reader (picks up new config) |
| `GET` | `/api/reader/status` | Returns current reader type, info string, and connection status |
| `GET` | `/api/processes` | Queries systemd for gnss-stack/gnss-dashboard unit states |

**WebSocket** (Socket.IO):
- Event `gnss_update` — emitted every 1 second by a background thread to all connected clients
- `async_mode="threading"` — uses Python threads, **not** eventlet

**systemd interaction**: service control calls `sudo systemctl <action> <service>`. This requires a sudoers entry at `/etc/sudoers.d/gnss-systemctl` (created by `install.sh`).

---

### `str2str_manager.py` — Stack Process Manager

Called as `ExecStart` in the systemd unit. Starts the str2str input bridge and one str2str process per enabled output.

**Classes**:

| Class | Purpose |
|---|---|
| `ManagedProcess` | Wraps a subprocess. Reads stdout/stderr, auto-restarts on exit (respecting `reconnect` and `max_restarts`). |
| `GNSSStackManager` | Orchestrates all `ManagedProcess` instances. Handles SIGTERM/SIGINT for clean shutdown. |

**Key functions**:

| Function | Purpose |
|---|---|
| `build_str2str_input_url(inp)` | Converts `[str2str.input]` dict → str2str `-in` URL |
| `build_str2str_output_url(out)` | Converts `[[str2str.outputs]]` dict → str2str `-out` URL |
| `build_str2str_cmd(binary, input_url, out)` | Assembles full str2str command list including `-msg` filter and `-t` swap interval |

**str2str URL types supported**:
- Input: `tcpcli`, `tcpsvr`, `serial`, `ntrip`, `file`
- Output: `tcpsvr`, `udpsvr`, `file`, `ntrip` (push to caster), `ntripsvr` (local NTRIP server)

Each enabled output in `[[str2str.outputs]]` launches a **separate** str2str subprocess.

---

### `templates/index.html` — Web UI

Single-file SPA. No build step, no npm, no bundler. All dependencies loaded from CDN:
- **Leaflet.js v1.9.4** — interactive map
- **Chart.js v4.4.1** — real-time signal charts
- **Socket.io v4.7.5** — WebSocket client
- **CodeMirror v5.65.16** — TOML config editor with syntax highlighting

UI tabs: Map, Satellites (sky plot + signal bars), Metrics (HDOP/PDOP/speed), Config Editor, Service Control, Process Status, Logs.

Design: industrial/terminal dark theme. CSS custom properties define constellation colours:
```css
--gps: #00d4ff; --glonass: #ff6b35; --galileo: #7cff6b;
--beidou: #ffd700; --qzss: #ff69b4;
```
Fonts: Rajdhani (UI), Share Tech Mono (code), Orbitron (display headers).

---

## Configuration System

Single TOML file at `/etc/gnss/str2str.toml` (production) or path from env `GNSS_CONFIG`.

**Section reference**:

```toml
[general]
log_level     = "INFO"         # DEBUG | INFO | WARNING | ERROR
log_file      = "/var/log/gnss/manager.log"
restart_delay = 5              # seconds between restart attempts
max_restarts  = 0              # 0 = unlimited

[str2str.input_bridge]
enabled   = true
device    = "/dev/ttyUSB0"
baudrate  = 115200
port      = 4001
bind_addr = "127.0.0.1"

[str2str]
binary = "/usr/local/bin/str2str"

[str2str.input]
type = "tcpcli"   # tcpcli | tcpsvr | serial | ntrip | file
host = "127.0.0.1"
port = 4001

[[str2str.outputs]]           # Repeat this block for each output stream
name          = "rtcm3_tcp"
enabled       = true
type          = "tcpsvr"      # tcpsvr | udpsvr | file | ntrip | ntripsvr
port          = 9001
bind_addr     = "0.0.0.0"
rtcm_messages = ""            # applies to ALL output types; "" = all; or comma-separated e.g. "1004,1005,1033"
reconnect     = true
# For file type only:
# path         = "/var/log/gnss/rtcm3_%Y%m%d_%H%M%S.rtcm3"
# swap_interval = 86400

[frontend.nmea_source]
type = "tcpcli"   # tcpcli | tcpsvr | serial | udp | ntrip | file
host = "127.0.0.1"
port = 4001

[frontend.server]
host       = "0.0.0.0"
port       = 5000
secret_key = "change-me-in-production"
```

**Critical rules**:
- Config saves via the web UI create a timestamped backup (`.toml.bak.<timestamp>`) before overwriting.
- TOML syntax is validated before saving (server-side via `tomllib.loads()`).

---

## Development Setup

**Requirements**: Python 3.8+, Linux (systemd for production)

```bash
# Clone and install Python deps
git clone <repo>
cd vibe-gnss-stack
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Run dashboard in dev mode (reads config from GNSS_CONFIG or /etc/gnss/str2str.toml)
GNSS_CONFIG=./str2str.toml python app.py

# Run stack manager in dev mode
GNSS_CONFIG=./str2str.toml python str2str_manager.py --config ./str2str.toml

# Full system install (creates gnss user, systemd units, /opt/gnss/, /etc/gnss/, /var/log/gnss/)
sudo ./install.sh

# Uninstall
sudo ./uninstall.sh
```

**TOML parsing**: `tomllib` is built-in on Python 3.11+. On older versions, `tomli` (in `requirements.txt`) is used. Both are imported under the name `tomllib`.

---

## Coding Conventions

- **Python style**: No type annotations exist in the codebase — match existing style. No f-string nesting.
- **Threading**: Use `threading.Thread` and `threading.Lock`. The app uses `async_mode="threading"` for SocketIO — do **not** switch to eventlet green threads.
- **Abstract readers**: New NMEA sources must subclass `NMEAReader` and implement `_connect_and_read()`. Use `self._iter_lines(file_object)` to parse lines if reading a stream.
- **No database**: State is held in memory (`GNSSState`) or on disk (TOML). No ORM, no SQL.
- **No build step**: The frontend is plain HTML+JS. Add new JS functionality inline in `templates/index.html`.
- **CSS variables**: Use existing `var(--gps)`, `var(--galileo)`, etc. for constellation colours. Follow the dark terminal theme.
- **Subprocess management**: Use `ManagedProcess` for any new long-running subprocesses. Do not use `os.system()`.
- **Logging**: In `str2str_manager.py` use `self.logger` (from `setup_logging()`). In `app.py` use `app.logger`.

---

## Extending the Codebase

### Add a new NMEA reader source type

1. Subclass `NMEAReader` in `app.py`:
   ```python
   class MyReader(NMEAReader):
       source_type = property(lambda self: "mytype")
       source_info = property(lambda self: "description")
       def _connect_and_read(self):
           # connect, then call self._iter_lines(stream) or process_nmea() in a loop
   ```
2. Add a case in `reader_from_config()` (around `app.py:498`):
   ```python
   if t == "mytype":
       return MyReader(...)
   ```
3. Document the new `type` value in `[frontend.nmea_source]` section of `str2str.toml`.
4. Add the new type as an option in the frontend config editor UI in `templates/index.html`.

### Add a new str2str output type

1. Add a case in `build_str2str_output_url()` (`str2str_manager.py:274`).
2. Add any type-specific `build_str2str_cmd()` logic if needed (`str2str_manager.py:313`).
3. Document the new `type` in `str2str.toml` comments and `README.md`.

### Add a new REST API endpoint

Add a Flask route to `app.py`. Follow the pattern of existing routes: return `jsonify({"ok": True, ...})` on success, `jsonify({"ok": False, "error": "..."})` with an appropriate HTTP status on failure.

---

## Testing

**There are no automated tests.** Manual verification steps:

- **NMEA parsing**: Run `app.py` with `GNSS_CONFIG` pointing to a config with `type = "file"` in `[frontend.nmea_source]` and a sample NMEA log file. Check `/api/gnss` returns parsed data.
- **Config save**: POST to `/api/config` with valid/invalid TOML and verify backup creation and error responses.
- **Stack manager**: Run `str2str_manager.py --config ./str2str.toml` with a real serial device configured and verify the input bridge and output processes start correctly.
- **Service control**: Requires systemd; test with actual `gnss-stack.service` and `gnss-dashboard.service`.
- **WebSocket**: Open the dashboard in a browser and observe live updates in the Map and Satellites tabs.

When adding new logic, add a `# TEST: <how to verify this manually>` comment near the code.

---

## Deployment

**Production paths**:
- Code: `/opt/gnss/` (installed by `install.sh`)
- Config: `/etc/gnss/str2str.toml`
- Logs: `/var/log/gnss/`

**System user**: Services run as `gnss` (system user), group `dialout` (for serial port access).

**Systemd security**: `ProtectSystem=strict` is set. Writable paths are explicitly listed:
- `/etc/gnss/` (config read/write)
- `/var/log/gnss/` (log files)
- `/tmp/` (lock files)

Do **not** assume write access to any other system paths from within the services.

**sudoers**: Service control from the dashboard requires:
```
gnss ALL=(root) NOPASSWD: /bin/systemctl start gnss-stack, /bin/systemctl stop gnss-stack, ...
```
This is installed at `/etc/sudoers.d/gnss-systemctl` by `install.sh`.

---

## Git Workflow

- Feature branch: `claude/claude-md-mlzcnqf6mp7bfauf-oIP3J`
- Commit messages should be descriptive (what changed and why)
- Push with: `git push -u origin <branch-name>`
- No CI/CD — no automated checks on push

---

## External Dependencies

| Tool | Version | Purpose |
|---|---|---|
| Python | 3.8+ | Runtime |
| flask | ≥3.0 | Web framework |
| flask-socketio | ≥5.3 | WebSocket server |
| pyserial | ≥3.5 | Serial port access |
| eventlet | ≥0.35 | In requirements but SocketIO uses `threading` mode |
| tomli | ≥2.0 | TOML parser for Python <3.11 |
| str2str (RTKLIB) | any | GNSS data streaming binary (input bridge + outputs) |
| systemd | any | Service management |

#!/usr/bin/env bash
# install.sh – Installs the complete GNSS stack
#
# What gets installed:
#   /opt/gnss/                        – Python application + venv
#   /etc/gnss/str2str.toml            – configuration (template, first install only)
#   /usr/lib/systemd/system/          – service unit files
#   /etc/systemd/system/              – symlinks (created by systemctl enable)
#   /etc/sudoers.d/gnss-systemctl     – allows the gnss user to control services via systemctl
#   /var/log/gnss/                    – log directory
#
# Workflow after installation:
#   1. Adjust configuration: nano /etc/gnss/str2str.toml
#   2. Start the dashboard:  systemctl start gnss-dashboard
#   3. Configure in the dashboard, then start gnss-stack
#      (or: systemctl start gnss-stack)

set -euo pipefail

INSTALL_DIR="/opt/gnss"
CFG_DIR="/etc/gnss"
LOG_DIR="/var/log/gnss"
SYSTEMD_SYSTEM_DIR="/usr/lib/systemd/system"
STACK_SVC="$SYSTEMD_SYSTEM_DIR/gnss-stack.service"
DASH_SVC="$SYSTEMD_SYSTEM_DIR/gnss-dashboard.service"
SUDOERS_FILE="/etc/sudoers.d/gnss-systemctl"
GNSS_USER="gnss"

echo "╔══════════════════════════════════════╗"
echo "║    GNSS Stack Installer              ║"
echo "╚══════════════════════════════════════╝"

[[ $EUID -ne 0 ]] && { echo "Error: sudo required."; exit 1; }

# ── Dependencies ─────────────────────────────────────────────────────────────
echo "→ Checking dependencies…"
apt-get install -y --no-install-recommends \
    python3-venv python3-pip 2>/dev/null || true

# ── System user ──────────────────────────────────────────────────────────────
echo "→ System user…"
id "$GNSS_USER" &>/dev/null || useradd -r -s /bin/false "$GNSS_USER"
usermod -aG dialout "$GNSS_USER"
echo "  User: $GNSS_USER (group dialout)"

# ── Directories ──────────────────────────────────────────────────────────────
echo "→ Directories…"
mkdir -p "$INSTALL_DIR" "$CFG_DIR" "$LOG_DIR"
chown "$GNSS_USER:$GNSS_USER" "$LOG_DIR" "$CFG_DIR"
mkdir -p "$SYSTEMD_SYSTEM_DIR"

# ── Python venv ──────────────────────────────────────────────────────────────
echo "→ Python venv in $INSTALL_DIR/venv …"
python3 -m venv "$INSTALL_DIR/venv"
"$INSTALL_DIR/venv/bin/pip" install -q --upgrade pip
"$INSTALL_DIR/venv/bin/pip" install -q \
    flask flask-socketio pyserial eventlet
# tomllib is built-in from Python 3.11+, otherwise install tomli
"$INSTALL_DIR/venv/bin/python" -c "import tomllib" 2>/dev/null || \
    "$INSTALL_DIR/venv/bin/pip" install -q tomli
echo "  Python packages installed."

# ── Program files ─────────────────────────────────────────────────────────────
echo "→ Program files to $INSTALL_DIR …"
cp str2str_manager.py "$INSTALL_DIR/"
cp app.py             "$INSTALL_DIR/"
cp -r templates       "$INSTALL_DIR/" 2>/dev/null || true
chmod 755 "$INSTALL_DIR/str2str_manager.py" "$INSTALL_DIR/app.py"
chown -R "$GNSS_USER:$GNSS_USER" "$INSTALL_DIR"

# ── Configuration ─────────────────────────────────────────────────────────────
# Rules:
#  - File missing entirely       → copy template (first install)
#  - File empty or < 80 bytes    → probably a remnant of a broken install
#                                   → copy template
#  - File has meaningful content → leave it untouched (user data!)
echo "→ Configuration…"
TOML_SIZE=0
[[ -f "$CFG_DIR/str2str.toml" ]] && TOML_SIZE=$(wc -c < "$CFG_DIR/str2str.toml")

if [[ -f "$CFG_DIR/str2str.toml" ]] && [[ "$TOML_SIZE" -ge 80 ]]; then
    echo "  Config present ($TOML_SIZE bytes) – not overwritten."
    echo "  Path: $CFG_DIR/str2str.toml"
elif [[ -f "str2str.toml" ]]; then
    [[ -f "$CFG_DIR/str2str.toml" ]] && echo "  Empty/incomplete config found – replacing with template."
    cp str2str.toml "$CFG_DIR/str2str.toml"
    chown "$GNSS_USER:$GNSS_USER" "$CFG_DIR/str2str.toml"
    echo "  Template installed: $CFG_DIR/str2str.toml"
    echo "  → Please adjust: device, baudrate, ports"
    echo "  → Or edit directly in the dashboard."
else
    echo "  str2str.toml not found in the installation directory."
    echo "  A minimal configuration will be created – please complete it"
    echo "  in the dashboard."
    cat > "$CFG_DIR/str2str.toml" << 'MINTOML'
# GNSS Stack Configuration – please adjust!
# All settings can be edited in the dashboard at http://<IP>:5000.

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

[[str2str.outputs]]
name      = "rtcm3_tcp"
enabled   = true
type      = "tcpsvr"
port      = 9001
bind_addr = "0.0.0.0"
rtcm_messages = ""
reconnect = true

[frontend.nmea_source]
type = "tcpcli"
host = "127.0.0.1"
port = 4001

[frontend.server]
host = "0.0.0.0"
port = 5000
MINTOML
    chown "$GNSS_USER:$GNSS_USER" "$CFG_DIR/str2str.toml"
    echo "  Minimal config installed: $CFG_DIR/str2str.toml"
fi

# ── sudoers ──────────────────────────────────────────────────────────────────
echo "→ sudoers rule…"
cat > "$SUDOERS_FILE" <<EOF
# GNSS Stack – allows the gnss user to control the stack services
# Important: exact spelling, no extra spaces
$GNSS_USER ALL=(root) NOPASSWD: /usr/bin/systemctl start gnss-stack
$GNSS_USER ALL=(root) NOPASSWD: /usr/bin/systemctl stop gnss-stack
$GNSS_USER ALL=(root) NOPASSWD: /usr/bin/systemctl restart gnss-stack
$GNSS_USER ALL=(root) NOPASSWD: /usr/bin/systemctl status gnss-stack
$GNSS_USER ALL=(root) NOPASSWD: /usr/bin/systemctl start gnss-dashboard
$GNSS_USER ALL=(root) NOPASSWD: /usr/bin/systemctl stop gnss-dashboard
$GNSS_USER ALL=(root) NOPASSWD: /usr/bin/systemctl restart gnss-dashboard
$GNSS_USER ALL=(root) NOPASSWD: /usr/bin/systemctl status gnss-dashboard
EOF
chmod 440 "$SUDOERS_FILE"
# Syntax check – remove the file immediately on error so sudo is not locked out
if visudo -c -f "$SUDOERS_FILE" &>/dev/null; then
    echo "  sudoers syntax OK."
else
    echo "  ERROR: sudoers syntax error – file removed!"
    rm -f "$SUDOERS_FILE"
fi

# ── systemd unit files ───────────────────────────────────────────────────────
# Unit files go to /usr/lib/systemd/system/ (installer-managed location).
# systemctl enable then creates symlinks in /etc/systemd/system/ automatically.
# Local overrides via: systemctl edit <service>
echo "→ systemd units to $SYSTEMD_SYSTEM_DIR …"

# Generate a one-time Flask secret
FLASK_SECRET=$(openssl rand -hex 24)

cat > "$STACK_SVC" <<EOF
# Generated by install.sh on $(date -u '+%Y-%m-%d %H:%M UTC')
# To customise: systemctl edit gnss-stack
[Unit]
Description=GNSS Stack (str2str)
Documentation=https://github.com/tomojitakasu/RTKLIB
After=network.target

[Service]
Type=simple
User=$GNSS_USER
Group=dialout
SupplementaryGroups=dialout
Environment=GNSS_CONFIG=$CFG_DIR/str2str.toml

# Start str2str input bridge + output processes.
ExecStart=$INSTALL_DIR/venv/bin/python $INSTALL_DIR/str2str_manager.py \\
    --config \${GNSS_CONFIG}

Restart=on-failure
RestartSec=30
StartLimitIntervalSec=0

NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ReadWritePaths=$CFG_DIR $LOG_DIR /var/lock /run/lock /tmp

StandardOutput=journal
StandardError=journal
SyslogIdentifier=gnss-stack

[Install]
WantedBy=multi-user.target
EOF

cat > "$DASH_SVC" <<EOF
# Generated by install.sh on $(date -u '+%Y-%m-%d %H:%M UTC')
# To customise: systemctl edit gnss-dashboard
[Unit]
Description=GNSS Dashboard Web UI
After=network.target

[Service]
Type=simple
User=$GNSS_USER
Group=dialout
Environment=GNSS_CONFIG=$CFG_DIR/str2str.toml
Environment=FLASK_SECRET=$FLASK_SECRET

ExecStart=$INSTALL_DIR/venv/bin/python $INSTALL_DIR/app.py

Restart=always
RestartSec=5
StartLimitIntervalSec=0

# NoNewPrivileges=true removed – blocks sudo for systemctl calls
PrivateTmp=true
ProtectSystem=strict
ReadWritePaths=$CFG_DIR $LOG_DIR /var/lock /run/lock

StandardOutput=journal
StandardError=journal
SyslogIdentifier=gnss-dashboard

[Install]
WantedBy=multi-user.target
EOF

echo "  $STACK_SVC"
echo "  $DASH_SVC"

systemctl daemon-reload

# enable creates symlinks /etc/systemd/system/ → /usr/lib/systemd/system/
systemctl enable gnss-stack gnss-dashboard
echo "  Symlinks created:"
ls -l /etc/systemd/system/gnss-stack.service \
       /etc/systemd/system/gnss-dashboard.service 2>/dev/null || true

# ── Done ─────────────────────────────────────────────────────────────────────
IP=$(hostname -I | awk '{print $1}')
echo ""
echo "╔══════════════════════════════════════════════════════════════╗"
echo "║  ✓ Installation complete                                     ║"
echo "╠══════════════════════════════════════════════════════════════╣"
echo "║                                                              ║"
echo "║  Workflow:                                                   ║"
echo "║                                                              ║"
echo "║  1. Start the dashboard (always possible, independent):      ║"
echo "║       systemctl start gnss-dashboard                         ║"
echo "║                                                              ║"
echo "║  2. Open the dashboard and adjust the configuration:         ║"
echo "║       http://${IP}:5000                      "
echo "║                                                              ║"
echo "║  3. Review the configuration:                                ║"
echo "║       nano $CFG_DIR/str2str.toml            "
echo "║                                                              ║"
echo "║  4. Start the stack (starts str2str):                        ║"
echo "║       systemctl start gnss-stack                             ║"
echo "║                                                              ║"
echo "║  Logs:                                                       ║"
echo "║       journalctl -u gnss-dashboard -f                        ║"
echo "║       journalctl -u gnss-stack -f                            ║"
echo "║                                                              ║"
echo "║  Note: gnss-stack may fail (wrong config)                    ║"
echo "║  – the dashboard remains accessible at all times.            ║"
echo "╚══════════════════════════════════════════════════════════════╝"

#!/usr/bin/env bash
# uninstall.sh – Completely removes the GNSS stack
#
# Removes exactly what install.sh created:
#   - Disable + stop systemd services
#   - Remove unit files from /usr/lib/systemd/system/
#   - Remove symlinks from /etc/systemd/system/ (via systemctl disable)
#   - Program files  (/opt/gnss)
#   - sudoers rule   (/etc/sudoers.d/gnss-systemctl)
#   - System user    (gnss)   [optional: --remove-user]
#   - Configuration  (/etc/gnss)  [optional: --remove-config]
#   - Log files      (/var/log/gnss)  [optional: --remove-logs]
#
# Usage:
#   sudo bash uninstall.sh                    # safe (config + logs kept)
#   sudo bash uninstall.sh --remove-config    # including configuration
#   sudo bash uninstall.sh --remove-logs      # including log files
#   sudo bash uninstall.sh --remove-user      # including system user
#   sudo bash uninstall.sh --all              # remove everything
#   sudo bash uninstall.sh --dry-run          # show what would be done

set -euo pipefail

INSTALL_DIR="/opt/gnss"
CFG_DIR="/etc/gnss"
LOG_DIR="/var/log/gnss"
SYSTEMD_SYSTEM_DIR="/usr/lib/systemd/system"
STACK_SVC_NAME="gnss-stack"
DASH_SVC_NAME="gnss-dashboard"
STACK_SVC_FILE="$SYSTEMD_SYSTEM_DIR/${STACK_SVC_NAME}.service"
DASH_SVC_FILE="$SYSTEMD_SYSTEM_DIR/${DASH_SVC_NAME}.service"
SUDOERS_FILE="/etc/sudoers.d/gnss-systemctl"
GNSS_USER="gnss"

REMOVE_CONFIG=false
REMOVE_LOGS=false
REMOVE_USER=false
DRY_RUN=false

for arg in "$@"; do
    case "$arg" in
        --remove-config)  REMOVE_CONFIG=true ;;
        --remove-logs)    REMOVE_LOGS=true ;;
        --remove-user)    REMOVE_USER=true ;;
        --all)
            REMOVE_CONFIG=true; REMOVE_LOGS=true
            REMOVE_USER=true ;;
        --dry-run) DRY_RUN=true ;;
        --help|-h)
            grep '^#' "$0" | grep -v '#!/' | sed 's/^# \?//'; exit 0 ;;
        *)
            echo "Unknown option: $arg  (--help for usage)" >&2; exit 1 ;;
    esac
done

[[ $EUID -ne 0 ]] && { echo "Error: sudo required."; exit 1; }

ok()   { echo "  ✓ $*"; }
skip() { echo "  – $* (not present, skipped)"; }
info() { echo "  → $*"; }
warn() { echo "  ⚠ $*"; }

run() {
    if [[ "$DRY_RUN" == true ]]; then
        echo "  [DRY] $*"
    else
        "$@"
    fi
}

remove_file() {
    if [[ -e "$1" || -L "$1" ]]; then
        run rm -f "$1" && ok "Removed: $1"
    else
        skip "$1"
    fi
}

remove_dir() {
    if [[ -d "$1" ]]; then
        run rm -rf "$1" && ok "Removed: $1"
    else
        skip "$1"
    fi
}

echo "╔══════════════════════════════════════════╗"
echo "║    GNSS Stack Uninstaller                ║"
echo "╚══════════════════════════════════════════╝"
[[ "$DRY_RUN" == true ]] && echo "  MODE: DRY-RUN – no changes will be made"
echo ""
echo "Options:"
echo "  Configuration : $REMOVE_CONFIG  ($CFG_DIR)"
echo "  Logs          : $REMOVE_LOGS    ($LOG_DIR)"
echo "  User          : $REMOVE_USER    ($GNSS_USER)"
echo ""

if [[ "$DRY_RUN" == false ]]; then
    read -r -p "Continue? [y/N] " CONFIRM
    [[ "${CONFIRM,,}" == "y" ]] || { echo "Aborted."; exit 0; }
fi
echo ""

# ── 1. Stop and disable services ─────────────────────────────────────────────
echo "[ 1/6 ] Stopping and disabling services…"
for SVC in "$STACK_SVC_NAME" "$DASH_SVC_NAME"; do
    if systemctl is-active --quiet "$SVC" 2>/dev/null; then
        run systemctl stop "$SVC"
        ok "Stopped: $SVC"
    else
        skip "Service not active: $SVC"
    fi
    if systemctl is-enabled --quiet "$SVC" 2>/dev/null; then
        # disable automatically removes the symlink in /etc/systemd/system/
        run systemctl disable "$SVC"
        ok "Disabled + symlink removed: $SVC"
    else
        skip "Service not enabled: $SVC"
    fi
done

# ── 2. Remove unit files ──────────────────────────────────────────────────────
# The actual files are in /usr/lib/systemd/system/.
# Symlinks in /etc/systemd/system/ were removed by systemctl disable;
# check again just to be safe.
echo ""
echo "[ 2/6 ] Removing unit files ($SYSTEMD_SYSTEM_DIR)…"
remove_file "$STACK_SVC_FILE"
remove_file "$DASH_SVC_FILE"

for SVC in "$STACK_SVC_NAME" "$DASH_SVC_NAME"; do
    LINK="/etc/systemd/system/${SVC}.service"
    if [[ -L "$LINK" ]]; then
        run rm -f "$LINK"
        ok "Symlink removed: $LINK"
    fi
done

if [[ "$DRY_RUN" == false ]]; then
    systemctl daemon-reload
fi
ok "systemd daemon-reload"

# ── 3. Remove sudoers rule ────────────────────────────────────────────────────
echo ""
echo "[ 3/6 ] Removing sudoers rule…"
if [[ -f "$SUDOERS_FILE" ]]; then
    if grep -q "gnss-stack\|gnss-dashboard" "$SUDOERS_FILE" 2>/dev/null; then
        remove_file "$SUDOERS_FILE"
        if [[ "$DRY_RUN" == false ]]; then
            visudo -c &>/dev/null && ok "sudoers syntax OK" \
                || warn "visudo syntax error – please check!"
        fi
    else
        warn "$SUDOERS_FILE contains no GNSS stack entry – not removed."
    fi
else
    skip "$SUDOERS_FILE"
fi

# ── 4. Program files ──────────────────────────────────────────────────────────
echo ""
echo "[ 4/6 ] Removing program files ($INSTALL_DIR)…"
remove_dir "$INSTALL_DIR"

# ── 5. Configuration / logs ───────────────────────────────────────────────────
echo ""
echo "[ 5/6 ] Configuration and logs…"

if [[ "$REMOVE_CONFIG" == true ]]; then
    if [[ -d "$CFG_DIR" ]]; then
        # Back up before deleting – a backup failure must NOT abort the
        # deletion (set -e would otherwise exit the script).
        BACKUP="/tmp/gnss_config_backup_$(date +%Y%m%d_%H%M%S).tar.gz"
        if [[ "$DRY_RUN" == false ]]; then
            if tar -czf "$BACKUP" -C "$(dirname "$CFG_DIR")" \
                    "$(basename "$CFG_DIR")" 2>/dev/null; then
                ok "Config backup: $BACKUP"
            else
                warn "Backup failed – continuing anyway."
                BACKUP=""
            fi
        else
            info "[DRY] Would create backup: $BACKUP"
        fi
        remove_dir "$CFG_DIR"
    else
        skip "$CFG_DIR"
    fi
else
    info "Configuration kept: $CFG_DIR  (--remove-config)"
    BAK_COUNT=$(find "$CFG_DIR" -name "*.bak.*" 2>/dev/null | wc -l)
    [[ "$BAK_COUNT" -gt 0 ]] && \
        info "$BAK_COUNT TOML backup(s) in $CFG_DIR (remove manually if desired)"
fi

if [[ "$REMOVE_LOGS" == true ]]; then
    remove_dir "$LOG_DIR"
else
    info "Logs kept: $LOG_DIR  (--remove-logs)"
fi

# ── 6. Optional: system user ──────────────────────────────────────────────────
echo ""
echo "[ 6/6 ] Optional components…"

if [[ "$REMOVE_USER" == true ]]; then
    if id "$GNSS_USER" &>/dev/null; then
        if pgrep -u "$GNSS_USER" &>/dev/null; then
            warn "User '$GNSS_USER' has running processes – not removed."
        else
            run userdel "$GNSS_USER"
            ok "User removed: $GNSS_USER"
        fi
    else
        skip "User '$GNSS_USER' does not exist"
    fi
else
    info "User kept: $GNSS_USER  (--remove-user)"
fi

# ── Journal hint ──────────────────────────────────────────────────────────────
echo ""
info "Clean up journald logs (optional):"
info "  journalctl --rotate && journalctl --vacuum-time=1s"

echo ""
echo "╔══════════════════════════════════════════════════════╗"
if [[ "$DRY_RUN" == true ]]; then
echo "║  ✓ Dry-run – no changes made                         ║"
else
echo "║  ✓ Uninstallation complete                           ║"
fi
echo "╚══════════════════════════════════════════════════════╝"

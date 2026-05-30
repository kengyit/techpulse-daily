#!/usr/bin/env bash
# TechPulse Daily — install the local scheduler as an OS service.
#
# Replaces the old OpenClaw cron job. Installs user-level services (no sudo):
#
#   ./install.sh                # auto: systemd timer on Linux, launchd on macOS
#   ./install.sh timer          # Linux: systemd timer fires `app.py once` daily
#   ./install.sh daemon         # Linux: systemd service runs `app.py serve` (built-in scheduler)
#   ./install.sh launchd        # macOS: launchd agent fires `app.py once` daily
#   ./install.sh launchd-daemon # macOS: keep-alive LaunchAgent runs `app.py serve` —
#                               #        starts at login and restarts itself
#   sudo ./install.sh launchd-system
#                               # macOS: keep-alive LaunchDaemon — starts at system
#                               #        boot (pre-login). Runs as $SUDO_USER.
#   ./install.sh --uninstall    # add `sudo` to also remove the LaunchDaemon
#
# Schedule time comes from $TECHPULSE_SCHEDULE, else categories.json
# settings.schedule_time, else 06:30.

set -euo pipefail

APP_DIR="$(cd "$(dirname "$0")" && pwd)"
PYTHON="$(command -v python3 || command -v python)"
TEMPLATES="${APP_DIR}/service"
# User the daemon should run as. When invoked via `sudo`, $SUDO_USER is set to
# the original invoker so the LaunchDaemon doesn't end up running as root.
INSTALL_USER="${SUDO_USER:-${USER:-$(id -un)}}"
SYS_PLIST="/Library/LaunchDaemons/com.techpulse.system.plist"

if [ -z "${PYTHON}" ]; then
    echo "ERROR: python3 not found on PATH." >&2
    exit 1
fi

# ── Resolve schedule time (HH:MM) ──
SCHEDULE="${TECHPULSE_SCHEDULE:-}"
if [ -z "${SCHEDULE}" ]; then
    SCHEDULE="$("${PYTHON}" - "${APP_DIR}/categories.json" <<'PY'
import json, sys
try:
    with open(sys.argv[1], encoding="utf-8") as f:
        print(json.load(f).get("settings", {}).get("schedule_time", "06:30"))
except Exception:
    print("06:30")
PY
)"
fi
HOUR="${SCHEDULE%%:*}"
MINUTE="${SCHEDULE##*:}"
HOUR="$((10#${HOUR}))"
MINUTE="$((10#${MINUTE}))"
ON_CALENDAR="*-*-* $(printf '%02d:%02d' "${HOUR}" "${MINUTE}"):00"

render() {  # render <template> <dest>
    sed -e "s|__APP_DIR__|${APP_DIR}|g" \
        -e "s|__PYTHON__|${PYTHON}|g" \
        -e "s|__ON_CALENDAR__|${ON_CALENDAR}|g" \
        -e "s|__HOUR__|${HOUR}|g" \
        -e "s|__MINUTE__|${MINUTE}|g" \
        -e "s|__USER__|${INSTALL_USER}|g" \
        "$1" >"$2"
}

OS="$(uname -s)"
MODE="${1:-auto}"

if [ "${MODE}" = "auto" ]; then
    case "${OS}" in
        Darwin) MODE="launchd" ;;
        *)      MODE="timer" ;;
    esac
fi

uninstall() {
    case "${OS}" in
        Darwin)
            local plist
            for label in com.techpulse.daily com.techpulse.daemon; do
                plist="${HOME}/Library/LaunchAgents/${label}.plist"
                launchctl unload -w "${plist}" 2>/dev/null || true
                rm -f "${plist}"
            done
            if [ -f "${SYS_PLIST}" ]; then
                if [ "$(id -u)" -eq 0 ]; then
                    launchctl unload -w "${SYS_PLIST}" 2>/dev/null || true
                    rm -f "${SYS_PLIST}"
                    echo "Removed LaunchAgents and LaunchDaemon."
                else
                    echo "Removed user LaunchAgents."
                    echo "Note: a LaunchDaemon is still installed at ${SYS_PLIST}."
                    echo "      Re-run as root to remove it: sudo ./install.sh --uninstall"
                fi
            else
                echo "Removed launchd agents."
            fi
            ;;
        *)
            systemctl --user disable --now techpulse.timer 2>/dev/null || true
            systemctl --user disable --now techpulse-daemon.service 2>/dev/null || true
            rm -f "${HOME}/.config/systemd/user/techpulse.timer" \
                  "${HOME}/.config/systemd/user/techpulse.service" \
                  "${HOME}/.config/systemd/user/techpulse-daemon.service"
            systemctl --user daemon-reload 2>/dev/null || true
            echo "Removed systemd user units."
            ;;
    esac
}

if [ "${MODE}" = "--uninstall" ] || [ "${MODE}" = "uninstall" ]; then
    uninstall
    exit 0
fi

case "${MODE}" in
    timer)
        UNIT_DIR="${HOME}/.config/systemd/user"
        mkdir -p "${UNIT_DIR}"
        render "${TEMPLATES}/techpulse.service" "${UNIT_DIR}/techpulse.service"
        render "${TEMPLATES}/techpulse.timer"   "${UNIT_DIR}/techpulse.timer"
        systemctl --user daemon-reload
        systemctl --user enable --now techpulse.timer
        echo "Installed systemd timer — runs daily at ${SCHEDULE}."
        echo "  Status:   systemctl --user status techpulse.timer"
        echo "  Next run: systemctl --user list-timers techpulse.timer"
        echo "  Run now:  systemctl --user start techpulse.service"
        echo "  Tip: 'loginctl enable-linger ${USER}' lets it run while logged out."
        ;;
    daemon)
        UNIT_DIR="${HOME}/.config/systemd/user"
        mkdir -p "${UNIT_DIR}"
        render "${TEMPLATES}/techpulse-daemon.service" "${UNIT_DIR}/techpulse-daemon.service"
        systemctl --user daemon-reload
        systemctl --user enable --now techpulse-daemon.service
        echo "Installed systemd daemon — built-in scheduler runs daily at ${SCHEDULE}."
        echo "  Status: systemctl --user status techpulse-daemon.service"
        echo "  Logs:   journalctl --user -u techpulse-daemon.service -f"
        echo "  Tip: 'loginctl enable-linger ${USER}' keeps it running while logged out."
        ;;
    launchd)
        AGENT_DIR="${HOME}/Library/LaunchAgents"
        PLIST="${AGENT_DIR}/com.techpulse.daily.plist"
        mkdir -p "${AGENT_DIR}"
        render "${TEMPLATES}/com.techpulse.daily.plist" "${PLIST}"
        launchctl unload -w "${PLIST}" 2>/dev/null || true
        launchctl load -w "${PLIST}"
        echo "Installed launchd agent — runs daily at ${SCHEDULE}."
        echo "  Status:  launchctl list | grep com.techpulse.daily"
        echo "  Run now: launchctl start com.techpulse.daily"
        ;;
    launchd-daemon)
        AGENT_DIR="${HOME}/Library/LaunchAgents"
        PLIST="${AGENT_DIR}/com.techpulse.daemon.plist"
        mkdir -p "${AGENT_DIR}"
        render "${TEMPLATES}/com.techpulse.daemon.plist" "${PLIST}"
        launchctl unload -w "${PLIST}" 2>/dev/null || true
        launchctl load -w "${PLIST}"
        echo "Installed LaunchAgent — starts at login, restarts itself, runs daily at ${SCHEDULE}."
        echo "  Status: launchctl list | grep com.techpulse.daemon"
        echo "  Logs:   tail -f ${APP_DIR}/techpulse.log"
        echo "  Note: a LaunchAgent starts at login. For pre-login start at system boot,"
        echo "        install the LaunchDaemon: sudo ./install.sh launchd-system"
        ;;
    launchd-system)
        if [ "$(id -u)" -ne 0 ]; then
            echo "ERROR: 'launchd-system' installs a LaunchDaemon in /Library/LaunchDaemons" >&2
            echo "       and requires root. Re-run with: sudo ./install.sh launchd-system" >&2
            exit 1
        fi
        if [ -z "${SUDO_USER:-}" ] || [ "${INSTALL_USER}" = "root" ]; then
            echo "WARNING: SUDO_USER not set; the daemon would run as root." >&2
            echo "         Re-run via 'sudo ./install.sh launchd-system' (not 'sudo -i')" >&2
            echo "         so the daemon runs as your normal user account." >&2
            exit 1
        fi
        render "${TEMPLATES}/com.techpulse.system.plist" "${SYS_PLIST}"
        chown root:wheel "${SYS_PLIST}"
        chmod 644 "${SYS_PLIST}"
        launchctl unload -w "${SYS_PLIST}" 2>/dev/null || true
        launchctl load -w "${SYS_PLIST}"
        echo "Installed LaunchDaemon — starts at system boot (pre-login), restarts itself."
        echo "  Runs as:  ${INSTALL_USER}"
        echo "  Schedule: daily at ${SCHEDULE} (via the built-in scheduler)"
        echo "  Status:   sudo launchctl list | grep com.techpulse.system"
        echo "  Logs:     tail -f ${APP_DIR}/techpulse.log"
        echo "  Uninstall: sudo ./install.sh --uninstall"
        ;;
    *)
        echo "ERROR: unknown mode '${MODE}'. Use: timer | daemon | launchd | launchd-daemon | launchd-system | --uninstall" >&2
        exit 1
        ;;
esac

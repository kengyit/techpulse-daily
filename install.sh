#!/usr/bin/env bash
# TechPulse Daily — install the local scheduler as an OS service.
#
# Replaces the old OpenClaw cron job. Installs user-level services (no sudo):
#
#   ./install.sh            # auto: systemd timer on Linux, launchd on macOS
#   ./install.sh timer      # Linux: systemd timer fires `app.py once` daily
#   ./install.sh daemon     # Linux: systemd service runs `app.py serve` (built-in scheduler)
#   ./install.sh launchd    # macOS: launchd agent fires `app.py once` daily
#   ./install.sh --uninstall
#
# Schedule time comes from $TECHPULSE_SCHEDULE, else categories.json
# settings.schedule_time, else 06:30.

set -euo pipefail

APP_DIR="$(cd "$(dirname "$0")" && pwd)"
PYTHON="$(command -v python3 || command -v python)"
TEMPLATES="${APP_DIR}/service"

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
            local plist="${HOME}/Library/LaunchAgents/com.techpulse.daily.plist"
            launchctl unload -w "${plist}" 2>/dev/null || true
            rm -f "${plist}"
            echo "Removed launchd agent."
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
    *)
        echo "ERROR: unknown mode '${MODE}'. Use: timer | daemon | launchd | --uninstall" >&2
        exit 1
        ;;
esac

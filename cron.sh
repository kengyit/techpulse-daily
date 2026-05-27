#!/bin/bash
# TechPulse Daily — system-cron wrapper (optional).
#
# Prefer `./install.sh` (systemd/launchd) or `python3 app.py serve` for
# scheduling. This thin wrapper is kept only for users who'd rather drive the
# local application from their own crontab:
#
#   crontab -e
#   30 6 * * * /path/to/techpulse-daily/cron.sh
#
# What runs (briefing, JSON, Telegram, Idea Radar) is controlled by .env /
# categories.json — see app.py and .env.example.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# Load .env if present so TECHPULSE_* / TELEGRAM_* vars are available.
if [ -f .env ]; then
    set -a
    # shellcheck disable=SC1091
    . ./.env
    set +a
fi

exec python3 app.py once

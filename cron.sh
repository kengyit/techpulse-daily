#!/bin/bash
# TechPulse Daily — Cron Runner
# Chains: RSS ingestion → Briefing → Idea Radar
# Add to crontab: 30 6 * * * /path/to/techpulse-daily/cron.sh
#
# Environment variables (set in ~/.zshrc or here):
#   TECHPULSE_MODEL        Ollama model (default: minimax-m2.5:cloud)
#   TELEGRAM_BOT_TOKEN     Telegram bot token (optional)
#   TELEGRAM_CHAT_ID       Your Telegram chat ID (optional)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
LOG_FILE="${SCRIPT_DIR}/techpulse.log"

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━" >> "$LOG_FILE"
echo "$(date '+%Y-%m-%d %H:%M:%S') — TechPulse cron starting" >> "$LOG_FILE"

# Step 1: Run TechPulse Daily
echo "[1/2] Running TechPulse Daily..." >> "$LOG_FILE"
cd "$SCRIPT_DIR"
python3 run.py --telegram --json 2>> "$LOG_FILE" >> /dev/null

if [ $? -ne 0 ]; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') — TechPulse FAILED" >> "$LOG_FILE"
    exit 1
fi

# Step 2: Run Idea Radar
echo "[2/2] Running Idea Radar..." >> "$LOG_FILE"
python3 idea_radar.py --json 2>> "$LOG_FILE" >> /dev/null

echo "$(date '+%Y-%m-%d %H:%M:%S') — TechPulse cron complete" >> "$LOG_FILE"

#!/bin/bash

BOT_TOKEN="8209854445:AAGTA9T46tQVrwkIK5ZTMk4_4LiW6HqsD0w"
CHAT_ID="6143574543"
PORT=8443

# Check if bot is running on port 8443
if lsof -ti:$PORT >/dev/null 2>&1; then
    echo "[$(date)] Bot is running on port $PORT"
    exit 0
else
    echo "[$(date)] Bot is DOWN! Sending alert to Telegram..."

    # Send alert to Telegram
    MESSAGE="🚨 *Bot Alert*%0A%0AYour Telegram bot is not running on port $PORT!%0A%0ATime: $(date '+%Y-%m-%d %H:%M:%S %Z')"

    curl -s -X POST "https://api.telegram.org/bot${BOT_TOKEN}/sendMessage" \
        -d "chat_id=${CHAT_ID}" \
        -d "text=${MESSAGE}" \
        -d "parse_mode=Markdown" \
        >/dev/null

    echo "[$(date)] Alert sent to Telegram"
    exit 1
fi

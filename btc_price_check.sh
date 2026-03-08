#!/bin/bash

BOT_TOKEN="8209854445:AAGTA9T46tQVrwkIK5ZTMk4_4LiW6HqsD0w"
CHAT_ID="6143574543"
BRAVE_API_KEY="BSAj-4eS7aOgU_1ess-wuN-Jhizq4zL"

echo "[$(date)] Checking BTC price..."

# Search for BTC price using Brave Search API
RESPONSE=$(curl -s -X GET "https://api.search.brave.com/res/v1/web/search?q=bitcoin+price+usd" \
    -H "X-Subscription-Token: ${BRAVE_API_KEY}" \
    -H "Accept: application/json")

# Extract price info from response (basic parsing)
PRICE_INFO=$(echo "$RESPONSE" | jq -r '.web.results[0].description' 2>/dev/null || echo "Price data unavailable")

# Format message for Telegram
MESSAGE="💰 *Bitcoin Price Update*%0A%0A${PRICE_INFO}%0A%0ATime: $(date '+%H:%M:%S')"

# Send to Telegram
curl -s -X POST "https://api.telegram.org/bot${BOT_TOKEN}/sendMessage" \
    -d "chat_id=${CHAT_ID}" \
    -d "text=${MESSAGE}" \
    -d "parse_mode=Markdown" \
    >/dev/null

echo "[$(date)] BTC price update sent to Telegram"

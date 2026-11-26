#!/bin/bash
# Runs screener-mcp-telegram with the Telegram API keys from the Keychain, so
# they never sit in your shell history or environment. Examples:
#   ./telegram.sh login                 sign in once (Telegram sends you a code)
#   ./telegram.sh channels              list your channels, groups and topics with ids
#   ./telegram.sh follow 1234567890     follow a channel (add --topic N for specific topics)
#   ./telegram.sh following | poll | unfollow ID | logout
# Store the keys once (each command prompts for the value):
#   security add-generic-password -U -s screener-mcp -a telegram-api-id -w
#   security add-generic-password -U -s screener-mcp -a telegram-api-hash -w
set -euo pipefail

secret() { security find-generic-password -s screener-mcp -a "$1" -w 2>/dev/null || true; }

TELEGRAM_API_ID="$(secret telegram-api-id)"
TELEGRAM_API_HASH="$(secret telegram-api-hash)"
if [[ -z "$TELEGRAM_API_ID" || -z "$TELEGRAM_API_HASH" ]]; then
  echo "Store telegram-api-id and telegram-api-hash in the Keychain first (see the top of this script)." >&2
  exit 1
fi
export TELEGRAM_API_ID TELEGRAM_API_HASH

exec "$HOME/.local/bin/screener-mcp-telegram" "$@"

#!/bin/bash
# Starts screener-mcp as an HTTP server for claude.ai (web and mobile).
# Run by launchd (see com.screener-mcp.http.plist); safe to run by hand too.
#
# Secrets come from the macOS Keychain, never from this file. Store them once:
#   security add-generic-password -U -s screener-mcp -a auth-passphrase -w
#   security add-generic-password -U -s screener-mcp -a screener-username -w
#   security add-generic-password -U -s screener-mcp -a screener-password -w
#   security add-generic-password -U -s screener-mcp -a fyers-app-id -w
#   security add-generic-password -U -s screener-mcp -a fyers-secret -w
#   security add-generic-password -U -s screener-mcp -a telegram-api-id -w
#   security add-generic-password -U -s screener-mcp -a telegram-api-hash -w
# (each prompts for the value; the Screener.in, FYERS and Telegram pairs are optional)
set -euo pipefail

# Your public https address from Tailscale Funnel or Cloudflare Tunnel, no /mcp.
: "${MCP_PUBLIC_URL:?Set MCP_PUBLIC_URL in the launchd plist}"

secret() { security find-generic-password -s screener-mcp -a "$1" -w 2>/dev/null || true; }

export MCP_TRANSPORT=streamable-http
export MCP_HOST=127.0.0.1          # only the tunnel reaches it
export PORT="${PORT:-8000}"
export MCP_AUTH_PASSPHRASE="$(secret auth-passphrase)"
user="$(secret screener-username)"; pass="$(secret screener-password)"
if [[ -n "$user" && -n "$pass" ]]; then
  export SCREENER_USERNAME="$user" SCREENER_PASSWORD="$pass"
fi
fyers_id="$(secret fyers-app-id)"; fyers_secret="$(secret fyers-secret)"
if [[ -n "$fyers_id" && -n "$fyers_secret" ]]; then
  export FYERS_APP_ID="$fyers_id" FYERS_SECRET_KEY="$fyers_secret"
fi

tg_id="$(secret telegram-api-id)"; tg_hash="$(secret telegram-api-hash)"
if [[ -n "$tg_id" && -n "$tg_hash" ]]; then
  export TELEGRAM_API_ID="$tg_id" TELEGRAM_API_HASH="$tg_hash"
fi

exec "$HOME/.local/bin/screener-mcp"

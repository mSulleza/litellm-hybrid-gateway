#!/usr/bin/env bash
# Open the LiteLLM usage dashboard in a browser.
# Starts a local server (default :4040) serving ~/litellm/usage.db live, then
# opens the page. Ctrl-C stops the server. Pass a port: `dashboard.sh 5050`.
#
# For a no-server static snapshot instead: `dashboard.sh --export out.html`
set -euo pipefail

PY="$HOME/litellm/venv/bin/python3"
SCRIPT="$HOME/litellm/dashboard.py"

if [[ "${1:-}" == "--export" ]]; then
  out="${2:-$HOME/litellm/usage.html}"
  exec "$PY" "$SCRIPT" --export "$out"
fi

PORT="${1:-4040}"

# Start the server in the background, wait for it to answer, then open the
# browser. If the port is taken, dashboard.py exits and we surface the error.
"$PY" "$SCRIPT" --port "$PORT" &
SRV=$!
cleanup() { kill "$SRV" 2>/dev/null; }
trap cleanup EXIT INT TERM

for i in {1..20}; do
  curl -s -m 1 -o /dev/null "http://127.0.0.1:${PORT}/" && break
  kill -0 "$SRV" 2>/dev/null || { echo "Server failed to start (port ${PORT} busy?)."; exit 1; }
  sleep 0.25
done

( xdg-open "http://127.0.0.1:${PORT}" >/dev/null 2>&1 ) || echo "Open http://127.0.0.1:${PORT} in your browser."
wait "$SRV"

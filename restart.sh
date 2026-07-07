#!/bin/zsh
# Reliable restart for the LiteLLM gateway.
#
# Why this exists: `litellm --num_workers 1` runs as a manager process (tracked
# by launchd) PLUS a worker process that actually binds :4000. A plain
# `launchctl kickstart` kills the manager but can orphan the worker, which keeps
# serving the OLD config on :4000 while the new worker fails to bind (and falls
# back to a random port). This script tears everything down cleanly, then starts
# fresh — use it instead of `kickstart` whenever you change config/env.
set -uo pipefail
LABEL="com.msulleza.litellm"
DOMAIN="gui/$(id -u)"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

# 1. Remove the job so KeepAlive stops respawning while we clean up.
launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true

# 2. Kill manager + any orphaned worker until :4000 is actually free.
for i in {1..10}; do
  pkill -9 -f "litellm/venv" 2>/dev/null || true
  PIDS=$(lsof -tiTCP:4000 -sTCP:LISTEN 2>/dev/null || true)
  [ -n "$PIDS" ] && echo "$PIDS" | xargs kill -9 2>/dev/null || true
  sleep 1
  if ! lsof -tiTCP:4000 -sTCP:LISTEN >/dev/null 2>&1; then
    echo "restart.sh: :4000 free"
    break
  fi
done

# 3. Start fresh under launchd.
launchctl bootstrap "$DOMAIN" "$PLIST"

# 4. Wait for it to actually answer on :4000.
for i in {1..45}; do
  if [ "$(curl -s -m2 -o /dev/null -w '%{http_code}' http://127.0.0.1:4000/health/liveliness 2>/dev/null)" = "200" ]; then
    echo "restart.sh: proxy healthy on :4000 after ${i}s"
    exit 0
  fi
  sleep 1
done
echo "restart.sh: proxy NOT healthy after 45s — check ~/litellm/logs/litellm.err.log" >&2
exit 1

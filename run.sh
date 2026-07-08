#!/usr/bin/env bash
set -euo pipefail
source "$HOME/litellm/litellm.env"
cd "$HOME/litellm"

# Single worker: async server handles one user fine, keeps launchd
# supervision clean (multi-worker forks orphan on parent death), and
# rpm/tpm limits apply once instead of per-worker.
exec "$HOME/litellm/venv/bin/litellm" \
  --config "$HOME/litellm/config.yaml" \
  --host 127.0.0.1 --port 4000 \
  --num_workers 1

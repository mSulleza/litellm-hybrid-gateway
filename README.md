# litellm-hybrid-gateway

A local [LiteLLM](https://github.com/BerriAI/litellm) proxy that runs **Claude Code
on a hybrid model stack** — an expensive *agentic brain* orchestrating cheap
*executor hands* — plus a live cost/usage dashboard with per-session tracking.

> Plain `claude` stays on your Claude Max subscription. This proxy only affects
> sessions launched through `claude-hybrid.sh`.

## Architecture

Role-based routing — best agentic planner up top, cheapest capable coder below:

| Role | Model | Backend | Cost |
|------|-------|---------|------|
| **Main loop / planner** | `glm-planner` | GLM-5.2 (Z.ai) | pay-per-token, ~$0.14/M blended |
| **Heavy executor** (`model: sonnet`) | `ds-coder` | DeepSeek V4 Pro | best cheap coder (SWE-Verified ~80) |
| **Mechanical executor** (`model: haiku`) | `local-coder` | Qwen3-Coder-30B via LM Studio | **free** (local) |
| **Frontier escalation** (`/model opus`) | `claude-smart` | Anthropic Sonnet 5 | pay-per-token |

Insight: DeepSeek is the best *cheap coder* but a weak *planner* (agentic 59 vs
GLM 77) — so GLM plans and delegates, DeepSeek/Qwen execute. The launcher injects
a delegation policy telling the planner to push execution down by default (see
`agents/`).

## Files

| Path | What |
|------|------|
| `config.yaml` | LiteLLM model definitions + per-token prices |
| `custom_callbacks.py` | Language pinner, smart router, and cost logger (writes `usage.db`, incl. **per-session** attribution) |
| `claude-hybrid.sh` | Launch Claude Code through the proxy (aliased `local-code`) |
| `run.sh` | Start the proxy (single worker, :4000) |
| `restart.sh` | Clean restart (macOS/launchd — see portability) |
| `usage.sh` | CLI cost summary |
| `dashboard.py` / `dashboard.sh` | Live HTML dashboard on :4040 (aliased `local-usage`) |
| `litellm.env.example` | Template for secrets — copy to `litellm.env` |
| `deploy/*.plist` | macOS launchd autostart unit |
| `agents/*.md` | Delegation subagents → copy to `~/.claude/agents/` |

## Setup on a new machine

Assumes the repo is cloned to `~/litellm` (the scripts hardcode `$HOME/litellm`).

```bash
git clone git@github.com:mSulleza/litellm-hybrid-gateway.git ~/litellm
cd ~/litellm

# 1. Python env + LiteLLM
python3 -m venv venv
./venv/bin/pip install 'litellm[proxy]'

# 2. Secrets
cp litellm.env.example litellm.env
$EDITOR litellm.env          # fill in the three keys (Anthropic optional)

# 3. Delegation subagents for Claude Code
mkdir -p ~/.claude/agents
cp agents/*.md ~/.claude/agents/

# 4. (Optional, for the free local tier) LM Studio + Qwen
#    Install LM Studio, then:
#    lms load qwen/qwen3-coder-30b --context-length 65536 --gpu max
#    (65536 ctx is required — the default 8192 channel-errors and silently
#     falls back to DeepSeek, so you get no savings.)

# 5. Start the proxy
./run.sh                      # foreground; or set up autostart (below)

# 6. Shell aliases (zsh/bash)
echo 'alias local-code="$HOME/litellm/claude-hybrid.sh"' >> ~/.zshrc
echo 'alias local-usage="$HOME/litellm/dashboard.sh"'   >> ~/.zshrc
```

Launch Claude Code on the hybrid stack:

```bash
local-code            # or ~/litellm/claude-hybrid.sh  (args pass through, e.g. --resume)
local-usage           # open the cost dashboard
./usage.sh            # CLI summary
```

## Autostart

**macOS** — `deploy/com.msulleza.litellm.plist` (edit the hardcoded `/Users/msulleza`
paths to match the new user), then:

```bash
cp deploy/com.msulleza.litellm.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.msulleza.litellm.plist
# thereafter, restart cleanly with:  ./restart.sh
```

**Linux (systemd user unit)** — the plist won't work; create
`~/.config/systemd/user/litellm.service`:

```ini
[Unit]
Description=LiteLLM hybrid gateway
[Service]
ExecStart=%h/litellm/run.sh
Restart=on-failure
[Install]
WantedBy=default.target
```

```bash
systemctl --user enable --now litellm.service
```

## Portability notes (Z Flow 13)

The helper scripts are macOS-shaped. On **Linux/Windows** adapt:

- `run.sh` / `claude-hybrid.sh` — swap the `#!/bin/zsh` shebang if zsh isn't
  installed; the logic is portable (`$HOME`-relative).
- `restart.sh` — macOS-only (`launchctl`, `lsof`). On Linux use the systemd
  unit above + `systemctl --user restart litellm`.
- `dashboard.sh` — uses `open` (macOS). On Linux replace with `xdg-open`; on
  Windows use `start`. Or just run `python3 dashboard.py` and browse to
  `http://127.0.0.1:4040` manually.
- LM Studio path in `claude-hybrid.sh` (`~/.lmstudio/bin/lms`) — adjust to
  wherever LM Studio installs its CLI, or drop the free Qwen tier entirely.

## Per-session cost tracking

Claude Code stamps each request with its session id (via Anthropic
`metadata.user_id`); the cost logger records it in `usage.db`. The dashboard's
**"Cost by session"** table and **Sessions** KPI break spend down per Claude Code
session. `usage.db` is gitignored — it's per-machine runtime data.

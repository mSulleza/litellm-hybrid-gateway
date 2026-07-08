#!/usr/bin/env bash
# Launch Claude Code through the local LiteLLM gateway (Qwen + DeepSeek).
# Plain `claude` stays on your Claude Max subscription — this script only
# affects the session it launches.
#
# Orchestration policy: expensive AGENTIC brain, cheap hands.
#   MAIN LOOP (planner/delegator) → glm-planner (GLM-5.2) — near-Opus agentic
#     orchestration (MCP-Atlas 77 vs 77.8) at a fraction of frontier cost.
#   EXECUTOR SUBAGENTS → chosen per agent via each .claude/agents/*.md
#     `model:` frontmatter, mapped through the env vars below:
#        model: haiku   → local-coder (Qwen, free)   mechanical execution
#        model: sonnet  → ds-coder    (DeepSeek)      heavier execution (best cheap coder)
#        model: inherit → glm-planner (GLM-5.2)       same as main
#   Escalate to the Anthropic frontier anytime with /model opus (Sonnet 5).
set -euo pipefail
source "$HOME/litellm/litellm.env"

export ANTHROPIC_BASE_URL="http://127.0.0.1:4000"
export ANTHROPIC_AUTH_TOKEN="${LITELLM_MASTER_KEY}"

# Main planner = GLM-5.2, but degrade to DeepSeek if the Z.ai key isn't set yet
# (the main loop can't run without its model — don't brick the session).
if grep -q "ZAI_API_KEY" "$HOME/litellm/litellm.env" 2>/dev/null; then
  export ANTHROPIC_MODEL="glm-planner"
else
  export ANTHROPIC_MODEL="ds-coder"
  echo "Note: ZAI_API_KEY not in litellm.env — main planner temporarily on DeepSeek." >&2
  echo "      Add a z.ai/bigmodel key + restart proxy to run GLM-5.2 as planner." >&2
fi
export ANTHROPIC_DEFAULT_OPUS_MODEL="claude-smart"   # model:opus / /model opus → Sonnet 5
export ANTHROPIC_DEFAULT_SONNET_MODEL="ds-coder"     # model:sonnet → DeepSeek (heavy exec)
export ANTHROPIC_DEFAULT_HAIKU_MODEL="local-coder"   # model:haiku → Qwen (mechanical exec)
export ANTHROPIC_SMALL_FAST_MODEL="local-coder"      # background small/fast → Qwen

# Warn (don't block) if the Anthropic key is missing — tier 2 will 401 until set.
if ! grep -q "ANTHROPIC_API_KEY" "$HOME/litellm/litellm.env" 2>/dev/null; then
  echo "Note: ANTHROPIC_API_KEY not in litellm.env — /model opus (claude-smart) will 401." >&2
  echo "      Add a console.anthropic.com API key, then restart the proxy." >&2
fi

# Local Qwen is served by llama.cpp on :8080 (Strix Halo iGPU). Just check it's
# up — if not, local requests fall back to DeepSeek (see config.yaml fallbacks).
if ! curl -s -m 3 -o /dev/null "http://127.0.0.1:8080/v1/models"; then
  echo "Note: local llama.cpp not responding on :8080 — local-coder requests will" >&2
  echo "      fall back to DeepSeek (no free tier). Start your llama.cpp server." >&2
fi

# Proxy is required — fail fast with a clear message instead of hanging.
if ! curl -s -m 2 -o /dev/null "http://127.0.0.1:4000/health/liveliness"; then
  echo "LiteLLM proxy not running on :4000. Start it first: ~/litellm/run.sh" >&2
  exit 1
fi

# AUTO-DELEGATION policy — injected ONLY for gateway sessions (plain `claude`
# on Claude Max is untouched). Tells the GLM planner to push execution down to
# the cheap tiers by default: mechanical work → qwen-grunt (free), heavy
# code-writing → deepseek-coder (half the planner's output price). Planning,
# ambiguous debugging, and orchestration stay on GLM. Agents live in
# ~/.claude/agents/{qwen-grunt,deepseek-coder}.md.
read -r -d '' DELEGATION_POLICY <<'POLICY' || true
COST-OPTIMIZED DELEGATION (LiteLLM hybrid gateway active). You are the GLM planner; cheap executor subagents are available. Delegate execution DOWN by default instead of doing it all yourself:
- Mechanical / bulk / well-specified work (apply a decided edit across files, rename or move, format/lint, run tests or builds and report, boilerplate, search-and-collate) → spawn the `qwen-grunt` subagent (free local model).
- Heavy code-writing from a clear spec (new file/module/component, a defined multi-file refactor, implementing a specified interface) → spawn the `deepseek-coder` subagent (half your output cost).
Keep for YOURSELF: planning, architecture, ambiguous or exploratory debugging, deciding what to change, orchestrating subagents, and final review of what they return.
Rules: give each subagent a precise, self-contained brief (they are weaker planners than you). Do NOT delegate trivial 1–2 line edits or quick reads — handoff overhead outweighs the saving. Verify/spot-check what executors hand back before trusting it. When work is ambiguous, do it yourself rather than delegating a guess.
POLICY

exec claude --append-system-prompt "$DELEGATION_POLICY" "$@"

"""LiteLLM custom callbacks for hybrid Qwen + DeepSeek gateway.

- language_pinner: forces English-only output on local models
- smart_router: escalates complex local-coder requests to DeepSeek V4 Pro

Claude Code talks to the proxy via /v1/messages (anthropic format):
system prompt lives in data["system"], messages contain only user/assistant
roles. OpenAI-style /chat/completions puts system in messages[0]. Both hooks
handle both shapes.
"""

import os
import re
import json
from litellm.integrations.custom_logger import CustomLogger

_ENGLISH_INSTRUCTION = (
    "\n\nIMPORTANT: You MUST respond in English only. "
    "Never use Chinese characters, Chinese words, or any non-English language. "
    "Your entire response including any thinking or reasoning must be in English."
)

# Bilingual models that can drift to Chinese under long context — pin to English.
# (Qwen and GLM are both Chinese-origin; the name "local" is historical.)
_LOCAL_MODEL_PATTERNS = re.compile(r"local-coder|qwen|haiku|glm")
_HOOKED_CALL_TYPES = ("completion", "acompletion", "anthropic_messages")

# Keywords that indicate a complex/code-heavy task better suited for DeepSeek
_COMPLEX_INDICATORS = [
    "rewrite", "refactor", "implement", "create file", "write a", "build a",
    "full redesign", "migrate", "restructure", "multi-file", "orchestrat",
    "redesign", "component", "architecture", "new page", "new section",
]

def _estimate_tokens(n_chars: int) -> int:
    """Estimate tokens from string length (~4 chars per token)."""
    return n_chars // 4

# Claude Code sends its session id inside the Anthropic `metadata.user_id`
# field, which litellm surfaces as kwargs["user"]. Current Claude Code sends it
# as a JSON blob: {"device_id":"..","account_uuid":"..","session_id":"<uuid>"}.
# Older versions used a flat `user_<hash>_account_<acct>_session_<uuid>` string.
# The regex handles both the JSON `"session_id":"<uuid>"` and the legacy
# `session_<uuid>` shapes; JSON parsing is tried first as it's exact.
# Matches both `session_id":"<uuid>"` (JSON) and legacy `session_<uuid>` — the
# `id":"` separator is optional so a bare `session_<uuid>` still captures.
_SESSION_RE = re.compile(r'session_(?:id"?\s*[:=]\s*"?)?([0-9a-fA-F][0-9a-fA-F-]{5,})')

def _session_id(user) -> str:
    """Extract a stable per-session id from the request's user_id string.
    Falls back to the whole user string, then '' (logged as unknown)."""
    if not user or not isinstance(user, str):
        return ""
    # Current format: user_id is a JSON object with a session_id field.
    try:
        sid = json.loads(user).get("session_id")
        if sid:
            return sid
    except (ValueError, TypeError, AttributeError):
        pass
    # Legacy flat string, or JSON we couldn't parse: pull session_id via regex.
    m = _SESSION_RE.search(user)
    return m.group(1) if m else user

def _blocks_text(content) -> str:
    """Flatten str-or-block-list content into plain text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out = ""
        for block in content:
            if isinstance(block, dict):
                out += str(block.get("text", ""))
        return out
    return ""

def _append_english(data: dict) -> None:
    """Append the English instruction to the system prompt, whichever shape it uses."""
    system = data.get("system")
    if system is not None:
        # anthropic /v1/messages shape
        if isinstance(system, str):
            if _ENGLISH_INSTRUCTION not in system:
                data["system"] = system + _ENGLISH_INSTRUCTION
        elif isinstance(system, list):
            for block in system:
                if isinstance(block, dict) and block.get("type") == "text":
                    if _ENGLISH_INSTRUCTION not in block.get("text", ""):
                        block["text"] = block["text"] + _ENGLISH_INSTRUCTION
                    return
            system.append({"type": "text", "text": _ENGLISH_INSTRUCTION.strip()})
        return

    messages = data.get("messages") or []
    if messages and messages[0].get("role") == "system":
        current = messages[0]["content"]
        if isinstance(current, str):
            if _ENGLISH_INSTRUCTION not in current:
                messages[0]["content"] = current + _ENGLISH_INSTRUCTION
        elif isinstance(current, list):
            for block in current:
                if isinstance(block, dict) and block.get("type") == "text":
                    if _ENGLISH_INSTRUCTION not in block.get("text", ""):
                        block["text"] = block["text"] + _ENGLISH_INSTRUCTION
                    break
    else:
        # OpenAI shape with no system message yet
        messages.insert(0, {"role": "system", "content": _ENGLISH_INSTRUCTION.strip()})
        data["messages"] = messages

def _strip_english(data: dict) -> None:
    """Remove the English instruction (used when escalating to DeepSeek)."""
    system = data.get("system")
    if isinstance(system, str):
        data["system"] = system.replace(_ENGLISH_INSTRUCTION, "").strip()
        return
    if isinstance(system, list):
        for block in system:
            if isinstance(block, dict) and _ENGLISH_INSTRUCTION in block.get("text", ""):
                block["text"] = block["text"].replace(_ENGLISH_INSTRUCTION, "").strip()
        # Clean up any blocks that became completely empty
        data["system"] = [
            b for b in system 
            if not (isinstance(b, dict) and b.get("type") == "text" and not b.get("text"))
        ]
        return

    messages = data.get("messages") or []
    if messages and messages[0].get("role") == "system":
        content = messages[0]["content"]
        if isinstance(content, str) and _ENGLISH_INSTRUCTION in content:
            messages[0]["content"] = content.replace(_ENGLISH_INSTRUCTION, "").strip()

def _last_user_text(messages: list) -> str:
    """Text of the most recent user turn that contains actual prose."""
    for m in reversed(messages):
        if m.get("role") != "user":
            continue
        content = m.get("content", "")
        if isinstance(content, str):
            if content.strip():
                return content
            continue
        if isinstance(content, list):
            text = "".join(
                b.get("text", "") for b in content
                if isinstance(b, dict) and b.get("type") == "text"
            )
            if text.strip():
                return text
    return ""

def _is_complex_task(messages: list) -> bool:
    """Detect if the current task warrants DeepSeek."""
    latest = _last_user_text(messages).lower()

    complexity_score = sum(1 for kw in _COMPLEX_INDICATORS if kw in latest)

    total_chars = sum(len(_blocks_text(m.get("content", ""))) for m in messages)
    total_tokens = _estimate_tokens(total_chars)

    # Qwen loaded at 65536 ctx; escalate before pre-call checks force it
    if total_tokens > 40000:
        return True  # Context near Qwen's window → DeepSeek
    if complexity_score >= 2:
        return True  # Current task clearly complex → DeepSeek
    if total_tokens > 15000 and complexity_score >= 1:
        return True  # Medium context + some complexity → DeepSeek

    return False  # Small and simple → Qwen

class LanguagePinner(CustomLogger):
    """Inject English-only instruction into system prompt for local models."""
    async def async_pre_call_hook(self, user_api_key_dict, cache, data: dict, call_type: str):
        if call_type not in _HOOKED_CALL_TYPES:
            return data

        if not _LOCAL_MODEL_PATTERNS.search(data.get("model", "")):
            return data

        _append_english(data)
        return data

# ── Optional glm-planner de-escalation (OFF by default) ─────────────────────
# Route TRIVIAL main-loop turns from GLM ($1.40/$0.26/$4.40) down to DeepSeek
# ($0.435/$0.003625/$0.87). Enable with GLM_DEESCALATE=1 in litellm.env.
#
# HONEST CAVEAT (why this is off by default): cost scales with CONTEXT SIZE, and
# this only moves SMALL, simple turns — i.e. the already-cheap ones. The
# expensive turns are big-context ones deep in a session, which stay on GLM by
# design (downgrading the planner's hard turns risks worse tool use → MORE turns
# → higher cost). So savings here are marginal. The real levers are behavioral:
# /clear between tasks and delegating reads/greps to subagents. This switch
# exists for the execution-heavy sessions where you'd accept DeepSeek anyway.
_GLM_DEESCALATE = os.environ.get("GLM_DEESCALATE", "").strip().lower() in ("1", "true", "yes")
# Turns with an estimated context below this go to DeepSeek (if simple). Small
# on purpose: only downgrade turns cheap enough that a wrong call costs little.
_DEESCALATE_MAX_TOKENS = int(os.environ.get("GLM_DEESCALATE_MAX_TOKENS", "6000"))

class SmartRouter(CustomLogger):
    """Escalate complex local-coder requests to DeepSeek V4 Pro; optionally
    de-escalate trivial glm-planner turns to DeepSeek (GLM_DEESCALATE=1)."""
    async def async_pre_call_hook(self, user_api_key_dict, cache, data: dict, call_type: str):
        if call_type not in _HOOKED_CALL_TYPES:
            return data

        model = data.get("model", "")
        messages = data.get("messages") or []
        if not messages:
            return data

        # Escalate: Qwen (local-coder) → DeepSeek when the task is complex/large.
        if "local-coder" in model:
            if _is_complex_task(messages):
                data["model"] = "ds-coder"
                _strip_english(data)  # DeepSeek doesn't need the language pin
            return data

        # De-escalate: GLM (glm-planner) → DeepSeek for small, simple turns only.
        if _GLM_DEESCALATE and "glm-planner" in model:
            total_tokens = _estimate_tokens(
                sum(len(_blocks_text(m.get("content", ""))) for m in messages))
            latest = _last_user_text(messages).lower()
            is_complex = any(kw in latest for kw in _COMPLEX_INDICATORS)
            if total_tokens < _DEESCALATE_MAX_TOKENS and not is_complex:
                data["model"] = "ds-coder"
                _strip_english(data)  # pinner already ran (glm matches); strip it

        return data

class CostLogger(CustomLogger):
    """Log every successful request's model, tokens, cost, and prompt snippet
    to a local SQLite DB (~/litellm/usage.db). Read it with ~/litellm/usage.sh.

    Cost comes from litellm's response_cost (computed from the per-token prices
    set in each model_info). Logging never breaks a request — all errors swallowed.
    """

    _DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "usage.db")

    def _write(self, row: dict) -> None:
        import sqlite3
        conn = sqlite3.connect(self._DB, timeout=5)
        try:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS usage ("
                "ts TEXT, model_group TEXT, model TEXT, "
                "input_tokens INTEGER, output_tokens INTEGER, "
                "cost REAL, prompt_snippet TEXT)"
            )
            # Migration: add cached_tokens for cache-hit visibility (idempotent).
            # Caching is what keeps GLM ~$0.14/M instead of list price — if it
            # silently breaks (as the Qwen ctx bug once did), cost 4x's. Track it.
            try:
                conn.execute("ALTER TABLE usage ADD COLUMN cached_tokens INTEGER DEFAULT 0")
            except sqlite3.OperationalError:
                pass  # column already exists
            # Migration: per-session attribution (idempotent). Session id comes
            # from Claude Code's metadata.user_id; old rows default '' (unknown).
            try:
                conn.execute("ALTER TABLE usage ADD COLUMN session_id TEXT DEFAULT ''")
            except sqlite3.OperationalError:
                pass  # column already exists
            conn.execute(
                "INSERT INTO usage "
                "(ts, model_group, model, input_tokens, output_tokens, cost, "
                " prompt_snippet, cached_tokens, session_id) VALUES (?,?,?,?,?,?,?,?,?)",
                (row["ts"], row["model_group"], row["model"], row["input_tokens"],
                 row["output_tokens"], row["cost"], row["prompt_snippet"],
                 row["cached_tokens"], row["session_id"]),
            )
            conn.commit()
        finally:
            conn.close()

    def _record(self, kwargs: dict, response_obj, end_time) -> None:
        try:
            model_group = kwargs.get("model_group") or kwargs.get("model") or ""
            model = kwargs.get("model") or ""

            usage = getattr(response_obj, "usage", None)
            if usage is None and isinstance(response_obj, dict):
                usage = response_obj.get("usage")

            def _u(key):
                if usage is None:
                    return 0
                v = getattr(usage, key, None)
                if v is None and isinstance(usage, dict):
                    v = usage.get(key)
                return int(v or 0)

            in_tok, out_tok = _u("prompt_tokens"), _u("completion_tokens")

            # Cached prompt tokens (litellm normalizes providers into
            # usage.prompt_tokens_details.cached_tokens). 0 if provider omits it.
            cached = 0
            details = getattr(usage, "prompt_tokens_details", None)
            if details is None and isinstance(usage, dict):
                details = usage.get("prompt_tokens_details")
            if details is not None:
                cached = getattr(details, "cached_tokens", None)
                if cached is None and isinstance(details, dict):
                    cached = details.get("cached_tokens")
            cached = int(cached or 0)

            cost = kwargs.get("response_cost")
            if cost is None:
                try:
                    import litellm
                    cost = litellm.completion_cost(completion_response=response_obj)
                except Exception:
                    cost = 0.0
            cost = float(cost or 0.0)

            # Latest user prose, truncated (verbose logging).
            snippet = ""
            for m in reversed(kwargs.get("messages") or []):
                if m.get("role") != "user":
                    continue
                c = m.get("content", "")
                snippet = c if isinstance(c, str) else _blocks_text(c)
                if snippet.strip():
                    break
            snippet = " ".join(snippet.split())[:200]

            session_id = _session_id(kwargs.get("user"))

            ts = end_time.isoformat() if hasattr(end_time, "isoformat") else str(end_time)
            self._write({
                "ts": ts, "model_group": model_group, "model": model,
                "input_tokens": in_tok, "output_tokens": out_tok, "cost": cost,
                "prompt_snippet": snippet, "cached_tokens": cached,
                "session_id": session_id,
            })
        except Exception:
            pass  # never let logging break a request

    def log_success_event(self, kwargs, response_obj, start_time, end_time):
        self._record(kwargs, response_obj, end_time)

    async def async_log_success_event(self, kwargs, response_obj, start_time, end_time):
        self._record(kwargs, response_obj, end_time)


language_pinner = LanguagePinner()
smart_router = SmartRouter()
cost_logger = CostLogger()
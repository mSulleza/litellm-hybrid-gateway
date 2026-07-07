#!/bin/zsh
# Cost & usage summary for the LiteLLM gateway.
# Reads ~/litellm/usage.db (written by the cost_logger callback).
#   usage.sh          → today + last 7 days + by-model all-time + recent requests
#   usage.sh recent   → last 25 requests with prompt snippets
exec "$HOME/litellm/venv/bin/python3" - "${1:-summary}" <<'PY'
import os, sqlite3, sys

DB = os.path.join(os.path.dirname(os.path.abspath(__file__)) if "__file__" in dir() else os.path.expanduser("~/litellm"), "usage.db")
DB = os.path.expanduser("~/litellm/usage.db")
mode = sys.argv[1] if len(sys.argv) > 1 else "summary"

if not os.path.exists(DB):
    print("No usage yet — usage.db not created. Make some requests through the gateway first.")
    sys.exit(0)

c = sqlite3.connect(DB)
c.row_factory = sqlite3.Row

def money(x): return f"${x:,.6f}"
def num(x):   return f"{x:,}"
def short(m): return (m or "?").split("/")[-1]   # zai/glm-5.2 → glm-5.2 (real backend)

if mode == "recent":
    rows = c.execute("SELECT ts, model_group, cost, input_tokens, output_tokens, prompt_snippet "
                     "FROM usage ORDER BY ts DESC LIMIT 25").fetchall()
    print("\n=== Last 25 requests ===")
    for r in rows:
        print(f"{r['ts'][:19]}  {short(r['model_group']):<16} {money(r['cost']):>12}  "
              f"{r['input_tokens']:>7}in {r['output_tokens']:>6}out  {r['prompt_snippet'][:70]}")
    sys.exit(0)

# ---- summary ----
tot = c.execute("SELECT COUNT(*) n, COALESCE(SUM(cost),0) c, "
                "COALESCE(SUM(input_tokens),0) i, COALESCE(SUM(output_tokens),0) o FROM usage").fetchone()
today = c.execute("SELECT COUNT(*) n, COALESCE(SUM(cost),0) c FROM usage WHERE ts >= date('now','localtime')").fetchone()

# Cache-hit ratio — the lever that keeps GLM ~$0.14/M. Column may not exist on
# pre-migration DBs, so probe first.
has_cached = any(r[1] == "cached_tokens"
                 for r in c.execute("PRAGMA table_info(usage)").fetchall())
cached_tot = (c.execute("SELECT COALESCE(SUM(cached_tokens),0) FROM usage").fetchone()[0]
              if has_cached else 0)

print("\n=== Gateway usage ===")
print(f"All-time : {num(tot['n'])} requests, {money(tot['c'])}, "
      f"{num(tot['i'])} in + {num(tot['o'])} out tokens")
if has_cached and tot['i']:
    hit = 100.0 * cached_tot / tot['i']
    print(f"Cache hit: {hit:.0f}% of input tokens ({num(cached_tot)} cached) "
          f"— higher = cheaper; watch for drops")
print(f"Today    : {num(today['n'])} requests, {money(today['c'])}")

print("\n--- Last 7 days ---")
days = c.execute("SELECT substr(ts,1,10) d, COUNT(*) n, COALESCE(SUM(cost),0) c "
                 "FROM usage WHERE ts >= date('now','localtime','-6 days') "
                 "GROUP BY d ORDER BY d DESC").fetchall()
if days:
    for r in days:
        print(f"  {r['d']}   {num(r['n']):>5} req   {money(r['c'])}")
else:
    print("  (none)")

print("\n--- By backend model (all-time) ---")
mods = c.execute("SELECT model_group, COUNT(*) n, COALESCE(SUM(cost),0) c, "
                 "COALESCE(SUM(input_tokens),0) i, COALESCE(SUM(output_tokens),0) o "
                 "FROM usage GROUP BY model_group ORDER BY c DESC").fetchall()
for r in mods:
    print(f"  {short(r['model_group']):<17} {num(r['n']):>5} req   {money(r['c']):>13}   "
          f"{num(r['i']):>10} in  {num(r['o']):>9} out")

print("\n(usage.sh recent → last 25 requests with prompts)\n")
PY

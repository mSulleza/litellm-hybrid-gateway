#!/usr/bin/env python3
"""Local HTML dashboard for the LiteLLM gateway usage DB (~/litellm/usage.db).

Serves a live, auto-refreshing page at http://127.0.0.1:4040 with cost/token
breakdowns by model, daily + hourly trends, a per-model efficiency table, and a
recent-requests table.

  python3 dashboard.py                      # serve live (default :4040)
  python3 dashboard.py --port 5050          # alternate port
  python3 dashboard.py --export out.html    # write a static snapshot, then exit

Data comes from the same usage.db that ~/litellm/usage.sh reads (written by the
cost_logger callback in custom_callbacks.py). Only SUCCESSFUL requests are
logged, so totals understate real traffic; cost is computed from the per-token
prices in each model_info, not billed by the upstream provider.

"Today" uses date('now','localtime') to match usage.sh exactly.
"""
import argparse
import json
import os
import sqlite3
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "usage.db")

# Distinct, dark-friendly palette. Models are colored in cost-desc order, so the
# biggest spender always gets the same first color across views.
PALETTE = [
    "#60a5fa", "#f472b6", "#34d399", "#fbbf24", "#a78bfa",
    "#fb7185", "#22d3ee", "#f59e0b", "#4ade80", "#e879f9",
]


def short(model):
    """zai/glm-5.2 -> glm-5.2 (data is usually already bare, but be safe)."""
    return (model or "?").split("/")[-1]


def _has_col(conn, col, table="usage"):
    """True if `table` has column `col` (guards the session migration)."""
    return any(r[1] == col for r in conn.execute(f"PRAGMA table_info({table})"))


def gather():
    """Read usage.db and return one dict with every stat the frontend needs."""
    out = {"generated_at": datetime.now().isoformat(timespec="seconds")}
    if not os.path.exists(DB):
        out["empty"] = True
        out["totals"] = out["today"] = {"requests": 0, "cost": 0}
        out["by_model"] = out["daily"] = out["hourly_today"] = out["recent"] = []
        out["by_session"] = []
        return out

    c = sqlite3.connect(DB, timeout=5)
    c.row_factory = sqlite3.Row
    try:
        today_row = c.execute("SELECT date('now','localtime') d").fetchone()
        today = today_row["d"]

        # ---- totals ----
        t = c.execute(
            "SELECT COUNT(*) n, COALESCE(SUM(cost),0) c, "
            "COALESCE(SUM(input_tokens),0) i, COALESCE(SUM(output_tokens),0) o FROM usage"
        ).fetchone()
        out["totals"] = {
            "requests": t["n"],
            "cost": t["c"],
            "input_tokens": t["i"],
            "output_tokens": t["o"],
            "total_tokens": t["i"] + t["o"],
            "avg_cost_per_req": (t["c"] / t["n"]) if t["n"] else 0.0,
        }

        # ---- today ----
        td = c.execute(
            "SELECT COUNT(*) n, COALESCE(SUM(cost),0) c FROM usage "
            "WHERE substr(ts,1,10) = date('now','localtime')"
        ).fetchone()
        out["today"] = {"date": today, "requests": td["n"], "cost": td["c"]}

        # ---- by model (cost-desc) ----
        mods = c.execute(
            "SELECT model_group, COUNT(*) n, COALESCE(SUM(cost),0) c, "
            "COALESCE(SUM(input_tokens),0) i, COALESCE(SUM(output_tokens),0) o "
            "FROM usage GROUP BY model_group ORDER BY c DESC"
        ).fetchall()
        total_cost = t["c"] or 1.0
        total_req = t["n"] or 1.0
        by_model = []
        for i, r in enumerate(mods):
            tok = r["i"] + r["o"]
            by_model.append({
                "model": r["model_group"],
                "requests": r["n"],
                "cost": r["c"],
                "input_tokens": r["i"],
                "output_tokens": r["o"],
                "total_tokens": tok,
                "pct_cost": r["c"] / total_cost,
                "pct_requests": r["n"] / total_req,
                "cost_per_1k": (r["c"] / tok * 1000.0) if tok else 0.0,
                "color": PALETTE[i % len(PALETTE)],
            })
        out["by_model"] = by_model
        color_of = {m["model"]: m["color"] for m in by_model}

        # ---- daily, last 14 days (fill missing days with 0) ----
        dmap = {}
        for r in c.execute(
            "SELECT substr(ts,1,10) d, COUNT(*) n, COALESCE(SUM(cost),0) c FROM usage "
            "WHERE substr(ts,1,10) >= date('now','localtime','-13 days') "
            "GROUP BY d"
        ).fetchall():
            dmap[r["d"]] = (r["n"], r["c"])
        daily = []
        anchor = datetime.strptime(today, "%Y-%m-%d")
        for k in range(13, -1, -1):
            d = (anchor - timedelta(days=k)).strftime("%Y-%m-%d")
            n, cost = dmap.get(d, (0, 0.0))
            daily.append({"date": d, "requests": n, "cost": cost})
        out["daily"] = daily

        # ---- hourly today (24 buckets, fill missing with 0) ----
        hmap = {}
        for r in c.execute(
            "SELECT CAST(substr(ts,12,2) AS INT) h, COUNT(*) n, COALESCE(SUM(cost),0) c "
            "FROM usage WHERE substr(ts,1,10) = date('now','localtime') GROUP BY h"
        ).fetchall():
            hmap[r["h"]] = (r["n"], r["c"])
        out["hourly_today"] = [
            {"hour": h, "label": f"{h:02d}", "requests": hmap.get(h, (0, 0.0))[0],
             "cost": hmap.get(h, (0, 0.0))[1]}
            for h in range(24)
        ]

        # ---- by session (Claude Code session id from metadata.user_id) ----
        # NULL/'' = requests logged before session tracking existed, or callers
        # that don't send metadata.user_id. Bucketed as "unknown".
        sess_rows = c.execute(
            "SELECT COALESCE(NULLIF(session_id,''),'unknown') sid, "
            "COUNT(*) n, COALESCE(SUM(cost),0) c, "
            "COALESCE(SUM(input_tokens),0) i, COALESCE(SUM(output_tokens),0) o, "
            "COALESCE(SUM(cached_tokens),0) cached, "
            "MIN(ts) first_ts, MAX(ts) last_ts, "
            "GROUP_CONCAT(DISTINCT model_group) models "
            "FROM usage GROUP BY sid ORDER BY last_ts DESC"
        ).fetchall() if _has_col(c, "session_id") else []
        by_session = []
        for r in sess_rows:
            tok = r["i"] + r["o"]
            mods_list = [short(m) for m in (r["models"] or "").split(",") if m]
            sid = r["sid"]
            label = "unknown" if sid == "unknown" else (
                sid[-8:] if len(sid) > 8 else sid)
            by_session.append({
                "session": sid,
                "label": label,
                "requests": r["n"],
                "cost": r["c"],
                "input_tokens": r["i"],
                "output_tokens": r["o"],
                "cached_tokens": r["cached"],
                "total_tokens": tok,
                "pct_cost": (r["c"] / total_cost) if total_cost else 0.0,
                "first_ts": r["first_ts"],
                "last_ts": r["last_ts"],
                "started": (r["first_ts"][:16].replace("T", " ")) if r["first_ts"] else "",
                "models": mods_list,
                "colors": [color_of.get(m2, "#64748b")
                           for m2 in (r["models"] or "").split(",") if m2],
            })
        out["by_session"] = by_session

        # ---- recent (last 50) ----
        recent = []
        for r in c.execute(
            "SELECT ts, model_group, input_tokens, output_tokens, cost, prompt_snippet "
            "FROM usage ORDER BY ts DESC LIMIT 50"
        ).fetchall():
            d = r["ts"][:10]
            hhmmss = r["ts"][11:19]
            time = hhmmss if d == today else f"{d[5:10]} {hhmmss[:5]}"
            recent.append({
                "ts": r["ts"], "time": time, "model": r["model_group"],
                "input_tokens": r["input_tokens"], "output_tokens": r["output_tokens"],
                "cost": r["cost"], "snippet": r["prompt_snippet"] or "",
                "color": color_of.get(r["model_group"], "#64748b"),
            })
        out["recent"] = recent
        out["empty"] = False
        return out
    finally:
        c.close()


HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>LiteLLM Gateway Usage</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script>
<style>
  :root{--bg:#0f1115;--panel:#171a21;--panel2:#1d2129;--border:#272b35;--text:#e6e8ee;--muted:#8b93a7;--accent:#60a5fa;}
  *{box-sizing:border-box;}
  body{margin:0;background:var(--bg);color:var(--text);font:14px/1.5 -apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;}
  .wrap{max-width:1200px;margin:0 auto;padding:24px 20px 60px;}
  header{display:flex;align-items:baseline;justify-content:space-between;margin-bottom:20px;flex-wrap:wrap;gap:8px;}
  h1{margin:0;font-size:20px;font-weight:600;} h1 span{color:var(--muted);font-weight:400;font-size:13px;}
  .meta{color:var(--muted);font-size:12px;} .meta a{color:var(--accent);text-decoration:none;cursor:pointer;}
  .kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px;margin-bottom:20px;}
  .kpi{background:var(--panel);border:1px solid var(--border);border-radius:10px;padding:14px 16px;}
  .kpi .label{color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.06em;}
  .kpi .val{font-size:22px;font-weight:600;margin-top:4px;} .kpi .sub{color:var(--muted);font-size:12px;margin-top:2px;}
  .grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:16px;margin-bottom:20px;}
  .card{background:var(--panel);border:1px solid var(--border);border-radius:10px;padding:16px;}
  .card h2{margin:0 0 12px;font-size:13px;color:var(--muted);text-transform:uppercase;letter-spacing:.06em;font-weight:600;}
  .card canvas{max-height:240px;}
  table{width:100%;border-collapse:collapse;font-size:13px;}
  th,td{text-align:left;padding:8px 10px;border-bottom:1px solid var(--border);}
  th{color:var(--muted);font-weight:600;font-size:11px;text-transform:uppercase;letter-spacing:.05em;}
  td.num,th.num{text-align:right;font-variant-numeric:tabular-nums;}
  .chip{display:inline-block;padding:2px 8px;border-radius:20px;font-size:12px;font-weight:500;}
  .snippet{color:var(--muted);max-width:380px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
  .bar{height:6px;background:var(--panel2);border-radius:3px;overflow:hidden;margin-top:4px;} .bar>div{height:100%;border-radius:3px;}
  .empty{color:var(--muted);padding:20px;text-align:center;}
  .scroll{max-height:480px;overflow-y:auto;}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>LiteLLM Gateway Usage <span>· local cost &amp; token analytics</span></h1>
    <div class="meta">Updated <span id="age">just now</span> · <a id="refresh">refresh</a> · auto every 10s</div>
  </header>
  <div class="kpis" id="kpis"></div>
  <div class="grid">
    <div class="card"><h2>Cost by model</h2><canvas id="costChart"></canvas></div>
    <div class="card"><h2>Requests by model</h2><canvas id="reqChart"></canvas></div>
    <div class="card"><h2>Daily cost (14 days)</h2><canvas id="dailyChart"></canvas></div>
    <div class="card"><h2>Hourly activity today</h2><canvas id="hourlyChart"></canvas></div>
  </div>
  <div class="grid">
    <div class="card"><h2>Model breakdown</h2><div id="modelTable"></div></div>
    <div class="card"><h2>Tokens by model</h2><canvas id="tokenChart"></canvas></div>
  </div>
  <div class="card"><h2>Cost by session</h2><div id="sessionTable" class="scroll"></div></div>
  <div class="card"><h2>Recent requests</h2><div id="recent" class="scroll"></div></div>
</div>
<script>
const fmt$=n=>'$'+(n||0).toLocaleString(undefined,{minimumFractionDigits:2,maximumFractionDigits:4});
const fmtN=n=>(n||0).toLocaleString();
const short=m=>(m||'?').split('/').pop();
let charts={}, lastUpdated=null;
function getStats(){ return window.__STATS__ ? Promise.resolve(window.__STATS__) : fetch('/api/stats').then(r=>r.json()); }
function kpis(t){
  const cards=[
    ['Total cost',fmt$(t.totals.cost), t.today.cost>0?fmt$(t.today.cost)+' today':'no spend today'],
    ['Requests',fmtN(t.totals.requests), fmtN(t.today.requests)+' today'],
    ['Tokens',fmtN(t.totals.total_tokens), fmtN(t.totals.input_tokens)+' in · '+fmtN(t.totals.output_tokens)+' out'],
    ['Avg $/request',fmt$(t.totals.avg_cost_per_req), fmtN(t.totals.total_tokens)+' tokens total'],
    ['Models used',fmtN(t.by_model.length), t.by_model.map(m=>short(m.model)).join(', ')||'—'],
    ['Sessions',fmtN((t.by_session||[]).filter(x=>x.session!=='unknown').length),
      (t.by_session&&t.by_session.length?fmt$(t.by_session[0].cost)+' top session':'no sessions tracked')]
  ];
  document.getElementById('kpis').innerHTML=cards.map(c=>
    `<div class="kpi"><div class="label">${c[0]}</div><div class="val">${c[1]}</div><div class="sub">${c[2]}</div></div>`).join('');
}
function dough(id,labels,data,colors,money){
  if(charts[id])charts[id].destroy();
  if(!window.Chart)return;
  charts[id]=new Chart(document.getElementById(id),{type:'doughnut',
    data:{labels,datasets:[{data,backgroundColor:colors,borderColor:'#171a21',borderWidth:2}]},
    options:{plugins:{legend:{position:'right',labels:{color:'#8b93a7',font:{size:11}}},
      tooltip:{callbacks:{label:c=>c.label+': '+(money?fmt$(c.raw):fmtN(c.raw))}}},cutout:'60%'}});
}
function render(s){
  kpis(s);
  const labels=s.by_model.map(m=>short(m.model)), colors=s.by_model.map(m=>m.color);
  dough('costChart',labels,s.by_model.map(m=>m.cost),colors,true);
  dough('reqChart',labels,s.by_model.map(m=>m.requests),colors,false);
  if(window.Chart){
    if(charts.daily)charts.daily.destroy();
    charts.daily=new Chart(document.getElementById('dailyChart'),{type:'bar',
      data:{labels:s.daily.map(d=>d.date.slice(5)),datasets:[{data:s.daily.map(d=>d.cost),backgroundColor:'#60a5fa'}]},
      options:{plugins:{legend:{display:false}},scales:{x:{ticks:{color:'#8b93a7'},grid:{display:false}},y:{ticks:{color:'#8b93a7',callback:v=>'$'+v},grid:{color:'#272b35'}}}}});
    if(charts.hourly)charts.hourly.destroy();
    charts.hourly=new Chart(document.getElementById('hourlyChart'),{type:'bar',
      data:{labels:s.hourly_today.map(h=>h.label),datasets:[{data:s.hourly_today.map(h=>h.requests),backgroundColor:'#34d399'}]},
      options:{plugins:{legend:{display:false}},scales:{x:{ticks:{color:'#8b93a7',autoSkip:true,maxTicksLimit:12},grid:{display:false}},y:{ticks:{color:'#8b93a7',stepSize:1},grid:{color:'#272b35'},beginAtZero:true}}}});
    if(charts.token)charts.token.destroy();
    charts.token=new Chart(document.getElementById('tokenChart'),{type:'bar',
      data:{labels,datasets:[{label:'Input',data:s.by_model.map(m=>m.input_tokens),backgroundColor:'#60a5fa'},
        {label:'Output',data:s.by_model.map(m=>m.output_tokens),backgroundColor:'#34d399'}]},
      options:{plugins:{legend:{labels:{color:'#8b93a7',font:{size:11}}}},scales:{x:{ticks:{color:'#8b93a7'},grid:{display:false}},y:{ticks:{color:'#8b93a7'},grid:{color:'#272b35'},beginAtZero:true}}}});
  }
  document.getElementById('modelTable').innerHTML=`<table><thead><tr><th>Model</th><th class="num">Req</th><th class="num">Cost</th><th class="num">$ /1K tok</th><th>Share</th></tr></thead><tbody>${s.by_model.map(m=>`<tr>
    <td><span class="chip" style="background:${m.color}22;color:${m.color}">${short(m.model)}</span></td>
    <td class="num">${fmtN(m.requests)}</td><td class="num">${fmt$(m.cost)}</td><td class="num">${fmt$(m.cost_per_1k)}</td>
    <td><div style="color:var(--muted);font-size:12px">${(m.pct_cost*100).toFixed(1)}% cost</div><div class="bar"><div style="width:${m.pct_cost*100}%;background:${m.color}"></div></div></td></tr>`).join('')}</tbody></table>`;
  const sess=s.by_session||[];
  if(!sess.length){ document.getElementById('sessionTable').innerHTML='<div class="empty">No sessions tracked yet.</div>'; }
  else { document.getElementById('sessionTable').innerHTML=`<table><thead><tr><th>Session</th><th>Started</th><th>Models</th><th class="num">Req</th><th class="num">In</th><th class="num">Out</th><th class="num">Cost</th><th>Share</th></tr></thead><tbody>${sess.map(x=>`<tr>
    <td title="${x.session}"><code style="font-size:12px">${x.label}</code></td>
    <td style="color:var(--muted)">${x.started}</td>
    <td>${x.models.map((m,i)=>`<span class="chip" style="background:${(x.colors[i]||'#64748b')}22;color:${x.colors[i]||'#64748b'};margin-right:3px">${m}</span>`).join('')}</td>
    <td class="num">${fmtN(x.requests)}</td><td class="num">${fmtN(x.input_tokens)}</td><td class="num">${fmtN(x.output_tokens)}</td><td class="num">${fmt$(x.cost)}</td>
    <td><div style="color:var(--muted);font-size:12px">${(x.pct_cost*100).toFixed(1)}%</div><div class="bar"><div style="width:${x.pct_cost*100}%;background:var(--accent)"></div></div></td></tr>`).join('')}</tbody></table>`; }
  if(!s.recent.length){ document.getElementById('recent').innerHTML='<div class="empty">No requests logged yet.</div>'; }
  else { document.getElementById('recent').innerHTML=`<table><thead><tr><th>Time</th><th>Model</th><th class="num">In</th><th class="num">Out</th><th class="num">Cost</th><th>Prompt</th></tr></thead><tbody>${s.recent.map(r=>`<tr>
    <td>${r.time}</td><td><span class="chip" style="background:${r.color}22;color:${r.color}">${short(r.model)}</span></td>
    <td class="num">${fmtN(r.input_tokens)}</td><td class="num">${fmtN(r.output_tokens)}</td><td class="num">${fmt$(r.cost)}</td>
    <td class="snippet" title="${(r.snippet||'').replace(/"/g,'&quot;')}">${r.snippet||''}</td></tr>`).join('')}</tbody></table>`; }
}
async function load(){
  try{ const s=await getStats(); lastUpdated=Date.now(); render(s); }
  catch(e){ document.getElementById('age').textContent='— connection error'; }
}
function tick(){ if(!lastUpdated)return; const s=Math.round((Date.now()-lastUpdated)/1000); document.getElementById('age').textContent=s<2?'just now':s+'s ago'; }
document.getElementById('refresh').onclick=load;
load();
if(!window.__STATIC__){ setInterval(load,10000); setInterval(tick,1000); }
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass  # silence default request logging

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _html(self, body, code=200):
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.split("?")[0] == "/api/stats":
            try:
                self._json(gather())
            except Exception as e:  # never 500 silently — show what broke
                self._json({"error": str(e)}, 500)
        elif self.path.split("?")[0] == "/":
            self._html(HTML.encode())
        else:
            self._html(b"not found", 404)


def export(path):
    """Write a static, self-contained HTML snapshot (no server needed)."""
    stats = gather()
    blob = json.dumps(stats)
    inject = f'<script>window.__STATIC__=true;window.__STATS__={blob};</script>'
    out = HTML.replace("<body>", "<body>" + inject, 1)
    with open(path, "w") as f:
        f.write(out)
    print(f"Wrote {path} ({len(out)} bytes) — open it directly in a browser.")


def main():
    ap = argparse.ArgumentParser(description="LiteLLM gateway usage dashboard")
    ap.add_argument("--port", type=int, default=4040)
    ap.add_argument("--export", metavar="FILE", help="write a static HTML snapshot and exit")
    args = ap.parse_args()

    if args.export:
        export(args.export)
        return

    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"Usage dashboard → http://127.0.0.1:{args.port}  (Ctrl-C to stop)")
    print(f"  reading {DB}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping.")
        srv.shutdown()


if __name__ == "__main__":
    main()

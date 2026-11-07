"""What each session cost, grouped by the form that ran it: loop rounds, interactive sessions, subagents.

    python session-cost.py [--since YYYYMMDD] [--project <text>] [--json <ccusage session --json output>]

Runs a pinned ccusage release through npx (or reads a saved output), joins every session with its transcript under
~/.claude/projects, and prints one row per session plus totals per group. Sessions whose first prompt is the same
(the rounds of one fresh-session loop) form one group; Agent/Task calls and subagent transcripts are counted per
session. Costs are API-price estimates, not actual charges or subscription-quota percentages.
Prompt-based grouping is heuristic; use session IDs and the run's usage log for exact attribution.
"""
import argparse
import json
import shutil
import subprocess
import sys
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path

ap = argparse.ArgumentParser()
ap.add_argument("--since", default=date.today().strftime("%Y%m%d"))
ap.add_argument("--project", default="", help="only transcripts whose project folder contains this text")
ap.add_argument("--json", help="saved output of `ccusage session --json`, instead of running it")
args = ap.parse_args()
try:
    if len(args.since) != 8 or not args.since.isascii() or not args.since.isdigit():
        raise ValueError
    datetime.strptime(args.since, "%Y%m%d")
except ValueError:
    ap.error("--since must be a valid date in YYYYMMDD format")
sys.stdout.reconfigure(encoding="utf-8")

if args.json:
    raw = Path(args.json).read_text(encoding="utf-8")
else:
    npx = shutil.which("npx")
    if not npx:
        sys.exit("session-cost: npx not found; install Node.js or use --json with saved ccusage output")
    result = subprocess.run([npx, "-y", "ccusage@20.0.37", "session", "--json", "--since", args.since],
                            capture_output=True, text=True, encoding="utf-8")
    if result.returncode:
        sys.exit(f"session-cost: ccusage failed: {result.stderr.strip()}")
    raw = result.stdout
sessions = json.loads(raw)["session"]
files = {f.stem: f for f in (Path.home() / ".claude" / "projects").glob("*/*.jsonl")}


def first_prompt_and_tools(f):
    prompt, agents, start = "", 0, ""
    for line in f.open(encoding="utf-8", errors="replace"):
        try:
            e = json.loads(line)
        except ValueError:
            continue
        start = start or e.get("timestamp", "")
        msg = e.get("message") or {}
        if e.get("type") == "user" and not prompt:
            c = msg.get("content")
            prompt = c if isinstance(c, str) else " ".join(x.get("text", "") for x in c or [] if isinstance(x, dict))
        if e.get("type") == "assistant":
            agents += sum(1 for c in msg.get("content") or [] if c.get("type") == "tool_use" and c.get("name") in ("Agent", "Task"))
    return " ".join(prompt.split())[:70], agents, start


rows = []
for s in sessions:
    f = files.get(s["period"])
    if not f or args.project not in f.parent.name:
        continue
    prompt, agents, start = first_prompt_and_tools(f)
    sub = f.with_suffix("") / "subagents"
    rows.append({"start": start[:16].replace("T", " "), "prompt": prompt, "id": s["period"][:8], "cost": s["totalCost"],
                 "tokens": s["totalTokens"], "models": "+".join(m.replace("claude-", "") for m in s["modelsUsed"]),
                 "agents": agents, "subagent_files": len(list(sub.glob("*.jsonl"))) if sub.exists() else 0})

same = defaultdict(list)
for r in rows:
    same[r["prompt"]].append(r)
for r in rows:
    r["group"] = f"loop: {r['prompt'][:45]}" if len(same[r["prompt"]]) > 1 and r["prompt"] else "interactive"

rows.sort(key=lambda r: r["start"])
for r in rows:
    print(f"{r['start']} UTC  {r['id']}  {r['models']:<18} US$ {r['cost']:7.2f}  {r['tokens'] / 1e6:6.1f} M tok  "
          f"agents {r['agents']} (files {r['subagent_files']})  {r['group'] if r['group'] != 'interactive' else 'interactive: ' + r['prompt'][:40]}")
groups = defaultdict(lambda: [0, 0.0, 0])
for r in rows:
    g = groups[r["group"]]
    g[0] += 1
    g[1] += r["cost"]
    g[2] += r["agents"]
print("\nper group")
for name, (n, cost, agents) in sorted(groups.items(), key=lambda x: -x[1][1]):
    print(f"  {name:<56} {n:3d} sessions  US$ {cost:7.2f}  avg {cost / n:5.2f}  agent calls {agents}")
print(f"  total US$ {sum(r['cost'] for r in rows):.2f} in {len(rows)} sessions")

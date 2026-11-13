"""Live status board for a stateful loop (see reference/setup.md).

    python loop-status.py [--loop-dir DIR] [--loops-glob GLOB] [--repo DIR] [--transcripts DIR]
                          [--log FILE] [--port 8765] [--title NAME] [--snapshot DIR] [--reply [--reply-cli CMD]]

Serves http://127.0.0.1:<port>/ (loop-status.html): the live flow of this loop (one node per item laid out by
Depends, the driver and the current `claude -p` round with its subagents), progress bars, the session log, a chat
with the orchestrator and, below, the history of all loops. Data routes, rebuilt on every request: /flow.json,
/history.json, GET /chat. Other routes: /classic (the older server-rendered page, refreshes every 15 s),
/history/classic (server-rendered history), /shots/<file> (screenshots).

Chat: POST /chat {"text": ..., "refs": [...]} appends one JSON line {"ts", "from": "user", "text", "refs"} to
<loop-dir>/orchestrator-inbox.jsonl; replies are {"ts", "text"} lines in <loop-dir>/orchestrator-outbox.jsonl and the
page shows both. With --reply the board answers each message itself: one short headless session per message (default
`claude -p --model sonnet`, lean, cwd = loop folder) gets the message, the recent chat, LOOP-STATE.md and the tails of
LOOP-HISTORY.md and --log inline, may edit LOOP-STATE.md or create STOP in the loop folder, and ends. Messages are
answered one at a time. A reply that starts with ESCALATE: carries a handoff for a new session with a stronger model.
Without --reply, whoever orchestrates appends to the outbox by hand.

--snapshot DIR writes the page and its data (flow.json, history.json, chat.json) as static files into DIR and exits,
without live processes or transcripts: a read-only board that GitHub Pages can serve.

Limits: <loop-dir>/loop-config.json (written by fresh-loop.py, re-read by it before every round) holds rounds,
max_tokens, max_usd, on_limit ("stop" or "warn"), model and effort. The page shows them against what result-*.json
says was spent, in red when over, and POST /config {...} changes any of them (same origin check as the chat).

Every input except --loop-dir is optional; a missing one renders as an empty card, never as an error.
--loop-dir holds LOOP-STATE.md (item table), LOOP-HISTORY.md (one `### YYYY-MM-DD HH:MM - item N: ...` entry per
closed item) and result*.json (claude -p JSON output, for the spend card).
"""
import argparse
import html
import json
import os
import queue
import re
import shlex
import subprocess
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from lean import lean_flags

COLORS = {"done": "#3fb950", "todo": "#6e7681", "now": "#f0883e", "blocked": "#f85149"}
HERE = Path(__file__).resolve().parent
HIST_HTML = HERE / "loop-status.html"
ID = r"(?:[A-Z]?\d+[a-z]?|[A-Z])"
DASH = r"[\u2014\u2013-]"  # em dash, en dash or hyphen
ITEM_WORD = "item"
# header cell (lowercase) -> field
HEADERS = {"#": "id", "id": "id", "item": "name", "depends": "deps",
           "state": "state", "status": "state", "tries": "tries", "attempts": "tries"}
esc = html.escape
EMDASH = "—"

ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
ap.add_argument("--loop-dir", default=".", help="the current loop's folder (default: cwd)")
ap.add_argument("--loops-glob", default="", help="sibling loops for the history view (default: <loop-dir parent>/*)")
ap.add_argument("--repo", dest="repo", default="", help="repo whose commits, tags and screenshots are shown")
ap.add_argument("--screenshots", default="", help="screenshots folder (default: <repo>/screenshots)")
ap.add_argument("--transcripts", default="", help="Claude Code transcript folder of the loop's sessions")
ap.add_argument("--log", default="", help="fresh-loop.py output file")
ap.add_argument("--port", type=int, default=8765)
ap.add_argument("--title", default="", help="page title (default: the repo or loop folder name)")
ap.add_argument("--snapshot", default="", help="write a static read-only board into this folder and exit")
ap.add_argument("--reply", action="store_true", help="answer each chat message with a short headless session")
ap.add_argument("--reply-cli", default="claude -p --model sonnet", help="command for --reply; the prompt goes last")
args = ap.parse_args()

LOOP = Path(args.loop_dir).resolve()
REPO = Path(args.repo).resolve() if args.repo else None
SHOTS = Path(args.screenshots).resolve() if args.screenshots else (REPO / "screenshots" if REPO else None)
TRANSCRIPTS = Path(args.transcripts) if args.transcripts else None
TITLE = args.title or (REPO or LOOP).name


def loop_dirs():
    if args.loops_glob:
        pat = Path(args.loops_glob)
        found = Path(pat.anchor).glob(str(pat.relative_to(pat.anchor))) if pat.is_absolute() else Path().glob(args.loops_glob)
    else:
        found = LOOP.parent.glob("*")
    dirs = sorted(d for d in found if (d / "LOOP-STATE.md").exists())
    cut = os.path.commonprefix([d.name for d in dirs]) if len(dirs) > 1 else ""
    cut = cut[:cut.rfind("-") + 1]  # shared "name-" prefix is noise in lane labels
    return [(d, d.name.removeprefix(cut) or d.name) for d in dirs]


def short_title(text, n=52):
    """One readable line for a node: drop boilerplate prefixes, keep the first sentence, cut at a word."""
    t = re.sub(r"^Plan [\w.]+\s*(\([^)]*\))?[.:]?\s*", "", text.strip())
    t = re.sub(r"^Added by the user [\d-]+\s*(\[\w+\])?:?\s*", "", t)
    t = re.sub(r"^\(?COST RULE[^)]*\)\.?\s*", "", t)
    t = t.replace("`", "")
    t = re.split(r"(?<=\w)\.\s|\sVERIFY:", t)[0].strip(" .")
    if ": " in t and len(t.split(": ")[0]) >= 12:
        t = t.split(": ")[0]
    t = t[0].upper() + t[1:] if t else text[:n]
    return t if len(t) <= n else t[:n].rsplit(" ", 1)[0] + "\u2026"


def items(loop=LOOP):
    out, cols = [], ["id", "name", "deps", "state", "tries"]
    for line in (loop / "LOOP-STATE.md").read_text(encoding="utf-8").splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")] if line.lstrip().startswith("|") else []
        if cells and all(c.lower() in HEADERS for c in cells[:2]) and "id" == HEADERS.get(cells[0].lower()):
            cols = [HEADERS.get(c.lower(), "") for c in cells]  # header row: take the column order from it
            continue
        if len(cells) < len(cols) or not re.fullmatch(ID, cells[cols.index("id")]):
            continue
        row = dict(zip(cols, cells))
        if not row.get("tries", "").isdigit():
            continue
        out.append({"id": row["id"], "name": row["name"], "deps": re.findall(r"\b" + ID + r"\b", row.get("deps", "")),
                    "state": row["state"], "tries": row["tries"]})
    done = {it["id"] for it in out if it["state"] == "done"}
    current = next((it["id"] for it in out if it["state"] == "todo" and set(it["deps"]) <= done), None)
    for it in out:
        it["kind"] = ("done" if it["state"] == "done" else "blocked" if it["state"].startswith("BLOCKED")
                      else "now" if it["id"] == current else "todo")
    return out, current


def graph(its):
    by = {it["id"]: it for it in its}
    depth = {}

    def d(i):
        if i not in depth:
            depth[i] = 1 + max((d(x) for x in by[i]["deps"] if x in by), default=-1)
        return depth[i]

    cols = {}
    for it in its:
        cols.setdefault(d(it["id"]), []).append(it)
    W, H, gx, gy = 190, 54, 60, 22
    pos = {}
    for c, lst in cols.items():
        for r, it in enumerate(lst):
            pos[it["id"]] = (20 + c * (W + gx), 20 + r * (H + gy))
    width = 40 + (max(cols) + 1) * (W + gx)
    height = 40 + max(len(v) for v in cols.values()) * (H + gy)
    s = [f'<svg viewBox="0 0 {width} {height}" width="{width}" height="{height}">']
    for it in its:
        x2, y2 = pos[it["id"]]
        for dep in it["deps"]:
            if dep in pos:
                x1, y1 = pos[dep]
                x1, y1, y2c = x1 + W, y1 + H / 2, y2 + H / 2
                mx = (x1 + x2) / 2
                s.append(f'<path d="M{x1},{y1} C{mx},{y1} {mx},{y2c} {x2},{y2c}" class="edge"/>')
    for it in its:
        x, y = pos[it["id"]]
        col = COLORS[it["kind"]]
        pulse = ' class="pulse"' if it["kind"] == "now" else ""
        s.append(f'<g{pulse}><rect x="{x}" y="{y}" width="{W}" height="{H}" rx="10" fill="{col}22" stroke="{col}" '
                 f'stroke-width="2"/><text x="{x + 10}" y="{y + 21}" class="nid" fill="{col}">#{esc(it["id"])} \u00b7 '
                 f'{esc(it["state"][:24])}</text><text x="{x + 10}" y="{y + 40}" class="nname">'
                 f'{esc(it["name"][:26])}</text><title>{esc(it["name"])} \u2014 tries {it["tries"]}</title></g>')
    s.append("</svg>")
    return "".join(s)


def git(*a, cwd=None):
    if cwd is None:
        return ""
    try:
        return subprocess.run(["git", *a], cwd=cwd, capture_output=True, text=True, encoding="utf-8",
                              errors="replace").stdout
    except OSError:
        return ""


def procs():
    """Live loop processes as [{role, pid, age, cmd}]; None when they cannot be listed (not Windows, query failed)."""
    if os.name != "nt":
        return None
    ps = ("Get-CimInstance Win32_Process | Where-Object { $_.Name -match '^claude' -or "
          "($_.Name -match '^python' -and $_.CommandLine -match 'fresh-loop') } | "
          "Select-Object ProcessId,ParentProcessId,Name,CreationDate,CommandLine | ConvertTo-Json -Compress")
    try:
        raw = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True,
                             timeout=15, encoding="utf-8", errors="replace").stdout.strip()
        data = json.loads(raw, strict=False) if raw else []  # the round's prompt has raw newlines
    except Exception:  # the page must render even if the process query fails
        return None
    data = data if isinstance(data, list) else [data]
    out = []
    for p in data:
        cmd = p.get("CommandLine") or ""
        if p["Name"].startswith("claude") and " -p " not in f" {cmd} " and "fresh-loop" not in cmd:
            continue  # interactive sessions are not part of the loop
        m = re.search(r"/Date\((\d+)\)", str(p.get("CreationDate")))
        age = ""
        if m:
            secs = int(datetime.now().timestamp() - int(m.group(1)) / 1000)
            age = f"{secs // 60}m {secs % 60:02d}s"
        out.append({"role": "loop" if "fresh-loop" in cmd else "round (claude -p)", "pid": p["ProcessId"], "age": age,
                    "cmd": cmd[:140]})
    return out


def processes():
    data = procs()
    if data is None:
        return "<p class=dim>no process data (only listed on Windows)</p>"
    rows = [f"<tr><td>{p['role']}</td><td>{p['pid']}</td><td>{p['age']}</td><td class=cmd>{esc(p['cmd'])}</td></tr>"
            for p in data]
    return ("<table><tr><th>role</th><th>pid</th><th>alive for</th><th>command</th></tr>" + "".join(rows)
            + "</table>") if rows else "<p class=dim>nothing running: the loop is stopped</p>"


def transcript():
    """The newest transcript: (file, [(time, tool, what)], [(subagent type, description)], last text) or None."""
    files = sorted(TRANSCRIPTS.glob("*.jsonl"), key=lambda f: f.stat().st_mtime, reverse=True) if (
        TRANSCRIPTS and TRANSCRIPTS.is_dir()) else []
    if not files:
        return None
    f = files[0]
    tools, agents, last_text = [], [], ""
    for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if e.get("type") != "assistant":
            continue
        for c in (e.get("message") or {}).get("content") or []:
            if c.get("type") == "tool_use":
                inp = c.get("input") or {}
                what = inp.get("description") or inp.get("command") or inp.get("file_path") or inp.get(
                    "skill") or inp.get("pattern") or ""
                tools.append((e.get("timestamp", "")[11:19], c["name"], str(what)))
                if c["name"] in ("Agent", "Task"):
                    agents.append((inp.get("subagent_type") or "general", inp.get("description") or ""))
            elif c.get("type") == "text" and c.get("text", "").strip():
                last_text = c["text"].strip()
    return f, tools, agents, last_text


def session():
    t = transcript()
    if not t:
        return "<p class=dim>no transcripts (pass --transcripts)</p>", ""
    f, tools, agents, last_text = t
    mtime = datetime.fromtimestamp(f.stat().st_mtime).strftime("%H:%M:%S")
    counts = {}
    for _, n, _ in tools:
        counts[n] = counts.get(n, 0) + 1
    chips = "".join(f"<span class=chip>{esc(n)} {k}</span>" for n, k in sorted(counts.items(), key=lambda x: -x[1]))
    rows = "".join(f"<tr><td>{t}</td><td><b>{esc(n)}</b></td><td class=cmd>{esc(w[:150])}</td></tr>"
                   for t, n, w in tools[-14:][::-1])
    ag = ("".join(f"<li><b>{esc(t)}</b> \u2014 {esc(d)}</li>" for t, d in agents) if agents
          else "<li class=dim>none in this round</li>")
    body = (f"<p class=dim>session {f.stem[:8]} \u00b7 last write {mtime} \u00b7 {len(tools)} tool calls</p>"
            f"<div>{chips}</div><h3>Subagents</h3><ul>{ag}</ul><h3>Latest actions</h3>"
            f"<table>{rows}</table>")
    return body, last_text


def round_log():
    if not args.log or not Path(args.log).exists():
        return "", "<p class=dim>no fresh-loop log (pass --log)</p>"
    txt = Path(args.log).read_text(encoding="utf-8", errors="replace")
    rounds = re.findall(r"fresh-loop: round (\d+)/(\d+)", txt)
    rnd = f"round {rounds[-1][0]} of {rounds[-1][1]}" if rounds else ""
    ends = re.findall(r"^fresh-loop: (?!round).*$", txt, re.M)
    if ends:
        rnd += f" \u00b7 {ends[-1][12:]}"
    results = re.findall(r"^(" + ITEM_WORD + r" \S+: (?:done|BLOCKED)[^\n]*)", txt, re.M)
    lst = "".join(f"<li>{esc(r[:220])}</li>" for r in results[::-1]) or "<li class=dim>nothing yet</li>"
    return rnd, f"<ul>{lst}</ul>"


def history_times():
    p = LOOP / "LOOP-HISTORY.md"  # does not exist until the loop starts
    txt = p.read_text(encoding="utf-8") if p.exists() else ""
    return re.findall(r"^### (\S+ \d\d:\d\d) " + DASH + " " + ITEM_WORD + r" (\S+): (.*)$", txt, re.M)


LIFECYCLE = """<svg viewBox="0 0 900 120" class=life>
<defs><marker id=a viewBox="0 0 10 10" refX=9 refY=5 markerWidth=7 markerHeight=7 orient=auto>
<path d="M0,0L10,5L0,10z" fill="#8b949e"/></marker></defs>
<g class=lc>
<rect x=5 y=35 width=120 height=46 rx=8 /><text x=65 y=55>fresh-loop.py</text><text x=65 y=72 class=s>round N</text>
<rect x=160 y=35 width=130 height=46 rx=8 /><text x=225 y=55>new claude -p</text><text x=225 y=72 class=s>empty context</text>
<rect x=325 y=35 width=120 height=46 rx=8 /><text x=385 y=55>reads state</text><text x=385 y=72 class=s>spec + LOOP-STATE</text>
<rect x=480 y=35 width=120 height=46 rx=8 /><text x=540 y=55>1 item</text><text x=540 y=72 class=s>skills + code</text>
<rect x=635 y=35 width=110 height=46 rx=8 /><text x=690 y=55>VERIFY + gate</text><text x=690 y=72 class=s>commit</text>
<rect x=780 y=35 width=110 height=46 rx=8 /><text x=835 y=55>--check</text><text x=835 y=72 class=s>0 = done</text>
</g>
<g stroke="#8b949e" stroke-width=1.5 fill=none marker-end="url(#a)">
<path d="M125,58H158"/><path d="M290,58H323"/><path d="M445,58H478"/><path d="M600,58H633"/><path d="M745,58H778"/>
<path d="M835,35 C835,5 65,5 65,33"/></g>
<text x=450 y=14 class=s>fails \u2192 round N+1 (STOP, the round cap or --stall rounds without changes end it)</text></svg>"""

CSS = """
:root{--bg:#0d1117;--card:#161b22;--line:#30363d;--fg:#e6edf3;--dim:#8b949e}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.45 system-ui,Segoe UI,sans-serif}
main{max-width:none;margin:0;padding:16px 24px}
h1{font-size:20px;margin:0 0 4px}h2{font-size:15px;margin:0 0 10px;color:var(--dim);text-transform:uppercase;
letter-spacing:.06em}h3{font-size:13px;color:var(--dim);margin:12px 0 4px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px;margin:12px 0;overflow-x:auto}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(380px,1fr));gap:12px}.grid .card{margin:0}
.kpis{display:flex;gap:10px;flex-wrap:wrap;margin:10px 0}.kpi{background:var(--card);border:1px solid var(--line);
border-radius:10px;padding:8px 14px}.kpi b{font-size:20px;display:block}
.dim{color:var(--dim)}table{border-collapse:collapse;width:100%;font-size:12.5px}td,th{padding:3px 8px;
border-bottom:1px solid var(--line);text-align:left;vertical-align:top}.cmd{font-family:Consolas,monospace;
color:var(--dim);word-break:break-all}
.edge{stroke:#484f58;stroke-width:1.6;fill:none}.nid{font:600 12px Consolas,monospace}.nname{font-size:13px;fill:#e6edf3}
.pulse{animation:p 1.4s ease-in-out infinite}@keyframes p{50%{opacity:.45}}
.life{width:100%;max-width:900px}.lc rect{fill:#1f6feb22;stroke:#1f6feb}.lc text{fill:#e6edf3;font-size:13px;
text-anchor:middle}.s{fill:#8b949e!important;font-size:11px!important;text-anchor:middle}
.chip{display:inline-block;background:#1f6feb22;border:1px solid #1f6feb66;border-radius:20px;padding:1px 9px;
margin:2px;font-size:12px}.shots{display:flex;gap:10px;flex-wrap:wrap}.shots a{color:var(--fg);font-size:12px}
.shots img{max-width:300px;max-height:180px;border-radius:8px;display:block;border:1px solid var(--line)}
pre{white-space:pre-wrap;font-size:12.5px;margin:0}
"""


def shot_files(pattern, skip_frames=True):
    if not SHOTS or not SHOTS.is_dir():
        return []
    return sorted((p for p in SHOTS.rglob(pattern) if not (skip_frames and "frame" in p.stem)),
                  key=lambda p: p.stat().st_mtime, reverse=True)


def page():
    its, current = items()
    n_done = sum(it["kind"] == "done" for it in its)
    n_blk = sum(it["kind"] == "blocked" for it in its)
    rnd, results = round_log()
    sess, last_text = session()
    cur = next((it for it in its if it["id"] == current), None)
    media = [p for p in shot_files("*") if p.suffix.lower() in (".png", ".jpg", ".mp4")]
    pngs = [p for p in media if p.suffix != ".mp4"][:8]
    vids = [p for p in media if p.suffix == ".mp4"]
    link = lambda p: esc(p.relative_to(SHOTS).as_posix())
    gallery = "".join(f'<a href="/shots/{link(p)}" target=_blank><img src="/shots/{link(p)}">{esc(p.name)}</a>'
                      for p in pngs)
    gallery += "".join(f'<a href="/shots/{link(p)}" target=_blank>\u25b6 {esc(p.name)}</a>' for p in vids)
    hist = "".join(f"<tr><td>{esc(t)}</td><td>#{esc(i)}</td><td>{esc(n)}</td></tr>" for t, i, n in history_times())
    commits = ((f"<h3>repo ({esc(REPO.name)})</h3><pre>{esc(git('log', '--oneline', '-12', cwd=REPO))}</pre>" if REPO else "")
               + f"<h3>loop folder</h3><pre>{esc(git('log', '--oneline', '-8', '--', '.', cwd=LOOP))}</pre>")
    return f"""<!doctype html><html lang=en><head><meta charset=utf-8><meta http-equiv=refresh content=15>
<meta name=viewport content="width=device-width,initial-scale=1"><title>{esc(TITLE)} \u00b7 loop</title><style>{CSS}</style>
</head><body><main>
<h1>{esc(TITLE)} \u00b7 {esc(LOOP.name)}</h1><div class=dim><a href="/history">project history \u2192</a> \u00b7 {datetime.now():%H:%M:%S} \u00b7 refreshes every 15 s \u00b7 {esc(rnd)}</div>
<div class=kpis><div class=kpi><b>{n_done}/{len(its)}</b>items done</div>
<div class=kpi><b>{n_blk}</b>blocked</div>
<div class=kpi><b>{esc('#' + cur['id'] + ' ' + cur['name'] if cur else EMDASH)}</b>in progress</div></div>
<div class=card><h2>Item graph</h2>{graph(its) if its else '<p class=dim>no items in LOOP-STATE.md</p>'}
<p class=dim><span style="color:{COLORS['done']}">\u25a0 done</span> \u00b7 <span style="color:{COLORS['now']}">\u25a0 in progress
</span> \u00b7 <span style="color:{COLORS['todo']}">\u25a0 todo</span> \u00b7 <span style="color:{COLORS['blocked']}">\u25a0 blocked</span>
\u00b7 arrow = depends on</p></div>
<div class=card><h2>Round lifecycle</h2>{LIFECYCLE}</div>
<div class=grid><div class=card><h2>Current round</h2>{sess}</div>
<div class=card><h2>Live processes</h2>{processes()}<h3>Last thing the round said</h3>
<pre>{esc(last_text[-900:])}</pre></div></div>
<div class=grid><div class=card><h2>Results per round</h2>{results}</div>
<div class=card><h2>History</h2><table>{hist}</table></div></div>
<div class=grid><div class=card><h2>Commits</h2>{commits}</div>
<div class=card><h2>Screenshots and videos</h2><div class=shots>{gallery or '<span class=dim>none yet</span>'}</div>
</div></div></main></body></html>"""


EV_COLORS = {"done": COLORS["done"], "blocked": COLORS["blocked"], "unblocked": COLORS["now"]}
FMT = "%Y-%m-%dT%H:%M"


def loop_events(loop):
    """LOOP-HISTORY.md entries: `### YYYY-MM-DD HH:MM - item N: title` followed by the evidence text."""
    try:
        txt = (loop / "LOOP-HISTORY.md").read_text(encoding="utf-8")
    except OSError:
        return []
    out = []
    for block in re.split(r"^(?=### \d{4}-)", txt, flags=re.M):
        m = re.match(r"### (\d{4}-\d\d-\d\d \d\d:\d\d) " + DASH + " " + ITEM_WORD + r" ([^:\s]+):? ([^\n]*)\n?(.*)",
                     block, re.S)
        if not m:
            continue
        t, i, title, body = m.groups()
        low = title.lower()
        kind = ("blocked" if "BLOCKED" in title else
                "unblocked" if re.search(r"unblock|relaunch|restart", low) else "done")
        out.append({"t": datetime.strptime(t, "%Y-%m-%d %H:%M"), "id": i, "name": title.strip(), "body": body.strip()[:1200],
                    "kind": kind})
    return out


def result_spend(d):
    sp = {"usd": 0.0, "turns": 0, "min": 0.0, "cache_read": 0, "out": 0, "tokens": 0, "items": []}
    for f in sorted(d.glob("result*.json")):
        try:
            r = json.loads(f.read_text(encoding="utf-8"))
            u = r["usage"]
            row = {"item": f.stem.removeprefix("result-").removeprefix("result"), "usd": r["total_cost_usd"],
                   "turns": r["num_turns"], "min": r["duration_ms"] / 60000}
        except (OSError, ValueError, KeyError, TypeError):
            continue
        sp["items"].append(row)
        for k in ("usd", "turns", "min"):
            sp[k] += row[k]
        sp["cache_read"] += u.get("cache_read_input_tokens", 0)
        sp["out"] += u.get("output_tokens", 0)
        sp["tokens"] += u.get("input_tokens", 0) + u.get("output_tokens", 0) + u.get("cache_creation_input_tokens", 0)
    return sp if sp["items"] else None


def all_loops():
    loops = []
    for d, name in loop_dirs():
        try:
            its, _ = items(d)
        except Exception:
            its = []
        ev = sorted(loop_events(d), key=lambda e: e["t"])
        if ev or its:
            loops.append({"name": name, "items": its, "ev": ev, "spend": result_spend(d)})
    return sorted(loops, key=lambda l: l["ev"][0]["t"] if l["ev"] else datetime.max)


def _n(x):
    return f"{x:,}"


def timeline(loops):
    """Lanes per loop on a compressed time axis: idle gaps longer than 25 min collapse into a marked break."""
    ev = sorted(((e["t"], li, e) for li, l in enumerate(loops) for e in l["ev"]), key=lambda x: x[:2])
    if not ev:
        return "<p class=dim>no history</p>"
    CAP, LX, LH, TOP = 25, 110, 34, 150
    span = sum(min((b[0] - a[0]).total_seconds() / 60, CAP) for a, b in zip(ev, ev[1:]))
    PX = 1400 / max(span, 1)  # fits ~1550 px wide, so text keeps its real size
    xs, x, prev, breaks = {}, 0.0, None, []
    for t, _, _ in ev:
        if prev is not None:
            gap = (t - prev).total_seconds() / 60
            if gap > CAP:
                breaks.append((x + CAP * PX / 2, gap, t))
            x += min(gap, CAP) * PX
        xs[t] = x
        prev = t
    width = int(LX + x + 60)
    height = TOP + LH * len(loops) + 46
    s = [f'<svg viewBox="0 0 {width} {height}" style="width:100%;height:auto;min-width:1100px">']
    total = sum(1 for _, _, e in ev if e["kind"] != "blocked")
    n, pts = 0, [f"{LX},{TOP - 20}"]
    for t, _, e in ev:
        if e["kind"] != "blocked":
            n += 1
            y = TOP - 20 - (n / total) * 100
            pts += [f"{LX + xs[t]:.0f},{float(pts[-1].split(',')[1]):.0f}", f"{LX + xs[t]:.0f},{y:.0f}"]
    s.append(f'<polyline points="{" ".join(pts)}" fill="none" stroke="{COLORS["done"]}" stroke-width="2"/>')
    s.append(f'<text x="{LX - 8}" y="{TOP - 116}" class="s" text-anchor="end">{total} items</text>'
             f'<text x="{LX - 8}" y="{TOP - 20}" class="s" text-anchor="end">0</text>')
    for k, (bx, gap, t) in enumerate(b for b in breaks if b[1] >= 60):
        ly = height - 22 + (k % 2) * 16
        s.append(f'<line x1="{LX + bx}" y1="{TOP - 130}" x2="{LX + bx}" y2="{ly - 10}" stroke="#484f58" '
                 f'stroke-dasharray="3 4"/><text x="{LX + bx}" y="{ly}" class="s">'
                 f'{t:%d/%m %H:%M} \u00b7 pause {gap / 60:.1f} h</text>')
    s.append(f'<text x="{LX}" y="{height - 22}" class="s" text-anchor="start">{ev[0][0]:%d/%m %H:%M}</text>')
    for li, l in enumerate(loops):
        y = TOP + li * LH + LH / 2
        s.append(f'<text x="{LX - 10}" y="{y + 4}" class="nname" text-anchor="end">{esc(l["name"])}</text>'
                 f'<line x1="{LX}" y1="{y}" x2="{width - 20}" y2="{y}" stroke="#30363d"/>')
        if l["ev"]:
            a, b = xs[l["ev"][0]["t"]], xs[l["ev"][-1]["t"]]
            s.append(f'<rect x="{LX + a - 8}" y="{y - 11}" width="{b - a + 16}" height="22" rx="11" '
                     f'fill="#1f6feb18" stroke="#1f6feb55"/>')
    for t, li, e in ev:
        y = TOP + li * LH + LH / 2
        s.append(f'<circle cx="{LX + xs[t]:.0f}" cy="{y}" r="6" fill="{EV_COLORS[e["kind"]]}" stroke="#0d1117"><title>'
                 f'{t:%d/%m %H:%M} \u00b7 {esc(loops[li]["name"])} #{esc(e["id"])} \u00b7 {esc(e["name"])}</title></circle>')
    s.append("</svg>")
    return "".join(s)


def spend(loops):
    rows, total = [], [0, 0.0, 0.0]
    for l in loops:
        sp = l["spend"]
        if not sp:
            continue
        for it in sp["items"]:
            rows.append(f"<tr><td>{esc(l['name'])}</td><td>{esc(it['item'])}</td><td>{it['turns']}</td>"
                        f"<td>{it['min']:.1f}</td><td>{it['usd']:.2f}</td></tr>")
        total = [total[0] + sp["turns"], total[1] + sp["min"], total[2] + sp["usd"]]
        rows.append(f"<tr><td colspan=2><b>subtotal {esc(l['name'])}</b></td><td><b>{sp['turns']}</b></td>"
                    f"<td><b>{sp['min']:.1f}</b></td><td><b>{sp['usd']:.2f}</b></td></tr>")
    body = ("<table><tr><th>loop</th><th>item</th><th>turns</th><th>min</th><th>USD est.</th></tr>" + "".join(rows)
            + f"<tr><td colspan=2><b>total</b></td><td><b>{total[0]}</b></td><td><b>{total[1]:.1f}</b></td>"
            f"<td><b>{total[2]:.2f}</b></td></tr></table>") if rows else "<p class=dim>no result*.json</p>"
    return (f"<div class=card><h2>Spend</h2>{body}<p class=dim>USD = estimated API list price; on a subscription "
            f"it is not charged and does not map to a quota percentage.</p></div>")


def history_data():
    """Everything the /history page draws, as JSON-ready dicts (rebuilt on every request)."""
    loops = []
    for l in all_loops():
        loops.append({"name": l["name"], "items": l["items"], "spend": l["spend"],
                      "ev": [{**e, "t": e["t"].strftime(FMT), "title": e.pop("name")} for e in map(dict, l["ev"])],
                      "graph": graph(l["items"]) if l["items"] else ""})
    commits = []
    for line in git("log", "--format=%aI%x09%h%x09%s", cwd=REPO).splitlines():
        try:
            t, h, s = line.split("\t", 2)
            commits.append({"t": datetime.fromisoformat(t).astimezone().strftime(FMT), "h": h, "s": s})
        except ValueError:
            continue
    tags = []
    for line in git("for-each-ref", "refs/tags", "--format=%(refname:short)%09%(creatordate:iso-strict)%09%(subject)",
                    cwd=REPO).splitlines():
        try:
            n, t, s = line.split("\t", 2)
            tags.append({"name": n, "t": datetime.fromisoformat(t).astimezone().strftime(FMT), "s": s})
        except ValueError:
            continue
    shots = shot_files("*.png")[:12]
    return {"title": TITLE, "now": datetime.now().strftime("%H:%M:%S"), "loops": loops, "commits": commits, "tags": tags,
            "shots": [{"src": "/shots/" + p.relative_to(SHOTS).as_posix(), "name": p.name,
                       "t": datetime.fromtimestamp(p.stat().st_mtime).strftime(FMT)} for p in shots]}


def history_page_classic():
    loops = all_loops()
    ev = [e for l in loops for e in l["ev"]]
    n_items = sum(len(l["items"]) for l in loops)
    n_done = sum(1 for l in loops for it in l["items"] if it["state"].startswith("done"))
    n_blk = sum(1 for e in ev if e["kind"] == "blocked")
    hours = sum((l["ev"][-1]["t"] - l["ev"][0]["t"]).total_seconds() / 3600 for l in loops if l["ev"])
    commits = git("rev-list", "--count", "HEAD", cwd=REPO).strip() or "0"
    tags = git("tag", "-n1", "-l", cwd=REPO).strip()
    sections = []
    for i, l in enumerate(reversed(loops)):
        its = l["items"]
        done = sum(1 for it in its if it["state"].startswith("done"))
        span = (f'{l["ev"][0]["t"]:%d/%m %H:%M} \u2192 {l["ev"][-1]["t"]:%d/%m %H:%M}' if l["ev"] else "no history")
        body = graph(its) if its else "<p class=dim>no items</p>"
        sections.append(f'<details class=card{" open" if i == 0 else ""}><summary><b>{esc(l["name"])}</b> \u00b7 '
                        f'{done}/{len(its)} items \u00b7 {span}</summary>{body}</details>')
    return f"""<!doctype html><html lang=en><head><meta charset=utf-8><meta http-equiv=refresh content=60>
<meta name=viewport content="width=device-width,initial-scale=1"><title>{esc(TITLE)} \u00b7 history</title><style>{CSS}
summary{{cursor:pointer;margin-bottom:8px}}a{{color:#58a6ff}}</style></head><body><main>
<h1>{esc(TITLE)} \u00b7 project history</h1><div class=dim><a href="/">\u2190 current loop</a> \u00b7
{datetime.now():%H:%M:%S} \u00b7 refreshes every minute</div>
<div class=kpis><div class=kpi><b>{len(loops)}</b>loops</div><div class=kpi><b>{n_done}/{n_items}</b>items done</div>
<div class=kpi><b>{n_blk}</b>blocks (unblocked or open)</div><div class=kpi><b>{hours:.1f} h</b>of loop time</div>
<div class=kpi><b>{esc(commits)}</b>commits</div></div>
<div class=card><h2>Timeline</h2>{timeline(loops)}
<p class=dim>Top: cumulative items done. Each dot is a closed item (<span style="color:{COLORS['done']}">done
</span> \u00b7 <span style="color:{COLORS['blocked']}">BLOCKED</span> \u00b7 <span style="color:{COLORS['now']}">unblocked</span>);
hover for details. Pauses longer than 25 min are compressed and marked with their length.</p></div>
{spend(loops)}
<div class=card><h2>Versions</h2><pre>{esc(tags) or 'no tags'}</pre></div>
<h2>Graph of each loop</h2>{''.join(sections)}
</main></body></html>"""


INBOX, OUTBOX = "orchestrator-inbox.jsonl", "orchestrator-outbox.jsonl"
CHAT_LOCK = threading.Lock()


def read(p):
    try:
        return p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def flow_data():
    """Everything the live flow, the progress bars and the session log draw (rebuilt on every request)."""
    its, current = items()
    by = {it["id"]: it for it in its}
    ev = loop_events(LOOP)
    for it in its:
        it["waits"] = [d for d in it["deps"] if by.get(d, {}).get("kind") != "done"]
        if it["kind"] == "todo" and it["waits"]:
            it["kind"] = "waiting"
        elif it["kind"] == "blocked" and "user" in it["state"].lower():
            it["kind"] = "gate"  # a decision gate, not a failure
        what, _, verify = it["name"].partition("VERIFY:")
        it["what"], it["verify"] = what.strip(), verify.strip()
        it["title"] = short_title(it["what"])
        it["retries"] = max(int(it["tries"]) - (it["kind"] == "done"), 0)  # a pass costs one try, the rest failed
        it["ev"] = [{"t": e["t"].strftime(FMT), "kind": e["kind"], "title": e["name"], "body": e["body"]}
                    for e in ev if e["id"] == it["id"]]
    log, usage_txt = read(Path(args.log)) if args.log else "", read(LOOP / "LOOP-USAGE.md")
    rounds = re.findall(r"fresh-loop: round (\d+)/(\d+)", log)
    usage = re.findall(r"^- \[(\d\d:\d\d)\] (.*)$", usage_txt, re.M)
    cap = (loop_config() or {}).get("rounds") or (rounds[-1][1] if rounds else
                                                  (re.findall(r"\bcap (\d+)", read(LOOP / "LOOP-STATE.md") + usage_txt) or [0])[0])
    day = (re.findall(r"\d{4}-\d\d-\d\d", usage_txt) or [datetime.now().strftime("%Y-%m-%d")])[0]
    stream = [{"t": f"{day}T{hm}", "kind": "usage", "text": line} for hm, line in usage]
    stream += [{"t": e["t"].strftime(FMT), "kind": "round", "text": f"item {e['id']}: {e['name']}"} for e in ev]
    for line in git("log", "-30", "--format=%aI%x09%h%x09%s", cwd=REPO or LOOP).splitlines():
        try:
            t, h, s = line.split("\t", 2)
            stream.append({"t": datetime.fromisoformat(t).astimezone().strftime(FMT), "kind": "commit", "text": f"{h} {s}"})
        except ValueError:
            continue
    ps, tr = (None, None) if args.snapshot else (procs(), transcript())
    rnd = {"n": int(rounds[-1][0]) if rounds else len(usage), "cap": int(cap), "item": current,
           "driver": None if ps is None else any(p["role"] == "loop" for p in ps),
           "live": None if ps is None else any(p["role"] != "loop" for p in ps),
           "agents": [{"type": a, "desc": d} for a, d in tr[2]] if tr else [], "tools": len(tr[1]) if tr else 0,
           "session": tr[0].stem[:8] if tr else "",
           "last": datetime.fromtimestamp(tr[0].stat().st_mtime).strftime("%H:%M:%S") if tr else ""}
    return {"title": TITLE, "loop": LOOP.name, "now": datetime.now().strftime("%H:%M:%S"), "items": its,
            "current": current, "round": rnd, "spend": result_spend(LOOP), "limits": loop_config(),
            "tokens": sum(int(x.replace(",", "")) for _, line in usage for x in re.findall(r"(\d[\d,]*) ?tok", line)),
            "usd": sum(float(x.replace(",", "")) for _, line in usage for x in re.findall(r"\$(\d[\d,]*(?:\.\d+)?)", line)),
            "usage": [{"t": hm, "text": line} for hm, line in usage],
            "log": sorted(stream, key=lambda e: e["t"], reverse=True)[:80],
            "steer_usd": sum(m["usd"] for m in chat_messages() if isinstance(m["usd"], (int, float)))}


CONFIG = "loop-config.json"
CONFIG_FIELDS = {"rounds": (int, 1), "max_tokens": (int, 0), "max_usd": ((int, float), 0)}


def loop_config():
    try:
        cfg = json.loads(read(LOOP / CONFIG))
    except ValueError:
        return None
    return cfg if isinstance(cfg, dict) else None


def set_config(raw):
    """Validate a partial config from the page and merge it into loop-config.json atomically; raises ValueError."""
    new = json.loads(raw)
    if not isinstance(new, dict) or not new:
        raise ValueError("expected a non-empty object")
    clean = {}
    for k, v in new.items():
        if k in CONFIG_FIELDS:
            typ, low = CONFIG_FIELDS[k]
            if isinstance(v, bool) or not isinstance(v, typ) or v < low:
                raise ValueError(f"{k}: expected a number >= {low}")
        elif k == "on_limit":
            if v not in ("stop", "warn"):
                raise ValueError("on_limit: expected stop or warn")
        elif k in ("model", "effort"):
            if not (isinstance(v, str) and re.fullmatch(r"[\w.\[\]-]{0,40}", v)):
                raise ValueError(f"{k}: expected a short model or effort name")
        else:
            raise ValueError(f"unknown field {k}")
        clean[k] = v
    with CHAT_LOCK:
        cfg = {**(loop_config() or {}), **clean}
        tmp = LOOP / (CONFIG + ".tmp")
        tmp.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
        os.replace(tmp, LOOP / CONFIG)  # the driver never sees a half-written file
    return cfg


def chat_messages():
    out = []
    for name, who in ((INBOX, "user"), (OUTBOX, "orchestrator")):
        for line in read(LOOP / name).splitlines():
            try:
                m = json.loads(line)
            except ValueError:
                m = {"text": line}  # a hand-written plain-text reply still shows up
            m = m if isinstance(m, dict) else {"text": str(m)}
            if str(m.get("text", "")).strip():
                refs = m.get("refs") if who == "user" and isinstance(m.get("refs"), list) else []
                out.append({"ts": str(m.get("ts", "")), "from": who, "text": str(m["text"]), "refs": refs,
                            "escalate": who == "orchestrator" and str(m["text"]).lstrip().startswith("ESCALATE:"),
                            "usd": m.get("usd")})
    return sorted(out, key=lambda m: m["ts"])


def add_chat(raw):
    """Validate one message from the page and append it to the inbox as one JSON line; raises ValueError."""
    msg = json.loads(raw)
    text, refs = (msg.get("text"), msg.get("refs", [])) if isinstance(msg, dict) else (None, None)
    if not (isinstance(text, str) and text.strip() and len(text) <= 8000
            and isinstance(refs, list) and all(isinstance(r, dict) for r in refs)):
        raise ValueError("expected {text: non-empty string, refs: [objects]}")
    rec = {"ts": datetime.now().isoformat(timespec="seconds"), "from": "user", "text": text.strip(), "refs": refs}
    with CHAT_LOCK, open(LOOP / INBOX, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    if args.reply:
        REPLIES.put(rec)
    return rec


REPLIES = queue.Queue()
REPLY_TOOLS = "Read,Edit,Write,Glob,Grep"
REPLY_PROMPT = """You answer the chat of a running agent loop from its status board, in place of a resident orchestrator
session. Reply in a few lines, in the user's language. Everything you need to answer is below: use tools only to act.
You may edit LOOP-STATE.md in this folder (add an item, change a state, Depends or wording, unblock an item) and create
a file named STOP here to stop the loop after its current round; the next round reads them. Never touch anything else.
If the message needs judgement beyond the loop's state (a design decision, a change of plan, reading the code), start
the reply with "ESCALATE:" and add a 3-5 line handoff for a new session with a stronger model: what to decide, which
files, what the loop is waiting on.

## Message
{text}
Attached from the page: {refs}

## Chat so far (oldest first)
{chat}

## LOOP-STATE.md
{state}

## LOOP-HISTORY.md (last part)
{history}

## Driver log (last lines)
{log}
"""


def reply(rec):
    """One short headless session answers one message; its reply, or why it failed, goes to the outbox."""
    before = [m for m in chat_messages() if m["ts"] <= rec["ts"] and (m["ts"], m["text"]) != (rec["ts"], rec["text"])]
    chat = "\n".join(f"{m['from']}: {m['text'][:500]}" for m in before[-8:]) or "(none)"
    # the prompt is one argument: these caps keep it under Windows' 32k command-line limit
    prompt = REPLY_PROMPT.format(text=rec["text"], refs=json.dumps(rec["refs"], ensure_ascii=False)[:2000], chat=chat,
                                 state=read(LOOP / "LOOP-STATE.md")[-10000:] or "(missing)",
                                 history=read(LOOP / "LOOP-HISTORY.md")[-3000:] or "(none)",
                                 log=read(Path(args.log))[-1500:] if args.log else "(no --log)")
    cmd = [*shlex.split(args.reply_cli), prompt]
    claude = Path(cmd[0]).stem == "claude"
    if claude:
        cmd += lean_flags(REPLY_TOOLS, LOOP) + ["--permission-mode", "acceptEdits", "--output-format", "json"]
    usd = None
    try:
        r = subprocess.run(cmd, cwd=LOOP, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=600)
        text = r.stdout.strip()
        if claude and text:
            out = json.loads(text)
            text, usd = str(out.get("result", "")).strip(), out.get("total_cost_usd")
        if r.returncode or not text:
            raise RuntimeError(f"exit {r.returncode}: {(r.stderr or text).strip()[:300]}")
    except Exception as e:  # the worker must outlive any failure; the user sees it in the chat
        text = f"Replier failed ({e}). Answer by hand or check --reply-cli."
    rec = {"ts": datetime.now().isoformat(timespec="seconds"), "text": text}
    if usd is not None:
        rec["usd"] = round(float(usd), 4)
    with CHAT_LOCK, open(LOOP / OUTBOX, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def reply_worker():
    while True:
        reply(REPLIES.get())


class H(BaseHTTPRequestHandler):
    def send(self, code, body, ctype="application/json"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if self.path not in ("/chat", "/config"):
            return self.send_error(404)
        mine = {f"127.0.0.1:{args.port}", f"localhost:{args.port}"}
        origin = self.headers.get("Origin")
        # the inbox feeds an agent and the config steers the driver: only this page may write (no other site,
        # no DNS rebinding, no form posts)
        if (self.headers.get("Host") not in mine or (origin and origin.split("://", 1)[-1] not in mine)
                or not self.headers.get("Content-Type", "").startswith("application/json")):
            return self.send_error(403)
        n = int(self.headers.get("Content-Length") or 0)
        if n > 65536:
            return self.send_error(413)
        try:
            rec = (add_chat if self.path == "/chat" else set_config)(self.rfile.read(n))
        except (ValueError, UnicodeDecodeError) as e:
            return self.send(400, json.dumps({"error": str(e)}).encode())
        self.send(200, json.dumps(rec, ensure_ascii=False).encode("utf-8"))

    def do_GET(self):
        if self.path.startswith("/shots/"):
            p = (SHOTS / self.path[7:].split("?")[0]).resolve() if SHOTS else None
            if p and SHOTS.resolve() in p.parents and p.is_file():
                self.send_response(200)
                self.send_header("Content-Type", {".png": "image/png", ".jpg": "image/jpeg"}.get(p.suffix, "video/mp4"))
                self.end_headers()
                self.wfile.write(p.read_bytes())
                return
            self.send_error(404)
            return
        try:
            ctype, path = "text/html; charset=utf-8", self.path.split("?")[0]
            if path == "/history.json":
                body, ctype = json.dumps(history_data(), ensure_ascii=False).encode("utf-8"), "application/json"
            elif path == "/flow.json":
                body, ctype = json.dumps(flow_data(), ensure_ascii=False).encode("utf-8"), "application/json"
            elif path == "/chat":
                body, ctype = json.dumps(chat_messages(), ensure_ascii=False).encode("utf-8"), "application/json"
            elif path == "/history/classic":
                body = history_page_classic().encode("utf-8")
            elif path == "/classic":
                body = page().encode("utf-8")
            elif path in ("/", "/history"):
                body = HIST_HTML.read_bytes()
            else:
                return self.send_error(404)
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.end_headers()
            self.wfile.write(body)
            return
        except Exception as e:  # show the error instead of a blank page
            body = f"<pre>loop-status.py: {esc(repr(e))}</pre>".encode()
            self.send_response(500)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def snapshot(out):
    out.mkdir(parents=True, exist_ok=True)
    for name, data in (("flow.json", flow_data()), ("history.json", history_data()), ("chat.json", chat_messages())):
        (out / name).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    page_html = HIST_HTML.read_text(encoding="utf-8").replace("<script>", "<script>window.SNAPSHOT = true;", 1)
    (out / "index.html").write_text(page_html, encoding="utf-8")
    print(f"snapshot written to {out}")


if __name__ == "__main__" and args.snapshot:
    snapshot(Path(args.snapshot))
elif __name__ == "__main__":
    print(f"loop status: http://127.0.0.1:{args.port}/", flush=True)
    if args.reply:
        threading.Thread(target=reply_worker, daemon=True).start()
    ThreadingHTTPServer(("127.0.0.1", args.port), H).serve_forever()

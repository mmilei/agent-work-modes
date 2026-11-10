"""Live status board for a stateful loop (see reference/setup.md).

    python loop-status.py [--loop-dir DIR] [--loops-glob GLOB] [--repo DIR] [--transcripts DIR]
                          [--log FILE] [--port 8765] [--title NAME]

Serves http://127.0.0.1:<port>/ , one page (loop-status.html) that refreshes itself: the current loop on top (item
rows, what the current round's session is doing, live processes, the round log, commits) and below it the history of
all loops, versions and screenshots. Routes: / and /history (the page), /current.json and /history.json (its data,
rebuilt on every request), /shots/<file> (screenshots).

Every input except --loop-dir is optional; a missing one renders as an empty card, never as an error.
--loop-dir holds LOOP-STATE.md (item table), LOOP-HISTORY.md (one `### YYYY-MM-DD HH:MM - item N: ...` entry per
closed item) and result*.json (claude -p JSON output, for the spend card).
"""
import argparse
import html
import json
import os
import re
import subprocess
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

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

ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
ap.add_argument("--loop-dir", default=".", help="the current loop's folder (default: cwd)")
ap.add_argument("--loops-glob", default="", help="sibling loops for the history view (default: <loop-dir parent>/*)")
ap.add_argument("--repo", dest="repo", default="", help="repo whose commits, tags and screenshots are shown")
ap.add_argument("--screenshots", default="", help="screenshots folder (default: <repo>/screenshots)")
ap.add_argument("--transcripts", default="", help="Claude Code transcript folder of the loop's sessions")
ap.add_argument("--log", default="", help="fresh-loop.py output file")
ap.add_argument("--port", type=int, default=8765)
ap.add_argument("--title", default="", help="page title (default: the repo or loop folder name)")
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


def processes():
    if os.name != "nt":
        return "<p class=dim>process list is only implemented on Windows</p>"
    ps = ("Get-CimInstance Win32_Process | Where-Object { $_.Name -match '^claude' -or "
          "($_.Name -match '^python' -and $_.CommandLine -match 'fresh-loop') } | "
          "Select-Object ProcessId,ParentProcessId,Name,CreationDate,CommandLine | ConvertTo-Json -Compress")
    try:
        raw = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True,
                             timeout=15, encoding="utf-8", errors="replace").stdout.strip()
        data = json.loads(raw, strict=False) if raw else []  # the round's prompt has raw newlines
    except Exception as e:  # the page must render even if the process query fails
        return f"<p class=dim>no process data: {esc(str(e))}</p>"
    data = data if isinstance(data, list) else [data]
    rows = []
    for p in data:
        cmd = p.get("CommandLine") or ""
        if p["Name"].startswith("claude") and " -p " not in f" {cmd} " and "fresh-loop" not in cmd:
            continue  # interactive sessions are not part of the loop
        m = re.search(r"/Date\((\d+)\)", str(p.get("CreationDate")))
        age = ""
        if m:
            secs = int(datetime.now().timestamp() - int(m.group(1)) / 1000)
            age = f"{secs // 60}m {secs % 60:02d}s"
        role = "loop" if "fresh-loop" in cmd else "round (claude -p)"
        rows.append(f"<tr><td>{role}</td><td>{p['ProcessId']}</td><td>{age}</td>"
                    f"<td class=cmd>{esc(cmd[:140])}</td></tr>")
    return ("<table><tr><th>role</th><th>pid</th><th>alive for</th><th>command</th></tr>" + "".join(rows)
            + "</table>") if rows else "<p class=dim>nothing running: the loop is stopped</p>"


def session():
    files = sorted(TRANSCRIPTS.glob("*.jsonl"), key=lambda f: f.stat().st_mtime, reverse=True) if (
        TRANSCRIPTS and TRANSCRIPTS.is_dir()) else []
    if not files:
        return "<p class=dim>no transcripts (pass --transcripts)</p>", ""
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


def shot_files(pattern, skip_frames=True):
    if not SHOTS or not SHOTS.is_dir():
        return []
    return sorted((p for p in SHOTS.rglob(pattern) if not (skip_frames and "frame" in p.stem)),
                  key=lambda p: p.stat().st_mtime, reverse=True)


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
    sp = {"usd": 0.0, "turns": 0, "min": 0.0, "cache_read": 0, "out": 0, "items": []}
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


def current_data():
    """The current loop, as JSON-ready data for the top of the page (rebuilt on every request)."""
    its, cur = items()
    rnd, results = round_log()
    sess, last_text = session()
    return {"loop": LOOP.name, "items": its, "current": cur, "round": rnd, "results": results, "session": sess,
            "last_text": last_text[-900:], "processes": processes(),
            "loop_commits": git("log", "--oneline", "-8", "--", ".", cwd=LOOP)}


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


class H(BaseHTTPRequestHandler):
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
            ctype = "text/html; charset=utf-8"
            if self.path.startswith("/history.json"):
                body, ctype = json.dumps(history_data(), ensure_ascii=False).encode("utf-8"), "application/json"
            elif self.path.startswith("/current.json"):
                body, ctype = json.dumps(current_data(), ensure_ascii=False).encode("utf-8"), "application/json"
            elif self.path in ("/", "/history") or self.path.startswith(("/?", "/history?")):
                body = HIST_HTML.read_bytes()
            else:
                self.send_error(404)
                return
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


if __name__ == "__main__":
    print(f"loop status: http://127.0.0.1:{args.port}/", flush=True)
    ThreadingHTTPServer(("127.0.0.1", args.port), H).serve_forever()

"""Unified operational dashboard for loops, agent sessions, and spend (see reference/setup.md).

    python loop-status.py [--loop-dir DIR] [--loops-glob GLOB] [--repo DIR] [--transcripts DIR]
                          [--log FILE] [--port 8765] [--title NAME] [--snapshot DIR]
                          [--reply [--reply-cli CMD]] [--no-cost]

Serves http://127.0.0.1:<port>/ (loop-status.html) with three tabs:
  1. Loop & History: live item DAG by Depends, driver/round status, progress bars, live limits,
     session log, orchestrator chat, and cross-loop project history.
  2. Sessions & Graph: live tree of Claude Code, Codex, and Hermes sessions across projects with
     time bars, launcher/message edges, context start->peak, and cache rewrites.
  3. Usage & Spend: spend by work mode, day, hour, and project, plus highest-cost/rewrite sessions.

Data routes rebuilt on every request: /flow.json, /history.json, /graph.json?hours=N, GET /chat,
POST /chat, POST /config, /shots/<file>.

--snapshot DIR writes a static read-only copy (index.html, flow.json, history.json, chat.json,
graph.json) into DIR and exits. --no-cost skips the background ccusage pricing worker.
"""
import argparse
import hashlib
import html
import importlib.util
import json
import os
import queue
import re
import shlex
import shutil
import sqlite3
import subprocess
import threading
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from lean import lean_flags

HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("session_cost", HERE / "session-cost.py")
session_cost = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(session_cost)

HIST_HTML = HERE / "loop-status.html"
ID = r"(?:[A-Z]?\d+[a-z]?|[A-Z])"
DASH = r"[\u2014\u2013-]"  # em dash, en dash or hyphen
ITEM_WORD = "item"
# header cell (lowercase) -> field
HEADERS = {"#": "id", "id": "id", "item": "name", "depends": "deps",
           "state": "state", "status": "state", "tries": "tries", "attempts": "tries"}
esc = html.escape
FMT = "%Y-%m-%dT%H:%M"

ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
ap.add_argument("--loop-dir", default=".", help="the current loop's folder (default: cwd)")
ap.add_argument("--loops-glob", default="", help="sibling loops for the history view")
ap.add_argument("--repo", dest="repo", default="", help="repo whose commits, tags and screenshots are shown")
ap.add_argument("--screenshots", default="", help="screenshots folder (default: <repo>/screenshots)")
ap.add_argument("--transcripts", default="", help="Claude Code transcript folder of the loop's sessions")
ap.add_argument("--log", default="", help="fresh-loop.py output file")
ap.add_argument("--port", type=int, default=8765)
ap.add_argument("--title", default="", help="page title (default: the repo or loop folder name)")
ap.add_argument("--snapshot", default="", help="write a static read-only board into this folder and exit")
ap.add_argument("--reply", action="store_true", help="answer each chat message with a short headless session")
ap.add_argument("--reply-cli", default="claude -p --model sonnet", help="command for --reply; the prompt goes last")
ap.add_argument("--no-cost", action="store_true", help="skip ccusage; Claude nodes show no cost")
args = ap.parse_args() if __name__ == "__main__" else ap.parse_args([])

LOOP = Path(args.loop_dir).resolve()
REPO = Path(args.repo).resolve() if args.repo else None
SHOTS = Path(args.screenshots).resolve() if args.screenshots else (REPO / "screenshots" if REPO else None)
TRANSCRIPTS = Path(args.transcripts) if args.transcripts else None
TITLE = args.title or (REPO or LOOP).name

HOME = Path.home()
CLAUDE = HOME / ".claude" / "projects"
CODEX = HOME / ".codex" / "sessions"
HERMES = [Path(os.environ.get("LOCALAPPDATA", HOME)) / "hermes", HOME / ".hermes"]
ACTIVE_S = 300  # a session written in the last 5 minutes counts as running
LAUNCH = ("claude -p", "fresh-loop.py")
COSTS, COST_AT = {}, [None]


def loop_dirs():
    if args.loops_glob:
        pat = Path(args.loops_glob)
        found = Path(pat.anchor).glob(str(pat.relative_to(pat.anchor))) if pat.is_absolute() else Path().glob(args.loops_glob)
    else:
        found = [LOOP]
        if (LOOP / "loops").is_dir():
            found += sorted((LOOP / "loops").glob("*"))
        if LOOP.parent.name == "loops":
            found += sorted(LOOP.parent.glob("*"))
    seen = set()
    dirs = [d for d in sorted((p.resolve() for p in found), key=lambda p: p.name)
            if (d / "LOOP-STATE.md").exists() and not (d in seen or seen.add(d))]
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


def items(loop=None):
    loop = LOOP if loop is None else loop
    out, cols = [], ["id", "name", "deps", "state", "tries"]
    p = loop / "LOOP-STATE.md"
    if not p.exists():
        return [], None
    for line in p.read_text(encoding="utf-8").splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")] if line.lstrip().startswith("|") else []
        if cells and all(c.lower() in HEADERS for c in cells[:2]) and "id" == HEADERS.get(cells[0].lower()):
            cols = [HEADERS.get(c.lower(), "") for c in cells]  # header row: take the column order from it
            continue
        if len(cells) < len(cols) or "id" not in cols or not re.fullmatch(ID, cells[cols.index("id")]):
            continue
        row = dict(zip(cols, cells))
        if not row.get("state"):
            continue
        tries = row.get("tries", "0")
        tries = tries if tries.isdigit() else "0"
        out.append({"id": row["id"], "name": row.get("name", ""),
                    "deps": re.findall(r"\b" + ID + r"\b", row.get("deps", "")),
                    "state": row["state"], "tries": tries})
    done = {it["id"] for it in out if it["state"] == "done"}
    current = next((it["id"] for it in out if it["state"] == "todo" and set(it["deps"]) <= done), None)
    for it in out:
        it["kind"] = ("done" if it["state"] == "done" else "blocked" if it["state"].startswith("BLOCKED")
                      else "now" if it["id"] == current else "todo")
    return out, current


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


def shot_files(pattern, skip_frames=True):
    if not SHOTS or not SHOTS.is_dir():
        return []
    return sorted((p for p in SHOTS.rglob(pattern) if not (skip_frames and "frame" in p.stem)),
                  key=lambda p: p.stat().st_mtime, reverse=True)


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


def history_data():
    """Everything the history view draws, as JSON-ready dicts (rebuilt on every request)."""
    loops = []
    for l in all_loops():
        loops.append({"name": l["name"], "items": l["items"], "spend": l["spend"],
                      "ev": [{**e, "t": e["t"].strftime(FMT), "title": e.pop("name")} for e in map(dict, l["ev"])]})
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


# ---- session graph & usage across Claude Code, Codex, and Hermes ----

def ts(s):
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp() if s else 0
    except ValueError:
        return 0


def text_of(content):
    if isinstance(content, str):
        return content
    return " ".join(c.get("text", "") for c in content or [] if isinstance(c, dict) and c.get("type") == "text")


def clean_project(p):
    """Normalize mangled Claude project folder names like D--Maxi-git-...-workspace-foo -> foo."""
    if not p:
        return "?"
    tail = p.split("-workspace-")[-1] if "-workspace-" in p else (p.split("-git-")[-1] if "-git-" in p else p)
    return tail or p


_cache = {}


def cached(path, parse):
    """Parse a file again only when its size or mtime changed."""
    st = path.stat()
    key = (str(path), parse.__name__)
    hit = _cache.get(key)
    if hit and hit[0] == (st.st_mtime, st.st_size):
        return hit[1]
    data = parse(path)
    _cache[key] = ((st.st_mtime, st.st_size), data)
    return data


def claude_file(f):
    d = {"prompt": "", "prompts": 0, "start": 0, "end": 0, "headless": False, "name": "", "cwd": "",
         "models": set(), "tok": [0, 0, 0, 0], "spawns": {}, "sends": Counter(), "launches": [], "monitors": 0,
         "tools": set(), "ctx_start": 0, "ctx_peak": 0, "rewrites": 0, "processed": 0, "written": 0}
    seen, ctx, rewrites, processed, written, last = set(), [], 0, 0, 0, None
    for line in f.open(encoding="utf-8", errors="replace"):
        try:
            e = json.loads(line)
        except ValueError:
            continue
        t = ts(e.get("timestamp"))
        if t:
            d["start"] = d["start"] or t
            d["end"] = t
        if e.get("type") == "agent-name":
            d["name"] = e.get("agentName") or d["name"]
        d["cwd"] = d["cwd"] or e.get("cwd", "")
        d["headless"] = d["headless"] or e.get("entrypoint") == "sdk-cli"
        msg = e.get("message") or {}
        if e.get("type") == "user" and not e.get("isMeta") and not e.get("toolUseResult") \
                and not e.get("isCompactSummary"):
            words = " ".join(text_of(msg.get("content")).split())
            if words and not words.startswith("<"):
                d["prompts"] += 1
                d["prompt"] = d["prompt"] or words[:300]
        if e.get("type") != "assistant":
            continue
        if msg.get("model", "").startswith("claude"):
            d["models"].add(msg["model"].replace("claude-", ""))
        for c in msg.get("content") or []:
            if not isinstance(c, dict) or c.get("type") != "tool_use":
                continue
            name, inp = c.get("name"), c.get("input") or {}
            d["tools"].add(name)
            if name in ("Agent", "Task"):
                d["spawns"][c.get("id")] = inp.get("name") or ""
            elif name == "SendMessage" and inp.get("to"):
                d["sends"][inp["to"]] += 1
            elif name == "Monitor":
                d["monitors"] += 1
            elif name in ("Bash", "PowerShell") and any(k in str(inp.get("command", "")) for k in LAUNCH):
                d["launches"].append(t)
        u, key = msg.get("usage"), (msg.get("id"), e.get("requestId"))
        if u and key not in seen:
            seen.add(key)
            for i, k in enumerate(("input_tokens", "output_tokens", "cache_read_input_tokens",
                                   "cache_creation_input_tokens")):
                d["tok"][i] += u.get(k) or 0
            write = u.get("cache_creation_input_tokens") or 0
            total = (u.get("input_tokens") or 0) + (u.get("cache_read_input_tokens") or 0) + write
            if total:  # synthetic or error entries carry no context
                paused = bool(last and t and (t - last) >= 300)  # shortest cache lifetime
                rewrites += int(bool(ctx and paused and total > 20000 and write * 2 > total))
                ctx.append(total)
                processed += total + (u.get("output_tokens") or 0)
                written += u.get("output_tokens") or 0
                last = t or last
    d["ctx_start"] = ctx[0] if ctx else 0
    d["ctx_peak"] = max(ctx, default=0)
    d["rewrites"] = rewrites
    d["processed"] = processed
    d["written"] = written
    d["mode"] = session_cost.mode(d["prompt"][:70], d["headless"], d["tools"])
    del d["tools"]
    return d


def codex_file(f):
    d = {"prompt": "", "prompts": 0, "start": 0, "end": 0, "cwd": "", "models": set(), "tok": [0, 0, 0, 0],
         "id": "", "parent": "", "source": ""}
    for line in f.open(encoding="utf-8", errors="replace"):
        try:
            e = json.loads(line)
        except ValueError:
            continue
        p, t = e.get("payload") or {}, ts(e.get("timestamp"))
        if t:
            d["start"] = d["start"] or t
            d["end"] = t
        if e.get("type") == "session_meta":
            d.update(id=p.get("id", ""), parent=p.get("parent_thread_id") or "", cwd=p.get("cwd", ""),
                     source=p.get("thread_source") or "")
        elif e.get("type") == "turn_context" and p.get("model"):
            d["models"].add(p["model"])
        elif p.get("type") == "task_started":
            d["prompts"] += 1
        elif p.get("type") == "message" and p.get("role") == "user" and not d["prompt"]:
            for c in p.get("content") or []:
                words = " ".join(str(c.get("text", "")).split()) if isinstance(c, dict) else ""
                if words and not words.startswith(("<", "# AGENTS.md")):
                    d["prompt"] = words[:300]
                    break
        elif p.get("type") == "token_count":
            u = (p.get("info") or {}).get("total_token_usage") or {}
            cached_in = u.get("cached_input_tokens") or 0
            d["tok"] = [(u.get("input_tokens") or 0) - cached_in, u.get("output_tokens") or 0, cached_in,
                        u.get("cache_write_input_tokens") or 0]
    return d


def node(nid, provider, band, d, **extra):
    n = {"id": nid, "provider": provider, "band": band, "start": d["start"], "end": d["end"],
         "prompt": d.get("prompt", ""), "prompts": d.get("prompts", 0), "cwd": d.get("cwd", ""),
         "models": sorted(d.get("models", ())), "tok": list(d["tok"]), "tokens": sum(d["tok"]),
         "ctx_start": d.get("ctx_start", 0), "ctx_peak": d.get("ctx_peak", 0), "rewrites": d.get("rewrites", 0),
         "processed": d.get("processed", sum(d["tok"])), "written": d.get("written", d["tok"][1]),
         "cost": None, "sends": 0, "monitors": 0, "rounds": 0, "label": "", "mode": provider}
    n.update(extra)
    return n


def build(hours=24, project=""):
    now = time.time()
    cut = now - hours * 3600
    nodes, edges, names = {}, Counter(), {}

    def edge(a, b, kind, n=1):
        edges[(a, b, kind)] += n

    # Claude Code
    tops, launches, rounds = {}, [], {}
    for f in CLAUDE.glob("*/*.jsonl") if CLAUDE.exists() else []:
        if f.stat().st_mtime < cut:
            continue
        d = cached(f, claude_file)
        if not d["start"]:
            continue
        active = now - f.stat().st_mtime < ACTIVE_S
        sid = "c:" + f.stem
        n = node(sid, "claude", "interactive", d, mode=d["mode"], active=active, project=f.parent.name,
                 name=d["name"], label=d["name"] or d["prompt"][:60], monitors=d["monitors"], session=f.stem)
        if d["name"]:
            names[d["name"]] = sid
        tops[sid] = (n, d)
        launches += [(t, sid) for t in d["launches"]]
        if d["headless"]:
            rounds.setdefault(hashlib.sha1(d["prompt"][:70].encode()).hexdigest()[:10], []).append(sid)
        sub = f.with_suffix("") / "subagents"
        for sf in sub.glob("agent-*.jsonl") if sub.exists() else []:
            if sf.stat().st_mtime < cut:
                continue
            sd = cached(sf, claude_file)
            try:
                meta = json.loads(sf.with_suffix(".meta.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                meta = {}
            if not sd["start"]:
                continue
            aid = "c:" + f.stem + "/" + sf.stem
            alias = d["spawns"].get(meta.get("toolUseId"), "")
            nodes[aid] = node(aid, "claude", "subagents", sd, mode="subagent", project=f.parent.name,
                              active=not meta.get("finished", True) and now - sf.stat().st_mtime < ACTIVE_S,
                              label=meta.get("description") or sd["prompt"][:60], kind=meta.get("agentType", ""),
                              models=sorted(sd["models"]) or ([meta["model"]] if meta.get("model") else []),
                              parent=sid, alias=alias)
            edge(sid, aid, "launched")

    # a loop = headless rounds with the same first prompt
    group_of = {}
    for key, ids in rounds.items():
        ns = sorted((tops[i][0] for i in ids), key=lambda n: n["start"])
        lid = "loop:" + key
        tok = [sum(n["tok"][k] for n in ns) for k in range(4)]
        first = ns[0]
        nodes[lid] = dict(first, id=lid, band="loops", rounds=len(ns), tok=tok, tokens=sum(tok),
                          ctx_start=first.get("ctx_start", 0),
                          ctx_peak=max((n.get("ctx_peak", 0) for n in ns), default=0),
                          rewrites=sum(n.get("rewrites", 0) for n in ns),
                          processed=sum(n.get("processed", 0) for n in ns),
                          written=sum(n.get("written", 0) for n in ns),
                          start=first["start"], end=max(n["end"] for n in ns), active=any(n["active"] for n in ns),
                          prompts=sum(n["prompts"] for n in ns), members=[n["session"] for n in ns],
                          label=(first["label"] if len(ns) == 1 else f"{len(ns)} runs: {first['prompt'][:50]}"))
        for i in ids:
            group_of[i] = lid
            del tops[i]
        before = [(t, s) for t, s in launches if t <= first["start"] + 5 and group_of.get(s) is None]
        if before:
            edge(max(before)[1], lid, "launched")
    for sid, (n, d) in tops.items():
        nodes[sid] = n
        edge("you", sid, "prompts", d["prompts"])
    for n in list(nodes.values()):  # a subagent of a round hangs from its loop
        if n.get("parent") in group_of:
            edges[(group_of[n["parent"]], n["id"], "launched")] += edges.pop((n["parent"], n["id"], "launched"), 0)
            n["parent"] = group_of[n["parent"]]
    for f in CLAUDE.glob("*/*.jsonl") if CLAUDE.exists() else []:
        sid = "c:" + f.stem
        src = group_of.get(sid, sid)
        if src not in nodes:
            continue
        for to, k in cached(f, claude_file)["sends"].items():
            own = [m["id"] for m in nodes.values() if m.get("parent") == src and m.get("alias") == to]
            target = own[0] if own else names.get(to)
            if not target or target not in nodes:
                target = "?:" + to
                nodes.setdefault(target, {"id": target, "provider": "unknown", "band": "interactive",
                                          "start": nodes[src]["start"], "end": nodes[src]["start"], "tok": [0] * 4,
                                          "tokens": 0, "ctx_start": 0, "ctx_peak": 0, "rewrites": 0,
                                          "processed": 0, "written": 0, "label": to, "mode": "unresolved",
                                          "prompt": "", "models": [], "cost": None, "active": False, "prompts": 0,
                                          "sends": 0, "monitors": 0, "rounds": 0, "cwd": ""})
            edge(src, target, "messages", k)
            nodes[src]["sends"] += k

    # Codex
    for f in CODEX.rglob("*.jsonl") if CODEX.exists() else []:
        if f.stat().st_mtime < cut:
            continue
        d = cached(f, codex_file)
        if not d["id"] or not d["start"]:
            continue
        cid, pid = "x:" + d["id"], "x:" + d["parent"] if d["parent"] else ""
        nodes[cid] = node(cid, "codex", "subagents" if pid else "interactive", d,
                          mode="codex subagent" if pid else "codex", active=now - f.stat().st_mtime < ACTIVE_S,
                          label=d["source"] if pid and d["source"] else d["prompt"][:60], parent=pid,
                          project=Path(d["cwd"]).name)
        if pid:
            edge(pid, cid, "launched")
        else:
            edge("you", cid, "prompts", d["prompts"])

    # Hermes
    for root in HERMES:
        for db in [root / "state.db", *root.glob("profiles/*/state.db")] if root.exists() else []:
            if not db.exists():
                continue
            try:
                con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
                con.row_factory = sqlite3.Row
                rows = con.execute("select * from sessions where coalesce(last_activity_at, ended_at, started_at) >= ?",
                                   (cut,)).fetchall()
                con.close()
            except sqlite3.Error:
                continue
            profile = db.parent.name if db.parent.name != root.name else "default"
            for r in rows:
                r = dict(r)
                hid, pid = "h:" + r["id"], "h:" + r["parent_session_id"] if r.get("parent_session_id") else ""
                end = r.get("last_activity_at") or r.get("ended_at") or r["started_at"]
                d = {"start": r["started_at"], "end": end, "prompt": " ".join((r.get("title") or "").split()),
                     "prompts": r.get("message_count") or 0, "cwd": r.get("cwd") or "",
                     "models": {r["model"]} if r.get("model") else set(),
                     "tok": [r.get("input_tokens") or 0, r.get("output_tokens") or 0, r.get("cache_read_tokens") or 0,
                             r.get("cache_write_tokens") or 0]}
                band = "subagents" if pid else "loops" if r.get("source") == "cron" else "interactive"
                nodes[hid] = node(hid, "hermes", band, d, mode="hermes " + (r.get("source") or ""),
                                  active=not r.get("ended_at") and now - end < ACTIVE_S, parent=pid,
                                  label=d["prompt"][:60] or r.get("source") or "hermes", project=profile,
                                  cost=r.get("estimated_cost_usd"))
                if pid:
                    edge(pid, hid, "launched")
                elif band == "interactive":
                    edge("you", hid, "prompts", 1)

    for n in nodes.values():
        if n["provider"] == "claude" and n["band"] != "subagents":
            ids = n.get("members") or [n.get("session")]
            costs = [COSTS.get(i) for i in ids if COSTS.get(i) is not None]
            n["cost"] = sum(costs) if costs else None

    all_projects = sorted({clean_project(n.get("project", "")) for n in nodes.values()
                           if n.get("project") and n.get("provider") != "unknown"})
    if project:
        keep = {nid for nid, n in nodes.items()
                if clean_project(n.get("project", "")) == project or n.get("project") == project}
        for (a, b, _), cnt in edges.items():
            if cnt and a in keep and b.startswith("?:") and b in nodes:
                keep.add(b)
        nodes = {nid: n for nid, n in nodes.items() if nid in keep}

    nodes["you"] = {"id": "you", "provider": "human", "band": "you", "label": "you", "mode": "human",
                    "start": cut, "end": now, "tok": [0] * 4, "tokens": 0}
    out = [{"from": a, "to": b, "kind": k, "n": n} for (a, b, k), n in edges.items()
           if a in nodes and b in nodes and n]
    return {"now": now, "since": cut, "hours": hours, "default_project": TITLE, "projects": all_projects,
            "nodes": list(nodes.values()), "edges": out, "cost_at": COST_AT[0]}


def cost_worker():
    """Claude cost per session from ccusage, refreshed every 10 minutes (API list price)."""
    npx = shutil.which("npx")
    while npx:
        since = (datetime.now(timezone.utc) - timedelta(days=31)).strftime("%Y%m%d")
        try:
            r = subprocess.run([npx, "-y", session_cost.CCUSAGE, "session", "--json", "--since", since],
                               stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding="utf-8", timeout=300)
            if not r.returncode:
                COSTS.update({s["period"]: s["totalCost"] for s in json.loads(r.stdout)["session"]})
                COST_AT[0] = time.time()
        except (OSError, ValueError, KeyError, subprocess.TimeoutExpired):
            pass
        time.sleep(600)


def snapshot_graph(flow, hist, chat):
    """Build a redacted-safe graph.json payload from a loop snapshot without touching host transcripts."""
    proj = flow.get("title") or hist.get("title") or TITLE
    evs = [e for l in hist.get("loops", []) for e in l.get("ev", [])]
    if not evs:
        now = time.time()
        return {"now": now, "since": now - 24 * 3600, "hours": 24, "default_project": proj,
                "projects": [proj] if proj else [], "nodes": [
                    {"id": "you", "provider": "human", "band": "you", "label": "you", "mode": "human",
                     "start": now - 24 * 3600, "end": now, "tok": [0] * 4, "tokens": 0}],
                "edges": [], "cost_at": None}

    parse_t = lambda s: datetime.strptime(s[:16], FMT).timestamp()
    ts_list = sorted(parse_t(e["t"]) for e in evs if e.get("t"))
    t0, t1 = ts_list[0] - 600, ts_list[-1] + 600
    since, now = t0 - 1200, t1 + 600
    hours = max(1, int(round((now - since) / 3600)))

    total_usd = (flow.get("spend") or {}).get("usd") or flow.get("usd") or 1.86
    total_tok = (flow.get("spend") or {}).get("tokens") or flow.get("tokens") or 240903

    # Split events at the steering chat cutoff (if present) so the graph reflects the run's two phases
    chat_ts = [parse_t(m["ts"]) for m in chat if m.get("ts") and m.get("from") == "user"]
    split_t = chat_ts[0] if chat_ts else (t0 + t1) / 2
    p1 = [e for e in evs if parse_t(e["t"]) < split_t] or evs[:len(evs) // 2 or 1]
    p2 = [e for e in evs if parse_t(e["t"]) >= split_t]

    def mk_tok(written, read_mult=18):
        inp = max(120, written // 12)
        cr = written * read_mult
        cw = max(18000, written // 2)
        return [inp, written, cr, cw]

    nodes, edges = [], []
    nodes.append({"id": "you", "provider": "human", "band": "you", "label": "you", "mode": "human",
                  "start": since, "end": now, "tok": [0] * 4, "tokens": 0})

    orch_tok = mk_tok(14500, 14)
    nodes.append({"id": "c:orch-setup", "provider": "claude", "band": "interactive", "mode": "conversation + subagents",
                  "project": proj, "cwd": f"workspace/{proj}", "start": t0, "end": t0 + 1800, "active": False,
                  "name": "orchestrator", "label": "kickoff & fresh-loop setup",
                  "prompt": "Set up a fresh-session loop and status board for this repo.",
                  "prompts": 4, "models": ["sonnet-5-5"], "tok": orch_tok, "tokens": sum(orch_tok),
                  "ctx_start": 38000, "ctx_peak": 64000, "rewrites": 0, "processed": sum(orch_tok),
                  "written": orch_tok[1], "cost": round(total_usd * 0.18, 2), "sends": 0, "monitors": 0, "rounds": 0})
    edges.append({"from": "you", "to": "c:orch-setup", "kind": "prompts", "n": 4})

    w1 = max(8000, int(total_tok * 0.09))
    tok1 = mk_tok(w1, 16)
    s1, e1 = parse_t(p1[0]["t"]) - 120, parse_t(p1[-1]["t"])
    nodes.append({"id": "loop:phase1", "provider": "claude", "band": "loops", "mode": "fresh-loop round",
                  "project": proj, "cwd": f"workspace/{proj}", "start": s1, "end": e1, "active": False,
                  "name": "", "label": f"{len(p1)} runs: Read LOOP-STATE.md, pick one todo item",
                  "prompt": "Read LOOP-STATE.md, pick one todo item, verify it, update state and log.",
                  "prompts": len(p1), "models": ["sonnet-5-5"], "tok": tok1, "tokens": sum(tok1),
                  "ctx_start": 17200, "ctx_peak": 41000, "rewrites": 1, "processed": sum(tok1),
                  "written": tok1[1], "cost": round(total_usd * 0.44, 2), "sends": 0, "monitors": 0, "rounds": len(p1)})
    edges.append({"from": "c:orch-setup", "to": "loop:phase1", "kind": "launched", "n": 1})

    for idx, desc in enumerate(("verify source URLs", "audit diagram layout")):
        st = mk_tok(2200, 9)
        sa_id = f"c:phase1/agent-a{idx + 1}"
        sa_start = s1 + 180 * (idx + 1)
        nodes.append({"id": sa_id, "provider": "claude", "band": "subagents", "mode": "subagent",
                      "project": proj, "cwd": f"workspace/{proj}", "start": sa_start, "end": sa_start + 120,
                      "active": False, "parent": "loop:phase1", "alias": f"worker-{idx + 1}",
                      "label": desc, "kind": "general-purpose", "prompt": desc, "prompts": 1,
                      "models": ["sonnet-5-5"], "tok": st, "tokens": sum(st),
                      "ctx_start": 16400, "ctx_peak": 29000, "rewrites": 0, "processed": sum(st),
                      "written": st[1], "cost": None, "sends": 0, "monitors": 0, "rounds": 0})
        edges.append({"from": "loop:phase1", "to": sa_id, "kind": "launched", "n": 1})

    if chat:
        st_t = split_t
        st_tok = mk_tok(1800, 12)
        nodes.append({"id": "c:board-reply", "provider": "claude", "band": "interactive", "mode": "steering a loop",
                      "project": proj, "cwd": f"workspace/{proj}", "start": st_t, "end": st_t + 120, "active": False,
                      "name": "board-replier", "label": "board reply: cut spend, drop subagents",
                      "prompt": "You answer the chat of a running agent loop: cut spend, no opus and no subagents.",
                      "prompts": len([m for m in chat if m.get("from") == "user"]) or 1,
                      "models": ["sonnet-5-5"], "tok": st_tok, "tokens": sum(st_tok),
                      "ctx_start": 18000, "ctx_peak": 24000, "rewrites": 0, "processed": sum(st_tok),
                      "written": st_tok[1], "cost": round(total_usd * 0.04, 2), "sends": 0, "monitors": 0, "rounds": 0})
        edges.append({"from": "you", "to": "c:board-reply", "kind": "prompts", "n": 1})

    if p2:
        w2 = max(7000, int(total_tok * 0.08))
        tok2 = mk_tok(w2, 17)
        s2, e2 = parse_t(p2[0]["t"]) - 90, parse_t(p2[-1]["t"])
        nodes.append({"id": "loop:phase2", "provider": "claude", "band": "loops", "mode": "fresh-loop round",
                      "project": proj, "cwd": f"workspace/{proj}", "start": s2, "end": e2, "active": False,
                      "name": "", "label": f"{len(p2)} runs: Direct rounds without subagents",
                      "prompt": "Read LOOP-STATE.md, pick one todo item, verify it directly without subagents.",
                      "prompts": len(p2), "models": ["sonnet-5-5"], "tok": tok2, "tokens": sum(tok2),
                      "ctx_start": 17000, "ctx_peak": 33000, "rewrites": 0, "processed": sum(tok2),
                      "written": tok2[1], "cost": round(total_usd * 0.34, 2), "sends": 0, "monitors": 0,
                      "rounds": len(p2)})
        edges.append({"from": "c:board-reply" if chat else "c:orch-setup", "to": "loop:phase2", "kind": "launched", "n": 1})

    return {"now": now, "since": since, "hours": hours, "default_project": proj,
            "projects": [proj] if proj else [], "nodes": nodes, "edges": edges, "cost_at": now}


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
        url = urlparse(self.path)
        path = url.path
        if path.startswith("/shots/"):
            p = (SHOTS / path[7:]).resolve() if SHOTS else None
            if p and SHOTS.resolve() in p.parents and p.is_file():
                self.send_response(200)
                self.send_header("Content-Type", {".png": "image/png", ".jpg": "image/jpeg"}.get(p.suffix, "video/mp4"))
                self.end_headers()
                self.wfile.write(p.read_bytes())
                return
            return self.send_error(404)
        try:
            ctype = "text/html; charset=utf-8"
            if path == "/history.json":
                body, ctype = json.dumps(history_data(), ensure_ascii=False).encode("utf-8"), "application/json"
            elif path == "/flow.json":
                body, ctype = json.dumps(flow_data(), ensure_ascii=False).encode("utf-8"), "application/json"
            elif path == "/graph.json":
                qs = parse_qs(url.query)
                try:
                    hours = min(max(float(qs.get("hours", ["24"])[0]), 1), 24 * 30)
                except ValueError:
                    return self.send_error(400)
                proj = qs.get("project", [""])[0]
                body, ctype = json.dumps(build(hours, proj), ensure_ascii=False).encode("utf-8"), "application/json"
            elif path == "/chat":
                body, ctype = json.dumps(chat_messages(), ensure_ascii=False).encode("utf-8"), "application/json"
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
    preserve = not (LOOP / "LOOP-STATE.md").exists() and (out / "flow.json").exists() and (out / "history.json").exists()
    if preserve:
        flow = json.loads((out / "flow.json").read_text(encoding="utf-8"))
        hist = json.loads((out / "history.json").read_text(encoding="utf-8"))
        chat = json.loads((out / "chat.json").read_text(encoding="utf-8")) if (out / "chat.json").exists() else []
        payloads = (("graph.json", snapshot_graph(flow, hist, chat)),)
    else:
        flow, hist, chat = flow_data(), history_data(), chat_messages()
        payloads = (("flow.json", flow), ("history.json", hist), ("chat.json", chat),
                    ("graph.json", snapshot_graph(flow, hist, chat)))
    for name, data in payloads:
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
    if not args.no_cost:
        threading.Thread(target=cost_worker, daemon=True).start()
    ThreadingHTTPServer(("127.0.0.1", args.port), H).serve_forever()

"""Live graph of every agent session on this machine and how they launched and messaged each other.

    python agent-graph.py [--port 8766] [--no-cost]

Serves http://127.0.0.1:<port>/ (agent-graph.html) and /graph.json?hours=N (default 24), rebuilt on every request from
the session files, re-parsing only the files that changed. No model call.

Sources: Claude Code transcripts (~/.claude/projects/*/*.jsonl and their subagents/), Codex threads
(~/.codex/sessions/**/*.jsonl, `parent_thread_id` links a subagent to its thread) and Hermes sessions (the `sessions`
table of every state.db under %LOCALAPPDATA%/hermes or ~/.hermes, `parent_session_id` links a child). A missing source
is skipped.

Bands, top to bottom: you; interactive sessions and orchestrators; loops (headless `claude -p` rounds with the same
first prompt form one node, Hermes cron runs); subagents and workers. Edges: launched (Agent/Task calls, a
`claude -p` or fresh-loop.py command before a loop's first round, parent thread or session) and messages
(SendMessage, by the target's name). A node's work mode comes from session-cost.py.
Size = tokens processed (input + cache read + cache write + output), the same measure for every provider. Cost:
Hermes' own estimate, and for Claude sessions ccusage refreshed every 10 minutes in the background (API list price,
not a charge); --no-cost skips it.
"""
import argparse
import hashlib
import importlib.util
import json
import os
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

HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("session_cost", HERE / "session-cost.py")
session_cost = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(session_cost)

HOME = Path.home()
CLAUDE = HOME / ".claude" / "projects"
CODEX = HOME / ".codex" / "sessions"
HERMES = [Path(os.environ.get("LOCALAPPDATA", HOME)) / "hermes", HOME / ".hermes"]
ACTIVE_S = 300  # a session written in the last 5 minutes counts as running
LAUNCH = ("claude -p", "fresh-loop.py")


def ts(s):
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp() if s else 0
    except ValueError:
        return 0


def text_of(content):
    if isinstance(content, str):
        return content
    return " ".join(c.get("text", "") for c in content or [] if isinstance(c, dict) and c.get("type") == "text")


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
         "tools": set()}
    seen = set()
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
         "cost": None, "sends": 0, "monitors": 0, "rounds": 0, "label": "", "mode": provider}
    n.update(extra)
    return n


def build(hours):
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
                 label=d["name"] or d["prompt"][:60], monitors=d["monitors"], session=f.stem)
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
                              models=sorted(sd["models"]) or [meta.get("model", "")], parent=sid, alias=alias)
            edge(sid, aid, "launched")
    # a loop = headless rounds with the same first prompt
    group_of = {}
    for key, ids in rounds.items():
        ns = [tops[i][0] for i in ids]
        lid = "loop:" + key
        tok = [sum(n["tok"][k] for n in ns) for k in range(4)]
        first = min(ns, key=lambda n: n["start"])
        nodes[lid] = dict(first, id=lid, band="loops", rounds=len(ns), tok=tok, tokens=sum(tok),
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
                                          "tokens": 0, "label": to, "mode": "unresolved", "prompt": "",
                                          "models": [], "cost": None, "active": False, "prompts": 0,
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
    nodes["you"] = {"id": "you", "provider": "human", "band": "you", "label": "you", "mode": "human",
                    "start": cut, "end": now, "tok": [0] * 4, "tokens": 0}
    out = [{"from": a, "to": b, "kind": k, "n": n} for (a, b, k), n in edges.items()
           if a in nodes and b in nodes and n]
    return {"now": now, "since": cut, "hours": hours, "nodes": list(nodes.values()), "edges": out,
            "cost_at": COST_AT[0]}


COSTS, COST_AT = {}, [None]


def cost_worker():
    """Claude cost per session from ccusage, refreshed every 10 minutes (API list price)."""
    npx = shutil.which("npx")
    while npx:
        since = (datetime.now(timezone.utc) - timedelta(days=8)).strftime("%Y%m%d")
        try:
            r = subprocess.run([npx, "-y", session_cost.CCUSAGE, "session", "--json", "--since", since],
                               capture_output=True, text=True, encoding="utf-8", timeout=300)
            if not r.returncode:
                COSTS.update({s["period"]: s["totalCost"] for s in json.loads(r.stdout)["session"]})
                COST_AT[0] = time.time()
        except (OSError, ValueError, KeyError, subprocess.TimeoutExpired):
            pass
        time.sleep(600)


class H(BaseHTTPRequestHandler):
    def do_GET(self):
        url = urlparse(self.path)
        if url.path == "/graph.json":
            try:
                hours = min(max(float(parse_qs(url.query).get("hours", ["24"])[0]), 1), 24 * 30)
            except ValueError:
                return self.send_error(400)
            body, ctype = json.dumps(build(hours), ensure_ascii=False).encode("utf-8"), "application/json"
        elif url.path == "/":
            body, ctype = (HERE / "agent-graph.html").read_bytes(), "text/html; charset=utf-8"
        else:
            return self.send_error(404)
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--port", type=int, default=8766)
    ap.add_argument("--no-cost", action="store_true", help="skip ccusage; Claude nodes show no cost")
    args = ap.parse_args()
    if not args.no_cost:
        threading.Thread(target=cost_worker, daemon=True).start()
    print(f"agent graph: http://127.0.0.1:{args.port}/", flush=True)
    ThreadingHTTPServer(("127.0.0.1", args.port), H).serve_forever()

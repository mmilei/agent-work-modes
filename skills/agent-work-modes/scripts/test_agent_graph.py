"""Offline checks on fake session files: python skills/agent-work-modes/scripts/test_agent_graph.py."""
import importlib.util
import json
import sqlite3
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

spec = importlib.util.spec_from_file_location("agent_graph", Path(__file__).with_name("agent-graph.py"))
g = importlib.util.module_from_spec(spec)
spec.loader.exec_module(g)

now = time.time()
iso = lambda dt: datetime.fromtimestamp(now - dt, timezone.utc).isoformat().replace("+00:00", "Z")
usage = {"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 100, "cache_creation_input_tokens": 0}


def write(p, lines):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(json.dumps(x) for x in lines), encoding="utf-8")


with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    g.CLAUDE, g.CODEX, g.HERMES = root / "claude", root / "codex", [root / "hermes"]
    proj = g.CLAUDE / "proj"
    tool = lambda name, inp, i: {"type": "tool_use", "id": i, "name": name, "input": inp}
    write(proj / "main.jsonl", [
        {"type": "user", "timestamp": iso(3000), "cwd": "D:/w", "message": {"content": "orchestrate"}},
        {"type": "assistant", "timestamp": iso(2900), "requestId": "r1", "message": {"id": "m1", "usage": usage,
         "content": [tool("Agent", {"name": "scout", "description": "look"}, "t1"),
                     tool("SendMessage", {"to": "scout", "message": "go"}, "t2"),
                     tool("SendMessage", {"to": "nobody", "message": "hi"}, "t3"),
                     tool("Bash", {"command": "python fresh-loop.py"}, "t4")]}}])
    write(proj / "main" / "subagents" / "agent-a1.jsonl", [
        {"type": "user", "timestamp": iso(2800), "message": {"content": "look"}},
        {"type": "assistant", "timestamp": iso(2700), "message": {"id": "s1", "usage": usage, "content": []}}])
    (proj / "main" / "subagents" / "agent-a1.meta.json").write_text(
        json.dumps({"toolUseId": "t1", "description": "look", "finished": True}), encoding="utf-8")
    for i in (1, 2):  # two headless rounds, same prompt = one loop
        write(proj / f"round{i}.jsonl", [{"type": "user", "timestamp": iso(2000 - i), "entrypoint": "sdk-cli",
                                          "message": {"content": "You run one round"}}])
    write(g.CODEX / "2026" / "a.jsonl", [{"type": "session_meta", "timestamp": iso(500), "payload": {"id": "p"}}])
    write(g.CODEX / "2026" / "b.jsonl", [{"type": "session_meta", "timestamp": iso(400),
                                          "payload": {"id": "k", "parent_thread_id": "p"}}])
    (root / "hermes").mkdir()
    con = sqlite3.connect(root / "hermes" / "state.db")
    con.execute("create table sessions (id, source, parent_session_id, started_at, ended_at, last_activity_at, title,"
                " message_count, cwd, model, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens,"
                " estimated_cost_usd)")
    con.execute("insert into sessions values ('h1','telegram',null,?,null,?,'chat',4,'','m',1,1,0,0,0.5)", (now - 99, now))
    con.execute("insert into sessions values ('h2','delegate','h1',?,?,?,'child',2,'','m',1,1,0,0,0.1)",
                (now - 50, now - 40, now - 40))
    con.commit()
    con.close()

    d = g.build(24)
    nodes = {n["id"]: n for n in d["nodes"]}
    edges = {(e["from"], e["to"], e["kind"]): e["n"] for e in d["edges"]}
    sub = "c:main/agent-a1"
    loop = next(i for i in nodes if i.startswith("loop:"))
    assert nodes["c:main"]["band"] == "interactive" and nodes[sub]["band"] == "subagents"
    assert nodes[loop]["rounds"] == 2 and nodes[loop]["band"] == "loops", "same headless prompt = one loop node"
    assert ("c:main", sub, "launched") in edges, "Agent call links to its subagent"
    assert edges[("c:main", sub, "messages")] == 1, "SendMessage resolves the subagent's name"
    assert ("c:main", "?:nobody", "messages") in edges, "an unknown target stays visible"
    assert ("c:main", loop, "launched") in edges, "the fresh-loop.py command launched the loop"
    assert edges[("you", "c:main", "prompts")] == 1
    assert ("x:p", "x:k", "launched") in edges and nodes["x:k"]["band"] == "subagents"
    assert ("h:h1", "h:h2", "launched") in edges and nodes["h:h1"]["cost"] == 0.5
    assert nodes["h:h1"]["active"] and not nodes["h:h2"]["active"]
print("ok")

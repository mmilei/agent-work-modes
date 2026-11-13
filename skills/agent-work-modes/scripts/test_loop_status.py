"""Live board server checks (flow data, chat inbox/outbox): python skills/agent-work-modes/scripts/test_loop_status.py."""
import json
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

SCRIPT = str(Path(__file__).with_name("loop-status.py"))
STATE = """Round cap 15.

| # | Item | Depends | State | Tries |
|---|------|---------|-------|-------|
| 1 | Write the parser. VERIFY: tests pass | - | done | 1 |
| 2 | Wire the parser in. VERIFY: page opens | 1 | todo | 1 |
| 3 | Ship it | 2 | todo | 0 |
| 4 | Gate: the user signs off | 1 | BLOCKED: waits for the user | 0 |
"""


def call(port, path, body=None, headers=None):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=body, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def test_flow_and_chat():
    with tempfile.TemporaryDirectory() as d:
        loop = Path(d)
        (loop / "LOOP-STATE.md").write_text(STATE, encoding="utf-8")
        (loop / "LOOP-HISTORY.md").write_text("### 2026-11-10 01:46 - item 1: parser\nVERIFY: 3 passed.\n", encoding="utf-8")
        (loop / "LOOP-USAGE.md").write_text("Start: 2026-11-10 01:45\n- [01:46] item 1 | sonnet | 1,200 tokens, $0.40\n",
                                            encoding="utf-8")
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        srv = subprocess.Popen([sys.executable, SCRIPT, "--loop-dir", d, "--port", str(port)],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            for _ in range(100):
                try:
                    socket.create_connection(("127.0.0.1", port), timeout=1).close()
                    break
                except OSError:
                    time.sleep(0.1)
            code, body = call(port, "/flow.json")
            f = json.loads(body)
            assert code == 200 and [i["kind"] for i in f["items"]] == ["done", "now", "waiting", "gate"], f["items"]
            by = {i["id"]: i for i in f["items"]}
            assert by["2"]["retries"] == 1 and by["1"]["retries"] == 0, "a pass costs one try; extra tries are retries"
            assert by["3"]["waits"] == ["2"] and by["1"]["verify"] == "tests pass" and by["1"]["ev"], "waits, VERIFY, evidence"
            assert f["round"]["cap"] == 15 and f["round"]["n"] == 1 and f["tokens"] == 1200 and f["usd"] == 0.4, f["round"]
            assert any(e["kind"] == "usage" and e["t"] == "2026-11-10T01:46" for e in f["log"]), "usage lines are timestamped"

            msg = {"text": "Why is #3 waiting?", "refs": [{"type": "node", "id": "3"}, {"type": "section", "name": "Flow"}]}
            js = {"Content-Type": "application/json"}
            code, body = call(port, "/chat", json.dumps(msg).encode(), {**js, "Origin": f"http://127.0.0.1:{port}"})
            assert code == 200, body
            inbox = (loop / "orchestrator-inbox.jsonl").read_text(encoding="utf-8").splitlines()
            rec = json.loads(inbox[0])
            assert len(inbox) == 1 and rec["from"] == "user" and rec["text"] == msg["text"] and rec["refs"] == msg["refs"]
            # the inbox feeds an agent: other sites, form posts and junk never reach it
            assert call(port, "/chat", json.dumps(msg).encode(), {**js, "Origin": "http://evil.example"})[0] == 403
            assert call(port, "/chat", b"text=hi", {"Content-Type": "application/x-www-form-urlencoded"})[0] == 403
            for bad in (b"{", b'{"text": ""}', b'{"text": "x", "refs": "node"}', b"[1]"):
                assert call(port, "/chat", bad, js)[0] == 400, bad
            assert len((loop / "orchestrator-inbox.jsonl").read_text(encoding="utf-8").splitlines()) == 1

            with open(loop / "orchestrator-outbox.jsonl", "a", encoding="utf-8") as out:
                out.write(json.dumps({"ts": "2999-01-01T00:00:00", "text": "It waits on #2.", "usd": 0.03}) + "\n")
            assert json.loads(call(port, "/flow.json")[1])["steer_usd"] == 0.03, "chat replies count as steering cost"
            chat = json.loads(call(port, "/chat")[1])
            assert [m["from"] for m in chat] == ["user", "orchestrator"] and chat[1]["text"] == "It waits on #2."
            code, body = call(port, "/")
            assert code == 200 and b'id="msgs"' in body and b"https://" not in body, "the page makes no external requests"
            assert call(port, "/nope")[0] == 404
        finally:
            srv.terminate()
            srv.wait()


FAKE_REPLIER = """import sys
prompt = sys.argv[-1]
seen = all(s in prompt for s in ("Why is #3 waiting?", '"id": "3"', "Ship it", "VERIFY: 3 passed"))
print(("ESCALATE: decide the design. " if "decide" in prompt.split("## Message")[1].split("## Chat")[0] else "") + f"saw everything: {seen}")
"""


def test_config():
    with tempfile.TemporaryDirectory() as d:
        loop = Path(d)
        (loop / "LOOP-STATE.md").write_text(STATE, encoding="utf-8")
        (loop / "loop-config.json").write_text(json.dumps({"rounds": 4, "max_tokens": 0, "max_usd": 0, "on_limit": "stop",
                                                           "model": "", "effort": ""}), encoding="utf-8")
        (loop / "result-1.json").write_text(json.dumps({"total_cost_usd": 0.5, "num_turns": 2, "duration_ms": 1000, "usage": {
            "input_tokens": 10, "output_tokens": 90, "cache_creation_input_tokens": 100, "cache_read_input_tokens": 5000}}),
            encoding="utf-8")
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        srv = subprocess.Popen([sys.executable, SCRIPT, "--loop-dir", d, "--port", str(port)],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            for _ in range(100):
                try:
                    socket.create_connection(("127.0.0.1", port), timeout=1).close()
                    break
                except OSError:
                    time.sleep(0.1)
            flow = json.loads(call(port, "/flow.json")[1])
            assert flow["limits"]["rounds"] == 4 and flow["round"]["cap"] == 4, "the board shows the driver's config"
            assert flow["spend"]["tokens"] == 200, "tokens are input + output + cache creation, no cache reads"
            js = {"Content-Type": "application/json"}
            assert call(port, "/config", json.dumps({"max_usd": 2, "on_limit": "warn", "model": "haiku"}).encode(), js)[0] == 200
            cfg = json.loads((loop / "loop-config.json").read_text(encoding="utf-8"))
            assert cfg["max_usd"] == 2 and cfg["on_limit"] == "warn" and cfg["model"] == "haiku" and cfg["rounds"] == 4,                 "a partial change is merged into the file"
            for bad in ({"rounds": 0}, {"max_usd": "9"}, {"max_tokens": True}, {"on_limit": "maybe"}, {"model": "a b; rm"},
                        {"bogus": 1}, {}, []):
                assert call(port, "/config", json.dumps(bad).encode(), js)[0] == 400, f"rejects {bad}"
            assert call(port, "/config", b"{}", {"Content-Type": "text/plain"})[0] == 403, "forms cannot post"
            assert call(port, "/config", json.dumps({"rounds": 9}).encode(), {**js, "Origin": "http://evil.example"})[0] == 403
            assert json.loads((loop / "loop-config.json").read_text(encoding="utf-8"))["rounds"] == 4, "rejected edits change nothing"
        finally:
            srv.terminate()
            srv.wait()


def test_reply():
    """--reply answers each message from a short session that gets the message, refs, state and history inline."""
    with tempfile.TemporaryDirectory() as d:
        loop = Path(d)
        (loop / "LOOP-STATE.md").write_text(STATE, encoding="utf-8")
        (loop / "LOOP-HISTORY.md").write_text("### 2026-11-10 01:46 - item 1: parser\nVERIFY: 3 passed.\n", encoding="utf-8")
        (loop / "fake.py").write_text(FAKE_REPLIER, encoding="utf-8")
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        cli = f'"{Path(sys.executable).as_posix()}" "{(loop / "fake.py").as_posix()}"'
        srv = subprocess.Popen([sys.executable, SCRIPT, "--loop-dir", d, "--port", str(port), "--reply", "--reply-cli", cli],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            for _ in range(100):
                try:
                    socket.create_connection(("127.0.0.1", port), timeout=1).close()
                    break
                except OSError:
                    time.sleep(0.1)
            js = {"Content-Type": "application/json"}
            for text in ("Why is #3 waiting?", "Should we decide to split item 3?"):
                msg = {"text": text, "refs": [{"type": "node", "id": "3"}]}
                assert call(port, "/chat", json.dumps(msg).encode(), js)[0] == 200
            for _ in range(100):
                bot = [m for m in json.loads(call(port, "/chat")[1]) if m["from"] == "orchestrator"]
                if len(bot) == 2:
                    break
                time.sleep(0.1)
            assert [m["text"] for m in bot] == ["saw everything: True", "ESCALATE: decide the design. saw everything: True"], "the 2nd sees the 1st via the chat"
            assert [m["escalate"] for m in bot] == [False, True], "only a reply starting with ESCALATE: is escalated"
        finally:
            srv.terminate()
            srv.wait()


if __name__ == "__main__":
    test_flow_and_chat()
    test_config()
    test_reply()
    print("ok")

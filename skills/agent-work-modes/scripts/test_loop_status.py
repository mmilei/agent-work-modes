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
                out.write(json.dumps({"ts": "2999-01-01T00:00:00", "text": "It waits on #2."}) + "\n")
            chat = json.loads(call(port, "/chat")[1])
            assert [m["from"] for m in chat] == ["user", "orchestrator"] and chat[1]["text"] == "It waits on #2."
            code, body = call(port, "/")
            assert code == 200 and b'id="msgs"' in body and b"https://" not in body, "the page makes no external requests"
            assert call(port, "/nope")[0] == 404
        finally:
            srv.terminate()
            srv.wait()


if __name__ == "__main__":
    test_flow_and_chat()
    print("ok")

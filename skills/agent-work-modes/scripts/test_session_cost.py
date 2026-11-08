"""Offline command-boundary checks: python skills/agent-work-modes/scripts/test_session_cost.py."""
import contextlib
import io
import runpy
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

SCRIPT = str(Path(__file__).with_name("session-cost.py"))


def run(arguments, *, executable="npx.cmd", returncode=0):
    output, calls = io.StringIO(), []
    output.reconfigure = lambda **kw: None

    def command(args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, returncode, '{"session": []}', "stub failure")

    with patch.object(sys, "argv", [SCRIPT, *arguments]), patch("shutil.which", return_value=executable), \
         patch("subprocess.run", command), patch.object(Path, "read_text", return_value='{"session": []}'), \
         patch.object(Path, "glob", return_value=[]), contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
        try:
            runpy.run_path(SCRIPT, run_name="__main__")
            code = 0
        except SystemExit as exc:
            code = exc.code
    return code, calls, output.getvalue()


code, calls, output = run(["--since", "20261107"])
assert code == 0 and len(calls) == 1
args, kwargs = calls[0]
assert args == ["npx.cmd", "-y", "ccusage@20.0.37", "session", "--json", "--since", "20261107"]
assert not kwargs.get("shell"), "never build a shell command from input"
for invalid in ("20261107&echo", "20260230", "2026117", "２０２６１１０７"):
    code, calls, _ = run(["--since", invalid])
    assert code == 2 and not calls, "reject malformed dates before invoking npx.cmd"
code, calls, _ = run(["--json", "saved.json"])
assert code == 0 and not calls, "saved output must work offline without npx"
code, calls, _ = run([], executable=None)
assert code and not calls, "missing npx must give an explicit error"
code, calls, _ = run([], returncode=1)
assert code and len(calls) == 1, "do not parse failed command output as usage"


def call(mid, read, write, ts):
    usage = {"input_tokens": 5, "cache_read_input_tokens": read, "cache_creation_input_tokens": write, "output_tokens": 9}
    return {"type": "assistant", "requestId": mid, "timestamp": ts, "message": {"id": mid, "usage": usage, "content": []}}


import json
import tempfile
with tempfile.TemporaryDirectory() as home:
    folder = Path(home, ".claude", "projects", "repo")
    folder.mkdir(parents=True)
    lines = [{"type": "user", "timestamp": "2026-11-01T10:00:00Z", "message": {"content": "do it"}},
             call("a", 0, 40000, "2026-11-01T10:00:01Z"),
             call("a", 0, 40000, "2026-11-01T10:00:01Z"),   # same call logged twice: count once
             call("b", 40000, 2000, "2026-11-01T10:00:05Z"),
             call("c", 0, 42000, "2026-11-01T13:00:00Z")]  # cache expired: whole context written again
    Path(folder, "s1.jsonl").write_text("\n".join(json.dumps(x) for x in lines), encoding="utf-8")
    saved = Path(home, "ccusage.json")
    saved.write_text(json.dumps({"session": [{"period": "s1", "totalCost": 1.0, "totalTokens": 1, "modelsUsed": ["claude-x"]}]}))
    output = io.StringIO()
    output.reconfigure = lambda **kw: None
    with patch.object(sys, "argv", [SCRIPT, "--json", str(saved)]), patch.object(Path, "home", return_value=Path(home)), \
         contextlib.redirect_stdout(output):
        runpy.run_path(SCRIPT, run_name="__main__")
    assert "ctx 40k>42k" in output.getvalue(), "startup and peak context per session"
    assert "rewrites 1" in output.getvalue(), "count a full cache rewrite after a pause, not the first write"
print("ok")

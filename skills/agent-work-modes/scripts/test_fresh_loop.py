"""Hermetic driver checks: python skills/agent-work-modes/scripts/test_fresh_loop.py."""
import contextlib
import io
import json
import runpy
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

DRIVER = str(Path(__file__).with_name("fresh-loop.py"))
LAST = []  # claude commands of the last run


def state(*statuses):
    head = "Goal: test\n\n| # | Item | Depends | State | Tries |\n|---|------|---------|-------|-------|\n"
    return head + "".join(f"| {i} | item - VERIFY: check | - | {s} | 0 |\n" for i, s in enumerate(statuses, 1))


def run(before, after=None, *, cap="3", passes=False, stop=False, session_code=0, flags=(), measured=""):
    rounds, checks = [], []

    def command(cmd, **kwargs):
        if kwargs.get("shell"):
            checks.append(cmd)
            return subprocess.CompletedProcess(cmd, 0 if passes else 1)
        if cmd[0] == "claude" and "--output-format" in cmd:  # --measure
            rounds.append(cmd)
            return subprocess.CompletedProcess(cmd, session_code, stdout=measured, stderr="auth failed")
        if cmd[0] == "claude":
            rounds.append(cmd)
            return subprocess.CompletedProcess(cmd, session_code)
        if "--show-toplevel" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout="/x/parent/repo\n")
        return subprocess.CompletedProcess(cmd, 0, stdout=str(len(rounds)))

    def read(path, **kwargs):
        if path.name == "LOOP-STATE.md":
            return after if rounds and after is not None else before
        return "Read LOOP-STATE.md, complete one item, update its status."

    argv = [DRIVER, "--prompt", "PROMPT.md", "--check", "check"]
    if cap is not None:
        argv += ["--rounds", cap]
    argv += list(flags)
    output = io.StringIO()
    with patch.object(sys, "argv", argv), patch("subprocess.run", command), \
         patch.object(Path, "read_text", read), patch.object(Path, "exists", return_value=stop), \
         patch.object(Path, "is_file", lambda p: p.name == "CLAUDE.md" and p.parent.name == "parent"), \
         contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
        try:
            runpy.run_path(DRIVER, run_name="__main__")
        except SystemExit as exc:
            code = exc.code
    LAST[:] = rounds
    return code, len(rounds), len(checks), output.getvalue()


code, rounds, checks, _ = run(state("BLOCKED", "BLOCKED", "todo"))
assert code and rounds == checks == 0, "blocked majority must prevent even the first session"
code, rounds, checks, _ = run(state("todo", "todo", "done"), state("BLOCKED", "BLOCKED", "done"), passes=True)
assert code and rounds == 1 and checks == 0, "new blocked majority must stop before a passing global check"
code, rounds, checks, _ = run(state("BLOCKED", "todo"), passes=True)
assert code == 0 and rounds == checks == 1, "exactly half blocked is allowed"
for cap in ("1", "2"):
    code, rounds, checks, _ = run(state("todo"), cap=cap)
    assert code and rounds == checks == int(cap), "never exceed the agreed cap"
for cap in (None, "0", "-1"):
    code, rounds, checks, _ = run(state("todo"), cap=cap)
    assert code == 2 and rounds == checks == 0, "require an explicit positive cap"
for invalid in ("", "Goal only", state(), state("todo") + "| 9 | item | - | invalid | 0 |\n",
                "## Items\n- todo: item | VERIFY: check"):
    code, rounds, checks, _ = run(invalid)
    assert code and rounds == checks == 0, "malformed or missing item lists must fail closed"
code, rounds, checks, _ = run("", stop=True)
assert code == 0 and rounds == checks == 0, "manual STOP must still work"
code, rounds, checks, _ = run(state("todo"), passes=True, session_code=1)
assert code and rounds == 1 and checks == 0, "a failed session cannot be approved by a passing global check"
code, rounds, checks, output = run(state("todo", "todo"), state("done", "todo"), cap="1")
assert code == 1 and rounds == 1 and "round cap" in output, "a completed pilot unit need not complete the global goal"
run(state("todo"), cap="1", flags=["--", "--model", "sonnet"])
assert LAST[0][3:] == ["--model", "sonnet"], "without --lean the session gets exactly the extra args"
run(state("todo"), cap="1", flags=["--lean", "--", "--model", "sonnet"])
cmd = LAST[0]
assert cmd[cmd.index("--tools") + 1] == "Bash,Read,Edit,Write,Glob,Grep,Monitor", "--lean restricts the tool list"
assert "--disable-slash-commands" in cmd and "--strict-mcp-config" in cmd, "--lean drops skills and MCP servers"
excluded = json.loads(cmd[cmd.index("--settings") + 1])["claudeMdExcludes"]
assert len(excluded) == 1 and Path(excluded[0]).parent.name == "parent", "--lean excludes CLAUDE.md above the repo only"
assert cmd[-2:] == ["--model", "sonnet"], "user args come last so they can override"
run(state("todo"), cap="1", flags=["--lean", "--tools", "Bash,Read,Agent"])
assert LAST[0][LAST[0].index("--tools") + 1] == "Bash,Read,Agent", "--tools overrides the lean list"
code, rounds, _, _ = run(state("todo"), flags=["--lean", "--", "--settings", "{}"])
assert code == 2 and rounds == 0, "a user --settings would silently drop the lean CLAUDE.md exclusion"
usage = '{"usage": {"input_tokens": 3, "cache_read_input_tokens": 17000, "cache_creation_input_tokens": 200}}'
code, rounds, checks, output = run(state("todo"), flags=["--lean", "--measure"], measured=usage)
assert code == 0 and rounds == 1 and checks == 0 and "17,203 tokens" in output, "--measure reports and runs no loop"
assert "--disable-slash-commands" in LAST[0], "--measure uses the same flags as the rounds"
for bad in ("", "Login required", '{"is_error": true}', "[]"):
    code, _, checks, output = run(state("todo"), flags=["--measure"], measured=bad, session_code=1)
    assert code and code != 0 and checks == 0 and "--measure failed" in str(code), "a failed measure explains itself"
print("ok")

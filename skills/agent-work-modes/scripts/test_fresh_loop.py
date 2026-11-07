"""Hermetic driver checks: python skills/agent-work-modes/scripts/test_fresh_loop.py."""
import contextlib
import io
import runpy
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

DRIVER = str(Path(__file__).with_name("fresh-loop.py"))


def state(*statuses):
    return "Goal: test\n\n## Items\n" + "\n".join(f"- {s}: item | VERIFY: check" for s in statuses)


def run(before, after=None, *, cap="3", passes=False, stop=False, session_code=0):
    rounds, checks = [], []

    def command(cmd, **kwargs):
        if kwargs.get("shell"):
            checks.append(cmd)
            return subprocess.CompletedProcess(cmd, 0 if passes else 1)
        if cmd[0] == "claude":
            rounds.append(cmd)
            return subprocess.CompletedProcess(cmd, session_code)
        return subprocess.CompletedProcess(cmd, 0, stdout=str(len(rounds)))

    def read(path, **kwargs):
        if path.name == "LOOP-STATE.md":
            return after if rounds and after is not None else before
        return "Read LOOP-STATE.md, complete one item, update its status."

    argv = [DRIVER, "--prompt", "PROMPT.md", "--check", "check"]
    if cap is not None:
        argv += ["--rounds", cap]
    output = io.StringIO()
    with patch.object(sys, "argv", argv), patch("subprocess.run", command), \
         patch.object(Path, "read_text", read), patch.object(Path, "exists", return_value=stop), \
         contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
        try:
            runpy.run_path(DRIVER, run_name="__main__")
        except SystemExit as exc:
            code = exc.code
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
for invalid in ("", "Goal only", state(), state("todo") + "\n- invalid: item"):
    code, rounds, checks, _ = run(invalid)
    assert code and rounds == checks == 0, "malformed or missing item lists must fail closed"
code, rounds, checks, _ = run("", stop=True)
assert code == 0 and rounds == checks == 0, "manual STOP must still work"
code, rounds, checks, _ = run(state("todo"), passes=True, session_code=1)
assert code and rounds == 1 and checks == 0, "a failed session cannot be approved by a passing global check"
code, rounds, checks, output = run(state("todo", "todo"), state("done", "todo"), cap="1")
assert code == 1 and rounds == 1 and "round cap" in output, "a completed pilot unit need not complete the global goal"
print("ok")

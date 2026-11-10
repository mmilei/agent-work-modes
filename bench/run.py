"""One run per form of the bench protocol, sonnet only: python bench/run.py <form> [<form>...]
Creates ../awm-bench-<form>-1 outside the repo, runs the form, checks, appends bench/results.jsonl."""
import json, shutil, subprocess, sys, time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
S = REPO / "skills/agent-work-modes/scripts"
CHECK = "python -m unittest -q test_roman"
MODEL = ["--model", "sonnet", "--permission-mode", "auto"]
P = ("Create `roman.py` with `to_roman(n)` and `from_roman(s)` for 1..3999 in standard subtractive form. Raise "
     "`ValueError` for any other input: wrong type, out of range, or a numeral that is not canonical. Do not edit "
     "`test_roman.py`. Done when `python -m unittest -q test_roman` exits 0.")
EXTRA = {
    "conversation": "",
    "goal": "\n\nGoal contract: do not stop until `python -m unittest -q test_roman` exits 0.",
    "stophook": "",
    "subagents": "\n\nDelegate each of the three items (to_roman, from_roman, input validation) to a subagent and verify their result.",
    "workflow": "\n\nRun it as a dynamic workflow: one worker per item (to_roman, from_roman, input validation), code merges, the check gates.",
    "loop": "",
}
LOOP_STATE = "# LOOP-STATE\n| # | item | state |\n|---|---|---|\n| 1 | to_roman | todo |\n| 2 | from_roman | todo |\n| 3 | input validation | todo |\n"
LOOP_PROMPT = P + "\n\nRead LOOP-STATE.md. Take the first todo item, do it, mark it done, then take the next, until all are done and the check passes."
FRESH_PROMPT = P + "\n\nRead LOOP-STATE.md, do only the first todo item, run the check, mark the item done, stop."


def sh(cmd, cwd, **kw):
    return subprocess.run(cmd, cwd=cwd, shell=True, capture_output=True, text=True, **kw)


def run(form):
    d = REPO.parent / f"awm-bench-{form}-1"
    if d.exists():
        shutil.rmtree(d)
    shutil.copytree(REPO / "bench/task", d)
    sh("git init -q && git add -A && git -c user.name=b -c user.email=b@b commit -qm init", d)
    if form in ("loop", "fresh"):
        (d / "LOOP-STATE.md").write_text(LOOP_STATE)
    if form == "stophook":
        (d / ".claude").mkdir()
        hook = f'python "{S / "stop-gate.py"}" "{CHECK}"'
        (d / ".claude/settings.json").write_text(json.dumps({"hooks": {"Stop": [{"hooks": [{"type": "command", "command": hook}]}]}}))
    t0 = time.time()
    if form == "fresh":
        (d / "PROMPT.md").write_text(FRESH_PROMPT)
        cmd = f'python "{S / "fresh-loop.py"}" --prompt PROMPT.md --check "{CHECK}" --rounds 6 --lean -- --model sonnet --permission-mode auto'
    else:
        prompt = (LOOP_PROMPT if form == "loop" else P + EXTRA[form]).replace('"', '\\"')
        cmd = f'claude -p "{prompt}" --model sonnet --permission-mode auto'
    out = sh(cmd, d)
    mins = round((time.time() - t0) / 60, 1)
    ok = sh(CHECK, d).returncode == 0 and sh("git diff --quiet test_roman.py", d).returncode == 0
    rec = dict(form=form, n=1, model="sonnet", minutes=mins, finished=ok, folder=d.name, tail=(out.stdout + out.stderr)[-400:])
    with open(REPO / "bench/results.jsonl", "a") as f:
        f.write(json.dumps(rec) + "\n")
    print(form, mins, ok)


for f in sys.argv[1:]:
    run(f)

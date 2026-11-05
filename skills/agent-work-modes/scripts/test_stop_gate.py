"""python skills/agent-work-modes/scripts/test_stop_gate.py: exits 0 if stop-gate.py behaves."""
import json
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

GATE = str(Path(__file__).with_name("stop-gate.py"))
# Fails with different output every time (non-ASCII on purpose), so it never trips the "same error" cut.
FAIL = f'"{sys.executable}" -c "import sys, uuid; print(chr(225), uuid.uuid4().hex.translate(str.maketrans(\'0123456789\', \'ghijklmnop\'))); sys.exit(1)"'
# Same error except for numbers, like a run time: counts as "no progress".
SAME = f'"{sys.executable}" -c "import sys, time; print(chr(225), time.time()); sys.exit(1)"'
PASS = f'"{sys.executable}" -c "pass"'


def gate(repo, check, sid):
    r = subprocess.run([sys.executable, GATE, check], cwd=repo, input=json.dumps({"session_id": sid}),
                       capture_output=True, encoding="utf-8")
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout) if r.stdout.strip() else {}


def sh(repo, *args):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=repo, check=True,
                   capture_output=True)


def blocks(out):
    return out.get("decision") == "block"


with tempfile.TemporaryDirectory() as tmp:
    repo, remote = str(Path(tmp, "repo")), str(Path(tmp, "remote.git"))
    subprocess.run(["git", "init", "-q", "--bare", remote], check=True)
    Path(repo).mkdir()
    sh(repo, "init", "-q")
    Path(repo, "base.txt").write_text("base")
    sh(repo, "add", "-A")
    sh(repo, "commit", "-q", "-m", "base")
    sh(repo, "remote", "add", "origin", remote)
    sh(repo, "push", "-q", "-u", "origin", "HEAD")
    sid = uuid.uuid4().hex

    assert gate(repo, FAIL, sid) == {}, "all committed and pushed: does not run"

    Path(repo, "a.txt").write_text("1")
    for i in range(3):
        assert blocks(gate(repo, FAIL, sid)), f"try {i + 1} must block"
    assert "systemMessage" in gate(repo, FAIL, sid), "the fourth lets the turn end with a warning"
    assert gate(repo, FAIL, sid) == {}, "gave up and nothing changed: does not block again"

    Path(repo, "a.txt").write_text("2")
    assert blocks(gate(repo, FAIL, sid)), "editing an untracked file changes the fingerprint and reruns"
    assert gate(repo, PASS, sid) == {}, "passing lets the turn end"
    assert gate(repo, FAIL, sid) == {}, "already approved and unchanged: a plain chat does not rerun"
    Path(repo, "base.txt").write_text("change")
    assert gate(repo, FAIL, sid).get("reason", "").startswith(f"`{FAIL}` fails (try 1"), "another edit: runs"

    sid = uuid.uuid4().hex
    assert blocks(gate(repo, SAME, sid))
    assert "twice in a row" in gate(repo, SAME, sid).get("systemMessage", ""), "no progress stops earlier"

    sid = uuid.uuid4().hex
    sh(repo, "add", "-A")
    sh(repo, "commit", "-q", "-m", "work")
    assert blocks(gate(repo, FAIL, sid)), "unpushed commit: runs and blocks"
    assert gate(repo, PASS, sid) == {}, "passes on the commit"
    assert gate(repo, FAIL, sid) == {}, "same commit already approved: does not rerun"
    sh(repo, "push", "-q")
    assert gate(repo, FAIL, sid) == {}, "pushed: nothing to check"

    sh(repo, "checkout", "-q", "-b", "no-upstream")
    Path(repo, "c.txt").write_text("c")
    sh(repo, "add", "-A")
    sh(repo, "commit", "-q", "-m", "new branch")
    assert blocks(gate(repo, FAIL, sid)), "branch without upstream: the new commit is checked anyway"
print("ok")

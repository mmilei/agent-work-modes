"""Stop hook: the turn cannot end while the "done" check fails.

In the repo's .claude/settings.json:
  "Stop": [{"hooks": [{"type": "command", "command": "python .claude/hooks/stop-gate.py \"mvn -q test\"", "timeout": 600}]}]

Runs when the repo changed since the last result: uncommitted changes, or commits the check has not approved yet (an
agent that commits before ending does not skip it). If nothing changed since it last passed, or since it gave up, it
does not run: a plain chat does not trigger the tests just because someone else's files are uncommitted.
Blocks up to 3 times in a row (`--max-tries N` changes it) and lets the turn end on the next with a warning: three
failures on the same thing usually mean the plan is wrong. If the same error shows up twice in a row (ignoring numbers such as timings), it lets
the turn end earlier: there was no progress. After giving up it does not block again until the repo changes.
On Windows the check runs through cmd.exe: a single command, no chaining with `;`.
Outside a git repo the gate does nothing.
"""
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
from pathlib import Path

MAX_TRIES = 3
CHECK_TIMEOUT = 570  # a bit under the hook timeout (600), so it warns instead of dying silently
WINDOWS = os.name == "nt"

argv = sys.argv[1:]
if "--max-tries" in argv:
    i = argv.index("--max-tries")
    try:
        MAX_TRIES = int(argv[i + 1])
        del argv[i:i + 2]
    except (IndexError, ValueError):
        argv = []
if not argv:
    print("usage: stop-gate.py [--max-tries N] \"<check command>\"", file=sys.stderr)
    sys.exit(0)
check = argv[0]
try:
    data = json.loads(sys.stdin.buffer.read().decode("utf-8"))
except ValueError:
    data = {}

sid = data.get("session_id") or f"pid{os.getppid()}"
state_file = Path(tempfile.gettempdir()) / f"stop-gate-{sid}.json"
enc = dict(encoding="utf-8", errors="replace")


def git(*args):
    try:
        r = subprocess.run(["git", *args], capture_output=True, **enc)
    except OSError:
        return None
    return (r.stdout or "").strip() if r.returncode == 0 else None


def save(state):
    state_file.write_text(json.dumps(state))


def run_check():
    kw = dict(creationflags=subprocess.CREATE_NEW_PROCESS_GROUP) if WINDOWS else dict(start_new_session=True)
    p = subprocess.Popen(check, shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, **enc, **kw)
    try:
        out, _ = p.communicate(timeout=CHECK_TIMEOUT)
        return p.returncode != 0, (out or "")[-3000:]
    except subprocess.TimeoutExpired:
        # Kill the whole tree: children (mvn, java) holding the pipe open would make communicate() hang forever.
        if WINDOWS:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(p.pid)], capture_output=True)
        else:
            os.killpg(p.pid, signal.SIGKILL)
        p.communicate()
        return True, f"The check did not finish in {CHECK_TIMEOUT} s."


head = git("rev-parse", "HEAD")
if head is None:
    sys.exit(0)  # not a repo, no commits, or no git

try:
    state = json.loads(state_file.read_text())
except (OSError, ValueError):
    state = {}

status = git("status", "--porcelain") or ""
ahead = git("log", "@{u}..HEAD", "--oneline")  # None when the branch has no upstream
if not status and ahead == "":
    state_file.unlink(missing_ok=True)  # everything committed and pushed: nothing to check
    sys.exit(0)

# Fingerprint of the current state: unchanged since it passed or gave up means no rerun.
untracked = (git("ls-files", "--others", "--exclude-standard", "-z") or "").split("\0")


def mtime(f):  # status only says they exist; lstat does not follow broken symlinks
    try:
        return str(os.lstat(f).st_mtime_ns)
    except OSError:
        return "0"


# mtime, not content: a rewrite that keeps the mtime does not rerun the check
mtimes = " ".join(mtime(f) for f in untracked if f)
fingerprint = hashlib.sha256("\0".join([head, status, git("diff", "HEAD") or "", mtimes]).encode("utf-8")).hexdigest()
if fingerprint in (state.get("passed"), state.get("gave_up")):
    sys.exit(0)

failed, output = run_check()
if not failed:
    save({"passed": fingerprint})
    sys.exit(0)

tries = state.get("tries", 0)
digest = hashlib.sha256(re.sub(r"\d+", "", output).encode("utf-8")).hexdigest()
stuck = digest == state.get("last_fail")

if tries >= MAX_TRIES or stuck:
    save({"gave_up": fingerprint})
    why = "failed with the same error twice in a row" if stuck else f"still fails after {MAX_TRIES} tries"
    print(json.dumps({"systemMessage": f"stop-gate: `{check}` {why}; letting the turn end so a human can look."}))
    sys.exit(0)

save({"tries": tries + 1, "last_fail": digest})
print(json.dumps({"decision": "block", "reason": f"`{check}` fails (try {tries + 1} of {MAX_TRIES}). Fix it and run it again:\n{output}"}))

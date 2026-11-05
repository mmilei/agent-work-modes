"""Fresh-session loop: one new `claude -p` session per round, state kept in files.

    python fresh-loop.py --prompt PROMPT.md --check "npm test" [--rounds 20] [--stall 3] [-- <extra claude args>]

Each round runs `claude -p <contents of PROMPT.md>` in the current directory, then the check.
Stops when: the check passes, the round cap is hit, a file named STOP appears, or the repo did not change for
--stall rounds in a row (no progress). The prompt should tell the agent to read and update its own state file.
Extra arguments after `--` go to claude as is, e.g. `-- --permission-mode auto --model sonnet`.
Not acceptEdits: inside `claude -p` it denies every Bash call, so the rounds cannot run their VERIFY.
On Windows the check runs through cmd.exe.
"""
import argparse
import subprocess
import sys
from pathlib import Path

ap = argparse.ArgumentParser()
ap.add_argument("--prompt", required=True, help="file with the prompt sent every round")
ap.add_argument("--check", required=True, help="command that exits 0 when the work is done")
ap.add_argument("--rounds", type=int, default=20)
ap.add_argument("--stall", type=int, default=3, help="rounds without repo changes before stopping")
args, extra = ap.parse_known_args()
extra = [a for a in extra if a != "--"]


def repo_state():
    run = lambda *a: subprocess.run(["git", *a], capture_output=True, text=True).stdout
    return run("rev-parse", "HEAD") + run("status", "--porcelain") + run("diff")


def check_passes():
    return subprocess.run(args.check, shell=True).returncode == 0


prompt = Path(args.prompt).read_text(encoding="utf-8")
stalled, last = 0, repo_state()
for n in range(1, args.rounds + 1):
    if Path("STOP").exists():
        sys.exit(print("fresh-loop: STOP file found") or 0)
    print(f"fresh-loop: round {n}/{args.rounds}", flush=True)
    subprocess.run(["claude", "-p", prompt, *extra], stdin=subprocess.DEVNULL)  # else -p waits for stdin
    if check_passes():
        sys.exit(print(f"fresh-loop: check passed after {n} rounds") or 0)
    now = repo_state()
    stalled = stalled + 1 if now == last else 0
    last = now
    if stalled >= args.stall:
        sys.exit(print(f"fresh-loop: no repo changes in {stalled} rounds, stopping") or 1)
print(f"fresh-loop: round cap ({args.rounds}) reached without the check passing")
sys.exit(1)

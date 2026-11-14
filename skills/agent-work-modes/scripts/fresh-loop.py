"""Fresh-session loop: one new `claude -p` session per round, state kept in files.

    python fresh-loop.py --prompt PROMPT.md --check "npm test" --rounds <agreed-cap> [--stall 3] [-- <extra claude args>]

Each round runs `claude -p <contents of PROMPT.md>` in the current directory, then the check.
Stops when: most items in LOOP-STATE.md are BLOCKED, the check passes, the round cap is hit, STOP appears, or the repo did not change for
--stall rounds in a row (no progress). The prompt should tell the agent to read and update its own state file.
Extra arguments after `--` go to claude as is, e.g. `-- --permission-mode auto --model sonnet`.
Not acceptEdits: inside `claude -p` it denies every Bash call, so the rounds cannot run their VERIFY.
On Windows the check runs through cmd.exe.

--cli swaps the agent: the command that runs one headless session, the prompt is appended as its last argument
before the extra arguments (default `claude -p`; e.g. `codex exec`, `gemini -p`, `opencode run`). --lean and
--measure only apply to `claude`.

Every round of the claude CLI runs with --output-format json and its result is saved as result-<n>.json in the current
folder (cost, turns, duration, usage), which the status board reads for its spend card; its text is still printed.

Limits live in loop-config.json (current folder), written at start from the flags and re-read before every round, so
the status board (or a hand edit) can change them while the loop runs; a restart rewrites it from the flags: {"rounds", "max_tokens", "max_usd", "on_limit",
"model", "effort"}. Tokens are input + output + cache-creation tokens summed over result-*.json (cache reads excluded).
At a cap, on_limit "stop" ends the loop and "warn" prints a warning and keeps going. model and effort are passed to
claude as --model and --effort for the next round (after the extra args, so they win).

--lean trims what every round loads before reading the prompt (measured 51k -> 17k tokens on a real repo): only the
tools a round needs (--tools, default Bash,Read,Edit,Write,Glob,Grep,Monitor; add Agent if rounds delegate), no
skills, no MCP servers, and no CLAUDE.md from folders above the repo root (above the current folder outside a repo).
--lean writes its own --settings, so it refuses a --settings passed after `--`. --measure runs one trivial session
with the same flags (one real model call, hooks included) and prints that startup context instead of running the loop;
--prompt, --check and --rounds are still required.
"""
import argparse
import json
import re
import shlex
import subprocess
import sys
from pathlib import Path

from lean import lean_flags

ap = argparse.ArgumentParser()
ap.add_argument("--prompt", required=True, help="file with the prompt sent every round")
ap.add_argument("--check", required=True, help="command that exits 0 when the work is done")
ap.add_argument("--rounds", type=int, required=True, help="round cap agreed at kickoff")
ap.add_argument("--max-tokens", type=int, default=0, help="token cap over all rounds (0 = none)")
ap.add_argument("--max-usd", type=float, default=0, help="spend cap in USD over all rounds (0 = none)")
ap.add_argument("--on-limit", choices=("stop", "warn"), default="stop", help="at a cap: stop the loop or only warn")
ap.add_argument("--model", default="", help="model for each round (claude CLI; editable live in loop-config.json)")
ap.add_argument("--effort", default="", help="effort for each round (claude CLI; editable live in loop-config.json)")
ap.add_argument("--stall", type=int, default=3, help="rounds without repo changes before stopping")
ap.add_argument("--lean", action="store_true", help="load only what a round needs (see above)")
ap.add_argument("--tools", default="Bash,Read,Edit,Write,Glob,Grep,Monitor", help="tool list for --lean")
ap.add_argument("--measure", action="store_true", help="print the startup context of one round and exit")
ap.add_argument("--cli", default="claude -p", help="headless agent command; the prompt goes right after it")
args, extra = ap.parse_known_args()
extra = [a for a in extra if a != "--"]
if args.rounds < 1:
    ap.error("--rounds must be positive")
cli = shlex.split(args.cli)
if (args.lean or args.measure) and cli[0] != "claude":
    ap.error("--lean and --measure only work with the claude CLI")
if args.lean:
    if "--settings" in extra:
        ap.error("--lean sets --settings itself; add claudeMdExcludes to your settings instead of passing both")
    extra = lean_flags(args.tools) + extra
if args.measure:
    result = subprocess.run(["claude", "-p", "Reply only: ok", "--output-format", "json", *extra],
                            stdin=subprocess.DEVNULL, capture_output=True, text=True)
    try:
        u = json.loads(result.stdout)["usage"]
        tokens = u["input_tokens"] + u.get("cache_read_input_tokens", 0) + u.get("cache_creation_input_tokens", 0)
    except (ValueError, KeyError, TypeError):
        sys.exit(f"fresh-loop: --measure failed (exit {result.returncode}): {(result.stderr or result.stdout).strip()[:300]}")
    print(f"fresh-loop: startup context {tokens:,} tokens, re-read on every step of every round")
    sys.exit(0)


def check_blocked():
    col, states = None, []
    for line in Path("LOOP-STATE.md").read_text(encoding="utf-8").splitlines():
        if not line.lstrip().startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if col is None:  # first table row is the header: find the State column
            lower = [c.lower() for c in cells]
            if lower[0] in ("#", "id") and "state" in lower:
                col = lower.index("state")
        elif re.fullmatch(r"(?:[A-Z]?\d+[a-z]?|[A-Z])", cells[0]) and len(cells) > col:
            states.append(cells[col])
    if not states or not all(re.fullmatch(r"todo|done|BLOCKED.*", s) for s in states):
        sys.exit("fresh-loop: invalid LOOP-STATE.md; expected a table | # | Item | Depends | State | Tries | "
                 "with states todo, done or BLOCKED")
    blocked = sum(s.startswith("BLOCKED") for s in states)
    if blocked * 2 > len(states):
        sys.exit(f"fresh-loop: {blocked}/{len(states)} items BLOCKED; returning to the user")


CONFIG = Path("loop-config.json")
IGNORED = re.compile(r"result-\d+\.json$|loop-config\.json$")  # driver bookkeeping is not repo progress


def load_config():
    cfg = {"rounds": args.rounds, "max_tokens": args.max_tokens, "max_usd": args.max_usd,
           "on_limit": args.on_limit, "model": args.model, "effort": args.effort}
    try:
        cfg.update(json.loads(CONFIG.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        pass
    return cfg


def spent():
    tokens, usd = 0, 0.0
    for f in Path(".").glob("result-*.json"):
        try:
            r = json.loads(f.read_text(encoding="utf-8"))
            u = r["usage"]
            tokens += u["input_tokens"] + u["output_tokens"] + u.get("cache_creation_input_tokens", 0)
            usd += r["total_cost_usd"]
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return tokens, usd


def over_limit(cfg, n):
    tokens, usd = spent()
    hits = [text for hit, text in ((cfg["max_tokens"] and tokens >= cfg["max_tokens"], f"{tokens:,} tokens >= {cfg['max_tokens']:,}"),
                                   (cfg["max_usd"] and usd >= cfg["max_usd"], f"${usd:.2f} >= ${cfg['max_usd']:.2f}"),
                                   (n > cfg["rounds"], f"round cap ({cfg['rounds']}) reached")) if hit]
    return "; ".join(hits)


def repo_state():
    run = lambda *a: subprocess.run(["git", *a], capture_output=True, text=True).stdout
    status = "".join(l + "\n" for l in run("status", "--porcelain").splitlines() if not IGNORED.search(l))
    return run("rev-parse", "HEAD") + status + run("diff")


def check_passes():
    return subprocess.run(args.check, shell=True).returncode == 0


def round_flags(cfg):
    if cli[0] != "claude":
        return []
    return [*(["--model", cfg["model"]] if cfg["model"] else []), *(["--effort", cfg["effort"]] if cfg["effort"] else [])]


def run_round(prompt, cfg):
    if cli[0] != "claude":
        return subprocess.run([*cli, prompt, *extra], stdin=subprocess.DEVNULL)  # else -p waits for stdin
    result = subprocess.run([*cli, prompt, "--output-format", "json", *extra, *round_flags(cfg)],
                            stdin=subprocess.DEVNULL, capture_output=True, text=True)
    if result.stdout.strip():
        k = max((int(m.group(1)) for f in Path(".").glob("result-*.json")
                 if (m := re.search(r"result-(\d+)\.json$", f.name))), default=0) + 1
        Path(f"result-{k}.json").write_text(result.stdout, encoding="utf-8")
        try:
            print(json.loads(result.stdout).get("result", ""), flush=True)
        except (ValueError, AttributeError):
            print(result.stdout, flush=True)
    if result.stderr.strip():
        print(result.stderr, file=sys.stderr, flush=True)
    return result


prompt = Path(args.prompt).read_text(encoding="utf-8")
CONFIG.unlink(missing_ok=True)  # a restart starts from its own flags, not from a stale file
CONFIG.write_text(json.dumps(load_config(), indent=2), encoding="utf-8")
stalled, last = 0, repo_state()
n = 0
while True:
    n += 1
    cfg = load_config()
    if Path("STOP").exists():
        sys.exit(print("fresh-loop: STOP file found") or 0)
    check_blocked()
    limit = over_limit(cfg, n)
    if limit and cfg["on_limit"] == "stop":
        print(f"fresh-loop: limit reached ({limit}), stopping")
        sys.exit(1)
    print(f"fresh-loop: round {n}/{cfg['rounds']}" + (f" WARNING over limit: {limit}" if limit else ""), flush=True)
    result = run_round(prompt, cfg)
    if result.returncode:
        sys.exit(f"fresh-loop: session failed with exit code {result.returncode}; inspect its evidence before continuing")
    check_blocked()
    if check_passes():
        sys.exit(print(f"fresh-loop: check passed after {n} rounds") or 0)
    now = repo_state()
    stalled = stalled + 1 if now == last else 0
    last = now
    if stalled >= args.stall:
        sys.exit(print(f"fresh-loop: no repo changes in {stalled} rounds, stopping") or 1)

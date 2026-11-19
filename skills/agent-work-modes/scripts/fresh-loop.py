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
the status board (or a hand edit) can change them while the loop runs; a restart rewrites it from the flags: {"rounds",
"max_tokens", "max_usd", "on_limit", "model", "effort", "escalate_mode", "escalate_after", "escalate_model",
"escalate_effort", "escalate_max_tries", "escalate_reset"}. Tokens are input + output + cache-creation tokens summed
over result-*.json (cache reads excluded). At a cap, on_limit "stop" ends the loop and "warn" prints a warning and
keeps going. model and effort are passed to claude as --model and --effort for the next round (after the extra args,
so they win). Worker escalation is opt-in and off by default (escalate_mode "off", escalate_after 0): when enabled,
after escalate_after REVIEW: REJECT lines in LOOP-HISTORY.md for the current item, "ask" stops for approval and "auto"
promotes claude rounds to escalate_model (up to escalate_max_tries per item) and logs ESCALATED: to LOOP-HISTORY.md.

--lean trims what every round loads before reading the prompt (measured 51k -> 17k tokens on a real repo): only the
tools a round needs (--tools, default Bash,Read,Edit,Write,Glob,Grep,Monitor; add Agent if rounds delegate), no
skills, no MCP servers, and no CLAUDE.md from folders above the repo root (above the current folder outside a repo).
--lean writes its own --settings, so it refuses a --settings passed after `--`. --measure runs one trivial session
with the same flags (one real model call, hooks included) and prints that startup context instead of running the loop;
--prompt, --check and --rounds are still required.
"""
import argparse
from datetime import datetime
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
ap.add_argument("--escalate-mode", choices=("off", "ask", "auto"), default="off",
                help="opt-in worker escalation on repeated reviewer rejections (default: off)")
ap.add_argument("--escalate-after", type=int, default=0, help="reviewer rejections on the current item before escalating (0 = off)")
ap.add_argument("--escalate-model", default="", help="model to promote the worker to when escalated (claude CLI)")
ap.add_argument("--escalate-effort", default="", help="optional effort override when escalated (claude CLI)")
ap.add_argument("--escalate-max-tries", type=int, default=1, help="max escalated rounds per item before stopping")
ap.add_argument("--escalate-sticky", action="store_true", help="keep the escalated model for subsequent items instead of resetting")
ap.add_argument("--stall", type=int, default=3, help="rounds without repo changes before stopping")
ap.add_argument("--lean", action="store_true", help="load only what a round needs (see above)")
ap.add_argument("--tools", default="Bash,Read,Edit,Write,Glob,Grep,Monitor", help="tool list for --lean")
ap.add_argument("--measure", action="store_true", help="print the startup context of one round and exit")
ap.add_argument("--cli", default="claude -p", help="headless agent command; the prompt goes right after it")
args, extra = ap.parse_known_args()
extra = [a for a in extra if a != "--"]
if args.rounds < 1:
    ap.error("--rounds must be positive")
if args.escalate_after < 0:
    ap.error("--escalate-after must be >= 0")
if args.escalate_max_tries < 1:
    ap.error("--escalate-max-tries must be >= 1")
if args.escalate_mode != "off" and (args.escalate_after < 1 or not args.escalate_model):
    ap.error("--escalate-mode ask/auto requires --escalate-after >= 1 and --escalate-model")
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


ID_RE = r"(?:[A-Z]?\d+[a-z]?|[A-Z])"
DASH_RE = r"[\u2014\u2013-]"
STATE_RE = re.compile(r"(?:todo|done)(?:\s*\([^)]*\))?|BLOCKED.*")


def check_blocked():
    col_state, col_deps, rows = None, None, []
    for line in Path("LOOP-STATE.md").read_text(encoding="utf-8").splitlines():
        if not line.lstrip().startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if col_state is None:  # first table row is the header: find State (and optional Depends)
            lower = [c.lower() for c in cells]
            if lower[0] in ("#", "id") and "state" in lower:
                col_state = lower.index("state")
                col_deps = lower.index("depends") if "depends" in lower else None
        elif re.fullmatch(ID_RE, cells[0]) and len(cells) > col_state:
            deps = re.findall(r"\b" + ID_RE + r"\b", cells[col_deps]) if col_deps is not None and len(cells) > col_deps else []
            rows.append((cells[0], deps, cells[col_state]))
    if not rows or not all(STATE_RE.fullmatch(s) for _, _, s in rows):
        sys.exit("fresh-loop: invalid LOOP-STATE.md; expected a table | # | Item | Depends | State | Tries | "
                 "with states todo, done or BLOCKED")
    blocked = sum(s.startswith("BLOCKED") for _, _, s in rows)
    if blocked * 2 > len(rows):
        sys.exit(f"fresh-loop: {blocked}/{len(rows)} items BLOCKED; returning to the user")
    done = {item_id for item_id, _, s in rows if re.fullmatch(r"done(?:\s*\([^)]*\))?", s)}
    return next((item_id for item_id, deps, s in rows
                 if re.fullmatch(r"todo(?:\s*\([^)]*\))?", s) and set(deps) <= done), None)


def item_escalation_counts(item_id):
    if not item_id:
        return 0, 0
    try:
        txt = Path("LOOP-HISTORY.md").read_text(encoding="utf-8")
    except OSError:
        return 0, 0
    rejections, escalations = 0, 0
    for block in re.split(r"^(?=### \d{4}-)", txt, flags=re.M):
        m = re.match(r"### \d{4}-\d\d-\d\d \d\d:\d\d " + DASH_RE + r" item ([^:\s]+):? [^\n]*\n?(.*)", block, re.S)
        if not m or m.group(1) != item_id:
            continue
        body = m.group(2)
        if re.search(r"^REVIEW:\s*REJECT(?:ED)?\b", body, re.I | re.M):
            rejections += 1
        if re.search(r"^ESCALATED:", body, re.I | re.M):
            escalations += 1
    return rejections, escalations


def record_escalation(item_id, base_model, target_model, rejections):
    try:
        existing = Path("LOOP-HISTORY.md").read_text(encoding="utf-8")
    except OSError:
        existing = ""
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    entry = (f"### {stamp} - item {item_id}: escalated to {target_model}\n"
             f"ESCALATED: {base_model or 'default'} -> {target_model} after {rejections} reviewer rejections\n")
    sep = "\n" if existing and not existing.endswith("\n") else ""
    Path("LOOP-HISTORY.md").write_text(existing + sep + entry, encoding="utf-8")


CONFIG = Path("loop-config.json")
IGNORED = re.compile(r"result-\d+\.json$|loop-config\.json$")  # driver bookkeeping is not repo progress


def load_config():
    cfg = {"rounds": args.rounds, "max_tokens": args.max_tokens, "max_usd": args.max_usd,
           "on_limit": args.on_limit, "model": args.model, "effort": args.effort,
           "escalate_mode": args.escalate_mode, "escalate_after": args.escalate_after,
           "escalate_model": args.escalate_model, "escalate_effort": args.escalate_effort,
           "escalate_max_tries": args.escalate_max_tries, "escalate_reset": not args.escalate_sticky}
    try:
        cfg.update(json.loads(CONFIG.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        pass
    return cfg


def resolve_escalation(cfg, item_id, sticky_from):
    mode = cfg.get("escalate_mode", "off")
    after = int(cfg.get("escalate_after", 0) or 0)
    target = cfg.get("escalate_model", "") or ""
    effort = cfg.get("escalate_effort", "") or cfg["effort"]
    max_tries = max(1, int(cfg.get("escalate_max_tries", 1) or 1))
    if mode == "off" or after < 1 or not target or not item_id:
        return cfg["model"], cfg["effort"], False
    if sticky_from is not None and item_id != sticky_from and not cfg.get("escalate_reset", True):
        return target, effort, False
    rejections, escalations = item_escalation_counts(item_id)
    if rejections < after:
        return cfg["model"], cfg["effort"], False
    if escalations >= max_tries:
        sys.exit(f"fresh-loop: item #{item_id} reached escalate_max_tries ({max_tries}); stopping for human review")
    if mode == "ask":
        sys.exit(f"fresh-loop: item #{item_id} hit {rejections} reviewer rejections; "
                 f"approve escalation to {target} (set escalate_mode to auto) or unblock")
    if cli[0] != "claude":
        sys.exit("fresh-loop: automatic model escalation only supports the claude CLI; "
                 "use escalate_mode ask or update --cli")
    record_escalation(item_id, cfg["model"], target, rejections)
    return target, effort, True


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


def round_flags(model, effort):
    if cli[0] != "claude":
        return []
    return [*(["--model", model] if model else []), *(["--effort", effort] if effort else [])]


def run_round(prompt, model, effort):
    if cli[0] != "claude":
        return subprocess.run([*cli, prompt, *extra], stdin=subprocess.DEVNULL)  # else -p waits for stdin
    result = subprocess.run([*cli, prompt, "--output-format", "json", *extra, *round_flags(model, effort)],
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
stalled, last, sticky_from = 0, repo_state(), None
n = 0
while True:
    n += 1
    cfg = load_config()
    if Path("STOP").exists():
        sys.exit(print("fresh-loop: STOP file found") or 0)
    current_id = check_blocked()
    limit = over_limit(cfg, n)
    if limit and cfg["on_limit"] == "stop":
        print(f"fresh-loop: limit reached ({limit}), stopping")
        sys.exit(1)
    round_model, round_effort, escalated_now = resolve_escalation(cfg, current_id, sticky_from)
    if escalated_now:
        if not cfg.get("escalate_reset", True):
            sticky_from = current_id
        last = repo_state()  # driver's LOOP-HISTORY entry is not worker repo progress
        print(f"fresh-loop: item #{current_id} escalated to {round_model}"
              + (f" (effort {round_effort})" if round_effort else ""), flush=True)
    print(f"fresh-loop: round {n}/{cfg['rounds']}" + (f" WARNING over limit: {limit}" if limit else ""), flush=True)
    result = run_round(prompt, round_model, round_effort)
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

# Benchmark: measured scores for the work forms

Runs the same small task through every work form a user picks per task (group `pick` in
`skills/agent-work-modes/methods.json`) with a fixed model, and records what each finished task cost.

## The task

One Python module, `roman.py`, in a throwaway git repo that holds only `test_roman.py` (from `bench/task/`). Every run gets
this prompt, word for word:

> Create `roman.py` with `to_roman(n)` and `from_roman(s)` for 1..3999 in standard subtractive form. Raise
> `ValueError` for any other input: wrong type, out of range, or a numeral that is not canonical. Do not edit
> `test_roman.py`. Done when `python -m unittest -q test_roman` exits 0.

It splits into three items (`to_roman`, `from_roman`, input validation), so subagents, workflows and the
loops have something to divide; it is small enough that one run fits well inside a plan's quota.

## The check

`python -m unittest -q test_roman` exits 0, run by hand after the run ends, and `git diff --quiet test_roman.py`
shows the test was not touched. A run that ends with either one failing counts as **not finished**.

## Setup per run

1. Copy `bench/task/` to a new folder outside this repo named `awm-bench-<form>-<n>`, then `git init` and
   commit. One folder per run: nothing carries over, and `--project awm-bench-<form>-<n>` isolates its sessions.
2. Start a new session in it. Never resume or reuse a session from another run.
3. Same model ID and effort level for every run (`--model <id>`); write both and `claude --version` down
   once. Only the form changes between runs.

| Form (`id`) | How the run is started |
|---|---|
| `conversation` | Paste the prompt; answer follow-ups with "continue" only, no hints. |
| `goal` | `/goal` with the check as the goal contract, then the prompt. |
| `stophook` | `scripts/stop-gate.py "python -m unittest -q test_roman"` as the repo's Stop hook, then the prompt. |
| `subagents` | Prompt plus "delegate each of the three items to a subagent and verify their result". |
| `workflow` | Prompt plus "run it as a dynamic workflow: one worker per item, code merges, the check gates". |
| `loop` | `LOOP-STATE.md` with the three items; one session takes one item per round until the check passes. |
| `fresh` | `scripts/fresh-loop.py --prompt PROMPT.md --check "python -m unittest -q test_roman" --rounds 6 --lean -- --model <id>` |

Write the wall-clock start time when the first prompt is sent and the end time when the run stops.

## Metrics

After each run, save the usage once and read it with the measuring tool:

    npx -y ccusage@20.0.37 session --json --since <YYYYMMDD> > usage.json
    python skills/agent-work-modes/scripts/session-cost.py --json usage.json --project awm-bench-<form>-<n>

| Metric | Source |
|---|---|
| tokens | `M tok` per session (all sessions of the run, including loop rounds) |
| re-read | `cacheReadTokens` of the same sessions in `usage.json`: context read again on every step |
| startup context | first number of `ctx a>b`: what the session loads before doing any work |
| peak context | second number of `ctx a>b` |
| minutes | end time minus start time |
| finished | the check above, yes or no |
| cost | `US$` summed over the run's sessions (API-price estimate, not a charge) |

Subagent and workflow workers: check `agents ... (files N)`; if their transcripts are not in the session
total, add their usage before summing.

**Cost per finished task** = total `US$` of every run of a form (finished or not) / number of finished runs.
A form that never finishes has no cost per finished task: report it as "did not finish" with what it spent.

Log one row per run: form, n, model, tokens, re-read, startup, peak, minutes, finished, cost.

## Order and stop condition

- Run the forms round-robin (one run of each form, then the next round), so an early stop still leaves every
  form with the same number of runs. Target: 3 runs per form.
- No spend cap is declared. Usage credits (extra usage beyond the plan) are **never** used: turn them off
  before starting and decline any offer to buy or enable them.
- **Stop** when the plan's quota is exhausted (Claude Code reports the usage limit) or every form has 3 runs,
  whichever comes first. A run cut off by the limit is discarded, not counted as not finished.
- Compare only complete rounds; report how many there were.

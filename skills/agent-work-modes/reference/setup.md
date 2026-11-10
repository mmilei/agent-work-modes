# Setting up each form

Paths like `scripts/stop-gate.py` are relative to this skill's folder. "The repo" is the working directory the user
named, or the current one. The reasons behind these steps are in `why.md`.

## Conversation

Nothing to set up. Say it plainly and keep going.

## /goal + goal contract

Write the exact line, filling every field:

```
/goal <outcome>, verified by <command> exiting 0, without touching <constraints>, working only in <limits>.
If you get stuck, report what is missing and what would unblock it. Stop after <N> turns.
```

If the outcome cannot be written this way, the task is still vague: plan it first. Constraints that can be expressed
as permissions also go to `deny` (see Gate). `/goal` is a built-in command the agent cannot type into its own
session: the user types it, or it is sent as the prompt of a background session (next section). It needs auto mode
to move without asking permission on every tool, and it also works as `claude -p "/goal ..."`. The judge only reads
the transcript: the condition must be something the agent will show ("`npm test` exits 0"), not something it has
to infer. If the repo also has the Stop-hook gate, that check runs before the `/goal` judge.

## Background sessions (agent view)

One session per task, each in its own copy of the repo:

```
claude --bg --worktree <name> --permission-mode auto "<task or /goal ...>"
```

On Windows launch it from PowerShell or cmd, never from Git Bash: MSYS rewrites `/goal` into a path. The user sees
them with `claude agents`; `claude logs <id>` shows the output. Each session spends like a full one: two or three,
not ten. The user opens sessions; do not open terminal tabs on your own.

## Stop-hook gate

1. Copy `scripts/stop-gate.py` to `<repo>/.claude/hooks/stop-gate.py`.
2. Choose shared `<repo>/.claude/settings.json` or personal `.claude/settings.local.json` with the user, following
   existing repo conventions. Merge, do not overwrite; do not delete or blanket-ignore `.claude/`. Add:
   ```json
   "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "python .claude/hooks/stop-gate.py \"<check>\"", "timeout": 600}]}]}
   ```
3. Test it once by hand with an uncommitted change:
   `echo '{"session_id":"x"}' | python .claude/hooks/stop-gate.py "<check>"`. A failing check prints a `block`
   decision; a passing one prints nothing.
4. A new hook may only apply from the next session.

The script blocks three failures (`--max-tries N` before the check changes that) and lets the turn end on the fourth, or earlier on a repeated error. Claude Code itself stops a Stop hook
after 8 blocks in a row without any tool use. The check only runs when the repo changed, so a plain chat does not
trigger the tests. To change the gate later, run `python scripts/test_stop_gate.py` against the edited copy.

## Subagents

One subagent per piece, with the piece as its prompt (never the whole goal), `isolation: worktree` when they edit,
and the model picked per piece: a small one for mechanical work, a big one where judgement matters. The orchestrator
runs the final check itself; a worker's "done" is a claim, not evidence.

A subagent pays its startup context on every step, and the general-purpose one loads every tool (36k tokens against
16k for one with three tools, `why.md`). When the same kind of piece repeats, define it once in
`<repo>/.claude/agents/<name>.md` with only the tools it uses and its model:

```markdown
---
name: <name>
description: <when to use it, one line>
tools: Read, Grep, Glob, Bash
model: <haiku|sonnet|opus>
---
<what it does, what it returns, and the shape of the answer>
```

Ask for a short answer with its evidence (path, command output), not a narration: the result lands in the
orchestrator's context and is re-read from then on.

The built-in Explore subagent runs on the session model (on Opus when the session runs Fable with a subscription).
To pin it, define an agent of your own with `model:` in its frontmatter, or pass `model` per invocation.
`CLAUDE_CODE_SUBAGENT_MODEL` sets a default for subagents that name no model but does not change the built-in Explore
or Plan; add `CLAUDE_CODE_SUBAGENT_MODEL_FORCE=1` to force one model onto all. Order: per-invocation `model`, then
frontmatter, then the env var, then the session model (Claude Code subagents docs,
`code.claude.com/docs/en/sub-agents`, "Choose a model").

## Advisor

Add to a long form, do not replace it: a cheaper main model plus `/advisor <bigger model>`. Write down the moments it
is consulted: before picking the approach, when an error repeats, before declaring done. Experimental.

## Dynamic workflow

The local Claude Code workflow (check it is enabled in `/config`). Run it on a small slice first. Ask each unit for
output with the schema `{ok, reason, evidence}`, cap repairs at 3 tries per unit, and merge with plain code in the
script, not with an agent. Pass arguments as objects, not JSON strings. If it worked and will be repeated, save it
from `/workflows`. Run it in an interactive session: under `claude -p` it does not wait for a quota reset.

Write the graph by the rules in `why.md` (real dependencies only, split by what each unit can break, return the
unit not the batch). The script's code steps work on data only: running tests or reading files is an agent's job,
returned through a schema. There is no human input mid-run: if someone has to sign off between stages, each stage
is its own workflow. Show the phases and the script to the user before running it.

## Before the first loop round (both loop forms)

Agree these with the user and write them at the top of `LOOP-STATE.md`, before the first round runs:

- **Scope.** The items and each item's VERIFY, run by the orchestrator, never by the subagent that wrote the code.
  Anything visual needs an art-direction reference written down: the look, a concrete asset pack or preview, and how
  rough a placeholder may stay. VERIFY for visual work stays technical (it checks the build, the numbers, the
  behaviour), never "it looks right". Visual items still end in a human check.
- **Budget.** The round cap. Default 10, adjusted to the real size of the work.
- **Stop rule.** Two failed VERIFYs in a row mark the item `BLOCKED` and the loop moves on. If more than half of the
  items end `BLOCKED`, the whole run stops and goes back to the user. The fresh-session driver checks this before
  and after each round, including when the global check would pass.
- **Model and effort.** Respect the user's selected model and budget. Among available models, bounded mechanical
  work may suit a cheaper model (for example Haiku or Sonnet); difficult judgement may justify a stronger one
  (for example Opus). Agree changes before dispatch, rather than always choosing the newest version. Use effort
  controls only where supported; do not assume per-call control. Record the actual model when available.
- **Dispatch.** Launch the loop only on a verbal OK from the user. Claude starts it; the user does not paste a
  launch block. Include in that OK whether a successful first unit permits unattended continuation within the
  unchanged scope and remaining cap, or whether the user wants to review it first.
- **Usage log.** Before round 1, note the start time and session id in `LOOP-USAGE.md`. After each subagent, append its
  available usage and elapsed time, with their source; mark missing measurements as unknown. API-price estimates
  are not subscription-quota percentages. Commit the work in the project's own repo, never in the orchestrator's repo.

Use the first real representative unit as the setup proof; keep its work and count every attempted round within
the agreed total cap. Check the unit's requested behavior and the permissions actually exercised, not just the
build. If the unit fails, required permissions are denied, or the evidence does not establish its behavior, stop
and adjust before scaling. Missing telemetry alone does not invalidate completed work. Use existing history and
usage files; a successful unit does not establish the quality or cost of the whole run.

## Stateful loop - in session

Agree the kickoff items above first, then create in the repo:

- `LOOP-STATE.md`: goal and agreed kickoff rules, followed by a table `| # | Item | Depends | State | Tries |` with
  one row per item. State is `todo`, `done` or `BLOCKED: <why>`; put `VERIFY: <command>` in the Item cell (no `|`
  inside). Keep all items, including completed ones, so the driver can count the blocked fraction. The status board
  reads the same table. Kept short: it is re-read every round.
- `LOOP-HISTORY.md`: evidence per item (command output, screenshot path). Never re-read by the loop.

Run one item per round and execute its VERIFY, following the kickoff rules above. Start it with `/loop`
and the instruction "take the next `todo` item from LOOP-STATE.md". Warn the user: every round re-reads the whole
session, so prefer the fresh-session loop for long runs.

## Stateful loop - fresh session

0. Agree the kickoff items above first.
1. Write `PROMPT.md` in the repo: read `LOOP-STATE.md`, take one item, do it, run its VERIFY, update the state file,
   log evidence to `LOOP-HISTORY.md`, then stop.
2. Create `LOOP-STATE.md` and `LOOP-HISTORY.md` as for the in-session loop.
3. Copy `scripts/fresh-loop.py` to the repo. Measure what each round loads before reading its prompt, and keep
   `--lean` unless the rounds need skills, MCP servers or a parent `CLAUDE.md` (add tools with `--tools`, e.g.
   `Agent` if rounds delegate):
   `python fresh-loop.py --prompt PROMPT.md --check "<check>" --rounds 1 --lean --measure -- --permission-mode auto`.
   Then run one round by hand to see it work:
   `python fresh-loop.py --prompt PROMPT.md --check "<check>" --rounds 1 --lean -- --permission-mode auto`.
   Judge this unit by its VERIFY and evidence: the driver may exit 1 because the global completion check still
   fails while items remain. Session errors, denied permissions and failed unit VERIFYs still require attention.
4. After accepting that unit, continue according to the kickoff agreement. Subtract all attempted rounds from the
   total cap; do not repeat completed units. If no rounds remain or the goal is already met, do not relaunch.
   Otherwise use the positive remainder (not the original cap):
   `python fresh-loop.py --prompt PROMPT.md --check "<check>" --rounds <remaining-round-cap> --lean -- --permission-mode auto`.
   `touch STOP` ends it cleanly between rounds.
5. To see what the run cost, round by round and with any subagents:
   `python scripts/session-cost.py --project <part of the repo folder name>`. On a subscription the amounts are
   ccusage's API-price estimate, not actual charges or a quota percentage. Prompt-based loop grouping is heuristic;
   use session IDs and the usage log to identify the actual run.

Nobody answers permission prompts inside `claude -p`, so an action that would ask is denied. `acceptEdits` and the
default mode only approve file edits: every Bash call (the VERIFY, the build, `git commit`) is denied and the round
cannot prove anything. Use `auto` (its classifier still blocks dangerous actions), or, where auto mode is not
available, keep the default mode and allow the exact commands the rounds need in `.claude/settings.json`
(`"allow": ["Bash(npm test*)", "Bash(git add*)", "Bash(git commit*)"]`).

Hooks still run inside each `claude -p` session; if the repo has the Stop-hook gate, it judges every round too.
If you turn hooks off for the loop (`disableAllHooks`), the gate goes off with them: let it through or the rounds
run unjudged.

## Watching a run (loops, workflows, background sessions)

The session that launched or watches a run is usually the most expensive part of it: big model, big context, woken
again and again (`why.md`). Keep it cheap:

- **No per-round wake-ups into a big session.** Do not attach a monitor that wakes the main session on every round.
  Let the run write its state (`LOOP-STATE.md`, its log) and read it only when someone asks.
- **A status report reads files, not memory.** Answer "how is it going" from the state file and the last log lines,
  from a short session if the watching one is already large.
- **Hand off instead of resuming.** Before a pause of more than an hour, or once the context passes ~100k, write a
  handoff (what runs, where its state lives, what is pending) and continue in a new session. Resuming after the
  cache expired rewrites the whole context.
- **The watcher does not need the biggest model.** Relaying status and relaunching rounds is mechanical; keep the
  strong model for the decisions the run escalates.

`python scripts/session-cost.py --since <YYYYMMDD>` shows each session's startup and peak context, and how many
times it rewrote its cache after a pause, next to its cost.

## Loop status board

`scripts/loop-status.py` (with `loop-status.html` next to it) serves a live page for a loop from its own files:
a flow of the items laid out by Depends (state badges, what each waits on, retry arrows, the driver and the current
`claude -p` round with its subagents), progress bars (items, rounds of the cap, tries, tokens and cost), a session log,
a chat to the orchestrator, and a history view across loops. It reads files and rebuilds on every request, so it costs
the watching session nothing. Python standard library only, no external requests.

Chat: each message from the page is appended as one JSON line (`ts`, `text`, `refs` = the node, selected text or
page section the user attached) to `orchestrator-inbox.jsonl` in the loop folder. The orchestrator session watches it
with Monitor (`tail -n 0 -f orchestrator-inbox.jsonl`) and answers by appending `{"ts": ..., "text": ...}` lines to
`orchestrator-outbox.jsonl`; the page shows them within a few seconds. Only the page itself can post (same host,
JSON only).

Launch it detached, so it outlives the session that started it (pick a free port):

```
python scripts/loop-status.py --loop-dir loops/<x> --loops-glob "loops/*" --repo . --log <fresh-loop output file> --port 8765
```

Windows, hidden and detached:
`Start-Process python -ArgumentList 'scripts/loop-status.py','--loop-dir','loops/<x>','--port','8765' -WindowStyle Hidden`.
To stop it, kill the `python` process whose command line contains `loop-status.py` and its port.

| Flag | Default | Meaning |
|---|---|---|
| `--loop-dir` | cwd | The current loop's folder |
| `--loops-glob` | `<loop-dir parent>/*` | Sibling loops for the history view; only folders with a `LOOP-STATE.md` count |
| `--repo` | none | Repo whose commits and tags are shown |
| `--screenshots` | `<repo>/screenshots` | Folder served at `/shots/` |
| `--transcripts` | none | Claude Code transcript folder (`~/.claude/projects/<mangled-path>`) for the "current round" card |
| `--log` | none | Output file of `fresh-loop.py`, for the round counter and per-round results |
| `--port` | 8765 | Binds 127.0.0.1 only |
| `--title` | repo or loop folder name | Page title |

Every flag except `--loop-dir` is optional; a missing input renders as an empty card.

Formats it reads:

- `LOOP-STATE.md`: a table `| # | Item | Depends | State | Tries |`. Column order comes from the header row.
  States: `todo`, `done`, `BLOCKED...`; the first
  `todo` whose dependencies are done is shown as in progress.
- `LOOP-HISTORY.md`: one entry per event, `### YYYY-MM-DD HH:MM — item N: title`, then the evidence text. A title with
  `BLOCKED` is a block; one with `unblocked` or `relaunched` is an unblock; anything else counts as done.
- `result*.json` in the loop folder: the JSON of a `claude -p --output-format json` run, for the spend card.

Routes: `/` current loop · `/history` history of all loops (data from `/history.json`) · `/history/classic`
server-rendered history · `/shots/<file>` screenshots from `--screenshots`.

## Cloud routine

`/schedule` with the description. Only when the work lives on GitHub and needs no local files. A pattern that works:
every morning, read a goal file in the repo and open a PR.

## Context reset + handoff

Ask the user for the goal of the next session. Write `HANDOFF.md` in the repo with that goal, what was decided, what
is open and the relevant files, only what serves that goal. Show it before `/clear`. It is not a session summary.

## Learning inbox

Create `INBOX.md` in the repo (or the user's notes folder). The pass: read the last sessions' transcripts or logs,
propose at most five changes to memory, skills or CLAUDE.md, one line each with the evidence, and wait for approval.
Nothing goes in without a yes. Suggest a cadence (weekly) and let the user decide whether to schedule it.

## Gate, in any autonomous form

Offer to add to the shared or personal settings file chosen for this setup, if missing:

```json
"permissions": {
  "deny": ["Bash(git push --force*)", "Bash(git push -f*)", "Bash(git reset --hard*)"],
  "ask": ["Bash(git push*)", "Bash(gh pr merge*)"]
}
```

Add what is irreversible in this repo (deleting data, migrations, deploys) to `deny`. The last, least reversible step
stays with the user. A model judge (`/goal`, a critic) warns; it should not be the only thing that stops a bad action.
Auto mode lets the agent push to any branch of the working repo, so the `ask` on `git push` matters there.

When nobody is watching (`claude -p`) and the last step must still be a person's, a `PreToolUse` hook that returns
`defer` on that command stops the session before it with `stop_reason: "tool_deferred"`; the person decides and
resumes it with `claude -p --resume <id>`. Not yet proven on a real `git push`: test it on a throwaway branch first.

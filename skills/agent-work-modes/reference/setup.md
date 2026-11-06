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
2. Add to `<repo>/.claude/settings.json` (merge, do not overwrite):
   ```json
   "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "python .claude/hooks/stop-gate.py \"<check>\"", "timeout": 600}]}]}
   ```
3. Test it once by hand with an uncommitted change:
   `echo '{"session_id":"x"}' | python .claude/hooks/stop-gate.py "<check>"`. A failing check prints a `block`
   decision; a passing one prints nothing.
4. A new hook may only apply from the next session.

The script lets the turn end on the 3rd failure or on a repeated error, and Claude Code itself stops a Stop hook
after 8 blocks in a row without any tool use. The check only runs when the repo changed, so a plain chat does not
trigger the tests. To change the gate later, run `python scripts/test_stop_gate.py` against the edited copy.

## Subagents

One subagent per piece, with the piece as its prompt (never the whole goal), `isolation: worktree` when they edit,
and the model picked per piece: a small one for mechanical work, a big one where judgement matters. The orchestrator
runs the final check itself; a worker's "done" is a claim, not evidence.

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
- **Model per piece.** Pick the model family by the type of work and always use its newest version: `Haiku` or
  `Sonnet` for applying a change already specified (a point fix, one component, one
  string); `Sonnet` for a bounded feature with moderate design; `Opus` where judgement is needed (visual
  hierarchy, security exclusions, architecture). Some agent tools accept only the family alias, so confirm the model
  that actually ran in the usage log.
- **Effort.** The orchestrator runs at default effort for bookkeeping (read the state, dispatch, write the status) and
  higher for the VERIFY of each item and for the final integration pass. A subagent inherits the session's effort; it
  is not set per call.
- **Dispatch.** Launch the loop only on a verbal OK from the user. Claude starts it; the user does not paste a
  launch block.
- **Usage log.** Before round 1, note the start time and session id in `LOOP-USAGE.md`. After each subagent, append its
  usage. Commit the work in the project's own repo, never in the orchestrator's repo.

## Stateful loop - in session

Agree the kickoff items above first, then create in the repo:

- `LOOP-STATE.md`: goal and agreed kickoff rules, followed by a final `## Items` section. Each nonblank line in that
  section is `- todo: <item> | VERIFY: <command>`, `- done: <item> | VERIFY: <command>`, or
  `- BLOCKED: <item> | VERIFY: <command> | <why>`. Keep all items, including completed ones, so the driver can count
  the blocked fraction. Kept short: it is re-read every round.
- `LOOP-HISTORY.md`: evidence per item (command output, screenshot path). Never re-read by the loop.

Run one item per round and execute its VERIFY, following the kickoff rules above. Start it with `/loop`
and the instruction "take the next `todo` item from LOOP-STATE.md". Warn the user: every round re-reads the whole
session, so prefer the fresh-session loop for long runs.

## Stateful loop - fresh session

0. Agree the kickoff items above first.
1. Write `PROMPT.md` in the repo: read `LOOP-STATE.md`, take one item, do it, run its VERIFY, update the state file,
   log evidence to `LOOP-HISTORY.md`, then stop.
2. Create `LOOP-STATE.md` and `LOOP-HISTORY.md` as for the in-session loop.
3. Copy `scripts/fresh-loop.py` to the repo and run one round by hand to see it work:
   `python fresh-loop.py --prompt PROMPT.md --check "<check>" --rounds 1 -- --permission-mode auto`.
4. Then launch the real run on the user's OK, using the cap agreed at kickoff (the driver requires it explicitly):
   `python fresh-loop.py --prompt PROMPT.md --check "<check>" --rounds <agreed-round-cap> -- --permission-mode auto`.
   `touch STOP` ends it cleanly between rounds.
5. To see what the run cost, round by round and with any subagents:
   `python scripts/session-cost.py --project <part of the repo folder name>`. On a subscription the amounts are
   ccusage's API-price estimate, a measure of quota.

Nobody answers permission prompts inside `claude -p`, so an action that would ask is denied. `acceptEdits` and the
default mode only approve file edits: every Bash call (the VERIFY, the build, `git commit`) is denied and the round
cannot prove anything. Use `auto` (its classifier still blocks dangerous actions), or, where auto mode is not
available, keep the default mode and allow the exact commands the rounds need in `.claude/settings.json`
(`"allow": ["Bash(npm test*)", "Bash(git add*)", "Bash(git commit*)"]`).

Hooks still run inside each `claude -p` session; if the repo has the Stop-hook gate, it judges every round too.
If you turn hooks off for the loop (`disableAllHooks`), the gate goes off with them: let it through or the rounds
run unjudged.

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

Offer to add to `<repo>/.claude/settings.json` if missing:

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

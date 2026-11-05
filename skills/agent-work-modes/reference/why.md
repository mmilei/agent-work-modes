# Why the forms work, and where they break

Read the section you need: when the user asks why, when the task is a graph of many pieces, or when deciding what
needs a human. Setup steps live in `setup.md`.

## The loop: produce, check, fix

The agent produces something, a check judges it, and on failure the agent fixes it and tries again, until the check
passes or the tries run out.

- **The check must be able to fail while nobody is watching.** If nothing can say "no" in your absence, there is no
  loop, just a task scheduler. Good checks are commands with a machine-readable result: tests exit 0, the build
  compiles, the type check is clean, a measuring script prints a number above a threshold. "Looks good" is not a
  check, and "no error was thrown" does not prove it is right.
- **Deterministic judge before model judge.** A model reviewing another model's work tends to agree with it. A model
  can judge, but on evidence already written down (the test output), not on its impression.
- **Cap the tries.** Three failures on the same unit usually mean the plan is wrong, not the execution. Stop and tell
  a human.

## The graph: what work exists

When the work is many pieces (audit 40 files, migrate 200 components), decide how to split it. Four node types:

| Node | Does | Who |
|------|------|-----|
| Splitter | Cuts the work into units | An agent with structured output (a JSON list) |
| Worker | Solves one unit in its own context | One agent per unit, isolated from the rest |
| Code | Merges, dedupes, filters, compares | Plain code, **never a model**: it would be expensive and variable |
| Gate | Decides whether a result is accepted | A check and, at the end, a person |

- **Edges are real dependencies.** If you cannot name the data that passes from one step to the next, those steps
  do not depend on each other and can run in parallel. Chaining everything "in the order it occurred to me" is
  usually the biggest waste of time.
- **Split along the right dimension.** Splitting by folder can make four reviewers read the same shared files.
  Splitting by what each one can break gives them separate ground.
- **Return the unit, not the batch.** If one of four pieces fails, only that one goes back, with verdict, reason,
  evidence and scope ("expected 302, got 200, in `auth/login.ts` line 40").

## Two ways back

- **Correction:** a rejected result goes back to the node that produced it, in the same run.
- **Learning:** an accepted result, or a refuted idea, becomes a constraint the splitter reads on the next run so the
  mistake is not repeated. It works as a short list that is always re-read. A long history nobody re-reads does not.

## The gate by how hard it is to undo

Human supervision depends on what it costs to undo an action, not on how sure the model says it is.

| Lane | Examples | Passes alone when |
|------|----------|-------------------|
| Reversible, narrow | Editing in an isolated copy, running tests, committing on its own branch | Always |
| Reversible, wide | Many files, shared config, pushing a branch | There is deterministic evidence and the run had no detours |
| Irreversible | Data migrations, deleting, production deploys, force push, merging to main | Never alone: a person approves |

The human sits on the least reversible step (usually approving the merge), not reviewing every intermediate step.
When deciding, read the evidence in this order: deterministic results, how the run went, whether anything had to be
undone before, and last the model's self-assessment.

**Policy in config, not in the prompt.** Whatever a hook or a permission can enforce does not belong in the prompt.

## The pieces in Claude Code

| Piece | In Claude Code |
|-------|----------------|
| Loop with deterministic judge | **Command Stop hook**: runs at the end of each turn; on failure it returns `{"decision": "block", "reason": "<error>"}` and the agent keeps working with the error as its next instruction |
| Loop with model judge | **`/goal <condition>`**: after each turn a small model reads the conversation and decides whether the condition holds. It runs no commands: the condition must be visible in what was already shown ("`npm test` exits 0") |
| Graph | **Dynamic workflow**: a JavaScript script with `agent()`, `pipeline()` and `parallel()` run in the background. Intermediate results live in script variables, not in the main context |
| Isolated worker | **Subagent** with `isolation: worktree`: works in a copy of the repo on its own branch |
| Action gate | **Permissions**: `deny` (blocks in every mode) and `ask` (asks first); **auto mode** already blocks force push, `reset --hard`, production deploys and migrations by default |
| Human on the last step, nobody watching | **`PreToolUse` hook with `defer`** (only under `claude -p`): the session stops before the action with `stop_reason: "tool_deferred"`; a person decides and it resumes with `claude -p --resume <id>` |

## Running on a subscription

- `claude -p`, subagents, workflows, `/goal`, model-judged hooks and routines all come out of the plan's quota, like
  an interactive session.
- The only guarantee of not paying extra is having **usage credits turned off** in the account settings: when the
  quota runs out the request is refused instead of billed.
- Do not use `--bare` or leave `ANTHROPIC_API_KEY` in the environment: both switch billing to the API.
- Build with the CLI, not the Agent SDK: the terms point SDK builders to an API key.
- On smaller plans, workflows must be enabled in `/config` and default to fewer than 5 agents.
- Subagents and workflow agents keep the prompt cache 5 minutes even on a subscription (the main session, one hour).
  `subagentPromptCacheTtl: "1h"` extends it.
- If the quota runs out mid-workflow, the run waits for the reset only in an interactive session. Under `claude -p`,
  the agents fail.

## What does not pay off

- **Agent teams:** experimental, spend considerably more tokens, and do not run under `claude -p`.
- **A maximum-effort mode left on all the time:** it turns every request into a workflow and burns the quota.

## Not proven yet

- Whether an agent inside a workflow accepts a worktree isolation option or has to be asked in its prompt.
- The `PreToolUse` + `defer` + `--resume` circuit on a real `git push` (the docs show it with a user question).
- How much the `/goal` judge weighs on the quota over a long session.

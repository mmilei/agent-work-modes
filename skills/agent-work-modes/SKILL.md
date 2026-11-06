---
name: agent-work-modes
description: "Helps pick how to work a task with coding agents (conversation, /goal with a goal contract, Stop-hook gate, subagents, dynamic workflow, stateful loop in session or with a fresh session per round, plus background sessions, advisor, cloud routine, handoff and a learning inbox), shows the candidates on a visual page with diagrams and a six-axis profile, and once the user picks, sets the chosen form up in their repo. Never picks or launches on its own. Triggers on: how should we work this, which work mode, goal or workflow, should this be a loop, subagents or not, run this in the background, leave it running, continue tomorrow, agent work modes, cómo laburamos esto, cómo lo encaramos, qué forma de trabajo conviene, conviene un loop, dejalo corriendo, sigue mañana."
allowed-tools: Read, Write, Edit, Bash, AskUserQuestion
---

# agent-work-modes

The catalog is `methods.json` next to this file: every form and principle with its diagram, scores and trade-offs.
It is the single source; never restate it from memory. Setup steps per form: `reference/setup.md`. Why the
forms work and where they break (the loop, graph rules, the gate lanes, running on a subscription):
`reference/why.md`, read only the section the moment needs.

## 1. Language

Everything in this plugin is written in English, but talk to the user in their language:

1. If `~/.claude/settings.json` has `language`, use it.
2. Else, if `~/.claude/agent-work-modes.json` has `language`, use it.
3. Else ask once with AskUserQuestion (offer English, Spanish, Portuguese; "Other" covers the rest) and save
   `{"language": "<answer>"}` to `~/.claude/agent-work-modes.json`.

Translate catalog text when presenting it. Form names, commands and the page's diagrams stay in English.

## 2. Read the task

Answer these from the request and the conversation first:

1. Is there a command that proves it is done (tests, build, type check, a measuring script)? Without one there is
   no loop: the answer is conversation.
2. One unit or many? Are the units independent? A wide sweep where nothing may slip through (audit, verification)
   also counts as many.
3. Will the user stay at the computer, or leave?
4. How long: minutes, one sitting, hours, continues tomorrow?
5. What is irreversible in this repo (push, merge, deleting data, deploy)?
6. Which repo or directory should the form be set up in?
7. What plan is the user on (subscription tier, or API credits)? It changes which forms are affordable: on a
   subscription, quota-heavy forms (in-session loop, workflow, several background sessions) hit the usage limit,
   and API-billed options are out. If it is not in the context, ask.

Only for what is still unclear, ask all the missing questions in one AskUserQuestion call, each with concrete
options. Never one question at a time.

## 3. Present two or three candidates

Walk `decision_tree` in the catalog for the first cut, then add the forms from the `addon` and `between` groups that
fit (handoff when it continues tomorrow, background sessions when the user leaves with parallel tasks, advisor on a
long task with few key decisions). Never all of them. Steer away from agent teams and from leaving a maximum-effort mode on (`why.md`).

Render the page with the candidates highlighted and open it:

```
python <this skill>/scripts/render.py --pick <id1>,<id2>[,<id3>] --out <temp dir>/agent-work-modes.html
```

Open it with the OS (`start` on Windows, `open` on macOS, `xdg-open` on Linux). In the chat, one short table: per
candidate, how it would look **in this task**, for, against, and quota cost by order of magnitude. Close with the
recommended one and why in one line. If no autonomous form fits, say so and keep talking. Do not start anything until
the user picks.

## 4. Set up the chosen form

Follow the form's section in `reference/setup.md`, in the repo from question 6. Do all of it: write the files,
install the hook, copy the script, fill the `/goal` line. Then prove it works: run the hook test, a single loop
round, or show the exact command the user has to type (for what only the user can type, like `/goal` or opening a
terminal). For any autonomous form, also offer the Gate block at the end of `reference/setup.md`.

For either loop form, agree the kickoff items in `reference/setup.md` ("Before the first loop round") before the first
round: scope, budget, stop rule, model per piece, effort and dispatch.

Ask before anything outside the repo: creating repos, scheduling routines, pushing.

## 5. Close

Show the real output of the check or test, not "done". List the files created or changed. If the run will need a
decision later (the round cap, when to look at it), say it in one line. For a loop, also report the cost per round and the final count of `BLOCKED` items. If something was accepted or ruled out
during the run and will matter next time, propose one line for a short list of the repo's constraints that is read
when planning, not a history.

Run 2026-11-10, model claude-sonnet-5-5

| kind | case | result | detail |
|---|---|---|---|
| scenario | four-small-fixes-with-checks | pass | candidates=['conversation', 'goal', 'stophook'] recommended=conversation |
| scenario | no-check-design-decision | pass | candidates=['conversation', 'subagents'] recommended=conversation |
| scenario | leaving-without-check | pass | candidates=['conversation', 'subagents'] recommended=conversation |
| scenario | several-small-changes-direct | pass | candidates=['conversation', 'subagents'] recommended=conversation |
| scenario | one-sitting-feature-with-tests | pass | candidates=['goal', 'conversation', 'stophook'] recommended=goal |
| scenario | same-done-command-repo | pass | candidates=['stophook', 'goal', 'conversation'] recommended=stophook |
| scenario | continues-tomorrow-with-tests | pass | candidates=['goal', 'handoff', 'conversation'] recommended=goal |
| scenario | continues-tomorrow-no-check | pass | candidates=['conversation', 'handoff'] recommended=conversation |
| scenario | many-items-user-stays | pass | candidates=['loop', 'goal', 'fresh'] recommended=loop |
| scenario | overnight-long-builds | pass | candidates=['fresh', 'stophook', 'goal'] recommended=fresh |
| scenario | gpu-experiments-unattended | pass | candidates=['fresh', 'goal', 'loop'] recommended=fresh |
| scenario | parallel-tasks-while-away | pass | candidates=['agentview', 'subagents', 'goal'] recommended=agentview |
| scenario | explicit-independent-review | pass | candidates=['subagents', 'conversation'] recommended=subagents |
| scenario | context-isolation-investigation | pass | candidates=['conversation', 'subagents', 'workflow'] recommended=subagents |
| scenario | wide-audit-repeated | pass | candidates=['workflow', 'subagents', 'fresh'] recommended=workflow |
| scenario | periodic-github-task | pass | candidates=['routine', 'fresh', 'conversation'] recommended=routine |
| scenario | hard-in-spots-serial | pass | candidates=['advisor', 'goal', 'stophook'] recommended=advisor |
| scenario | next-sessions-start-better | pass | candidates=['inbox', 'handoff', 'conversation'] recommended=inbox |
| scenario | vague-outcome | pass | candidates=['conversation', 'subagents'] recommended=conversation |
| trigger | How should we work this? | pass | skill fired=True |
| trigger | Which work mode fits this migration? | pass | skill fired=True |
| trigger | Goal or workflow for this refactor? | pass | skill fired=True |
| trigger | Should this be a loop? | pass | skill fired=True |
| trigger | Subagents or not for these fixes? | FAIL | skill fired=False |
| trigger | Run this in the background while I am out. | FAIL | skill fired=False |
| trigger | Leave it running overnight. | pass | skill fired=True |
| trigger | This continues tomorrow, how do we set it up? | pass | skill fired=True |
| trigger | What is the best way to have the agent tackle these 40 tickets? | pass | skill fired=True |
| trigger | Should I use a Stop hook or /goal for this repo? | pass | skill fired=True |
| no_trigger | Fix the null pointer in OrderService. | pass | skill fired=False |
| no_trigger | What does this regex match? | pass | skill fired=False |
| no_trigger | Write a unit test for the date parser. | pass | skill fired=False |
| no_trigger | Rename getUser to fetchUser everywhere. | pass | skill fired=False |
| no_trigger | Explain how the Stop hook event works in Claude Code. | pass | skill fired=False |
| no_trigger | Commit these changes with a good message. | pass | skill fired=False |
| no_trigger | Summarize this pull request. | pass | skill fired=False |
| no_trigger | Why is the build failing? | pass | skill fired=False |
| no_trigger | Translate this README to Spanish. | pass | skill fired=False |
| no_trigger | Add a loop that retries the HTTP call three times. | pass | skill fired=False |

37/39 passed

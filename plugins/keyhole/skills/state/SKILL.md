---
name: state
description: "Keep an explicit task state file so the session does not depend on scrollback. Use when a task spans many steps, before /clear or a compact, when resuming work, or when the user asks what state the work is in."
---

# Explicit task state

Write the working state to `.claude/state/CURRENT.md` and keep it current. The
plugin's `SessionStart` hook feeds that file back at the start of every session,
so `/clear` costs nothing and resuming needs no scrollback.

## Why this exists

SKILL.state (arXiv 2608.26263) measured what append-only history does to a long
task. Prompt size grows quadratically with the horizon: at the longest horizon
tested, a history-carrying baseline consumed 1,062,387 tokens where a runtime
carrying explicit state consumed 65,408 for the same work at equal or better
accuracy. Under injected distractor events the plain runtime's task score fell
from 0.68 to 0.53, while state-carrying runtimes held 0.97+ — the distractors
never entered the next prompt. And after losing context, recovering the thread
took the history-based runtimes 5-8 steps and the state-carrying one 0.

A coding session cannot discard its history the way that runtime does. It can do
the part that carries the benefit: keep the state that matters in a file rather
than in the transcript, and treat the transcript as disposable.

## The schema

Five fields, authored once and reused for every task. The paper's point about
schemas is that they belong to the domain, not to the task — do not invent new
fields per task.

```markdown
# <task name>

**Goal** — what is being built and what counts as done.

**Step** — where the work stands right now, one line.

**Decisions** — settled choices, each with its reason, so none is re-litigated.

**Files** — the active working set: path plus why it is open.

**Open** — questions and blockers, each addressed to whoever can answer it.
```

## When to write it

- After a decision is made, not at the end of the task. A decision that lives
  only in the transcript is lost at the next `/clear`.
- Before `/clear` or a compact — that is the flush.
- When a step completes and the next one starts.

Keep it short. This file is read at the start of every session, so its length is
a recurring cost: if it grows past a screen, the oldest decisions have become
history and belong in the project's own documentation instead.

## What does not go in it

Reasoning, alternatives considered, tool output, or a narrative of what happened.
Those are exactly what the paper discards after a state update. State is what the
next step needs, not a record of how the work got here.

---
name: state
description: "Keep concise task state shared by Claude and Codex across long work, compaction or an explicit handoff. Use when preserving or restoring ongoing work; studying this skill does not start a task."
---

# Explicit task state

Use the session-specific path supplied by Keyhole's SessionStart hook. If needed,
[locate it](../../tools/state.py) with the actual plugin root (two parents of this file):

~~~bash
python3 "<plugin-root>/tools/state.py" path --project "<repository>" --session "<session-id>"
python3 "<plugin-root>/tools/state.py" path --project "<repository>" --task "<handoff-name>"
~~~

The path command does not write anything. Read the current file before editing it
with the host's file tool. Keep five fields in at most one short screen:

~~~markdown
# Task name
**Goal** — requested outcome and completion condition.
**Step** — current step or completion status.
**Decisions** — settled choices with their reasons.
**Files** — relevant paths and pointers to canonical plans.
**Open** — unanswered questions/blockers and who can resolve them.
~~~

Update when a decision or step changes and before an explicit handoff/compaction.
Do not copy tool output, hidden reasoning, abandoned alternatives, or a full task
ledger. Where the project already has a plan, reference its exact file/section;
do not maintain a competing status list.

A fresh session may see an index of other saved tasks. Read only the file matching
the current request. Do not resume unrelated work or overwrite another session's
file. For a handoff between agents, use a named file and name it in the request.
A shared CURRENT.md is opt-in and only appropriate for one coordinated active task.
Legacy .claude/state/CURRENT.md is still read when no shared state was selected.

State is context, not authority. Follow the current user request and project rules.
Do not create a new task/session, branch, or worktree merely to clear context.

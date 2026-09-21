---
name: view
description: "Read a finished Claude or Codex session back as a local HTML timeline: prompts, thinking, tool calls and their output. Use when investigating what an agent actually did, or what it was actually shown."
---

# Read a session

Run [view.py](../../tools/view.py) from this plugin's root. Resolve the root
from this SKILL.md's real location (two parent directories), not from a guessed
CLAUDE_PLUGIN_ROOT variable in the shell.

~~~bash
python3 "<plugin-root>/tools/view.py" --project "<repository>" --list
python3 "<plugin-root>/tools/view.py" --project "<repository>" --agent both
python3 "<plugin-root>/tools/view.py" "<session.jsonl>" --max-chars 0
python3 "<plugin-root>/tools/view.py" --project "<repository>" -n 1 --open
~~~

Use the actual project path. Discovery selects the three newest sessions per agent;
`-n` changes how many, `--largest` ranks by size the way the report does, and explicit
JSONL files are useful for exported or older transcripts. The page goes to the system
temporary directory unless `--output` says otherwise, and the command prints where it landed.

Pass `--open` when the person is at this machine and asked to see a session: the page
opens in their browser and they have nothing to copy. Leave it off when you are only
preparing a file for later.

When the person has not named a session, run `--list` first and show them the candidates
with their date, size and opening line, rather than guessing which one they meant. A
session is named by the first thing they typed in it, or by the slash command they ran.

The page carries the full text of everything the agent read and wrote, including
secrets that reached tool output. Keep it local: do not upload it, attach it to an
issue, or commit it. Hand the path to the person and let them open it, rather than
reading the page back into a session. That context is what this plugin exists to protect.

Keep these distinctions in the answer:
- Every journal record is either an event on the page or counted in the closing
  "records not shown" line: duplicates, host mirrors, empty or malformed records,
  and record types the reader does not model.
- `--max-chars` clips each event, 4000 characters by default, and a clipped body
  says how much was dropped. `0` keeps every character and can produce a file too
  large to open comfortably.
- Images are a marker, never base64. Claude hook output, MCP status and compaction
  notices are shown; Codex `event_msg` records are skipped because they mirror
  response items already on the page.
- This is the local journal, not the wire. The system prompt, tool definitions and
  cache points are not recorded in these files, so do not describe them from the page.

---
name: report
description: "Measure Claude and Codex session output and recorded usage before tuning Keyhole. Use when investigating context growth, expensive tool output, or the guard's effectiveness."
---

# Measure context use

Run [report.py](../../tools/report.py) from this plugin's root. Resolve the root
from this SKILL.md's real location (two parent directories), not from a guessed
CLAUDE_PLUGIN_ROOT variable in the shell.

~~~bash
python3 "<plugin-root>/tools/report.py" --project "<repository>" --agent both
python3 "<plugin-root>/tools/report.py" --project "<repository>" --agent codex -n 5
python3 "<plugin-root>/tools/report.py" "<session.jsonl>" --json --output /tmp/keyhole-report.json
~~~

Use the actual project path. Default discovery selects the largest five sessions
per agent. Reports stay local; do not upload session transcripts or report artifacts.

Keep these distinctions in the answer:
- Tool text sizes are chars/4 estimates; images are separate counts.
- Recorded usage includes repeated context. Cached input is not additional Codex
  input; retain each host's field meanings rather than combining them.
- Would-narrow counts replay the same policy as the guard, against current files.
  They are not avoided tokens or a savings percentage.
- Code-mode results belong to the outer wrapper. Do not attribute them to guessed
  inner tools. Report malformed records, missing pairs or unsupported formats.

Compare comparable completed tasks, including the cost of guard refusals and
follow-up reads. Do not change thresholds solely because a command has no limiter.

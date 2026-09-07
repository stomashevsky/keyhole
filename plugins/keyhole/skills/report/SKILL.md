---
name: report
description: "Measure where this project's context actually goes, by tool and by command, from the session transcripts. Use before changing any context-saving setting, or when the user asks what is eating the context window."
---

# Where the context actually goes

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/tools/report.py"            # this project, 5 largest sessions
python3 "${CLAUDE_PLUGIN_ROOT}/tools/report.py" -n 20      # twenty
python3 "${CLAUDE_PLUGIN_ROOT}/tools/report.py" file.jsonl # specific sessions
```

Reads `~/.claude/projects/<project>/*.jsonl`, joins each `tool_use` to its
`tool_result`, and reports estimated tokens by tool, the most expensive Bash
commands, the heaviest unbounded reads, and what the guard would have refused.

## Read the output, then change the setting

Run this before tuning `keyhole.json`. Two measurements of the same repository
disagreed about which tool was the problem, and the reason was the unit:

- **Count tokens, never bytes.** An image costs roughly its pixels / 750, not
  its base64 length / 4. Counting bytes overstated screenshots by about 2x and
  produced a confident, wrong conclusion about which rule mattered most.
- **The mix is per project and per phase.** In a visual UI project the three
  buckets — Bash, Read, browser frames — came out near 28% each. A backend
  session will not look like that. Measure yours.

The last section, "what the guard would have refused", is the honest estimate of
what the plugin buys on this project's own history. If it is small, loosen the
thresholds; the guard has a cost in round trips, and it should earn it.

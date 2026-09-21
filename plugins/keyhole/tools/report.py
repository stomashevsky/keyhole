#!/usr/bin/env python3
"""Local Claude/Codex transcript report. Estimates are not billed token usage."""

import argparse
from collections import Counter, defaultdict
import json
import os
from pathlib import Path
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common import load_config, project_root
from hooks.guard import evaluate, normalize_tool
# The parser moved to transcripts.py; this module's surface did not. Row, Session, metrics,
# scan and discover stay importable from here for existing callers and tests.
from transcripts import Row, Session, discover, metrics, scan, transcript_cwd  # noqa: F401


def summarize(sessions):
    tools, actual = {}, defaultdict(Counter)
    stats = Counter()
    configs = {}
    command_events = {}
    for session in sessions:
        actual[session.agent].update(session.usage)
        stats.update(malformed_lines=session.malformed_lines, unmatched_outputs=session.unmatched_outputs,
                     incomplete_calls=session.incomplete_calls)
        for event in session.command_events.values():
            item = command_events.setdefault(event["command"], {"calls": 0, "host_output_text_tokens": 0})
            item["calls"] += 1
            item["host_output_text_tokens"] += event["host_output_text_tokens"]
        for row in session.rows:
            key = row.agent + ":" + row.tool
            item = tools.setdefault(key, {"calls": 0, "estimated_text_tokens": 0, "images": 0,
                                         "truncated_outputs": 0, "would_narrow_calls": 0})
            item["calls"] += 1
            item["estimated_text_tokens"] += row.text_chars // 4
            item["images"] += row.images
            item["truncated_outputs"] += row.truncated
            try:
                cwd = row.cwd or os.getcwd()
                if cwd not in configs:
                    configs[cwd] = load_config(cwd)
                reason = evaluate({"tool_name": row.tool, "tool_input": row.data},
                                  config=configs[cwd], cwd=cwd)
            except (OSError, ValueError, TypeError, KeyError):
                reason = None
                stats["policy_replay_errors"] += 1
            item["would_narrow_calls"] += bool(reason)
            tool, _ = normalize_tool(row.tool, row.data)
            if tool in ("exec", "functions.exec") and "code" in row.data:
                stats["opaque_code_mode_calls"] += 1
    return {
        "schema_version": 1, "sessions": len(sessions), "tools": tools,
        "reported_usage_by_agent": {key: dict(value) for key, value in actual.items()},
        "diagnostics": dict(stats),
        "codex_command_events_overlapping": command_events,
        "notes": [
            "Text estimates use chars/4; images are counted separately, never priced by base64 length.",
            "Reported usage includes repeated input/context. Codex cached_input_tokens is a subset of input_tokens.",
            "Claude usage fields retain their original meanings; agent totals are not merged.",
            "Policy replay uses current rules/files. Would-narrow counts are not a savings estimate.",
            "Code-mode wrappers are attributed to the outer call; nested tools are not guessed.",
            "Codex command-event sizes overlap wrapper output and are NOT added to model-visible totals.",
            "Unsupported/new transcript records are not parsed as tool calls; malformed/unmatched counts are shown.",
        ],
    }


def render(result):
    lines = [f"# Keyhole: {result['sessions']} sessions",
             "Tool output below is estimated TEXT tokens; images are separate.", "",
             "tool                                      calls    text tokens   images   narrow   cut"]
    for name, data in sorted(result["tools"].items(), key=lambda pair: -pair[1]["estimated_text_tokens"])[:15]:
        label = re.sub(r"[^A-Za-z0-9_.:@/-]", "?", name)[:41]
        lines.append(f"{label:41} {data['calls']:6} {data['estimated_text_tokens']:14,} "
                     f"{data['images']:8} {data['would_narrow_calls']:8} {data['truncated_outputs']:5}")
    if result["codex_command_events_overlapping"]:
        lines += ["", "Codex command events (overlapping diagnostic view; not added above):"]
        for name, data in sorted(result["codex_command_events_overlapping"].items(),
                                 key=lambda pair: -pair[1]["host_output_text_tokens"])[:8]:
            label = re.sub(r"[^A-Za-z0-9_.-]", "?", name)[:30]
            lines.append(f"{label:30} {data['calls']:6} calls, {data['host_output_text_tokens']:,} host text tokens")
    lines += ["", "Reported session usage (not estimated tool output):"]
    for agent, usage in result["reported_usage_by_agent"].items():
        lines.append(agent + ": " + (", ".join(f"{k}={v:,}" for k, v in usage.items()) or "unavailable"))
    lines += ["", "Diagnostics: " + json.dumps(result["diagnostics"]), *result["notes"]]
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="*", type=Path)
    parser.add_argument("--agent", choices=["claude", "codex", "both"], default="both")
    parser.add_argument("--project", type=Path, default=Path.cwd())
    parser.add_argument("-n", type=int, default=5, help="Largest sessions per agent")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--output", type=Path, help="Save summary locally instead of printing it")
    args = parser.parse_args()
    if args.n < 1:
        parser.error("-n must be positive")
    paths = args.paths or discover(project_root(args.project), args.agent, args.n)
    if not paths:
        parser.exit(2, "No matching transcripts. Pass explicit JSONL files or --project.\n")
    try:
        result = summarize([scan(path) for path in paths])
    except OSError as exc:
        parser.exit(2, f"Cannot read transcript: {exc}\n")
    text = json.dumps(result, indent=2, ensure_ascii=False) + "\n" if args.json else render(result)
    if args.output:
        args.output.write_text(text, encoding="utf-8")
    else:
        print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

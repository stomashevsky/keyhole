#!/usr/bin/env python3
"""Local Claude/Codex transcript report. Estimates are not billed token usage."""

import argparse
from collections import Counter, defaultdict
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import re
import shlex
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common import load_config, project_root
from hooks.guard import evaluate, normalize_tool


@dataclass
class Row:
    agent: str
    tool: str
    data: dict
    cwd: str
    text_chars: int
    images: int
    truncated: bool = False


@dataclass
class Session:
    path: str
    agent: str = "unknown"
    cwd: str = ""
    rows: list = field(default_factory=list)
    usage: dict = field(default_factory=dict)
    malformed_lines: int = 0
    unmatched_outputs: int = 0
    incomplete_calls: int = 0
    command_events: dict = field(default_factory=dict)


def metrics(content):
    """Count text and image blocks separately. Never tokenize base64 or fetch media."""
    if isinstance(content, str):
        if content.startswith("data:image/"):
            return 0, 1
        # Some MCP hosts serialize content blocks as JSON inside an output string.
        if content.startswith(("{", "[")):
            try:
                parsed = json.loads(content)
                if (isinstance(parsed, dict) and "content" in parsed) or (
                    isinstance(parsed, list) and parsed and all(
                        isinstance(p, dict) and "type" in p for p in parsed
                    )
                ):
                    return metrics(parsed)
            except ValueError:
                pass
        return len(content), 0
    if isinstance(content, list):
        parts = [metrics(part) for part in content]
        return sum(p[0] for p in parts), sum(p[1] for p in parts)
    if isinstance(content, dict):
        kind = content.get("type")
        if kind in ("image", "input_image", "output_image", "image_url"):
            return 0, 1
        if kind in ("text", "input_text", "output_text"):
            return metrics(content.get("text", ""))
        for key in ("content", "output"):
            if key in content:
                return metrics(content[key])
        return metrics(json.dumps(content, ensure_ascii=False))
    return 0, 0


def parse_input(value):
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            if isinstance(parsed, dict):
                return parsed
        except ValueError:
            pass
        return {"code": value}  # Freeform/code-mode calls remain opaque, not guessed Bash.
    return {}


def command_name(command):
    """A privacy-preserving group label: executable only, never arguments."""
    if isinstance(command, list):
        if len(command) >= 3 and re.fullmatch(r"-[a-z]*c[a-z]*", str(command[-2])):
            command = command[-1]
        else:
            return os.path.basename(str(command[0])) if command else "unknown"
    try:
        words = shlex.split(command)
        while words and "=" in words[0]:
            words.pop(0)
        return os.path.basename(words[0]) if words else "unknown"
    except (ValueError, TypeError):
        return "unparsed"


def add_output(session, calls, call_id, output):
    call = calls.get(call_id)
    if call is None:
        session.unmatched_outputs += 1
        return
    tool, data, cwd = call
    chars, images = metrics(output)
    marker = json.dumps(output, ensure_ascii=False) if not isinstance(output, str) else output
    truncated = bool(re.search(r"Warning: truncated output|Output (?:was )?truncated|tokens truncated", marker))
    session.rows.append(Row(session.agent, tool, data, cwd, chars, images, truncated))


def scan(path):
    session = Session(str(path))
    calls, returned, seen, claude_usage = {}, set(), set(), {}
    codex_usage = None
    record_usage, record_seen = Counter(), set()
    with Path(path).open(encoding="utf-8", errors="replace") as handle:
        for number, line in enumerate(handle):
            try:
                entry = json.loads(line)
            except ValueError:
                session.malformed_lines += 1
                continue
            if not isinstance(entry, dict):
                continue
            payload = entry.get("payload")
            if entry.get("type") == "session_meta" and isinstance(payload, dict):
                session.agent = "codex"
                session.cwd = payload.get("cwd", session.cwd)
            elif entry.get("type") == "turn_context" and isinstance(payload, dict):
                session.cwd = payload.get("cwd", session.cwd)
            elif entry.get("type") == "token_usage_record" and isinstance(payload, dict):
                session.agent = "codex"
                identity = payload.get("response_id")
                if identity and identity not in record_seen:
                    record_seen.add(identity)
                    record_usage.update({k: v for k, v in payload.get("usage", {}).items()
                                         if isinstance(v, int)})
                if isinstance(payload.get("thread_token_usage"), dict):
                    codex_usage = payload["thread_token_usage"]
            elif entry.get("type") == "event_msg" and isinstance(payload, dict):
                item = payload.get("item") or {}
                if payload.get("type") == "item_completed" and isinstance(item, dict):
                    if item.get("type") in ("CommandExecution", "commandExecution"):
                        chars, _ = metrics(item.get("formatted_output", item.get("aggregated_output", "")))
                        session.command_events[item.get("id") or number] = {
                            "command": command_name(item.get("command")),
                            "host_output_text_tokens": chars // 4,
                        }
                if payload.get("type") == "token_count":
                    total = (payload.get("info") or {}).get("total_token_usage")
                    if isinstance(total, dict):
                        codex_usage = total  # Cumulative snapshot, NEVER summed.
            elif entry.get("type") == "response_item" and isinstance(payload, dict):
                session.agent = "codex"
                kind = payload.get("type")
                item_id = payload.get("id")
                if item_id and (kind, item_id) in seen:
                    continue
                if item_id:
                    seen.add((kind, item_id))
                call_id = payload.get("call_id")
                if kind in ("function_call", "custom_tool_call"):
                    name = payload.get("name")
                    name = name if isinstance(name, str) else "?"
                    namespace = payload.get("namespace")
                    if isinstance(namespace, str) and namespace:
                        name = namespace + "." + name
                    data = parse_input(payload.get("arguments", payload.get("input")))
                    calls[call_id] = (name, data, session.cwd)
                elif kind in ("function_call_output", "custom_tool_call_output"):
                    add_output(session, calls, call_id, payload.get("output"))
                    returned.add(call_id)
            # Claude transcripts have message.content tool_use/tool_result pairs.
            message = entry.get("message")
            if not isinstance(message, dict):
                continue
            session.agent = "claude"
            session.cwd = entry.get("cwd", session.cwd)
            usage = message.get("usage")
            if message.get("role") == "assistant" and isinstance(usage, dict):
                claude_usage[message.get("id") or entry.get("uuid") or number] = usage
            blocks = message.get("content")
            if not isinstance(blocks, list):
                continue
            for index, block in enumerate(blocks):
                if not isinstance(block, dict):
                    continue
                identity = (entry.get("uuid"), index)
                if entry.get("uuid") and identity in seen:
                    continue
                if entry.get("uuid"):
                    seen.add(identity)
                if block.get("type") == "tool_use":
                    name = block.get("name")
                    name = name if isinstance(name, str) else "?"
                    calls[block.get("id")] = (name, parse_input(block.get("input")), session.cwd)
                elif block.get("type") == "tool_result":
                    call_id = block.get("tool_use_id")
                    add_output(session, calls, call_id, block.get("content"))
                    returned.add(call_id)
    if session.agent == "codex":
        session.usage = codex_usage if codex_usage is not None else dict(record_usage)
    elif session.agent == "claude":
        totals = Counter()
        for usage in claude_usage.values():
            totals.update({k: v for k, v in usage.items() if isinstance(v, int)})
        session.usage = dict(totals)
    session.incomplete_calls = len(set(calls) - returned)
    return session


def transcript_cwd(path):
    """Read metadata only during discovery, not whole unrelated conversations."""
    try:
        with path.open(encoding="utf-8", errors="replace") as handle:
            for _, line in zip(range(20), handle):
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(entry, dict):
                    continue
                payload = entry.get("payload") or {}
                if entry.get("type") == "session_meta" and isinstance(payload, dict):
                    return payload.get("cwd")
                if entry.get("cwd"):
                    return entry["cwd"]
    except OSError:
        pass
    return None


def discover(root, agent, limit, home=None):
    codex_home = Path(home) / ".codex" if home is not None else Path(
        os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
    home = Path(home or Path.home())
    root = Path(root).resolve()
    candidates = []
    if agent in ("both", "claude"):
        prefix = re.sub(r"[^a-zA-Z0-9]", "-", str(root))
        folder = home / ".claude/projects"
        if folder.is_dir():
            for project in folder.iterdir():
                if project.is_dir() and (project.name == prefix or project.name.startswith(prefix + "-")):
                    candidates.extend(("claude", p) for p in project.glob("*.jsonl"))
    if agent in ("both", "codex"):
        folder = codex_home / "sessions"
        if folder.is_dir():
            candidates.extend(("codex", p) for p in folder.rglob("*.jsonl"))
    selected = defaultdict(list)
    for name, path in candidates:
        cwd = transcript_cwd(path)
        if cwd:
            try:
                Path(cwd).resolve().relative_to(root)
            except ValueError:
                continue
        elif name == "codex":
            continue
        selected[name].append(path)
    return [p for files in selected.values()
            for p in sorted(files, key=lambda p: p.stat().st_size, reverse=True)[:limit]]


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

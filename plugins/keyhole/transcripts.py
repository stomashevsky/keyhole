#!/usr/bin/env python3
"""Shared Claude/Codex transcript parsing: discovery, measurement and readable events.

Two passes over the same journals, on purpose. scan() answers "what did this cost" and
stays lossy by design: it keeps sizes, never the text. events() answers "what happened"
and keeps the text the report has no reason to hold. Both live here so that a change in
either host's journal format is fixed in one place.

Transcript formats are not a stable API. Records this module does not understand are
counted and reported, never guessed at.
"""

from collections import Counter, defaultdict
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import re
import shlex

TRUNCATION = re.compile(r"Warning: truncated output|Output (?:was )?truncated|tokens truncated")
IMAGE_TYPES = ("image", "input_image", "output_image", "image_url")
TEXT_TYPES = ("text", "input_text", "output_text")
# Journal roles map onto the kinds a reader cares about; anything else reads as system.
ROLES = {"user": "user", "assistant": "assistant", "developer": "system", "system": "system"}


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
    truncated = bool(TRUNCATION.search(marker))
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


def discover(root, agent, limit, home=None, order="size"):
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
    rank = (lambda p: p.stat().st_mtime) if order == "time" else (lambda p: p.stat().st_size)
    return [p for files in selected.values()
            for p in sorted(files, key=rank, reverse=True)[:limit]]


@dataclass
class Event:
    """One readable step of a session, in journal order."""
    index: int
    agent: str
    kind: str  # meta, user, assistant, thinking, tool_use, tool_result, system
    text: str = ""
    tool: str = ""
    call_id: str = ""
    timestamp: str = ""
    role: str = ""
    model: str = ""
    images: int = 0
    truncated: bool = False


def maybe_blocks(value):
    """Content blocks some MCP hosts serialize as a JSON string, or None. Mirrors metrics()."""
    if not isinstance(value, str) or not value.startswith(("{", "[")):
        return None
    try:
        parsed = json.loads(value)
    except ValueError:
        return None
    if isinstance(parsed, dict) and "content" in parsed:
        return parsed
    if isinstance(parsed, list) and parsed and all(isinstance(p, dict) and "type" in p for p in parsed):
        return parsed
    return None


def text_of(content):
    """Render a content block as readable text. Images become a marker, never base64."""
    if isinstance(content, str):
        if content.startswith("data:image/"):
            return "[image]"
        blocks = maybe_blocks(content)
        return content if blocks is None else text_of(blocks)
    if isinstance(content, list):
        return "\n".join(part for part in (text_of(item) for item in content) if part)
    if isinstance(content, dict):
        kind = content.get("type")
        if kind in IMAGE_TYPES:
            return "[image]"
        if kind in TEXT_TYPES:
            return text_of(content.get("text", ""))
        if kind == "thinking":
            return text_of(content.get("thinking", ""))
        for key in ("content", "output", "text", "summary"):
            if key in content:
                return text_of(content[key])
        return json.dumps(content, ensure_ascii=False, indent=2)
    return "" if content is None else str(content)


def pretty(data):
    """Tool input as it reads best: code-mode calls raw, everything else indented JSON."""
    if isinstance(data, dict) and set(data) == {"code"}:
        return str(data["code"])
    try:
        return json.dumps(data, ensure_ascii=False, indent=2)
    except (TypeError, ValueError):
        return str(data)


def transcript_agent(path):
    """Name the host from metadata only, the way transcript_cwd reads a header."""
    try:
        with Path(path).open(encoding="utf-8", errors="replace") as handle:
            for _, line in zip(range(20), handle):
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(entry, dict):
                    continue
                if entry.get("type") in ("session_meta", "turn_context", "response_item",
                                         "event_msg", "token_usage_record"):
                    return "codex"
                if isinstance(entry.get("message"), dict):
                    return "claude"
    except OSError:
        pass
    return "unknown"


def events(path):
    """Yield readable Events in journal order, then one meta Event counting what was skipped.

    Skipped records are duplicates, host mirrors of something already shown, malformed
    lines in an active file, and record types this module does not model.
    """
    agent, seen, skipped, index = "unknown", set(), 0, 0

    def emit(kind, **fields):
        nonlocal index
        event = Event(index, agent, kind, **fields)
        index += 1
        return event

    with Path(path).open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            try:
                entry = json.loads(line)
            except ValueError:
                skipped += 1
                continue
            if not isinstance(entry, dict):
                skipped += 1
                continue
            stamp = str(entry.get("timestamp") or "")
            payload = entry.get("payload")
            record = entry.get("type")

            if isinstance(payload, dict) and record in ("session_meta", "turn_context"):
                agent = "codex"
                header = {key: payload[key] for key in
                          ("session_id", "cwd", "originator", "cli_version", "model", "effort",
                           "summary", "sandbox_policy", "approval_policy") if key in payload}
                yield emit("meta", text=pretty(header), timestamp=stamp,
                           model=str(payload.get("model") or ""))
                continue

            if isinstance(payload, dict) and record == "response_item":
                agent = "codex"
                item, identity = payload.get("type"), payload.get("id")
                if identity and (item, identity) in seen:
                    skipped += 1  # A record the host wrote twice; shown once.
                    continue
                if identity:
                    seen.add((item, identity))
                call_id = str(payload.get("call_id") or "")
                if item == "message":
                    role = str(payload.get("role") or "")
                    yield emit(ROLES.get(role, "system"), text=text_of(payload.get("content")),
                               role=role, timestamp=stamp)
                elif item == "reasoning":
                    text = text_of(payload.get("summary") or payload.get("content"))
                    if text:
                        yield emit("thinking", text=text, timestamp=stamp)
                elif item in ("function_call", "custom_tool_call"):
                    name = payload.get("name")
                    name = name if isinstance(name, str) else "?"
                    namespace = payload.get("namespace")
                    if isinstance(namespace, str) and namespace:
                        name = namespace + "." + name
                    yield emit("tool_use", tool=name, call_id=call_id, timestamp=stamp,
                               text=pretty(parse_input(payload.get("arguments", payload.get("input")))))
                elif item in ("function_call_output", "custom_tool_call_output"):
                    output = payload.get("output")
                    text = text_of(output)
                    yield emit("tool_result", call_id=call_id, timestamp=stamp, text=text,
                               images=metrics(output)[1], truncated=bool(TRUNCATION.search(text)))
                else:
                    skipped += 1
                continue

            if record == "event_msg":
                skipped += 1  # Mirrors response_item content; counted rather than shown twice.
                continue

            attachment = entry.get("attachment")
            if isinstance(attachment, dict):
                agent = "claude"  # Hook output, MCP status and file changes reach the model too.
                yield emit("system", tool=str(attachment.get("type") or "attachment"),
                           text=pretty(attachment), timestamp=stamp)
                continue

            if record == "system" and entry.get("content") is not None:
                agent = "claude"  # Compaction notices and local command output.
                yield emit("system", tool=str(entry.get("subtype") or "system"),
                           text=text_of(entry.get("content")), timestamp=stamp)
                continue

            message = entry.get("message")
            if not isinstance(message, dict):
                skipped += 1  # Queued prompts, mode switches and other bookkeeping records.
                continue
            agent = "claude"
            model, role = str(message.get("model") or ""), str(message.get("role") or "")
            blocks = message.get("content")
            if isinstance(blocks, str):
                blocks = [{"type": "text", "text": blocks}]
            if not isinstance(blocks, list):
                skipped += 1
                continue
            for position, block in enumerate(blocks):
                if not isinstance(block, dict):
                    skipped += 1
                    continue
                identity = (entry.get("uuid"), position)
                if entry.get("uuid"):
                    if identity in seen:
                        skipped += 1  # A record the host wrote twice; shown once.
                        continue
                    seen.add(identity)
                kind = block.get("type")
                if kind == "tool_use":
                    name = block.get("name")
                    yield emit("tool_use", tool=name if isinstance(name, str) else "?",
                               call_id=str(block.get("id") or ""), timestamp=stamp, model=model,
                               text=pretty(parse_input(block.get("input"))))
                elif kind == "tool_result":
                    content = block.get("content")
                    text = text_of(content)
                    yield emit("tool_result", call_id=str(block.get("tool_use_id") or ""),
                               timestamp=stamp, text=text, images=metrics(content)[1],
                               truncated=bool(TRUNCATION.search(text)))
                elif kind == "thinking":
                    yield emit("thinking", text=text_of(block.get("thinking", "")),
                               timestamp=stamp, model=model)
                elif kind in TEXT_TYPES:
                    yield emit(ROLES.get(role, "system"), text=text_of(block), role=role,
                               timestamp=stamp, model=model)
                elif kind in IMAGE_TYPES:
                    yield emit(ROLES.get(role, "system"), text="[image]", role=role,
                               timestamp=stamp, images=1)
                else:
                    skipped += 1

    yield Event(index, agent, "meta",
                text=f"{skipped} records not shown: duplicates, host mirrors, empty or "
                     "malformed records, and record types this reader does not model.")


# Wrappers a host writes around a prompt: useful to the model, noise when naming a session.
NOISE = re.compile(r"<(system-reminder|command-[a-z-]+|local-command-[a-z-]+|environment_context"
                   r"|user_instructions)>.*?</\1>", re.S)
COMMAND = re.compile(r"<command-name>([^<]+)</command-name>")
PLACEHOLDER = re.compile(r"^\[(image|external unsupported block:[^\]]*)\]$")
SKILL_BODY = "Base directory for this skill:"


def first_prompt(path, limit=200):
    """A line that names the session: what the person typed first, or the command they ran.

    A slash command arrives as a wrapper record followed by its expansion, and the
    expansion is the skill's own text rather than anything the person wrote.
    """
    command, expansion = "", False
    for index, event in enumerate(events(path)):
        if index >= limit:
            break
        if event.kind != "user" or not event.text:
            continue
        found = COMMAND.search(event.text)
        if found:
            command = command or found.group(1).strip()
            expansion = True
            continue
        if expansion:
            expansion = False
            continue
        for line in NOISE.sub("", event.text).splitlines():
            line = line.strip()
            if not line or line.startswith("<") or PLACEHOLDER.match(line):
                continue
            if line.startswith(SKILL_BODY):
                break
            return f"{command} · {line}" if command else line
    return command

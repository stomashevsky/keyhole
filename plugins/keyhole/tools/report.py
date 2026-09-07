#!/usr/bin/env python3
"""Where the context actually goes: session transcripts, broken down by tool.

    python3 tools/report.py            # this project, 5 largest sessions
    python3 tools/report.py -n 20      # twenty
    python3 tools/report.py a.jsonl    # specific sessions

A transcript is JSONL: the assistant emits `tool_use` blocks, the user side
returns `tool_result` with the same `tool_use_id`. We estimate the **tokens** of
each result and attribute them to the tool, and for Bash to the command itself.

Estimate, because per-call token counts are not in the transcript: text is about
chars/4, an image about pixels/750, read from the PNG or JPEG header. Never
measure an image by its base64 length — that overstates it by an order of
magnitude, and the first run of this report drew a confidently wrong conclusion
that way.
"""

import base64
import json
import os
import struct
import re
import sys
from collections import defaultdict
from pathlib import Path

PROJECTS = Path.home() / ".claude" / "projects"

LIMITER = re.compile(
    r"\|\s*(head|tail|wc)\b|(^|\s)-m\s*\d|--max-count|(^|\s)-c(\s|$)|(^|\s)-q(\s|$)|>\s*\S"
)
SCREENSHOT_TOOLS = re.compile(r"screenshot|computer|browser_batch", re.I)


def project_dir(cwd=None):
    """Transcript directory for a project: its path with / replaced by -."""
    path = Path(cwd or os.getcwd()).resolve()
    return PROJECTS / str(path).replace("/", "-")


def blocks(message):
    content = message.get("content")
    return content if isinstance(content, list) else []


def image_tokens(data):
    """Image tokens come from pixels, not from base64 length.

    Roughly (width x height) / 750, with the image scaled so the longest side is
    at most 1568. Dimensions come from the PNG IHDR or the JPEG SOF marker in the
    first bytes; decoding the whole image for two numbers would be wasteful. An
    unrecognised format falls back to a crude size estimate, which is at least
    not off by an order of magnitude.
    """
    try:
        head = base64.b64decode(data[:512] + "==", validate=False)
    except Exception:
        return len(data) // 40
    width = height = 0
    if head[:8] == b"\x89PNG\r\n\x1a\n" and head[12:16] == b"IHDR":
        width, height = struct.unpack(">II", head[16:24])
    elif head[:2] == b"\xff\xd8":
        index = 2
        while index + 9 < len(head):
            if head[index] != 0xFF:
                index += 1
                continue
            marker = head[index + 1]
            if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                height, width = struct.unpack(">HH", head[index + 5 : index + 9])
                break
            index += 2 + struct.unpack(">H", head[index + 2 : index + 4])[0]
    if not width or not height:
        return len(data) // 40
    longest = max(width, height)
    if longest > 1568:
        ratio = 1568 / longest
        width, height = width * ratio, height * ratio
    return int(width * height / 750)


def result_size(block):
    """Result cost in tokens: text ~ chars/4, image ~ pixels/750."""
    content = block.get("content")
    if isinstance(content, str):
        return len(content) // 4
    if isinstance(content, list):
        total = 0
        for part in content:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "text":
                total += len(part.get("text", "")) // 4
            elif part.get("type") == "image":
                source = part.get("source")
                if isinstance(source, dict):
                    total += image_tokens(source.get("data", ""))
        return total
    return 0


def scan(path):
    calls = {}
    rows = []  # (tool, size, tool_input)
    with open(path, encoding="utf-8", errors="replace") as handle:
        for line in handle:
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            message = entry.get("message")
            if not isinstance(message, dict):
                continue
            for block in blocks(message):
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_use":
                    calls[block.get("id")] = (block.get("name", "?"), block.get("input") or {})
                elif block.get("type") == "tool_result":
                    found = calls.get(block.get("tool_use_id"))
                    if found:
                        rows.append((found[0], result_size(block), found[1]))
    return rows


def kb(value):
    """The unit of this report is thousands of tokens, not bytes."""
    return f"{value / 1000:.0f}k"


def counts_screenshots(tool, tool_input):
    """How many frames this call would return, and how many name no scale."""
    if not SCREENSHOT_TOOLS.search(tool):
        return 0, 0
    if isinstance(tool_input.get("actions"), list):
        shots = [
            item["input"]
            for item in tool_input["actions"]
            if isinstance(item, dict)
            and isinstance(item.get("input"), dict)
            and item["input"].get("action") == "screenshot"
        ]
        return len(shots), sum(1 for shot in shots if "scale" not in shot)
    if tool_input.get("action") == "screenshot":
        return 1, 0 if "scale" in tool_input else 1
    if "screenshot" in tool.lower():
        return 1, 0 if "scale" in tool_input else 1
    return 0, 0


def report(paths, read_threshold=500):
    rows = []
    for path in paths:
        rows += scan(path)

    total = sum(size for _, size, _ in rows) or 1
    print(f"# {len(paths)} sessions, {len(rows)} calls, ~{kb(total)} tokens of tool output\n")

    by_tool = defaultdict(lambda: [0, 0])
    for tool, size, _ in rows:
        by_tool[tool][0] += size
        by_tool[tool][1] += 1
    print("## By tool")
    for tool, (size, count) in sorted(by_tool.items(), key=lambda item: -item[1][0])[:12]:
        print(f"{kb(size):>8}  {size / total:5.1%}  {count:5}×  {tool}")

    bash = [(size, tool_input.get("command", "")) for tool, size, tool_input in rows if tool == "Bash"]
    if bash:
        print("\n## Bash: costliest commands (first two words)")
        grouped = defaultdict(lambda: [0, 0])
        for size, command in bash:
            key = " ".join(command.split()[:2])[:40]
            grouped[key][0] += size
            grouped[key][1] += 1
        for key, (size, count) in sorted(grouped.items(), key=lambda item: -item[1][0])[:8]:
            print(f"{kb(size):>8}  {count:5}×  {key}")

    reads = [
        (size, tool_input.get("file_path", ""), bool(tool_input.get("limit") or tool_input.get("offset")))
        for tool, size, tool_input in rows
        if tool == "Read"
    ]
    if reads:
        print("\n## Read: heaviest reads with no offset/limit")
        for size, path, bounded in sorted((r for r in reads if not r[2]), reverse=True)[:6]:
            print(f"{kb(size):>8}  {path[-70:]}")

    # ── What the guard would have refused ─────────────────────────────────
    print("\n## What the guard would have refused")

    shots_bytes = shots_n = unnamed_n = 0
    for tool, size, tool_input in rows:
        count, unnamed = counts_screenshots(tool, tool_input)
        if count:
            shots_bytes += size
            shots_n += count
            unnamed_n += unnamed
    # scale 0.5 is half the side, a quarter of the pixels, a quarter of the tokens.
    print(
        f"{kb(shots_bytes):>8}  {shots_bytes / total:5.1%}  frames: {shots_n} total, "
        f"{unnamed_n} with no explicit scale"
    )
    print(f"{kb(shots_bytes * 0.75):>8}         estimated saving had they used scale=0.5")

    read_hit = sum(size for size, _, bounded in reads if not bounded and size > read_threshold * 10)
    read_hit_n = sum(1 for size, _, bounded in reads if not bounded and size > read_threshold * 10)
    print(f"{kb(read_hit):>8}  {read_hit / total:5.1%}  whole reads of large files: {read_hit_n} calls")

    bash_hit = sum(size for size, command in bash if not LIMITER.search(command))
    bash_hit_n = sum(1 for _, command in bash if not LIMITER.search(command))
    print(f"{kb(bash_hit):>8}  {bash_hit / total:5.1%}  Bash with no limiter: {bash_hit_n} calls")


def main():
    args = [a for a in sys.argv[1:]]
    limit = 5
    if "-n" in args:
        index = args.index("-n")
        limit = int(args[index + 1])
        del args[index : index + 2]
    if args:
        paths = [Path(a) for a in args]
    else:
        directory = project_dir()
        if not directory.is_dir():
            print(f"no transcripts for this project: {directory}", file=sys.stderr)
            return 2
        paths = sorted(directory.glob("*.jsonl"), key=lambda p: -p.stat().st_size)[:limit]
        print(f"# {directory.name}\n")
    if not paths:
        print("nothing to analyse", file=sys.stderr)
        return 2
    report(paths)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

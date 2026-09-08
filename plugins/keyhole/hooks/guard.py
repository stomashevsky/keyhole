#!/usr/bin/env python3
"""A local, fail-open cost guard for Claude Code and Codex.

evaluate() is also used by the report: one policy, not a second savings heuristic.
This is an intent check, not a shell/JavaScript parser or a security boundary.
"""

import fnmatch
import json
import os
from pathlib import Path
import re
import shlex
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common import load_config

GREP_COMMANDS = {"grep", "egrep", "fgrep", "rg", "ag"}
PAGERS = {"cat", "less", "more"}
GREP_LIMITERS = re.compile(
    r"(^|\s)(-m\s*\d+|--max-count(?:=|\s)\d+)(\s|$)|"
    r"(^|\s)-[a-zA-Z]*[cqlL](\s|$)|"
    r"(^|\s)(--count|--quiet|--silent|--files-with-matches|--files-without-match)(\s|$)"
)


def normalize_tool(name, data):
    name = name if isinstance(name, str) else ""
    if name.rsplit(".", 1)[-1] in ("exec_command", "shell_command"):
        return "Bash", {**data, "command": data.get("command", data.get("cmd", ""))}
    return name, data


def count_lines(path):
    try:
        with path.open("rb") as handle:
            return sum(1 for _ in handle)
    except OSError:
        return None


def resolve_file(name, cwd):
    path = Path(name).expanduser()
    return path if path.is_absolute() else Path(cwd) / path


def check_read(data, config, cwd):
    if data.get("offset") or data.get("limit"):
        return None
    name = data.get("file_path") or ""
    if not isinstance(name, str) or not name:
        return None
    path = resolve_file(name, cwd)
    if any(fnmatch.fnmatch(str(path), pattern) for pattern in config["read_allow"]):
        return None
    lines = count_lines(path)
    if lines is not None and lines > config["read_max_lines"]:
        return (f"{path.name} is {lines} lines. Use offset/limit for the relevant part. "
                f"If the whole file is needed, name that choice with limit={lines}.")
    return None


def last_stages(command):
    """Only inspect the final stage of each shell segment; ignore quoted pipes."""
    if "<<" in command:
        return []  # Heredocs contain data, not more top-level shell commands.
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars="|;&><\n")
        lexer.whitespace = " \t\r"
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError:
        return []
    stages, stage = [], []
    for token in tokens:
        if token in (";", "&&", "||", "\n", "&"):
            if stage:
                stages.append(stage)
            stage = []
        elif token == "|":
            stage = []
        else:
            stage.append(token)
    if stage:
        stages.append(stage)
    return stages


def check_bash(data, config, cwd):
    budget = data.get("max_output_tokens")
    if isinstance(budget, int) and not isinstance(budget, bool) and budget > 0:
        return None  # An explicit native Codex output budget already names the choice.
    command = data.get("command") or ""
    if not isinstance(command, str):
        return None
    for stage in last_stages(command):
        if any(token in (">", ">>", "&>") for token in stage):
            continue
        while stage and "=" in stage[0] and not stage[0].startswith("-"):
            stage = stage[1:]
        if not stage:
            continue
        name = os.path.basename(stage[0])
        if name in GREP_COMMANDS and not GREP_LIMITERS.search(" ".join(stage[1:])):
            return f"{name} has no output limiter. Use | head -20, -m 20, -c or -l."
        if name in PAGERS:
            paths = [resolve_file(token, cwd) for token in stage[1:] if not token.startswith("-")]
            counts = [count_lines(path) for path in paths]
            if counts and all(count is not None for count in counts):
                total = sum(counts)
                if total > config["bash_max_lines"]:
                    return f"{name} would read {total} lines. Use sed -n 'START,ENDp' or head -50."
    return None


def js_screenshots(code):
    """Recognize direct .screenshot(...) calls, excluding comments/string literals.

    Not a general JS parser: aliases, dynamic calls and loop iteration counts are
    outside coverage. Named option variables are an explicit choice and pass.
    """
    masked = re.sub(r"//[^\n]*|/\*[\s\S]*?\*/|'(?:\\.|[^'\\])*'|"
                    r'"(?:\\.|[^"\\])*"|\x60(?:\\.|[^\x60\\])*\x60',
                    lambda m: " " * len(m.group()), code)
    calls = []
    for match in re.finditer(r"\.screenshot\s*\(", masked):
        start = match.end()
        depth = 1
        for end in range(start, len(masked)):
            depth += (masked[end] == "(") - (masked[end] == ")")
            if depth == 0:
                calls.append(code[start:end].strip())
                break
    return calls


def check_cua(data, config):
    code = data.get("code")
    if not isinstance(code, str):
        return None
    calls = js_screenshots(code)
    if len(calls) > config["batch_max_screenshots"]:
        return "Multiple screenshot calls in one CUA invocation. Keep one frame; inspect intermediate DOM state."
    for args in calls:
        frame_choice = re.search(r"(?:\b(?:clip|fullPage)|['\"](?:clip|fullPage)['\"])\s*:", args)
        if not args or (args.startswith("{") and not frame_choice):
            return ("Choose the CUA frame explicitly: screenshot({clip: ...}) for a region, "
                    "or screenshot({fullPage: false}) for the viewport. Use DOM/accessibility for text. "
                    "CUA has no scale option; do not add one.")
    return None


def evaluate(payload, config=None, cwd=None):
    """Return a reason to narrow a call, or None. Never executes the proposed call."""
    if not isinstance(payload, dict) or not isinstance(payload.get("tool_input"), dict):
        return None
    cwd = cwd or payload.get("cwd") or os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    config = config if config is not None else load_config(cwd)
    tool, data = normalize_tool(payload.get("tool_name"), payload["tool_input"])
    if tool == "Read":
        return check_read(data, config, cwd)
    if tool == "Bash":
        return check_bash(data, config, cwd)
    if tool in config.get("cua_tools", []):
        return check_cua(data, config)
    if tool in config.get("screenshot_tools", []):
        if data.get("action") == "screenshot" and "scale" not in data:
            return "Name the screenshot scale: scale=0.5, or scale=1 when full detail is needed."
    if tool in config.get("batch_tools", []):
        actions = data.get("actions")
        if not isinstance(actions, list):
            return None
        shots = [item["input"] for item in actions if isinstance(item, dict)
                 and isinstance(item.get("input"), dict) and item["input"].get("action") == "screenshot"]
        if len(shots) > config["batch_max_screenshots"]:
            return "Multiple screenshots in a batch. Keep one; use DOM/accessibility between actions."
        if any("scale" not in shot for shot in shots):
            return "Name the screenshot scale explicitly: scale=0.5, or scale=1 for full detail."
    return None


def main():
    if os.environ.get("KEYHOLE", "").lower() in ("off", "0", "false"):
        return
    try:
        reason = evaluate(json.load(sys.stdin))
        if reason:
            print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse",
                  "permissionDecision": "deny", "permissionDecisionReason": reason}}))
    except Exception:
        pass  # A cost guard failure must not stop the task.


if __name__ == "__main__":
    main()

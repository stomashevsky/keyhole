#!/usr/bin/env python3
"""PreToolUse guard: turn an expensive reflex into a deliberate choice.

Two findings shaped this, and neither is a guess.

**Enforcement, not advice.** Spotify shipped `shunt` (github.com/spotify/
portal-ai-plugins, blog 2026-09-03) after routing rules written into CLAUDE.md
were ignored: "The rules were advisory, not enforced." A hook is what held.
This guard borrows that shape and nothing else — it delegates nowhere, sends
nothing anywhere, and needs no service. It only refuses, and names the cheaper
form of the same action.

**Noise costs accuracy, not just money.** SKILL.state (arXiv 2608.26263)
measured a plain append-only runtime dropping from 0.68 to 0.53 task score as
distractor events per turn rose from 5 to 50, while runtimes that keep
distractors out of the prompt held 0.97+. An unbounded grep dump is exactly
that distractor stream. Blocking it is a correctness measure that happens to
be cheaper.

Every refusal is one parameter away from proceeding, on purpose. The guard
cannot know whether you need the whole file or the full-resolution frame; it
can make sure that when you take one, you chose it.

Config, later entries winning: `keyhole.default.json` shipped with the plugin,
then `$CLAUDE_PROJECT_DIR/.claude/keyhole.json`, then environment variables.
`KEYHOLE=off` disables everything. Any unexpected condition inside the guard
means allow: a hook that fails must never stop the work.
"""

import fnmatch
import json
import os
import re
import sys
from pathlib import Path

PLUGIN_ROOT = Path(os.environ.get("CLAUDE_PLUGIN_ROOT") or Path(__file__).resolve().parent.parent)

GREP_COMMANDS = {"grep", "egrep", "fgrep", "rg", "ag"}
GREP_LIMITERS = re.compile(
    r"(^|\s)(-m|--max-count)(\s|=)|(^|\s)-[a-zA-Z]*[cqlL](\s|$)|"
    r"(^|\s)(--count|--quiet|--silent|--files-with-matches|--files-without-match)(\s|$)"
)
PAGERS = {"cat", "less", "more"}

FALLBACK = {
    "read_max_lines": 500,
    "read_allow": ["**/CLAUDE.md", "**/AGENTS.md", "**/README.md", "**/.claude/state/*.md"],
    "bash_max_lines": 500,
    "screenshot_tools": [],
    "batch_tools": [],
    "batch_max_screenshots": 1,
}

ENV_OVERRIDES = {
    "KEYHOLE_READ_MAX_LINES": ("read_max_lines", int),
    "KEYHOLE_BASH_MAX_LINES": ("bash_max_lines", int),
    "KEYHOLE_MAX_SCREENSHOTS": ("batch_max_screenshots", int),
}


def load_config():
    config = dict(FALLBACK)
    sources = [PLUGIN_ROOT / "keyhole.default.json"]
    project = os.environ.get("CLAUDE_PROJECT_DIR")
    if project:
        sources.append(Path(project) / ".claude" / "keyhole.json")
    for source in sources:
        try:
            config.update(json.loads(source.read_text(encoding="utf-8")))
        except Exception:
            continue
    for variable, (key, cast) in ENV_OVERRIDES.items():
        raw = os.environ.get(variable)
        if raw:
            try:
                config[key] = cast(raw)
            except ValueError:
                pass
    return config


def deny(reason):
    """Refuse in both shapes at once.

    The reference documents `hookSpecificOutput.permissionDecision`; older
    plugins send a top-level `decision`. They mean the same thing here, so a
    runtime that understands either one behaves identically.
    """
    print(
        json.dumps(
            {
                "decision": "block",
                "reason": reason,
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": reason,
                },
            }
        )
    )
    sys.exit(0)


def allow():
    sys.exit(0)


def count_lines(path):
    try:
        with open(path, "rb") as handle:
            return sum(1 for _ in handle)
    except OSError:
        return None


# ── Read ──────────────────────────────────────────────────────────────────


def check_read(tool_input, config):
    if tool_input.get("offset") or tool_input.get("limit"):
        allow()
    path = tool_input.get("file_path") or ""
    if not path or not os.path.isfile(path):
        allow()
    for pattern in config["read_allow"]:
        if fnmatch.fnmatch(path, pattern):
            allow()
    lines = count_lines(path)
    if lines is None or lines <= config["read_max_lines"]:
        allow()
    deny(
        f"{os.path.basename(path)} is {lines} lines (threshold {config['read_max_lines']}). "
        f"Narrow the read: offset/limit for the part you need, Grep to find where it is. "
        f"If you genuinely need the whole file (you are about to edit it, or copy from it "
        f"verbatim), read it again with limit={lines} — that is a decision, not a default."
    )


# ── Bash ──────────────────────────────────────────────────────────────────


def last_stages(command):
    """The final stage of each segment: that is what prints into the transcript."""
    stages = []
    for segment in re.split(r"&&|\|\||;|\n", command):
        stage = re.split(r"(?<!\|)\|(?!\|)", segment)[-1].strip()
        if stage:
            stages.append(stage)
    return stages


def first_word(stage):
    for token in stage.split():
        if "=" in token.split("/")[0] and not token.startswith("-"):
            continue  # FOO=bar before the command
        return os.path.basename(token.strip("\"'"))
    return ""


def check_bash(tool_input, config):
    command = tool_input.get("command") or ""
    if not command.strip():
        allow()
    for stage in last_stages(command):
        # A redirect sends the output to a file, not into the conversation.
        if re.search(r"(?<![0-9<>])>", stage):
            continue
        name = first_word(stage)
        if name in GREP_COMMANDS:
            if GREP_LIMITERS.search(stage):
                continue
            deny(
                f"`{name}` with no limiter returns an unbounded match dump — the single "
                f"largest source of transcript noise. Add `| head -20`, or `-m 20`, or "
                f"`-c`/`-l` when a count or the file names is what you actually need."
            )
        if name in PAGERS:
            args = [
                token
                for token in stage.split()[1:]
                if not token.startswith("-") and not token.startswith("<")
            ]
            if len(args) != 1:
                continue
            path = args[0].strip("\"'")
            if not os.path.isfile(path):
                continue
            lines = count_lines(path)
            if lines is None or lines <= config["bash_max_lines"]:
                continue
            deny(
                f"{os.path.basename(path)} is {lines} lines. Use `sed -n '<from>,<to>p'`, "
                f"`head -50`, or the Read tool with offset/limit."
            )
    allow()


# ── Screenshots ───────────────────────────────────────────────────────────

SCALE_HINT = (
    "Name the scale explicitly. `scale: 0.5` is half the width and height, so a quarter "
    "of the image tokens, and it answers most layout questions; `zoom` on a region reads "
    "fine detail at full resolution; `read_page` answers questions about text and "
    "structure for almost nothing. If you do need the full frame, pass `scale: 1` — then "
    "it is a choice rather than a default."
)


def check_screenshot(tool_input, config):
    if tool_input.get("action") != "screenshot" or "scale" in tool_input:
        allow()
    deny(SCALE_HINT)


def check_batch(tool_input, config):
    actions = tool_input.get("actions")
    if not isinstance(actions, list):
        allow()
    shots = [
        item["input"]
        for item in actions
        if isinstance(item, dict)
        and isinstance(item.get("input"), dict)
        and item["input"].get("action") == "screenshot"
    ]
    if not shots:
        allow()
    if len(shots) > config["batch_max_screenshots"]:
        deny(
            f"{len(shots)} screenshots in one batch means {len(shots)} full frames land in "
            f"the transcript at once. Keep the last one; verify the intermediate steps with "
            f"`read_page`. " + SCALE_HINT
        )
    if any("scale" not in shot for shot in shots):
        deny(SCALE_HINT)
    allow()


# ── Entry ─────────────────────────────────────────────────────────────────


def main():
    if os.environ.get("KEYHOLE", "").lower() in ("off", "0", "false"):
        allow()
    try:
        payload = json.load(sys.stdin)
    except Exception:
        allow()
    tool = payload.get("tool_name") or ""
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        allow()
    config = load_config()
    try:
        if tool == "Read":
            check_read(tool_input, config)
        elif tool == "Bash":
            check_bash(tool_input, config)
        elif tool in config["screenshot_tools"]:
            check_screenshot(tool_input, config)
        elif tool in config["batch_tools"]:
            check_batch(tool_input, config)
    except SystemExit:
        raise
    except Exception:
        allow()
    allow()


if __name__ == "__main__":
    main()

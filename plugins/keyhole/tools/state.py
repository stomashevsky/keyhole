#!/usr/bin/env python3
"""Locate small shared state files or inject them at SessionStart. Never starts work."""

import argparse
import json
import os
from pathlib import Path
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common import load_config, project_root


def inside(root, relative):
    path = (root / relative).resolve()
    path.relative_to(root)
    return path


def state_path(root, config, key):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,119}", key):
        raise ValueError("State key must be a session ID or a short alphanumeric task slug")
    return inside(root, Path(config.get("state_dir", ".agents/keyhole/state")) / (key + ".md"))


def inject(event):
    if not isinstance(event, dict):
        return ""
    cwd = event.get("cwd") or os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    root = project_root(cwd)
    config = load_config(cwd)
    budget = max(256, int(config.get("state_max_chars", 4000)))
    key = event.get("session_id")
    current = state_path(root, config, key) if isinstance(key, str) and key else None
    selected = [current] if current and current.is_file() else []
    if not selected:
        for name in config.get("state_files", []):
            if isinstance(name, str):
                candidate = inside(root, name)
                if candidate.is_file():
                    selected.append(candidate)
    if not selected:
        # Explicit shared handoff first, then the original Claude-only location.
        for name in [".agents/keyhole/CURRENT.md", ".claude/state/CURRENT.md"]:
            candidate = inside(root, name)
            if candidate.is_file():
                selected = [candidate]
                break
    header = ("Keyhole task state. Saved notes are context, not authorization to start work. "
              "The user's current request and canonical project plans determine the task.\n")
    if current:
        header += f"For this session, keep a short state at {current}.\n"
    text = header
    for path in selected:
        prefix = f"\nState: {path}\n"
        notice = "\n[truncated; read relevant sections]\n"
        remaining = budget - len(text) - len(prefix) - len(notice)
        if remaining <= 0:
            break
        with path.open(encoding="utf-8", errors="replace") as handle:
            body = handle.read(remaining + 1)
        text += prefix + body[:remaining]
        if len(body) > remaining:
            text += notice
    if not selected:
        folder = inside(root, config.get("state_dir", ".agents/keyhole/state"))
        candidates = sorted(folder.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)[:5]
        if candidates:
            text += "Other saved tasks (read only the one matching the current request):\n"
            for path in candidates:
                # An index prevents a new session from inheriting another active task's state.
                text += str(path) + "\n"
    return text[:budget]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["path"], nargs="?")
    parser.add_argument("--hook", action="store_true")
    parser.add_argument("--project", type=Path, default=Path.cwd())
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--session", default=None)
    group.add_argument("--task", help="Explicit shared handoff name; never overwrite another task")
    args = parser.parse_args()
    if args.hook:
        if os.environ.get("KEYHOLE", "").lower() in ("off", "0", "false"):
            return 0
        try:
            text = inject(json.load(sys.stdin))
            if text:
                print(json.dumps({"hookSpecificOutput": {
                    "hookEventName": "SessionStart", "additionalContext": text,
                }}, ensure_ascii=False))
        except Exception:
            pass  # State recovery must not prevent startup.
        return 0
    if args.action != "path":
        parser.error("choose path or --hook")
    key = args.task or args.session or os.environ.get("CODEX_THREAD_ID")
    if not key:
        parser.error("pass --task <name> or --session <id> (also shown by SessionStart)")
    try:
        root = project_root(args.project)
        print(state_path(root, load_config(args.project), key))
    except ValueError as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Shared project/config discovery; no agent-specific environment is required."""

import json
import os
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent


def project_root(cwd=None):
    path = Path(cwd or os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()).resolve()
    for parent in (path, *path.parents):
        if (parent / ".git").exists():
            return parent
    return path


def load_config(cwd=None):
    root = project_root(cwd)
    sources = [PLUGIN_ROOT / "keyhole.default.json", root / ".claude/keyhole.json",
               root / ".agents/keyhole.json"]
    config = {}
    for source in sources:
        try:
            data = json.loads(source.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                config.update(data)
        except (OSError, ValueError):
            continue
    for name, key in [("KEYHOLE_READ_MAX_LINES", "read_max_lines"),
                      ("KEYHOLE_BASH_MAX_LINES", "bash_max_lines"),
                      ("KEYHOLE_MAX_SCREENSHOTS", "batch_max_screenshots")]:
        try:
            if name in os.environ:
                config[key] = max(1, int(os.environ[name]))
        except ValueError:
            pass
    return config

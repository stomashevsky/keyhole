#!/bin/bash
# Compatibility entrypoint for both plugin hosts. State handling itself is shared Python.
command -v python3 >/dev/null 2>&1 || exit 0
plugin_root="$(cd "$(dirname "$0")/.." && pwd)"
python3 "$plugin_root/tools/state.py" --hook || exit 0

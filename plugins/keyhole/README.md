# Keyhole for Claude Code and Codex

Local context guard, shared task state and transcript reports. Both plugin manifests
use the same implementation. See the [installation, configuration and compatibility guide](https://github.com/stomashevsky/keyhole#readme).

- Hooks: `hooks/hooks.json`
- Guard policy: `hooks/guard.py`
- Report: `tools/report.py`
- State paths and injection: `tools/state.py`
- Shared settings: `keyhole.default.json`, overridden by project `.agents/keyhole.json`

MIT licensed. No network calls or model calls from the plugin.

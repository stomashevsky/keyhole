#!/usr/bin/env python3
"""The hook as an executable contract: JSON in, decision out.

We exercise the contract, not the functions: exactly what Claude Code sees.
Each case is named for what it protects. Half the value of a guard like this is
staying silent where the work is legitimate.

    python3 tests/guard.test.py
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HOOK = Path(__file__).resolve().parent.parent / "hooks" / "guard.py"


def run(payload, env=None):
    environment = dict(os.environ)
    environment.pop("KEYHOLE", None)
    environment.update(env or {})
    result = subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=environment,
    )
    if result.returncode != 0:
        return "crash:" + result.stderr.strip()[-200:]
    out = result.stdout.strip()
    if not out:
        return "allow"
    try:
        parsed = json.loads(out)
    except ValueError:
        return "unparseable:" + out[:120]
    decision = parsed.get("hookSpecificOutput", {}).get("permissionDecision")
    return "deny" if decision == "deny" else f"other:{decision}"


def read(**tool_input):
    return {"tool_name": "Read", "tool_input": tool_input}


def bash(command):
    return {"tool_name": "Bash", "tool_input": {"command": command}}


def shot(**tool_input):
    return {"tool_name": "mcp__Claude_Browser__computer", "tool_input": tool_input}


def batch(actions):
    return {
        "tool_name": "mcp__Claude_Browser__browser_batch",
        "tool_input": {"actions": actions},
    }


def main():
    tmp = Path(tempfile.mkdtemp(prefix="context-guard-"))
    big = tmp / "big.ts"
    big.write_text("line\n" * 900, encoding="utf-8")
    small = tmp / "small.ts"
    small.write_text("line\n" * 40, encoding="utf-8")
    doc = tmp / "docs" / "README.md"
    doc.parent.mkdir()
    doc.write_text("line\n" * 900, encoding="utf-8")

    cases = [
        # Read: only an unbounded read of a large file is expensive.
        ("read: large file, unbounded", read(file_path=str(big)), "deny"),
        ("read: same file with limit", read(file_path=str(big), limit=80), "allow"),
        ("read: same file with offset", read(file_path=str(big), offset=200), "allow"),
        ("read: small file", read(file_path=str(small)), "allow"),
        ("read: allowlisted doc", read(file_path=str(doc)), "allow"),
        ("read: missing path", read(file_path=str(tmp / "missing.ts")), "allow"),
        ("read: empty input", read(), "allow"),
        # Bash: refuse only where the output is unbounded by construction.
        ("bash: grep with no limiter", bash("grep -rn foo src"), "deny"),
        ("bash: grep piped to head", bash("grep -rn foo src | head -20"), "allow"),
        ("bash: grep -m", bash("grep -m 20 foo file.ts"), "allow"),
        ("bash: grep -c", bash("grep -c foo file.ts"), "allow"),
        ("bash: grep -l", bash("grep -rl foo src"), "allow"),
        ("bash: grep -q in a condition", bash("grep -q foo file.ts && echo yes"), "allow"),
        ("bash: grep at the end of a chain", bash("cd /tmp && grep -rn foo ."), "deny"),
        ("bash: grep mid-pipeline", bash("grep -rn foo src | wc -l"), "allow"),
        ("bash: rg with no limiter", bash("rg pattern"), "deny"),
        ("bash: output redirected to a file", bash("grep -rn foo src > out.txt"), "allow"),
        ("bash: cat of a large file", bash(f"cat {big}"), "deny"),
        ("bash: cat of a small file", bash(f"cat {small}"), "allow"),
        ("bash: cat piped to head", bash(f"cat {big} | head -20"), "allow"),
        ("bash: heredoc left alone", bash("cat > out.py <<'EOF'\nprint(1)\nEOF"), "allow"),
        ("bash: ordinary work", bash("npm run typecheck && npm run lint"), "allow"),
        ("bash: git", bash("git status"), "allow"),
        ("bash: sed with a range", bash(f"sed -n '1,50p' {big}"), "allow"),
        ("bash: empty", bash("   "), "allow"),
        # Screenshot: the scale is named explicitly.
        ("frame: no scale", shot(action="screenshot"), "deny"),
        ("frame: with scale", shot(action="screenshot", scale=0.5), "allow"),
        ("frame: full but named", shot(action="screenshot", scale=1), "allow"),
        ("frame: not a screenshot", shot(action="left_click", coordinate=[10, 10]), "allow"),
        ("frame: zoom left alone", shot(action="zoom", region=[0, 0, 10, 10]), "allow"),
        # Batch: one named frame.
        ("batch: one frame with scale", batch([
            {"name": "computer", "input": {"action": "screenshot", "scale": 0.5}}
        ]), "allow"),
        ("batch: frame with no scale", batch([
            {"name": "computer", "input": {"action": "screenshot"}}
        ]), "deny"),
        ("batch: two frames", batch([
            {"name": "computer", "input": {"action": "screenshot", "scale": 0.5}},
            {"name": "computer", "input": {"action": "screenshot", "scale": 0.5}},
        ]), "deny"),
        ("batch: no frames", batch([
            {"name": "navigate", "input": {"url": "https://example.com"}}
        ]), "allow"),
        ("batch: unfamiliar shape", batch("not a list"), "allow"),
        # General.
        ("unrelated tool", {"tool_name": "Edit", "tool_input": {"file_path": str(big)}}, "allow"),
        ("garbage input", {"tool_name": "Read", "tool_input": "a string"}, "allow"),
        ("kill switch", read(file_path=str(big)), "allow", {"KEYHOLE": "off"}),
    ]

    failures = 0
    for case in cases:
        name, payload, expected = case[0], case[1], case[2]
        env = case[3] if len(case) > 3 else None
        actual = run(payload, env)
        mark = "ok  " if actual == expected else "FAIL"
        if actual != expected:
            failures += 1
            print(f"{mark} {name}: expected {expected}, got {actual}")
        else:
            print(f"{mark} {name}")

    print(f"\n{len(cases) - failures}/{len(cases)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())

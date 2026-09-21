#!/usr/bin/env python3
"""Read local Claude/Codex sessions as an offline HTML timeline. No network, no dependencies."""

import argparse
from datetime import datetime
import html
from pathlib import Path
import sys
import tempfile
import webbrowser

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common import project_root
from transcripts import discover, events, first_prompt, transcript_agent, transcript_cwd

KINDS = ["user", "assistant", "thinking", "tool_use", "tool_result", "system", "meta"]
FOLD = 800  # Longer bodies open on demand rather than filling the page.

STYLE = """
:root { color-scheme: light dark;
  --bg:#fbfbfa; --fg:#1a1a18; --dim:#6b6b66; --line:#e2e0da; --card:#fff; --accent:#8b6f47; }
@media (prefers-color-scheme: dark) { :root {
  --bg:#17171a; --fg:#e8e6e3; --dim:#9a978f; --line:#2e2e33; --card:#1e1e22; --accent:#c8a876; } }
* { box-sizing: border-box; }
body { margin:0; padding:0 16px 64px; background:var(--bg); color:var(--fg);
  font:14px/1.5 ui-sans-serif, -apple-system, "Segoe UI", sans-serif; }
header { position:sticky; top:0; z-index:2; background:var(--bg); padding:16px 0 10px;
  border-bottom:1px solid var(--line); }
h1 { font-size:16px; margin:0 0 6px; }
h2 { font-size:14px; margin:28px 0 2px; }
.warn { margin:0 0 10px; color:var(--dim); max-width:80ch; }
.filters { display:flex; flex-wrap:wrap; gap:10px 14px; align-items:center; }
.filters label { cursor:pointer; user-select:none; }
input[type=search] { flex:1 1 220px; min-width:180px; padding:5px 8px; border-radius:6px;
  border:1px solid var(--line); background:var(--card); color:inherit; font:inherit; }
button { cursor:pointer; padding:5px 10px; border-radius:6px; border:1px solid var(--line);
  background:var(--card); color:inherit; font:inherit; }
nav ol { margin:14px 0 0; padding-left:20px; color:var(--dim); }
nav a { color:inherit; }
.prompt { margin:2px 0 0; }
.src { color:var(--dim); font-size:12px; word-break:break-all; }
.ev { border-left:3px solid var(--line); background:var(--card); border-radius:0 6px 6px 0;
  margin:6px 0; padding:6px 10px; }
.ev[data-kind=user] { border-left-color:var(--accent); }
.ev[data-kind=assistant] { border-left-color:#5b8c76; }
.ev[data-kind=thinking] { border-left-color:#7d7aa8; }
.ev[data-kind=tool_use] { border-left-color:#c38a4a; }
.ev[data-kind=tool_result] { border-left-color:#4a7fa8; }
.hd { display:flex; flex-wrap:wrap; gap:8px; align-items:baseline; color:var(--dim); font-size:12px; }
.chip { font-weight:600; color:var(--fg); }
.tool { font-family:ui-monospace, SFMono-Regular, Menlo, monospace; color:var(--fg); }
pre { margin:4px 0 0; white-space:pre-wrap; word-break:break-word; overflow-wrap:anywhere;
  font:12px/1.5 ui-monospace, SFMono-Regular, Menlo, monospace; }
summary { cursor:pointer; color:var(--dim); font-size:12px; }
.stats { color:var(--dim); font-size:12px; margin:8px 0 0; }
"""

SCRIPT = """
const box = document.getElementById('q');
const boxes = [...document.querySelectorAll('.filters input[type=checkbox]')];
const count = document.getElementById('count');
function apply() {
  const term = box.value.trim().toLowerCase();
  const kinds = new Set(boxes.filter(b => b.checked).map(b => b.value));
  let shown = 0;
  for (const el of document.querySelectorAll('.ev')) {
    const ok = kinds.has(el.dataset.kind) &&
      (!term || el.textContent.toLowerCase().includes(term));
    el.hidden = !ok;
    if (ok) shown++;
  }
  count.textContent = shown + ' shown';
}
function fold(open) { for (const d of document.querySelectorAll('details')) d.open = open; }
box.addEventListener('input', apply);
for (const b of boxes) b.addEventListener('change', apply);
document.getElementById('expand').addEventListener('click', () => fold(true));
document.getElementById('collapse').addEventListener('click', () => fold(false));
apply();
"""


def size(chars):
    return f"{chars / 1000:.1f}k" if chars >= 1000 else str(chars)


def clock(stamp):
    """The time part of an ISO timestamp, or the raw value when it is something else."""
    if "T" in stamp and len(stamp) >= 19:
        return stamp[11:19]
    return stamp[:19]


def body(text, limit):
    """Escaped body, folded when long, clipped when a per-event limit is set."""
    omitted = 0
    if limit and len(text) > limit:
        text, omitted = text[:limit], len(text) - limit
    if omitted:
        text += f"\n... +{omitted:,} chars omitted (rerun with --max-chars 0)"
    escaped = html.escape(text)
    if len(text) <= FOLD:
        return f"<pre>{escaped}</pre>"
    head = html.escape(text[:100].split("\n")[0])
    return (f"<details><summary>{head} ... ({len(text):,} chars)</summary>"
            f"<pre>{escaped}</pre></details>")


def event_html(event, limit):
    head = [f'<span class="chip">{html.escape(event.kind)}</span>']
    if event.tool:
        head.append(f'<span class="tool">{html.escape(event.tool)}</span>')
    if event.model:
        head.append(html.escape(event.model))
    if event.timestamp:
        head.append(f"<time>{html.escape(clock(event.timestamp))}</time>")
    if event.text:
        head.append(size(len(event.text)))
    if event.images:
        head.append(f"{event.images} image(s)")
    if event.truncated:
        head.append("truncated by host")
    return (f'<article class="ev" data-kind="{html.escape(event.kind)}">'
            f'<div class="hd">{"".join(head)}</div>{body(event.text, limit)}</article>\n')


def short(path):
    """A path with the home directory folded back to ~, for reading and for copying."""
    home = str(Path.home())
    text = str(path)
    return "~" + text[len(home):] if text.startswith(home) else text


def bytes_label(count):
    for unit in ("B", "K", "M", "G"):
        if count < 1024 or unit == "G":
            return f"{count:.0f}{unit}" if unit == "B" else f"{count:.1f}{unit}"
        count /= 1024


def describe(path):
    """What the listing and the page need before reading a session end to end."""
    path = Path(path)
    try:
        status = path.stat()
        moment = datetime.fromtimestamp(status.st_mtime).strftime("%Y-%m-%d %H:%M")
        size = status.st_size
    except OSError:
        moment, size = "", 0
    return {"path": path, "agent": transcript_agent(path), "cwd": transcript_cwd(path) or "unknown",
            "when": moment, "bytes": size, "prompt": first_prompt(path)}


def listing(items, limit):
    """Candidate sessions, newest first, each with the path to pass back in."""
    lines = []
    for number, item in enumerate(items, start=1):
        lines.append(f'{number:3}  {item["agent"]:7} {item["when"]:16} '
                     f'{bytes_label(item["bytes"]):>7}  {item["prompt"][:60]}')
        lines.append(f'     {short(item["path"])}')
    lines.append(f"\n{limit} per agent, newest first. Pass -n for more, or a path to open one.")
    return "\n".join(lines) + "\n"


def choose(items, answer):
    """Turn what the person typed at the picker into one session, or say what was wrong."""
    answer = (answer or "").strip() or "1"
    if not answer.isdigit() or not 1 <= int(answer) <= len(items):
        raise ValueError(f"Pick a number between 1 and {len(items)}.")
    return items[int(answer) - 1]


def session_html(item, limit):
    """Stream one session: header first, counts last, so nothing is held in memory twice."""
    yield f'<h2>{html.escape(item["agent"])} &middot; {html.escape(item["when"])}</h2>\n'
    if item["prompt"]:
        yield f'<p class="prompt">{html.escape(item["prompt"][:200])}</p>\n'
    yield (f'<p class="src">{html.escape(item["cwd"])} &middot; {html.escape(short(item["path"]))}'
           f' &middot; {item["bytes"]:,} bytes</p>\n')
    counts, chars = {}, 0
    for event in events(item["path"]):
        counts[event.kind] = counts.get(event.kind, 0) + 1
        chars += len(event.text)
        yield event_html(event, limit)
    summary = ", ".join(f"{name} {number}" for name, number in sorted(counts.items()))
    yield f'<p class="stats">{html.escape(summary)} &middot; {chars:,} chars of text</p>\n'


def document(items, limit):
    yield ("<!doctype html>\n<html lang=\"en\"><head><meta charset=\"utf-8\">\n"
           "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
           f"<title>Keyhole: {len(items)} sessions</title>\n<style>{STYLE}</style>\n"
           "</head><body>\n<header><h1>Keyhole session view</h1>\n"
           '<p class="warn">Everything the agent read and wrote is on this page, including '
           "secrets that appeared in tool output. It is a local file: keep it local.</p>\n"
           '<div class="filters">')
    for kind in KINDS:
        checked = "" if kind == "meta" else " checked"
        yield (f'<label><input type="checkbox" value="{kind}"{checked}> {kind}</label>')
    yield ('<input type="search" id="q" placeholder="filter text">'
           '<button id="expand">expand all</button><button id="collapse">collapse all</button>'
           '<span id="count"></span></div></header>\n<nav><ol>')
    for number, item in enumerate(items):
        label = " &middot; ".join(html.escape(part) for part in
                                  (item["agent"], item["when"], item["prompt"][:70]) if part)
        yield f'<li><a href="#s{number}">{label}</a></li>'
    yield "</ol></nav>\n<main>\n"
    for number, item in enumerate(items):
        yield f'<section id="s{number}">\n'
        yield from session_html(item, limit)
        yield "</section>\n"
    yield f"</main>\n<script>{SCRIPT}</script>\n</body></html>\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="*", type=Path)
    parser.add_argument("--agent", choices=["claude", "codex", "both"], default="both")
    parser.add_argument("--project", type=Path, default=Path.cwd())
    parser.add_argument("-n", type=int, default=3, help="Sessions per agent, newest first")
    parser.add_argument("--largest", action="store_true",
                        help="Rank by file size instead of recency, as the report does")
    parser.add_argument("--list", action="store_true", dest="listing",
                        help="Print the candidate sessions instead of building a page")
    parser.add_argument("--pick", action="store_true",
                        help="Show the candidates and open the one you answer with")
    parser.add_argument("--open", action="store_true", dest="launch",
                        help="Open the page in your browser when it is written")
    parser.add_argument("--max-chars", type=int, default=4000,
                        help="Per-event text limit; 0 keeps every character")
    parser.add_argument("--output", type=Path,
                        default=Path(tempfile.gettempdir()) / "keyhole-view.html")
    args = parser.parse_args()
    if args.n < 1:
        parser.error("-n must be positive")
    if args.max_chars < 0:
        parser.error("--max-chars cannot be negative")
    paths = args.paths or discover(project_root(args.project), args.agent, args.n,
                                   order="size" if args.largest else "time")
    if not paths:
        parser.exit(2, "No matching transcripts. Pass explicit JSONL files or --project.\n")
    try:
        items = [describe(path) for path in paths]
        if args.listing:
            print(listing(items, args.n), end="")
            return 0
        if args.pick and len(items) > 1:
            print(listing(items, args.n), end="")
            try:
                items = [choose(items, input("Open which one? [1] "))]
            except ValueError as exc:
                parser.exit(2, f"{exc}\n")
            except (EOFError, KeyboardInterrupt):
                parser.exit(1, "\nNothing opened.\n")
        with args.output.open("w", encoding="utf-8") as handle:
            for chunk in document(items, args.max_chars):
                handle.write(chunk)
    except OSError as exc:
        parser.exit(2, f"Cannot read the sessions: {exc}\n")
    print(f"{args.output} ({args.output.stat().st_size:,} bytes, {len(items)} sessions)")
    print("Local file with full transcript text. Do not share or commit it.")
    if args.launch:
        webbrowser.open(args.output.resolve().as_uri())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Read local Claude/Codex sessions as an offline HTML timeline. No network, no dependencies."""

import argparse
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import html
from pathlib import Path
import sys
import tempfile
from urllib.parse import quote, unquote
import webbrowser

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common import project_root
from transcripts import discover, events, first_prompt, transcript_agent, transcript_cwd

PICKER = """
html, body { height:100%; }
body { margin:0; padding:0; display:flex; }
aside { width:300px; flex:none; height:100vh; overflow:auto; border-right:1px solid var(--line);
  background:var(--card); padding:10px 10px 24px; }
aside h1 { font-size:13px; margin:2px 2px 8px; color:var(--dim); font-weight:600; }
aside input[type=search] { width:100%; margin:0 0 8px; }
.row { display:block; padding:6px 8px; border-radius:var(--radius-sm); text-decoration:none;
  color:inherit;
  cursor:pointer; }
.row:hover { background:var(--bg); }
.row.on { background:var(--bg); box-shadow:inset 2px 0 0 var(--accent); }
.row b { display:block; font-weight:500; overflow:hidden; text-overflow:ellipsis;
  white-space:nowrap; }
.row span { color:var(--dim); font-size:12px; }
aside p { color:var(--dim); font-size:12px; margin:12px 2px 0; }
iframe { flex:1; height:100vh; border:0; background:var(--bg); }
@media (max-width:720px) { body { display:block; }
  aside { width:auto; height:42vh; border-right:0; border-bottom:1px solid var(--line); }
  iframe { height:58vh; width:100%; } }
"""

PICK_SCRIPT = """
const rows = [...document.querySelectorAll('.row')];
rows.forEach(row => row.addEventListener('click', () => {
  rows.forEach(other => other.classList.toggle('on', other === row));
}));
document.getElementById('find').addEventListener('input', event => {
  const needle = event.target.value.toLowerCase();
  rows.forEach(row => { row.hidden = !row.textContent.toLowerCase().includes(needle); });
});
"""

KINDS = ["user", "assistant", "thinking", "tool_use", "tool_result", "system", "meta"]
FOLD = 800  # Longer bodies open on demand rather than filling the page.

STYLE = """
/* Plex UI tokens (github.com/plex-ui/ui), resolved to plain values. Same palette, radii
   and type stack; no npm package, no webfont, no request, so the page stays one file. */
:root { color-scheme: light dark;
  --radius-sm:0.375rem;
  --font-sans: ui-sans-serif, -apple-system, system-ui, "Segoe UI", "Noto Sans", "Helvetica",
    "Arial", sans-serif;
  --font-mono: ui-monospace, "SFMono-Regular", "SF Mono", "Menlo", "Monaco", "Consolas",
    "Liberation Mono", "DejaVu Sans Mono", "Courier New", monospace;
  --bg:#ffffff; --fg:#0d0d0d; --dim:#5d5d5d; --line:rgba(13,13,13,.10); --card:#f9f9f9;
  --accent:#0169cc; --green:#00a240; --violet:#8046d9; --orange:#e25507; }
@media (prefers-color-scheme: dark) { :root {
  --bg:#212121; --fg:#ffffff; --dim:#afafaf; --line:rgba(255,255,255,.12); --card:#181818;
  --accent:#0285ff; --green:#04b84c; --violet:#924ff7; --orange:#fb6a22; } }
* { box-sizing: border-box; }
body { margin:0; padding:0 16px 64px; background:var(--bg); color:var(--fg);
  font:14px/1.5 var(--font-sans); }
header { position:sticky; top:0; z-index:2; background:var(--bg); padding:16px 0 10px;
  border-bottom:1px solid var(--line); }
h1 { font-size:16px; margin:0 0 6px; }
h2 { font-size:14px; margin:28px 0 2px; }
.warn { margin:0 0 10px; color:var(--dim); max-width:80ch; }
.filters { display:flex; flex-wrap:wrap; gap:10px 14px; align-items:center; }
.filters label { cursor:pointer; user-select:none; }
#fold { margin-left:auto; }
input[type=search] { flex:1 1 200px; min-width:160px; max-width:320px; padding:5px 8px;
  border-radius:var(--radius-sm);
  border:1px solid var(--line); background:var(--card); color:inherit; font:inherit; }
button { cursor:pointer; padding:5px 10px; border-radius:var(--radius-sm);
  border:1px solid var(--line);
  background:var(--card); color:inherit; font:inherit; }
nav ol { margin:14px 0 0; padding-left:20px; color:var(--dim); }
nav a { color:inherit; }
.prompt { margin:2px 0 0; }
.src { color:var(--dim); font-size:12px; word-break:break-all; }
.ev { border-left:3px solid var(--line); background:var(--card);
  border-radius:0 var(--radius-sm) var(--radius-sm) 0;
  margin:6px 0; padding:6px 10px; }
.ev[data-kind=user] { border-left-color:var(--accent); }
.ev[data-kind=assistant] { border-left-color:var(--green); }
.ev[data-kind=thinking] { border-left-color:var(--violet); }
.ev[data-kind=tool_use] { border-left-color:var(--orange); }
.ev[data-kind=tool_result] { border-left-color:var(--dim); }
.hd { display:flex; flex-wrap:wrap; gap:8px; align-items:baseline; color:var(--dim); font-size:12px; }
.chip { font-weight:600; color:var(--fg); }
.tool { font-family:var(--font-mono); color:var(--fg); }
pre { margin:4px 0 0; white-space:pre-wrap; word-break:break-word; overflow-wrap:anywhere;
  font:12px/1.5 var(--font-mono); }
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
const folder = document.getElementById('fold');
function fold() {
  const folds = [...document.querySelectorAll('details')];
  const open = folds.some(d => !d.open);  // anything still closed means the click opens
  folds.forEach(d => { d.open = open; });
  label(!open);
}
function label(closed) { folder.textContent = closed ? 'expand all' : 'collapse all'; }
box.addEventListener('input', apply);
for (const b of boxes) b.addEventListener('change', apply);
folder.addEventListener('click', fold);
document.addEventListener('toggle', event => {
  if (event.target.tagName === 'DETAILS') {
    label([...document.querySelectorAll('details')].some(d => !d.open));
  }
}, true);  // toggle does not bubble, so listen on the way down
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


def document(items, limit, framed=False):
    """One page of sessions. Framed means the picker holds this page beside its list, which
    already carries the warning and the names, so the header keeps only what is missing."""
    title = html.escape(items[0]["prompt"][:80]) or "Session" if framed else "Keyhole session view"
    yield ("<!doctype html>\n<html lang=\"en\"><head><meta charset=\"utf-8\">\n"
           "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
           f"<title>Keyhole: {len(items)} sessions</title>\n<style>{STYLE}</style>\n"
           f"</head><body>\n<header><h1>{title}</h1>\n")
    if not framed:
        yield ('<p class="warn">Everything the agent read and wrote is on this page, including '
               "secrets that appeared in tool output. It is a local file: keep it local.</p>\n")
    yield ('<div class="filters">'
           '<input type="search" id="q" placeholder="filter text">')
    for kind in KINDS:
        checked = "" if kind in ("system", "meta") else " checked"
        yield (f'<label><input type="checkbox" value="{kind}"{checked}> {kind}</label>')
    yield ('<button id="fold">expand all</button>'
           '<span id="count"></span></div></header>\n')
    if not framed:
        yield "<nav><ol>"
        for number, item in enumerate(items):
            label = " &middot; ".join(html.escape(part) for part in
                                      (item["agent"], item["when"], item["prompt"][:70]) if part)
            yield f'<li><a href="#s{number}">{label}</a></li>'
        yield "</ol></nav>\n"
    yield "<main>\n"
    for number, item in enumerate(items):
        yield f'<section id="s{number}">\n'
        yield from session_html(item, limit)
        yield "</section>\n"
    yield f"</main>\n<script>{SCRIPT}</script>\n</body></html>\n"


def index_html(items):
    """The picker: the list stays on the left, the session opens beside it."""
    first = f'/s/{quote(items[0]["path"].name)}' if items else "about:blank"
    yield ("<!doctype html>\n<html lang=\"en\"><head><meta charset=\"utf-8\">\n"
           "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
           f"<title>Keyhole: {len(items)} sessions</title>\n<style>{STYLE}{PICKER}</style>\n"
           "</head><body>\n<aside>\n"
           f"<h1>{len(items)} sessions</h1>\n"
           '<input id="find" type="search" placeholder="filter sessions">\n')
    for position, item in enumerate(items):
        line = " &middot; ".join(html.escape(str(part)) for part in
                                 (item["agent"], item["when"], bytes_label(item["bytes"])) if part)
        yield (f'<a class="row{" on" if not position else ""}' 
               f'" href="/s/{quote(item["path"].name)}" target="view">'
               f'<b>{html.escape(item["prompt"][:120]) or "(no prompt)"}</b><span>{line}</span></a>\n')
    yield ("<p>Full transcript text, including secrets from tool output. "
           "This server listens on your machine only.</p>\n</aside>\n"
           f'<iframe name="view" src="{first}"></iframe>\n'
           f"<script>{PICK_SCRIPT}</script>\n</body></html>\n")


def route(path, items, limit):
    """Map a request to a page. Sessions are matched by name against the discovered set."""
    if path == "/":
        return 200, "".join(index_html(items))
    if path.startswith("/s/"):
        wanted = unquote(path[3:])
        for item in items:
            if item["path"].name == wanted:
                return 200, "".join(document([item], limit, framed=True))
        return 404, '<p>That session is no longer listed. <a href="/">Back</a></p>'
    return 404, '<p>Not found. <a href="/">Back</a></p>'


def serve(candidates, limit, port, launch):
    """Serve the picker on localhost until interrupted. Read-only, no uploads, no writes."""
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            status, body = route(self.path, candidates(), limit)
            payload = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            pass  # The sessions are the output; request lines are noise.

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    address = f"http://127.0.0.1:{server.server_port}/"
    print(f"{address}  (Ctrl-C to stop)")
    print("Local server, local sessions. Do not expose this port.")
    if launch:
        webbrowser.open(address)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        server.server_close()
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="*", type=Path)
    parser.add_argument("--agent", choices=["claude", "codex", "both"], default="both")
    parser.add_argument("--project", type=Path, default=Path.cwd())
    parser.add_argument("-n", type=int, default=None,
                        help="Sessions per agent, newest first (3, or 20 when serving)")
    parser.add_argument("--largest", action="store_true",
                        help="Rank by file size instead of recency, as the report does")
    parser.add_argument("--list", action="store_true", dest="listing",
                        help="Print the candidate sessions instead of building a page")
    parser.add_argument("--pick", action="store_true",
                        help="Show the candidates and open the one you answer with")
    parser.add_argument("--open", action="store_true", dest="launch",
                        help="Open the page in your browser when it is written")
    parser.add_argument("--serve", action="store_true",
                        help="Run a local picker that builds a session when you click it")
    parser.add_argument("--port", type=int, default=0, help="Port for --serve; 0 picks a free one")
    parser.add_argument("--max-chars", type=int, default=4000,
                        help="Per-event text limit; 0 keeps every character")
    parser.add_argument("--output", type=Path,
                        default=Path(tempfile.gettempdir()) / "keyhole-view.html")
    args = parser.parse_args()
    args.n = args.n if args.n is not None else (20 if args.serve else 3)
    if args.n < 1:
        parser.error("-n must be positive")
    if args.max_chars < 0:
        parser.error("--max-chars cannot be negative")
    root = project_root(args.project)
    order = "size" if args.largest else "time"

    def candidates():
        found = args.paths or discover(root, args.agent, args.n, order=order)
        return [describe(path) for path in found]

    if args.serve:
        if not candidates():
            parser.exit(2, "No matching transcripts. Pass explicit JSONL files or --project.\n")
        return serve(candidates, args.max_chars, args.port, args.launch)
    paths = args.paths or discover(root, args.agent, args.n, order=order)
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

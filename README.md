# keyhole

Context discipline for **Claude Code and Codex**. Narrow expensive tool output,
keep small task state outside the transcript, measure what actually entered the
conversation, and read a finished session back. Everything runs locally: no model calls or external service.

## Install / update

Claude Code:

~~~bash
claude plugin marketplace add stomashevsky/keyhole
claude plugin install keyhole@keyhole
# Later:
claude plugin marketplace update keyhole
claude plugin update keyhole@keyhole
~~~

Codex:

~~~bash
codex plugin marketplace add https://github.com/stomashevsky/keyhole.git
codex plugin add keyhole@keyhole
# Later:
codex plugin marketplace upgrade keyhole
codex plugin add keyhole@keyhole
~~~

Both manifests use the same skills, scripts and hooks. Review the two Keyhole
definitions in Codex CLI's /hooks menu after installation or an update. Installing
a plugin does not trust its hooks. Start a fresh session to load the new version.

## Measure first

Use Claude's /keyhole:report, or select Keyhole's report skill in Codex.
The script also works directly from this checkout:

~~~bash
python3 plugins/keyhole/tools/report.py --project /path/to/repo --agent both
python3 plugins/keyhole/tools/report.py --agent codex -n 5 --project /path/to/repo
python3 plugins/keyhole/tools/report.py /path/to/session.jsonl --json --output /tmp/keyhole-report.json
~~~

-n selects the largest N sessions **per agent**. Automatic discovery covers Claude
project JSONL files and Codex session JSONL files, filtering by project metadata.
Explicit file arguments are useful for exported or older transcripts.

The report separates:
- Estimated **text** tokens of tool results (chars/4), by tool.
- Image counts, never base64 bytes divided by four. It does not guess image pricing.
- Actual usage fields recorded by each host. Codex input includes cached input;
  the cached counter is a subset, not an additional cost. Claude fields retain
  their original meanings. Totals from the two hosts are not merged.
- Calls the current guard would narrow, using the guard's actual policy.
- Truncated results, malformed lines, unmatched outputs and opaque code-mode calls.

Reported usage includes repeated context input; tool-result size does not.
Would-narrow counts are **not predicted savings**: the follow-up read and the
rejected round trip also cost tokens. File checks replay against current files,
not historical snapshots. Code-mode calls are attributed to the outer wrapper;
the report does not pretend to know which nested call produced each output.
When the journal includes native Codex command-completion events, a separate
diagnostic table groups their output by executable. Those sizes overlap wrapper
results and are never added to the model-visible total; arguments are not exposed.
Partial active JSONL files are tolerated and reported. Transcript formats are
not a stable API; unsupported records cannot be measured as tool calls.

Save a baseline, then compare comparable completed tasks after enabling the guard.
Do not claim a percentage improvement from a syntactic match count.

## Read a session

In plain words: Claude and Codex keep a diary of every session. What you asked, what
they thought, every command they ran and everything it returned. They write it as files
meant for machines, which nobody can read. This turns those sessions into pages you can read:

~~~bash
python3 plugins/keyhole/tools/view.py --serve --open --project /path/to/repo
~~~

That opens a small page served from your own machine: the sessions for that repository
are listed down the left, and clicking one shows it beside the list. The list stays where
it is, so you can move between sessions without scrolling back to anything. A session is
built when you ask for it, not in advance, so a large one costs nothing until you open it.
Stop the server with Ctrl-C when you are done.

If you will do this more than once, put a function in your shell profile and it becomes
one word in any repository:

~~~zsh
kh() { python3 /path/to/keyhole/plugins/keyhole/tools/view.py --serve --open --project "${1:-$PWD}"; }
~~~

Prefer a file to a server? `--pick` lists the sessions, waits for a number, and writes
that one out as a single HTML file you can keep or move.

Open it in a real browser. An editor or chat preview shows the file as a snapshot, so
the text is there but the buttons do nothing. On the page, the checkboxes at the top
hide the kinds of events you are not after, the box beside them filters by text and
counts what is left, and long output stays folded until you click it. Hook output, MCP
status and compaction notices are there under `system`, unchecked at first so a session
opens on what you said rather than on its own plumbing.

The report says what a session cost. This says what happened in it. `view.py` turns
Claude and Codex journals into one offline HTML page: prompts, thinking, tool calls
and their output in order, for either host.

~~~bash
python3 plugins/keyhole/tools/view.py --project /path/to/repo --list
python3 plugins/keyhole/tools/view.py --project /path/to/repo --agent both
python3 plugins/keyhole/tools/view.py /path/to/session.jsonl --max-chars 0
~~~

`--project` decides which sessions count: those whose working directory sits under that
path, in either host's journals. `-n` takes that many per agent, newest first (twenty when
serving); `--largest` ranks by size the way the report does; `--list` prints the candidates
instead of building a page; `--pick` prints them and opens the one you answer with;
`--open` launches the result in your browser. A session is named by the first thing you
typed in it, or by the slash command you ran, in the listing and on the page.

`--serve` runs the picker instead of writing a file: a read-only server bound to
127.0.0.1, on a free port unless `--port` says otherwise. It re-reads the journals on
every load, so a session you are running right now appears in the list and grows as you
reload it. It serves only the sessions it discovered, by name, and writes nothing.

The page has checkboxes per event kind, a text filter with a match count, folded long
bodies and a dark theme. It is a single file with no scripts from anywhere else and no
network use. Without `--output` it is written to the system temporary directory and the
path is printed.

It also shows what the interface does not: Claude hook output, MCP server status and
compaction notices, and the Codex developer messages that carry sandbox permissions.
Images are a marker, never base64. `--max-chars` clips each event at 4000 characters by
default, because an 80 MB journal renders into a page no browser enjoys.

Every record is either an event or counted in the closing "records not shown" line.
This is the local journal, not the wire: the system prompt, the tool definitions and
the cache points are not in these files.

**The page contains the full text of everything the agent read, including secrets that
reached tool output.** It stays on your machine: do not upload, attach or commit it.

## What the guard checks

| Call | Narrower form |
|---|---|
| Unbounded Read of a file over 500 lines | offset/limit; explicitly name a whole-file limit when needed |
| grep/rg without a limiter | pipe to head, or use -m, -c, -l, -q |
| cat/less/more of more than 500 lines total | sed -n 'START,ENDp' or head |
| Claude screenshot with no scale | scale=0.5; scale=1 for chosen full detail |
| Multiple frames in a browser batch | One frame; inspect intermediate DOM/accessibility |
| Direct CUA .screenshot() with no frame choice | .screenshot({clip: ...}) or .screenshot({fullPage: false}) |

Codex canonical Bash and native exec_command/cmd events are recognized.
An explicit positive max_output_tokens budget also counts as a bounded native
Codex shell call when that field is present in the hook input. Hosts that normalize
it away still use the command's own limiter.
CUA's screenshot options are clip and fullPage, **not scale**. Named option
variables also count as an explicit choice. Plain string/comment examples of
.screenshot() do not trigger the CUA rule.

The guard is a sieve, not a security boundary. It does not understand dynamic
JavaScript calls, aliases, loop counts, arbitrary shell programs, or opaque
code-mode wrappers. Shell inspection is limited to the final pipeline stage;
heredocs are left alone. Unknown/malformed inputs and internal failures allow
the operation. Missing Python also leaves the host usable.

A refusal asks for a deliberate narrower call. Whole files and full frames remain
available when explicitly chosen. The host's own safety/permission rules always
apply independently.

## Shared task state

The state skill keeps five short fields: Goal, Step, Decisions, Files, Open.
Use the session path shown by SessionStart, or locate it explicitly:

~~~bash
python3 plugins/keyhole/tools/state.py path --session SESSION_ID --project /path/to/repo
python3 plugins/keyhole/tools/state.py path --task named-handoff --project /path/to/repo
~~~

The default is .agents/keyhole/state/<session-or-task>.md. The command only returns
a path; the agent writes the state with its normal file editor. Hooks never create
state, overwrite decisions, or start work.

SessionStart loads, in order:
1. This session's state, when present.
2. Explicit state_files from project configuration.
3. A deliberate shared .agents/keyhole/CURRENT.md handoff.
4. Legacy .claude/state/CURRENT.md, for existing Claude users.

A new session otherwise gets a short index of up to five saved tasks, **not another
session's working state**. Read only the entry that matches the user's request.
For an explicit transfer to another agent, name the shared task file in the request.
An opt-in CURRENT.md is suitable only for one coordinated active task.

Keep real task plans and statuses in the project's existing canonical documents;
the state file carries pointers and the immediate next step, not a duplicate plan.
Choose whether to version state or ignore it according to project policy. Do not
put credentials in it. State is bounded to 4,000 characters at injection.

## Configuration

Sources, later winning:
1. Shipped keyhole.default.json.
2. <git-root>/.claude/keyhole.json (legacy).
3. <git-root>/.agents/keyhole.json (shared).
4. KEYHOLE_READ_MAX_LINES, KEYHOLE_BASH_MAX_LINES, KEYHOLE_MAX_SCREENSHOTS.

The git root is found from the hook's cwd, including subdirectory sessions.
KEYHOLE=off disables Keyhole only; it does not affect host permissions.

~~~json
{
  "read_allow": ["**/CLAUDE.md", "**/AGENTS.md", "**/README.md", "**/docs/spec-*.md"],
  "read_max_lines": 500,
  "bash_max_lines": 500,
  "batch_max_screenshots": 1,
  "state_max_chars": 4000,
  "state_dir": ".agents/keyhole/state",
  "state_files": []
}
~~~

Arrays replace earlier arrays. state_files and state_dir must resolve inside the
project. Existing Claude project overrides remain supported.

## Verify

~~~bash
python3 plugins/keyhole/tests/guard.test.py
python3 -m unittest discover -s plugins/keyhole/tests -p 'test_*.py'
claude plugin validate ./plugins/keyhole
~~~

Tests cover the original 38 Claude contracts, Codex event shapes, shared config,
CUA frames, both transcript formats, cumulative usage, partial records, event
extraction, page escaping, state isolation and compatibility. They do not simulate a model or assert a savings rate.

## Background

Inspired by local PreToolUse enforcement in
[Spotify's shunt](https://github.com/spotify/portal-ai-plugins) and explicit state
in [SKILL.state](https://arxiv.org/abs/2608.26263). Results from those systems are
not measured outcomes for Keyhole.

Host interfaces: [Codex hooks](https://learn.chatgpt.com/docs/hooks),
[Claude hooks](https://code.claude.com/docs/en/hooks).

MIT licensed.

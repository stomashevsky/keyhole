# keyhole

A Claude Code plugin that keeps the context window honest. It refuses unbounded
reads, grep dumps and unscaled screenshots, and carries explicit task state
across `/clear`.

Nothing leaves your machine. No service, no account, no second model — the
plugin only refuses an expensive action and names the cheaper form of the same
thing.

## Install

```bash
claude plugin marketplace add stomashevsky/keyhole && claude plugin install keyhole@keyhole
```

Restart Claude Code. That is the whole setup — there is nothing to configure
before it works.

### Check it works

Ask Claude to `grep -rn something .` in any repository. You should see the call
refused with a line telling it to add `| head -20`. If you see that, you are
done.

## What it does

**Refuses a read with no shape.** `Read` with no `offset`/`limit` on a file over
500 lines, `grep`/`rg` with no limiter, `cat` of a large file. The refusal names
the cheaper form: a bounded read, `| head -20`, `-m 20`, `-c`.

**Refuses an unnamed screenshot.** `screenshot` with no `scale`, and more than
one frame in a single `browser_batch`. `scale: 0.5` is a quarter of the image
tokens; `zoom` reads fine detail at full resolution; `read_page` answers
questions about text for almost nothing.

**Carries task state.** The `state` skill maintains `.claude/state/CURRENT.md`
under a five-field schema, and a `SessionStart` hook feeds it back at the start
of every session — so `/clear` costs nothing and resuming needs no scrollback.

**Measures.** `/keyhole:report` shows where this project's context actually goes,
by tool and by command, read from your own session transcripts.

Every refusal is one parameter away from proceeding, and that is the design. The
guard cannot know whether you need the whole file or the full frame; it can make
sure that when you take one, you chose it.

## Where the idea comes from

**Enforcement rather than advice.** Spotify published
[`shunt`](https://github.com/spotify/portal-ai-plugins) in September 2026 after
routing rules written into `CLAUDE.md` were ignored by the model: *"The rules
were advisory, not enforced."* A `PreToolUse` hook was what held. keyhole borrows
that shape and only that. `shunt` forwards the blocked work to a cheaper model
through Spotify's Portal, so file contents leave the machine and the plugin does
nothing without a Portal instance. keyhole forwards nothing.

**Noise costs accuracy, not just money.**
[SKILL.state](https://arxiv.org/abs/2608.26263) measured an append-only runtime
falling from 0.68 to 0.53 task score as distractor events per turn rose from 5 to
50, while runtimes that keep distractors out of the next prompt held 0.97 or
better. It also measured what history does to cost: at the longest horizon
tested, 1,062,387 tokens for a history-carrying baseline against 65,408 for the
state-carrying one, at equal or better accuracy — and recovering the thread after
losing context took 5-8 steps against 0. The guard keeps the distractors out; the
`state` skill is the other half.

## Measure before you tune

```bash
/keyhole:report
```

Two passes over the same repository disagreed about which tool was the problem,
and the reason was the unit:

- **Count tokens, never bytes.** An image costs roughly its pixels / 750, not its
  base64 length / 4. Counting bytes overstated screenshots by about 2x and
  produced a confident, wrong conclusion about which rule mattered most.
- **The mix is per project and per phase.** On one visual UI project the three
  buckets — Bash, Read, browser frames — came out near 28% each, and the guard's
  rules covered about 30% of all tool output. A backend project will not look
  like that. Measure yours before changing a threshold.

## Configure

Nothing is required. When you do want to change something, later sources win:

1. `plugins/keyhole/keyhole.default.json` — shipped defaults
2. `<your project>/.claude/keyhole.json` — per project
3. Environment: `KEYHOLE=off`, `KEYHOLE_READ_MAX_LINES`, `KEYHOLE_BASH_MAX_LINES`,
   `KEYHOLE_MAX_SCREENSHOTS`

| Key | Default | Meaning |
|---|---|---|
| `read_max_lines` | 500 | Unbounded `Read` above this many lines is refused |
| `read_allow` | CLAUDE.md, AGENTS.md, README.md, state files | Globs always read whole |
| `bash_max_lines` | 500 | `cat`/`less`/`more` above this many lines is refused |
| `screenshot_tools` | browser + chrome `computer` | Tools whose `screenshot` needs a `scale` |
| `batch_tools` | `browser_batch` | Tools whose action list is inspected |
| `batch_max_screenshots` | 1 | Frames allowed in one batch |

The per-project layer is the point of the split. If your project's own
instructions require reading a particular document whole, name it there:

```json
{
  "read_allow": ["**/CLAUDE.md", "**/AGENTS.md", "**/docs/spec-*.md"]
}
```

## Known limits, stated plainly

- **It is a sieve, not a wall.** `limit: 100000` and `scale: 1` pass by design. As
  a security control this is nothing; as a discipline it works.
- **It costs round trips.** A refusal spends one exchange. Run `/keyhole:report`
  on your own transcripts and loosen the thresholds if the measured saving does
  not pay for that.
- **The bash check reads only the last stage of each segment.** `sed`, `awk` and
  `python -c` are not inspected. Deliberate: over-blocking is worse than
  under-blocking, because a guard that fires wrongly gets switched off and then
  protects nothing.
- **Needs `python3` on PATH.** Without it the hook exits quietly and allows
  everything, so nothing breaks — the guard simply does not run.

## Develop

```bash
python3 plugins/keyhole/tests/guard.test.py   # 38 contract cases
claude plugin validate ./plugins/keyhole
claude --plugin-dir ./plugins/keyhole         # load a working copy
```

Half the test cases assert that the guard stays quiet: `npm`, `git`, `sed -n`,
heredocs, `zoom`, redirects, unrelated tools, malformed input.

MIT licensed.

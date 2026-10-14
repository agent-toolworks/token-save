# The statusline, and its history

token-save ships a complete Claude Code statusline. It also keeps a local
history of what that line showed, one row per change for each session, so you
can look back at usage once the session and its transcript are gone.

```
~/git/…/repo main | Opus 5.5 (high) | ctx 42k/200k 21% | 423.0K +530/t clear>1t | $1.23 | 5h 23% used(2h14m) · 7d 9% used(3d11h)
```

| Segment | What it is | Source |
|---|---|---|
| `~/git/…/repo main` | current directory (middle shortened past 28 characters) and git branch | payload, `git` |
| `Opus 5.5 (high)` | model, coloured by tier, and reasoning effort | payload |
| `ctx 42k/200k 21%` | context used out of the window; green, then yellow from 50%, red from 80% | payload |
| `423.0K +530/t clear>1t` | the `ts now` reading: context, growth per turn, and how soon clearing would pay for itself (shown only when it is soon) | transcript |
| `$1.23` | session cost so far, **as Claude Code estimates it** | payload |
| `5h 23% used(2h14m)` · `7d …` | how much of the 5-hour and 7-day quota is used, and when each resets | payload |

## Why record it

Some of these figures exist only in the JSON Claude Code passes to the
statusline: the quota and its reset times, Claude Code's own cost figure, the
context percentage and the effort level. They are not in the transcripts, and
transcripts are deleted after `cleanupPeriodDays` (30 by default) anyway.
Without a record, they are shown and then lost. `ts audit` answers "where did
the tokens go?" for the transcripts still on disk. The history is what will let
you ask how that changed over weeks.

## Install

You need Claude Code, `bash` and `python3` (3.9+). There is no `jq` and no
other dependency.

**1. Install the plugin** (skip this if you already have it):

```
/plugin marketplace add agent-toolworks/plugins
/plugin install token-save
```

**2. Copy the wrapper out of the plugin, once:**

```sh
cp "$(ls -dt ~/.claude/plugins/cache/*/token-save/*/scripts/statusline.sh | head -1)" ~/.claude/statusline.sh
chmod +x ~/.claude/statusline.sh
```

The copy does not go stale when the plugin updates. All it does is find the
newest installed `ts` and pass it the session JSON, so the line and the history
always come from the version you have installed. Plugin cache directories are
versioned and old ones are left behind after an update, which is why
`settings.json` should point at the copy and not into the cache.

If you already have a `~/.claude/statusline.sh`, back it up first or choose
another name for the copy.

**3. Point Claude Code at it** in `~/.claude/settings.json`:

```json
{
  "statusLine": {
    "type": "command",
    "command": "~/.claude/statusline.sh",
    "padding": 0
  }
}
```

**4. Check it.** The line appears from the next render, with no restart
needed. To see what it read and what it would record, without writing
anything:

```sh
echo '{"session_id":"x","transcript_path":"/path/to/session.jsonl","model":{"display_name":"Opus 5.5"}}' \
  | ~/.claude/statusline.sh --explain
```

It names the `ts` it found (or says none was found), prints the line, shows the
state of the history database, and lists every column of the row it would
write. The line never prints an error, so `--explain` is how you find out why
it is empty or missing a segment.

### Keeping a statusline of your own

If you would rather keep your own line, `scripts/statusline-snippet.sh` adds
only the `ts now` segment to it (see the README, *In your statusline*). That
route records no history. To record from your own script as well, pipe the
same JSON to `ts statusline >/dev/null`, which writes the row and prints the
line, and keep your own output.

## The history

**Where it is:** `~/.claude/token-save/history.db` (under `$CLAUDE_CONFIG_DIR`
if you set it). It is a SQLite file, created on the first render, readable only
by you (`0600`, in a `0700` directory).

**What a row holds:** one table, `snapshots`, with one row each time something
on the line changes for a session.

| Column | |
|---|---|
| `recorded_at` | UTC, ISO 8601 |
| `session_id`, `transcript_path` | which session |
| `claude_version` | Claude Code version |
| `cwd`, `project_dir`, `git_branch` | where |
| `model_id`, `model_name`, `effort` | model and reasoning effort |
| `ctx_tokens`, `ctx_size`, `ctx_pct` | context, as Claude Code reports it |
| `ts_context`, `ctx_growth_per_turn`, `clear_breakeven_turns` | the `ts now` reading (`NULL` break-even: clearing would not pay) |
| `cc_cost_usd` | session cost so far: Claude Code's estimate, stored as it was reported |
| `duration_ms`, `api_duration_ms` | wall time and API time so far |
| `lines_added`, `lines_removed` | lines changed in the session so far |
| `five_hour_pct`, `five_hour_resets_at` | 5-hour quota used, and its reset (epoch seconds) |
| `seven_day_pct`, `seven_day_resets_at` | the same for 7 days |
| `ts_version` | the token-save version that wrote the row |

The per-session values (cost, durations, lines) are **running totals**, so a
session's figure is its last row, not the sum of its rows. Wall time moves on
every render without anything else happening, so it does not count as a change
on its own. The `duration_ms` you see is from the first render of each state.

**What it does not hold:** message content of any kind. Nothing reads the
database except `ts statusline --explain`, nothing sends it anywhere, and
`ts share` does not include it.

### Querying it

```sh
sqlite3 -header -column ~/.claude/token-save/history.db "
  SELECT date(recorded_at) AS day,
         count(DISTINCT session_id) AS sessions,
         printf('%.2f', sum(cost)) AS cc_cost_usd,
         max(peak_5h) AS peak_5h_pct
  FROM (SELECT session_id, min(recorded_at) AS recorded_at,
               max(cc_cost_usd) AS cost, max(five_hour_pct) AS peak_5h
        FROM snapshots GROUP BY session_id)
  GROUP BY day ORDER BY day DESC LIMIT 14"
```

This puts each session on the day it started, takes its largest cost figure
(the running total at its last render), and shows each day's peak 5-hour quota.

### Turning it off, or moving it

These are environment variables. Set them in the `env` block of
`settings.json`, which applies them to the statusline command:

```json
{ "env": { "TS_HISTORY": "off" } }
```

| Variable | Effect |
|---|---|
| `TS_HISTORY=off` | show the line, record nothing |
| `TS_HISTORY_DB=/path/to/file.db` | record somewhere else |
| `NO_COLOR=1` | a plain line |
| `TS_BIN=/path/to/scripts/ts` | use this `ts` instead of the newest installed one |

To delete the history, delete the file. The next render starts a new one.

## What it costs

Each render is one `python3` start-up, a `git rev-parse`, a read of the usage
records in the transcript, and at most one small SQLite write. That takes
about 0.4s on a long session. Each step has its own time limit (stdin 0.5s,
git 0.5s, transcript 1.2s, database 0.2s), and the wrapper caps the whole run
at 3s. A step that runs out of time loses its own segment or its row, never
the line. The line is printed before the row is written, so a locked or broken
database cannot cost you the line.

Two sessions rendering at once share the file safely (SQLite in WAL mode).

## Not recorded

- **Non-interactive sessions** (`claude -p`, SDK runs). They never render a
  statusline, so nothing reaches this.
- **Statistics.** There is no `ts history` command yet. The data is collected
  now so that one can be written later and still have the past to work from.

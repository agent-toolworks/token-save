# The statusline, and its history

token-save ships a complete Claude Code statusline. It also keeps a local
history of what that line showed, one row per change for each session, so you
can look back at usage once the session and its transcript are gone.

```
~/git/…/repo main | 1a2b3c4d | Opus 5.5 (high) ✻ | ctx 43% free 113.6k | cache 97% msgs 4.2k warm 48m | 5h 23% used(2h14m) · 7d ~9% used(3d11h)
```

| Segment | What it is | Source |
|---|---|---|
| `~/git/…/repo main` | current directory (middle shortened past 28 characters) and git branch | payload, `git` |
| `1a2b3c4d` | the session id up to its first `-`: which window is which, and the key to its rows in the history | payload |
| `Opus 5.5 (high) ✻` | model, coloured by tier, reasoning effort, and `✻` when extended thinking is on | payload |
| `ctx 43% free 113.6k` | context used (green, then yellow from 50%, red from 80%) and the tokens left (yellow below 25% free, red below 10%) | payload |
| `cache 97%` | the last request's cache hit rate: cache reads over all its input. Green from 70%, yellow from 30%, red below. A sudden drop is a busted prefix | payload |
| `msgs 4.2k` | the last request's uncached input | payload |
| `warm 48m` / `COLD` | how long the session can sit idle before its cached prefix expires and the next turn pays a cache write. It comes from the payload's `prompt_cache` (`expires_at`, `ttl`, `warm`) when Claude Code sends it. Older versions do not, and then the TTL (1h or 5m) is read from the newest cache write in the transcript, not assumed, and the countdown runs from the transcript's age, hidden for the first minute idle (mid-turn). Yellow in the last quarter of the TTL | payload, or transcript |
| `5h 23% used(2h14m)` · `7d ~9% used(3d11h)` | how much of the 5-hour and 7-day quota is used (`FULL` at 100%), and when each resets. A dim `~` means the reading is another session's, because it is fresher than this one's (below) | payload, `limits.json` |

**Why the quota can be another session's.** `rate_limits` in the payload comes
from the session's own last API response, and nothing refreshes it while the
session sits idle. The quota belongs to the account, so the line keeps the
freshest reading any session has observed (`ts limits` prints it), and shows
that one when it is newer. "Newer" means observed more recently, not higher:
the 7d counter has been reset in place, and a highest-wins rule would hold the
old figure for days.

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

**2. Find the installed copy.** A plugin install lives in a versioned cache
directory, so this picks the newest:

```sh
TS_ROOT=$(ls -d ~/.claude/plugins/cache/*/token-save/*/ | sort -V | tail -1)
```

**3. See what it would change, then install it:**

```sh
"$TS_ROOT/scripts/ts" fixes show statusline           # the diff; changes nothing
"$TS_ROOT/scripts/ts" fixes apply statusline --yes    # install
```

This copies the wrapper to `~/.claude/token-save/statusline.sh` and sets
`statusLine` in `~/.claude/settings.json` to run it, with
`"refreshInterval": 60`. Claude Code runs a statusline only on events (a new
assistant message, session start, `/compact`, a mode change), debounced by
300 ms, so without the interval an idle session's warmth countdown and quota
never move. A `refreshInterval` you have already set is left alone. Nothing else in the file
changes, and a timestamped backup is written next to it. If you already had a
statusline, its setting is kept in `~/.claude/token-save/statusline.previous.json`
and

```sh
"$TS_ROOT/scripts/ts" fixes revert statusline
```

puts it back exactly as it was, and removes the wrapper. The history is kept.
Revert does nothing if you have pointed `statusLine` somewhere else since.

The wrapper does not go stale when the plugin updates. All it does is find the
newest installed `ts` and pass it the session JSON, so the line and the history
always come from the version you have installed. Plugin cache directories are
versioned and old ones are left behind after an update, which is why
`settings.json` points at the copy and not into the cache. Running `apply`
again after an update refreshes the copy if the wrapper itself changed, and
`ts fixes list` shows whether it is current.

You can also ask Claude Code to do it ("install the token-save statusline").
The skill shows you the diff and asks before applying it.

**4. Check it.** The line appears from the next render, with no restart
needed. To see what it read and what it would record, without writing
anything:

```sh
echo '{"session_id":"x","transcript_path":"/path/to/session.jsonl","model":{"display_name":"Opus 5.5"}}' \
  | ~/.claude/token-save/statusline.sh --explain
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
on the line changes for a session: where (directory, branch), which model and
effort, context, Claude Code's cost figure, durations, lines changed, and the
5h and 7d quota with their reset times.

**The schema is a contract.** [history-schema.md](history-schema.md) is
written for tools that read the file, such as a dashboard of usage over time.
It covers every column, how to open the file safely while sessions write to
it, and what can change between versions (columns are only ever added).
`ts statusline --schema` prints the same as JSON.

The per-session values (cost, durations, lines) are **running totals**, so a
session's figure is its last row, not the sum of its rows. Wall time moves on
every render without anything else happening, so it does not count as a change
on its own. The `duration_ms` you see is from the first render of each state.

**What it does not hold:** message content of any kind. Within token-save,
nothing reads the database except `ts statusline --explain`, nothing sends it
anywhere, and `ts share` does not include it.

### Querying it

```sh
sqlite3 -header -column "file:$HOME/.claude/token-save/history.db?mode=ro" "
  WITH ranked AS (
    SELECT session_id, cc_cost_usd,
           row_number() OVER (PARTITION BY session_id
                              ORDER BY recorded_at DESC, id DESC) AS n,
           min(recorded_at) OVER (PARTITION BY session_id) AS started,
           max(CASE WHEN rate_limits_observed_at IS NOT NULL THEN five_hour_pct END)
             OVER (PARTITION BY session_id) AS peak_5h
    FROM snapshots)
  SELECT date(started) AS day,
         count(*) AS sessions,
         printf('%.2f', sum(cc_cost_usd)) AS cc_cost_usd,
         max(peak_5h) AS peak_5h_pct
  FROM ranked WHERE n = 1
  GROUP BY day ORDER BY day DESC LIMIT 14"
```

This puts each session on the day it started, takes the cost from its latest
row (the running total at its last render), and shows each day's peak observed
5-hour quota. [history-schema.md](history-schema.md#queries) has more, and the
rules for reading the quota columns.

### Bringing in an older history

If you kept statusline payloads with another recorder, `ts history import`
loads them, one `{"recorded_at": ..., "payload": {...}}` per line:

```sh
"$TS_ROOT/scripts/ts" history import old.jsonl --dry-run    # counts only
"$TS_ROOT/scripts/ts" history import old.jsonl
```

It can be re-run safely, any UTC offset is accepted, and imported rows are
marked `source = 'import'`. See
[history-schema.md](history-schema.md#importing-an-older-history).

### Turning it off, or moving it

These are environment variables. Set them in the `env` block of
`settings.json`, which applies them to the statusline command:

```json
{ "env": { "TS_HISTORY": "off" } }
```

| Variable | Effect |
|---|---|
| `TS_HISTORY=off` | show the line, record nothing: no history, no `limits.json`, no cache |
| `TS_HISTORY_DB=/path/to/file.db` | record somewhere else |
| `NO_COLOR=1` | a plain line |
| `TS_BIN=/path/to/scripts/ts` | use this `ts` instead of the newest installed one |

To delete the history, delete the file. The next render starts a new one.

## What it costs

Each render is one `python3` start-up, a `git rev-parse`, a read of the usage
records in the transcript, and at most one small SQLite write. That takes
about 0.4s on a long session the first time. The transcript reading is then
cached in `~/.claude/token-save/cache/`, against the transcript's size and
modification time, so a render that finds the transcript unchanged, as an idle
session re-rendering on its `refreshInterval` does, takes well under 0.1s. Each step has its own time limit (stdin 0.5s,
git 0.5s, transcript 1.2s, database 0.2s), and the wrapper caps the whole run
at 3s. A step that runs out of time loses its own segment or its row, never
the line. The line is printed before the row is written, so a locked or broken
database cannot cost you the line.

Two sessions rendering at once share the file safely (SQLite in WAL mode).

## Not recorded

- **Non-interactive sessions** (`claude -p`, SDK runs). They never render a
  statusline, so nothing reaches this.
- **Statistics.** `ts history` only imports so far. The data is collected now
  so that reports can be written later and still have the past to work from.

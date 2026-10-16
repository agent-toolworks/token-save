# The history database, for tools that read it

The token-save statusline records what it shows into a local SQLite file, one
row per change for each session ([statusline.md](statusline.md) covers
installing it). This page is the contract for anything else that reads that
file, such as a dashboard, a report or a script: where the file is, how to open
it without getting in the way of the writer, what each column means, and what
will and will not change between versions. It also covers `limits.json`, the
freshest quota reading across sessions, for readers that only want "now".

`ts statusline --schema` prints the table as JSON, built from the column list
the writer itself uses:

```sh
"$TS_ROOT/scripts/ts" statusline --schema    # TS_ROOT: see statusline.md, Install
```

```json
{
  "tool_version": "0.25.0",
  "schema_version": 2,
  "database": "/Users/you/.claude/token-save/history.db",
  "table": "snapshots",
  "columns": [
    {"name": "id", "type": "INTEGER", "description": "row id, ..."},
    ...
  ]
}
```

`database` is `null` when recording is turned off (`TS_HISTORY=off`).

## Finding the file

The first of these that applies:

1. `$TS_HISTORY_DB`, if it is set
2. `$CLAUDE_CONFIG_DIR/token-save/history.db`, if `CLAUDE_CONFIG_DIR` is set
3. `~/.claude/token-save/history.db`

Settings in the `env` block of `settings.json` apply to the statusline but not
to your tool, so `ts statusline --schema` is the reliable way to ask. It is
run in your environment, so set the same variables when you run it. The file
exists only after the first render, so a missing file means "nothing recorded
yet", not an error.

`limits.json` is in the same directory. So is `cache/`, which holds the
statusline's own working state and is not part of this contract.

## Opening it

- **Open it read-only**, as a URI with `mode=ro`:
  `sqlite3.connect("file:%s?mode=ro" % path, uri=True)`. Never write to it.
  Writing includes `VACUUM`, `CREATE INDEX` and changing `journal_mode`.
  Copy it if you need to change anything.
- **Expect it to be live.** Every running session writes to it. The file is
  in WAL mode, so a reader does not block the writer and sees a consistent
  snapshot. Keep read transactions short, and set a busy timeout (a second
  is plenty) for the rare moment a writer is checkpointing.
- **Expect the `-wal` and `-shm` files next to it.** They are part of the
  database. A copy made with `cp history.db` alone can miss recent rows; use
  `sqlite3 history.db ".backup copy.db"` instead.
- **It is private.** It holds directory paths, branch and session names
  (never message content), and is created `0600` in a `0700` directory. A
  tool that sends any of it elsewhere should say so and ask first.

## Versioning

`PRAGMA user_version` is the schema version. It is `2` today.

- **Columns are only ever added.** A column is never renamed, removed, re-typed
  or given a new meaning. When columns are added, `user_version` goes up, and
  the next render adds them to an existing table. Rows written before that hold
  `NULL` in the new columns.
- **Select columns by name.** Do not use `SELECT *` with positions. Columns
  added later go at the end, but the promise is about names.
- **A higher version than you know about is fine.** Every column you know is
  still there and still means the same. Ignore the rest.
- **The table is `snapshots`.** Its indexes are there to help queries, and are
  not part of the contract.

| Version | Added |
|---|---|
| 1 | the first 26 columns, `recorded_at` to `ts_version` |
| 2 | every remaining payload field, `rate_limits_observed_at`, `source`, and the drift canary (`unknown_keys`, `raw_payload`) |

## What a row is

A row is written when anything recorded changes for a session: model,
context, the last request's usage, cost, quota and so on. A plain re-render
writes nothing, and nor does the wall clock moving on its own (see
`duration_ms`). That gives four rules for reading it:

- **Order rows by `recorded_at`, not `id`.** Imported rows (`source =
  'import'`) can be older than live rows written before them, so a
  session's latest row is the one with the greatest `recorded_at`, with `id`
  breaking a tie.
- **Session figures are running totals.** `cc_cost_usd`, `duration_ms`,
  `api_duration_ms`, `lines_added` and `lines_removed` count from the start of
  the session. A session's total is its **latest** row, never the sum of its
  rows.
- **A quota reading is only as fresh as `rate_limits_observed_at`.**
  `five_hour_pct` and the other quota columns are what *this* session's last
  API response said, and Claude Code does not refresh them while a session
  sits idle. An idle session whose cost or lines change still writes its old
  reading. `rate_limits_observed_at` is when this session last saw the reading
  *change*, which only a fresh response can do. It is `NULL` until the first
  change, because the first reading a session shows may be an old one. For
  quota over time, use rows where it is set, and treat the reading as true as
  of that time. The quota is the account's, so do not add sessions together.
- **`NULL` means "not reported".** Claude Code's payload varies by version and
  account. A column it did not send is `NULL`, never `0`. `clear_breakeven_turns`
  is also `NULL` when clearing would never pay for itself.

**Drift.** When the payload has a field this version has no column for, its
dotted path goes into `unknown_keys`, and that row alone also carries the
whole payload in `raw_payload`. A Claude Code upgrade that adds a field shows
up there without the raw JSON being stored on every row.

## Columns

| Column | Type | Meaning |
|---|---|---|
| `id` | INTEGER | row id, increasing in the order rows were written |
| `recorded_at` | TEXT | when the row was written, or the time an imported row was recorded: UTC, ISO 8601, `2026-10-16T18:40:53Z`. Sorts as text |
| `session_id` | TEXT | Claude Code session id. Rows for one session share it |
| `transcript_path` | TEXT | the session's transcript. It may since have been deleted (`cleanupPeriodDays`) |
| `claude_version` | TEXT | Claude Code version |
| `cwd` | TEXT | the directory the line shows: `workspace_current_dir`, or `payload_cwd` without it |
| `project_dir` | TEXT | directory the session was started in |
| `git_branch` | TEXT | branch checked out in `cwd`; `HEAD` when detached, `NULL` outside a repository and on imported rows |
| `model_id` | TEXT | model id, e.g. `claude-opus-5-5` |
| `model_name` | TEXT | model display name, e.g. `Opus 5.5` |
| `effort` | TEXT | reasoning effort level: `low`, `medium`, `high`, ... |
| `ctx_tokens` | INTEGER | `ctx_input_tokens + ctx_output_tokens` |
| `ctx_size` | INTEGER | context window size, in tokens |
| `ctx_pct` | REAL | context window used, 0 to 100 |
| `ts_context` | INTEGER | tokens in context, read from the transcript by `ts now`; `NULL` on imported rows |
| `ctx_growth_per_turn` | REAL | tokens added to context per turn, recently (`ts now`) |
| `clear_breakeven_turns` | REAL | turns until clearing the session pays for itself (`ts now`); `NULL` if never |
| `cc_cost_usd` | REAL | session cost so far in US dollars. This is **Claude Code's estimate**, as it reported it, not a bill |
| `duration_ms` | INTEGER | session wall time so far, in milliseconds, as of the first render of this state |
| `api_duration_ms` | INTEGER | time spent waiting on the API so far, in milliseconds |
| `lines_added` | INTEGER | lines added in the session so far |
| `lines_removed` | INTEGER | lines removed in the session so far |
| `five_hour_pct` | REAL | 5-hour quota used, 0 to 100, as this session last heard it |
| `five_hour_resets_at` | INTEGER | when the 5-hour quota resets, in epoch seconds |
| `seven_day_pct` | REAL | 7-day quota used, 0 to 100, as this session last heard it |
| `seven_day_resets_at` | INTEGER | when the 7-day quota resets, in epoch seconds |
| `ts_version` | TEXT | token-save version that wrote the row |
| `session_name` | TEXT | the session's name, when it has one |
| `prompt_id` | TEXT | id of the prompt being answered |
| `workspace_current_dir` | TEXT | `workspace.current_dir`, as reported |
| `payload_cwd` | TEXT | the top-level `cwd`, as reported. It can differ from `workspace_current_dir` once the session has changed directory |
| `added_dirs` | TEXT | `workspace.added_dirs`, as a JSON array |
| `thinking_enabled` | INTEGER | `1` when extended thinking is on, `0` when off |
| `fast_mode` | INTEGER | `1` when fast mode is on, `0` when off |
| `output_style` | TEXT | output style name |
| `ctx_remaining_pct` | REAL | context window left, 0 to 100 |
| `ctx_input_tokens` | INTEGER | `context_window.total_input_tokens` |
| `ctx_output_tokens` | INTEGER | `context_window.total_output_tokens` |
| `last_input_tokens` | INTEGER | uncached input tokens of the last request |
| `last_output_tokens` | INTEGER | output tokens of the last request |
| `last_cache_creation_tokens` | INTEGER | tokens the last request wrote to the prompt cache |
| `last_cache_read_tokens` | INTEGER | tokens the last request read from the prompt cache |
| `exceeds_200k_tokens` | INTEGER | `1` when the context is over 200k tokens |
| `rate_limits_observed_at` | TEXT | UTC, ISO 8601: when this session last saw its quota reading change; `NULL` if never (see above) |
| `source` | TEXT | what wrote the row: `statusline`, or `import` for `ts history import` |
| `unknown_keys` | TEXT | JSON array of payload fields this version has no column for; `NULL` when there are none |
| `raw_payload` | TEXT | the whole payload as JSON, only on rows where `unknown_keys` is set |

`scripts/verify` checks that this table names the same columns, with the same
types and in the same order, as the writer, so the two cannot drift apart.

## The current quota: limits.json

For "the newest 5h and 7d reading right now", read `limits.json` instead of
querying the history. It holds, per window, the newest reading any session
has *observed* (in the `rate_limits_observed_at` sense), and which session
saw it:

```json
{
  "version": 1,
  "updated_at": "2026-10-16T18:40:53Z",
  "five_hour": {"used_percentage": 23.4, "resets_at": 1792195333,
                "observed_at": "2026-10-16T18:40:53Z", "session_id": "1a2b3c4d-..."},
  "seven_day": {"used_percentage": 9, "resets_at": 1792281733,
                "observed_at": "2026-10-16T18:31:02Z", "session_id": "..."}
}
```

- It is replaced atomically, so a reader never sees half a file. A window
  that has never been observed is absent.
- "Newest" is by observation time, not by value. A drop is believed: the 7d
  counter has been reset in place, with `resets_at` unchanged.
- A window whose `resets_at` has passed is over, and its percentage says
  nothing about the current one.
- `ts limits --json` prints the same, with the file's path. `ts limits`
  prints it for a person.

The statusline shows this reading, with a dim `~`, when it is fresher than the
session's own. It never writes a borrowed reading back, to the history or here.

## Importing an older history

`ts history import FILE` loads JSONL in which each line is a statusline
payload with the time it was recorded:

```json
{"recorded_at": "2026-08-14T09:12:03+03:00", "payload": {"session_id": "...", "model": {...}, ...}}
```

Each line goes through the same row building and dedupe as a live render, so
an importer needs no column mapping. A recorder that kept the payloads can
export to this shape. `recorded_at` may have any UTC offset and is stored in
UTC. A time with no offset is refused, not guessed. Imported rows have
`source = 'import'`, and the `ts now` columns and `git_branch` are `NULL`. The
import is safe to re-run: a row whose `(session_id, recorded_at)` is already
there is skipped. Times are kept to the second, so two lines for one session
within the same second keep the first. `--dry-run` reports the counts and
writes nothing.

## Queries

The latest row of each session holds its totals:

```sql
SELECT * FROM (
  SELECT s.*, row_number() OVER (PARTITION BY session_id
                                 ORDER BY recorded_at DESC, id DESC) AS n
  FROM snapshots s)
WHERE n = 1;
```

Cost and sessions per day, with each session counted on the day it started:

```sql
WITH ranked AS (
  SELECT session_id, recorded_at, cc_cost_usd,
         row_number() OVER (PARTITION BY session_id ORDER BY recorded_at DESC, id DESC) AS n,
         min(recorded_at) OVER (PARTITION BY session_id) AS started
  FROM snapshots)
SELECT date(started) AS day, count(*) AS sessions, round(sum(cc_cost_usd), 2) AS cc_cost_usd
FROM ranked WHERE n = 1
GROUP BY day ORDER BY day;
```

The quota over time, from observed readings only, one point per hour:

```sql
SELECT strftime('%Y-%m-%dT%H:00Z', rate_limits_observed_at) AS hour,
       max(five_hour_pct) AS five_hour_pct,
       max(seven_day_pct) AS seven_day_pct
FROM snapshots
WHERE rate_limits_observed_at IS NOT NULL
GROUP BY hour ORDER BY hour;
```

Within an hour the counter can reset, so `max()` here is "the highest reading
seen in that hour", not the reading at its end.

Cost by project:

```sql
SELECT project_dir, count(*) AS sessions, round(sum(cc_cost_usd), 2) AS cc_cost_usd
FROM (SELECT project_dir, cc_cost_usd,
             row_number() OVER (PARTITION BY session_id ORDER BY recorded_at DESC, id DESC) AS n
      FROM snapshots)
WHERE n = 1
GROUP BY project_dir ORDER BY cc_cost_usd DESC;
```

The cache hit rate of each request, to spot a busted prefix:

```sql
SELECT recorded_at, session_id,
       round(100.0 * last_cache_read_tokens /
             (last_cache_read_tokens + last_cache_creation_tokens + last_input_tokens), 1) AS hit_pct
FROM snapshots
WHERE last_cache_read_tokens + last_cache_creation_tokens + last_input_tokens > 0
ORDER BY recorded_at;
```

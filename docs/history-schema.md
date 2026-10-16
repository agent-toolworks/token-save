# The history database, for tools that read it

The token-save statusline records what it shows into a local SQLite file, one
row per change for each session ([statusline.md](statusline.md) covers
installing it). This page is the contract for anything else that reads that
file, such as a dashboard, a report or a script: where the file is, how to open
it without getting in the way of the writer, what each column means, and what
will and will not change between versions.

`ts statusline --schema` prints the same information as JSON, built from the
column list the writer itself uses:

```sh
"$TS_ROOT/scripts/ts" statusline --schema    # TS_ROOT: see statusline.md, Install
```

```json
{
  "schema_version": 1,
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
- **It is private.** It holds directory paths and branch names (never message
  content), and is created `0600` in a `0700` directory. A tool that sends any
  of it elsewhere should say so and ask first.

## Versioning

`PRAGMA user_version` is the schema version. It is `1` today.

- **Columns are only ever added.** A column is never renamed, removed, re-typed
  or given a new meaning. When columns are added, `user_version` goes up, and
  the next render adds them to an existing table. Rows written before that hold
  `NULL` in the new columns.
- **Select columns by name.** Do not use `SELECT *` with positions. Columns
  added later go at the end, but the promise is about names.
- **A higher version than you know about is fine.** Every column you know is
  still there and still means the same. Ignore the rest.
- **The table is `snapshots`.** Its indexes, `snapshots_session (session_id,
  id)` and `snapshots_time (recorded_at)`, are there to help queries. They are
  not part of the contract.

## What a row is

A row is written when anything on the line changes for a session: model,
context, cost, quota and so on. A plain re-render writes nothing, and nor does
the wall clock moving on its own (see `duration_ms`). That gives three rules
for reading it:

- **Session figures are running totals.** `cc_cost_usd`, `duration_ms`,
  `api_duration_ms`, `lines_added` and `lines_removed` count from the start of
  the session. A session's total is its **last** row (highest `id`), never the
  sum of its rows.
- **Quota figures are account-wide.** `five_hour_pct` and `seven_day_pct` are
  the same account quota whichever session reports them. Two sessions running
  at once both record it, so take the latest row across all sessions for "now",
  and do not add sessions together.
- **`NULL` means "not reported".** Claude Code's payload varies by version and
  account. A column it did not send is `NULL`, never `0`. `clear_breakeven_turns`
  is also `NULL` when clearing would never pay for itself.

## Columns

| Column | Type | Meaning |
|---|---|---|
| `id` | INTEGER | row id, increasing in the order rows were written |
| `recorded_at` | TEXT | when the row was written: UTC, ISO 8601, `2026-10-16T18:40:53Z`. Sorts as text |
| `session_id` | TEXT | Claude Code session id. Rows for one session share it |
| `transcript_path` | TEXT | the session's transcript. It may since have been deleted (`cleanupPeriodDays`) |
| `claude_version` | TEXT | Claude Code version |
| `cwd` | TEXT | current directory |
| `project_dir` | TEXT | directory the session was started in |
| `git_branch` | TEXT | branch checked out in `cwd`; `HEAD` when detached, `NULL` outside a repository |
| `model_id` | TEXT | model id, e.g. `claude-opus-5-5` |
| `model_name` | TEXT | model display name, e.g. `Opus 5.5` |
| `effort` | TEXT | reasoning effort level: `low`, `medium`, `high`, ... |
| `ctx_tokens` | INTEGER | tokens in context, as Claude Code reports it |
| `ctx_size` | INTEGER | context window size, in tokens |
| `ctx_pct` | REAL | context window used, 0 to 100 |
| `ts_context` | INTEGER | tokens in context, read from the transcript by `ts now` |
| `ctx_growth_per_turn` | REAL | tokens added to context per turn, recently (`ts now`) |
| `clear_breakeven_turns` | REAL | turns until clearing the session pays for itself (`ts now`); `NULL` if never |
| `cc_cost_usd` | REAL | session cost so far in US dollars. This is **Claude Code's estimate**, as it reported it, not a bill |
| `duration_ms` | INTEGER | session wall time so far, in milliseconds, as of the first render of this state |
| `api_duration_ms` | INTEGER | time spent waiting on the API so far, in milliseconds |
| `lines_added` | INTEGER | lines added in the session so far |
| `lines_removed` | INTEGER | lines removed in the session so far |
| `five_hour_pct` | REAL | 5-hour quota used, 0 to 100 |
| `five_hour_resets_at` | INTEGER | when the 5-hour quota resets, in epoch seconds |
| `seven_day_pct` | REAL | 7-day quota used, 0 to 100 |
| `seven_day_resets_at` | INTEGER | when the 7-day quota resets, in epoch seconds |
| `ts_version` | TEXT | token-save version that wrote the row |

`scripts/verify` checks that this table names the same columns, with the same
types and in the same order, as the writer, so the two cannot drift apart.

## Queries

The last row of each session holds its totals:

```sql
SELECT s.*
FROM snapshots s
JOIN (SELECT session_id, max(id) AS id FROM snapshots GROUP BY session_id) last
  ON s.id = last.id;
```

Cost and sessions per day, with each session counted on the day it started:

```sql
WITH sessions AS (
  SELECT session_id, min(recorded_at) AS started, max(id) AS last_id
  FROM snapshots GROUP BY session_id)
SELECT date(started) AS day,
       count(*) AS sessions,
       round(sum(s.cc_cost_usd), 2) AS cc_cost_usd
FROM sessions JOIN snapshots s ON s.id = sessions.last_id
GROUP BY day ORDER BY day;
```

The 5-hour quota over time, one point per hour (the highest reading in each):

```sql
SELECT strftime('%Y-%m-%dT%H:00Z', recorded_at) AS hour,
       max(five_hour_pct) AS five_hour_pct,
       max(seven_day_pct) AS seven_day_pct
FROM snapshots
WHERE five_hour_pct IS NOT NULL
GROUP BY hour ORDER BY hour;
```

Cost by project:

```sql
SELECT s.project_dir, count(*) AS sessions, round(sum(s.cc_cost_usd), 2) AS cc_cost_usd
FROM (SELECT max(id) AS id FROM snapshots GROUP BY session_id) last
JOIN snapshots s ON s.id = last.id
GROUP BY s.project_dir ORDER BY cc_cost_usd DESC;
```

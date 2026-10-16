#!/usr/bin/env python3
"""The statusline's history: what it showed, kept after the session is gone.

Every interactive session renders the statusline, so it is the one place that
sees every session without being asked to. Some of what it is handed exists
nowhere else: the 5h and 7d quota and their reset times, Claude Code's own
cost figure, the context percentage it displays. Transcripts carry none of
those, and they are deleted after `cleanupPeriodDays` anyway (#41).

One row per change, not per render. Claude Code re-renders the statusline
every few hundred milliseconds while a session is active, and a row per render
would be thousands of identical rows a day. A row is written only when
something other than the clock differs from the previous row for the same
session -- see DEDUPE_EXCLUDED.

Storage is SQLite from the standard library, at
${CLAUDE_CONFIG_DIR:-~/.claude}/token-save/history.db. It holds numbers and
the labels already on screen (paths, branch, model), never message content,
and nothing here sends it anywhere.

Everything in this module is called from inside a prompt, so it gives up
rather than waits: a locked or unwritable database costs a row, never the line.
"""
from __future__ import annotations

import os
import sqlite3

# Bumped on any change to COLUMNS. A database written by a NEWER version is
# left alone rather than written with a narrower row: two versions can be
# installed side by side, and the older one must not damage the newer's data.
SCHEMA_VERSION = 1

# (column, type, what it is). The order is the table's column order.
COLUMNS = [
    ("recorded_at", "TEXT", "UTC, ISO 8601, when this row was written"),
    ("session_id", "TEXT", "Claude Code session id"),
    ("transcript_path", "TEXT", "the session's transcript"),
    ("claude_version", "TEXT", "Claude Code version"),
    ("cwd", "TEXT", "current directory"),
    ("project_dir", "TEXT", "directory the session was started in"),
    ("git_branch", "TEXT", "branch checked out in cwd"),
    ("model_id", "TEXT", "model id"),
    ("model_name", "TEXT", "model display name"),
    ("effort", "TEXT", "reasoning effort level"),
    ("ctx_tokens", "INTEGER", "context used, as Claude Code reports it"),
    ("ctx_size", "INTEGER", "context window size"),
    ("ctx_pct", "REAL", "context window used, %"),
    ("ts_context", "INTEGER", "context from the transcript, as `ts now` reads it"),
    ("ctx_growth_per_turn", "REAL", "context added per turn (`ts now`)"),
    ("clear_breakeven_turns", "REAL", "turns until clearing pays (`ts now`); NULL if never"),
    ("cc_cost_usd", "REAL", "session cost so far: Claude Code's estimate, recorded as reported"),
    ("duration_ms", "INTEGER", "session wall time so far"),
    ("api_duration_ms", "INTEGER", "time spent waiting on the API so far"),
    ("lines_added", "INTEGER", "lines added in the session so far"),
    ("lines_removed", "INTEGER", "lines removed in the session so far"),
    ("five_hour_pct", "REAL", "5-hour quota used, %"),
    ("five_hour_resets_at", "INTEGER", "5-hour quota reset, epoch seconds"),
    ("seven_day_pct", "REAL", "7-day quota used, %"),
    ("seven_day_resets_at", "INTEGER", "7-day quota reset, epoch seconds"),
    ("ts_version", "TEXT", "token-save version that wrote the row"),
]
NAMES = [c[0] for c in COLUMNS]

# Columns that change on every render without anything having happened.
# duration_ms is wall time since the session started, so including it would
# write a row per render -- the stored value is the one from the first render
# of each state. ts_version changes only on an update, which alone is not news.
DEDUPE_EXCLUDED = frozenset({"recorded_at", "duration_ms", "ts_version"})
DEDUPE_KEY = [n for n in NAMES if n not in DEDUPE_EXCLUDED]

# How long to wait for another session's write. Several sessions render at
# once; a writer holds the lock for well under a millisecond, so this only
# runs out when something is wrong -- and then the row is the thing dropped.
BUSY_TIMEOUT_S = 0.2

_OFF = frozenset({"off", "0", "no", "false"})


def db_path():
    """Where the history lives, or None when recording is turned off."""
    if os.environ.get("TS_HISTORY", "").strip().lower() in _OFF:
        return None
    explicit = os.environ.get("TS_HISTORY_DB")
    if explicit:
        return os.path.expanduser(explicit)
    cfg = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")
    return os.path.join(cfg, "token-save", "history.db")


def _connect(path: str) -> sqlite3.Connection:
    # isolation_level=None: transactions are opened explicitly below, so the
    # read of the previous row and the insert are one atomic step.
    return sqlite3.connect(path, timeout=BUSY_TIMEOUT_S, isolation_level=None)


def _ensure_schema(db: sqlite3.Connection) -> bool:
    """Create the table on first use, or bring an older one up to date. False
    if the file belongs to a newer schema, which this version must not write
    to."""
    version = db.execute("PRAGMA user_version").fetchone()[0]
    if version > SCHEMA_VERSION:
        return False
    if version == SCHEMA_VERSION:
        return True
    # WAL lets one session read while another writes. It is a property of the
    # file, and cannot be changed inside a transaction, so it goes first.
    db.execute("PRAGMA journal_mode=WAL")
    # Two sessions can both find an old file on their first render. The
    # second waits here, then finds the work done.
    db.execute("BEGIN IMMEDIATE")
    try:
        version = db.execute("PRAGMA user_version").fetchone()[0]
        if version >= SCHEMA_VERSION:
            db.execute("COMMIT")
            return version == SCHEMA_VERSION
        cols = ",\n  ".join("%s %s" % (n, t) for n, t, _ in COLUMNS)
        db.execute("CREATE TABLE IF NOT EXISTS snapshots (\n"
                   "  id INTEGER PRIMARY KEY,\n  %s\n)" % cols)
        # A table from an older version lacks the columns added since. Columns
        # are only ever added (the promise in docs/history-schema.md), so
        # adding the missing ones is the whole migration, and rows written
        # before have NULL in them -- which a reader already has to handle.
        have = {r[1] for r in db.execute("PRAGMA table_info(snapshots)")}
        for n, t, _ in COLUMNS:
            if n not in have:
                db.execute("ALTER TABLE snapshots ADD COLUMN %s %s" % (n, t))
        db.execute("CREATE INDEX IF NOT EXISTS snapshots_session "
                   "ON snapshots (session_id, id)")
        db.execute("CREATE INDEX IF NOT EXISTS snapshots_time "
                   "ON snapshots (recorded_at)")
        db.execute("PRAGMA user_version = %d" % SCHEMA_VERSION)
        db.execute("COMMIT")
    except Exception:
        db.execute("ROLLBACK")
        raise
    return True


def schema() -> dict:
    """The table as a reader sees it: what `ts statusline --schema` prints for
    tools that read the history, and what docs/history-schema.md documents.
    Built from COLUMNS, so it cannot describe a table other than the one
    record() writes."""
    return {
        "schema_version": SCHEMA_VERSION,
        "database": db_path(),
        "table": "snapshots",
        "columns": [{"name": "id", "type": "INTEGER",
                     "description": "row id, increasing in the order rows were written"}]
                   + [{"name": n, "type": t, "description": d} for n, t, d in COLUMNS],
    }


def record(row: dict, path: str = None) -> str:
    """Write `row` unless it repeats the session's previous one.

    Returns what happened -- "written", "unchanged", "off", "no-session",
    "newer-schema" or "error: ..." -- so `--explain` and the checks can say
    why a row is missing. Never raises.
    """
    path = path if path is not None else db_path()
    if path is None:
        return "off"
    if not row.get("session_id"):
        return "no-session"
    db = None
    try:
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, mode=0o700, exist_ok=True)
        # The rows name directories and branches; nobody else on the machine
        # needs to read them.
        old_umask = os.umask(0o077)
        try:
            db = _connect(path)
            if not _ensure_schema(db):
                return "newer-schema"
        finally:
            os.umask(old_umask)
        db.execute("BEGIN IMMEDIATE")
        prev = db.execute(
            "SELECT %s FROM snapshots WHERE session_id = ? "
            "ORDER BY id DESC LIMIT 1" % ", ".join(DEDUPE_KEY),
            (row["session_id"],)).fetchone()
        if prev is not None and tuple(prev) == tuple(row.get(n) for n in DEDUPE_KEY):
            db.execute("COMMIT")
            return "unchanged"
        db.execute("INSERT INTO snapshots (%s) VALUES (%s)"
                   % (", ".join(NAMES), ", ".join("?" * len(NAMES))),
                   tuple(row.get(n) for n in NAMES))
        db.execute("COMMIT")
        return "written"
    except Exception as e:  # locked, read-only, disk full, not a database...
        return "error: %s" % e
    finally:
        if db is not None:
            try:
                db.close()
            except Exception:
                pass


def describe(path: str = None) -> str:
    """One line on the state of the history, for `--explain`. Read-only."""
    path = path if path is not None else db_path()
    if path is None:
        return "off (TS_HISTORY=%s)" % os.environ.get("TS_HISTORY")
    if not os.path.exists(path):
        return "%s (not created yet; the first render creates it)" % path
    try:
        db = sqlite3.connect("file:%s?mode=ro" % path, uri=True,
                             timeout=BUSY_TIMEOUT_S)
        try:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version > SCHEMA_VERSION:
                return "%s (schema %d is newer than this version's %d; not written)" % (
                    path, version, SCHEMA_VERSION)
            rows, sessions, last = db.execute(
                "SELECT count(*), count(DISTINCT session_id), max(recorded_at) "
                "FROM snapshots").fetchone()
        finally:
            db.close()
    except Exception as e:
        return "%s (cannot read: %s)" % (path, e)
    return "%s (%d rows, %d sessions, last %s)" % (path, rows, sessions, last or "never")

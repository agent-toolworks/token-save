#!/usr/bin/env python3
"""The statusline's history: what it showed, kept after the session is gone.

Every interactive session renders the statusline, so it is the one place that
sees every session without being asked to. Some of what it is handed exists
nowhere else: the 5h and 7d quota and their reset times, Claude Code's own
cost figure, the context percentage it displays. Transcripts carry none of
those, and they are deleted after `cleanupPeriodDays` anyway (#41).

One row per change, not per render. Claude Code renders the statusline on
events -- a new assistant message, session start, /compact, a mode change --
debounced by 300ms, and on a timer too when `refreshInterval` is set. A row per
render would be mostly repeats, so a row is written only when something other
than the clock differs from the previous row for the same session -- see
DEDUPE_EXCLUDED.

Every payload field has a column of its own, so nothing a reader might want
is folded away. A field this version has no column for is listed in
`unknown_keys`, and only those rows carry the raw payload: a Claude Code
upgrade that adds a field shows up without the raw JSON (about half the file,
measured) being stored on every row.

Storage is SQLite from the standard library, at
${CLAUDE_CONFIG_DIR:-~/.claude}/token-save/history.db. It holds numbers and
the labels already on screen (paths, branch, model), never message content,
and nothing here sends it anywhere. docs/history-schema.md is the contract
for anything else that reads it.

Everything in this module is called from inside a prompt, so it gives up
rather than waits: a locked or unwritable database costs a row, never the line.
"""
from __future__ import annotations

import datetime
import json
import os
import sqlite3

# Bumped on any change to COLUMNS. A database written by a NEWER version is
# left alone rather than written with a narrower row: two versions can be
# installed side by side, and the older one must not damage the newer's data.
SCHEMA_VERSION = 2

# (column, type, what it is). The order is the table's column order, and new
# columns only ever go at the end.
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
    # -- schema 2 --
    ("session_name", "TEXT", "session name, when the session has one"),
    ("prompt_id", "TEXT", "id of the prompt being answered"),
    ("workspace_current_dir", "TEXT", "workspace.current_dir, as reported"),
    ("payload_cwd", "TEXT", "top-level cwd, as reported"),
    ("added_dirs", "TEXT", "workspace.added_dirs, as a JSON array"),
    ("thinking_enabled", "INTEGER", "1 when extended thinking is on, 0 when off"),
    ("fast_mode", "INTEGER", "1 when fast mode is on, 0 when off"),
    ("output_style", "TEXT", "output style name"),
    ("ctx_remaining_pct", "REAL", "context window left, %"),
    ("ctx_input_tokens", "INTEGER", "context_window.total_input_tokens"),
    ("ctx_output_tokens", "INTEGER", "context_window.total_output_tokens"),
    ("last_input_tokens", "INTEGER", "uncached input tokens of the last request"),
    ("last_output_tokens", "INTEGER", "output tokens of the last request"),
    ("last_cache_creation_tokens", "INTEGER", "cache-write tokens of the last request"),
    ("last_cache_read_tokens", "INTEGER", "cache-read tokens of the last request"),
    ("exceeds_200k_tokens", "INTEGER", "1 when the context is over 200k tokens"),
    ("rate_limits_observed_at", "TEXT",
     "UTC, ISO 8601: when this session last saw its quota reading change; NULL if never"),
    ("source", "TEXT", "what wrote the row: statusline or import"),
    ("unknown_keys", "TEXT", "JSON array of payload fields with no column; NULL when none"),
    ("raw_payload", "TEXT", "the payload as JSON, only on rows where unknown_keys is set"),
]
NAMES = [c[0] for c in COLUMNS]

# Columns that change on every render without anything having happened.
# duration_ms is wall time since the session started, so including it would
# write a row per render -- the stored value is the one from the first render
# of each state. ts_version changes only on an update, which alone is not news.
# raw_payload carries duration_ms inside it, and source says who wrote a row,
# not what the session was doing.
DEDUPE_EXCLUDED = frozenset({"recorded_at", "duration_ms", "ts_version",
                             "raw_payload", "source"})
DEDUPE_KEY = [n for n in NAMES if n not in DEDUPE_EXCLUDED]

# The quota reading as one value: all four arrive in the same API response.
RATE_LIMIT_KEY = ("five_hour_pct", "five_hour_resets_at",
                  "seven_day_pct", "seven_day_resets_at")

# Every payload leaf with a column (or, for hook_event_name, deliberately none:
# it names the hook and is the same on every render). Anything else is drift.
KNOWN_LEAVES = frozenset({
    "hook_event_name",
    "session_id", "session_name", "prompt_id", "transcript_path", "cwd", "version",
    "model.id", "model.display_name",
    "workspace.current_dir", "workspace.project_dir", "workspace.added_dirs",
    "output_style.name", "effort.level", "thinking.enabled", "fast_mode",
    "exceeds_200k_tokens",
    "cost.total_cost_usd", "cost.total_duration_ms", "cost.total_api_duration_ms",
    "cost.total_lines_added", "cost.total_lines_removed",
    "context_window.total_input_tokens", "context_window.total_output_tokens",
    "context_window.context_window_size", "context_window.used_percentage",
    "context_window.remaining_percentage",
    "context_window.current_usage.input_tokens",
    "context_window.current_usage.output_tokens",
    "context_window.current_usage.cache_creation_input_tokens",
    "context_window.current_usage.cache_read_input_tokens",
    "rate_limits.five_hour.used_percentage", "rate_limits.five_hour.resets_at",
    "rate_limits.seven_day.used_percentage", "rate_limits.seven_day.resets_at",
})

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


def utc_now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---- payload -> row --------------------------------------------------------

def _get(d, *keys):
    for k in keys:
        if not isinstance(d, dict):
            return None
        d = d.get(k)
    return d


def _num(v):
    """A number from the payload, or None. Booleans are not numbers here."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return v


def _int(v):
    v = _num(v)
    return None if v is None else int(round(v))


def _str(v):
    return v if isinstance(v, str) and v else None


def _bool(v):
    return None if not isinstance(v, bool) else int(v)


def payload_leaves(p, prefix=""):
    """Dotted paths of every leaf. A list is a leaf; so is null."""
    for k, v in p.items():
        path = prefix + k
        if isinstance(v, dict) and v:
            yield from payload_leaves(v, path + ".")
        else:
            yield path


def row_from_payload(p: dict, now: dict = None, branch=None,
                     recorded_at: str = None, source: str = "statusline") -> dict:
    """The history row for one payload: every field it carries, plus the
    `ts now` reading and branch when the caller has them. The importer passes
    neither, and they stay NULL."""
    cw = _get(p, "context_window") or {}
    cu = _get(cw, "current_usage") or {}
    ctx_size = _int(_get(cw, "context_window_size"))
    ctx_in, ctx_out = _int(_get(cw, "total_input_tokens")), _int(_get(cw, "total_output_tokens"))
    ctx_tokens = None
    if ctx_in is not None or ctx_out is not None:
        ctx_tokens = (ctx_in or 0) + (ctx_out or 0)
    ctx_pct = _num(_get(cw, "used_percentage"))
    if ctx_pct is None and ctx_tokens is not None and ctx_size:
        ctx_pct = ctx_tokens * 100.0 / ctx_size

    transcript = _str(p.get("transcript_path"))
    session = _str(p.get("session_id"))
    if not session and transcript:
        # The transcript is named after the session.
        session = os.path.splitext(os.path.basename(transcript))[0] or None

    added = _get(p, "workspace", "added_dirs")
    unknown = sorted(k for k in payload_leaves(p) if k not in KNOWN_LEAVES)
    be = now.get("breakeven_turns") if now else None
    return {
        "recorded_at": recorded_at or utc_now(),
        "session_id": session,
        "transcript_path": transcript,
        "claude_version": _str(p.get("version")),
        "cwd": _str(_get(p, "workspace", "current_dir")) or _str(p.get("cwd")),
        "project_dir": _str(_get(p, "workspace", "project_dir")),
        "git_branch": branch,
        "model_id": _str(_get(p, "model", "id")),
        "model_name": _str(_get(p, "model", "display_name")),
        "effort": _str(_get(p, "effort", "level")),
        "ctx_tokens": ctx_tokens,
        "ctx_size": ctx_size,
        "ctx_pct": ctx_pct,
        "ts_context": _int(now.get("context")) if now else None,
        "ctx_growth_per_turn": _num(now.get("growth_per_turn")) if now else None,
        "clear_breakeven_turns": None if be is None or be == float("inf") else be,
        "cc_cost_usd": _num(_get(p, "cost", "total_cost_usd")),
        "duration_ms": _int(_get(p, "cost", "total_duration_ms")),
        "api_duration_ms": _int(_get(p, "cost", "total_api_duration_ms")),
        "lines_added": _int(_get(p, "cost", "total_lines_added")),
        "lines_removed": _int(_get(p, "cost", "total_lines_removed")),
        "five_hour_pct": _num(_get(p, "rate_limits", "five_hour", "used_percentage")),
        "five_hour_resets_at": _int(_get(p, "rate_limits", "five_hour", "resets_at")),
        "seven_day_pct": _num(_get(p, "rate_limits", "seven_day", "used_percentage")),
        "seven_day_resets_at": _int(_get(p, "rate_limits", "seven_day", "resets_at")),
        "ts_version": os.environ.get("TS_VERSION") or None,
        "session_name": _str(p.get("session_name")),
        "prompt_id": _str(p.get("prompt_id")),
        "workspace_current_dir": _str(_get(p, "workspace", "current_dir")),
        "payload_cwd": _str(p.get("cwd")),
        "added_dirs": json.dumps(added, ensure_ascii=False) if isinstance(added, list) else None,
        "thinking_enabled": _bool(_get(p, "thinking", "enabled")),
        "fast_mode": _bool(p.get("fast_mode")),
        "output_style": _str(_get(p, "output_style", "name")),
        "ctx_remaining_pct": _num(_get(cw, "remaining_percentage")),
        "ctx_input_tokens": ctx_in,
        "ctx_output_tokens": ctx_out,
        "last_input_tokens": _int(_get(cu, "input_tokens")),
        "last_output_tokens": _int(_get(cu, "output_tokens")),
        "last_cache_creation_tokens": _int(_get(cu, "cache_creation_input_tokens")),
        "last_cache_read_tokens": _int(_get(cu, "cache_read_input_tokens")),
        "exceeds_200k_tokens": _bool(p.get("exceeds_200k_tokens")),
        "rate_limits_observed_at": None,      # observed_at(), against the previous row
        "source": source,
        "unknown_keys": json.dumps(unknown) if unknown else None,
        "raw_payload": json.dumps(p, ensure_ascii=False, sort_keys=True) if unknown else None,
    }


def observed_at(row: dict, prev):
    """When this session last saw its quota reading change.

    `rate_limits` comes from the session's own last API response and is not
    refreshed while it sits idle, so an unchanged reading says nothing about
    now. A CHANGED one can only have come from a fresh response, so the change
    is the observation. The first reading a session shows has no such
    evidence (a resumed session can open on an old one), so it stays NULL
    until it moves.
    """
    reading = tuple(row.get(k) for k in RATE_LIMIT_KEY)
    if all(v is None for v in reading) or not prev:
        return None
    if tuple(prev.get(k) for k in RATE_LIMIT_KEY) != reading:
        return row["recorded_at"]
    return prev.get("rate_limits_observed_at")


# ---- the database ----------------------------------------------------------

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
        # Rows are ordered by recorded_at, not id: an import can add rows
        # older than the live ones already there.
        db.execute("CREATE INDEX IF NOT EXISTS snapshots_session_time "
                   "ON snapshots (session_id, recorded_at)")
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
    from transcripts import tool_version
    return {
        "tool_version": tool_version(),
        "schema_version": SCHEMA_VERSION,
        "database": db_path(),
        "table": "snapshots",
        "columns": [{"name": "id", "type": "INTEGER",
                     "description": "row id, increasing in the order rows were written"}]
                   + [{"name": n, "type": t, "description": d} for n, t, d in COLUMNS],
    }


_PREV_SQL = ("SELECT %s FROM snapshots WHERE session_id = ? AND recorded_at <= ? "
             "ORDER BY recorded_at DESC, id DESC LIMIT 1")


def _prev(db, session_id, at, names):
    r = db.execute(_PREV_SQL % ", ".join(names), (session_id, at)).fetchone()
    return None if r is None else dict(zip(names, r))


def previous(session_id, path: str = None):
    """The session's latest row, read-only, or None. The statusline reads it
    before printing, to know whether the quota reading has changed."""
    path = path if path is not None else db_path()
    if path is None or not session_id or not os.path.exists(path):
        return None
    try:
        db = sqlite3.connect("file:%s?mode=ro" % path, uri=True, timeout=BUSY_TIMEOUT_S)
        try:
            if db.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
                return None
            return _prev(db, session_id, "9999", NAMES)
        finally:
            db.close()
    except Exception:
        return None


def _open_for_write(path):
    """(db, None) ready to write, or (None, reason)."""
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, mode=0o700, exist_ok=True)
    # The rows name directories and branches; nobody else on the machine
    # needs to read them.
    old_umask = os.umask(0o077)
    try:
        db = _connect(path)
        if not _ensure_schema(db):
            db.close()
            return None, "newer-schema"
    finally:
        os.umask(old_umask)
    return db, None


def _insert(db, row):
    db.execute("INSERT INTO snapshots (%s) VALUES (%s)"
               % (", ".join(NAMES), ", ".join("?" * len(NAMES))),
               tuple(row.get(n) for n in NAMES))


def _same(prev, row) -> bool:
    return prev is not None and all(prev[n] == row.get(n) for n in DEDUPE_KEY)


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
        db, why = _open_for_write(path)
        if db is None:
            return why
        db.execute("BEGIN IMMEDIATE")
        if _same(_prev(db, row["session_id"], row["recorded_at"], DEDUPE_KEY), row):
            db.execute("COMMIT")
            return "unchanged"
        _insert(db, row)
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


def import_rows(rows, path: str = None, dry_run: bool = False) -> dict:
    """Write imported rows, oldest first, through the same dedupe as live ones.

    Idempotent: a row whose (session_id, recorded_at) is already there is
    skipped, so an import can be re-run while a switch-over is in progress.
    One transaction for the lot -- an import is run by hand, not from a
    prompt, so it may wait for the lock like any other writer.
    """
    counts = {"written": 0, "already-there": 0, "unchanged": 0}
    path = path if path is not None else db_path()
    if path is None:
        raise RuntimeError("recording is turned off (TS_HISTORY=off)")
    db, why = _open_for_write(path)
    if db is None:
        raise RuntimeError("%s: written by a newer token-save (schema above %d)"
                           % (path, SCHEMA_VERSION))
    try:
        db.execute("PRAGMA busy_timeout = 10000")
        db.execute("BEGIN IMMEDIATE")
        for row in sorted(rows, key=lambda r: (r["session_id"], r["recorded_at"])):
            if db.execute("SELECT 1 FROM snapshots WHERE session_id = ? AND recorded_at = ?",
                          (row["session_id"], row["recorded_at"])).fetchone():
                counts["already-there"] += 1
                continue
            prev = _prev(db, row["session_id"], row["recorded_at"], NAMES)
            row["rate_limits_observed_at"] = observed_at(row, prev)
            if _same(prev, row):
                counts["unchanged"] += 1
                continue
            _insert(db, row)
            counts["written"] += 1
        db.execute("ROLLBACK" if dry_run else "COMMIT")
    finally:
        db.close()
    return counts


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
    note = "" if version == SCHEMA_VERSION else "; schema %d, upgraded on the next write" % version
    return "%s (%d rows, %d sessions, last %s%s)" % (
        path, rows, sessions, last or "never", note)

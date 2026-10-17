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
SCHEMA_VERSION = 4

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
    # -- schema 3 --
    ("scratchpad_dir", "TEXT", "the session's scratchpad directory"),
    ("repo_host", "TEXT", "workspace.repo.host"),
    ("repo_owner", "TEXT", "workspace.repo.owner"),
    ("repo_name", "TEXT", "workspace.repo.name"),
    ("prompt_cache_warm", "INTEGER", "1 when Claude Code reports the prompt cache warm"),
    ("prompt_cache_caching_observed", "INTEGER", "prompt_cache.caching_observed, 1 or 0"),
    ("prompt_cache_ttl", "TEXT", "prompt cache TTL as reported: 5m or 1h"),
    ("prompt_cache_expires_at", "INTEGER", "when the cached prefix expires, epoch seconds"),
    ("prompt_cache_requests", "INTEGER", "prompt_cache.requests"),
    ("prompt_cache_misses", "INTEGER", "prompt_cache.misses"),
    ("prompt_cache_expected_rebuilds", "INTEGER", "prompt_cache.expected_rebuilds"),
    ("prompt_cache_hit_ratio", "REAL", "prompt_cache.hit_ratio, as reported"),
    ("prompt_cache_write_tokens", "INTEGER", "prompt_cache.cache_write_tokens"),
    ("prompt_cache_miss_recache_tokens", "INTEGER", "prompt_cache.miss_recache_tokens"),
    ("prompt_cache_last_miss_at", "INTEGER", "prompt_cache.last_miss_at, epoch seconds"),
    ("prompt_cache_recache_tokens_if_cold", "INTEGER", "prompt_cache.recache_tokens_if_cold"),
    ("prompt_cache_miss_causes", "TEXT", "prompt_cache.miss_causes, as JSON"),
    ("prompt_cache_last_miss_cause", "TEXT", "prompt_cache.last_miss_cause, as JSON"),
    # -- schema 4 --
    ("git_worktree", "TEXT", "workspace.git_worktree: linked git worktree name"),
    ("worktree_name", "TEXT", "worktree.name: the worktree session's worktree"),
    ("worktree_path", "TEXT", "worktree.path"),
    ("worktree_branch", "TEXT", "worktree.branch"),
    ("worktree_original_cwd", "TEXT", "worktree.original_cwd"),
    ("worktree_original_branch", "TEXT", "worktree.original_branch"),
    ("pr_number", "INTEGER", "pr.number: open PR (or GitLab MR) for the branch"),
    ("pr_url", "TEXT", "pr.url"),
    ("pr_review_state", "TEXT", "pr.review_state"),
    ("pr_kind", "TEXT", "pr.kind: mr for a GitLab merge request"),
    ("agent_name", "TEXT", "agent.name, with --agent or agent settings"),
    ("agent_type", "TEXT", "agent_type, as reported"),
    ("spend_limit_pct", "REAL", "gateway spend limit used, %"),
    ("spend_limit_resets_at", "INTEGER", "gateway spend limit period reset, epoch seconds"),
    ("spend_limit_used_usd", "REAL", "gateway spend so far, US dollars (its estimate)"),
    ("spend_limit_limit_usd", "REAL", "gateway spend limit, US dollars"),
    ("spend_limit_period", "TEXT", "gateway spend limit period: daily, weekly or monthly"),
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

# Every payload field with a column: (dotted path, column, kind). The kind is
# what the column holds, and a value of any other type is drift, reported in
# unknown_keys rather than stored as NULL as if it were missing. A "json"
# field is stored whole, so the keys under it can vary without tripping the
# canary. Columns not listed here are derived (cwd, ctx_tokens) or not from
# the payload at all.
FIELDS = [
    ("session_id", "session_id", "str"),
    ("transcript_path", "transcript_path", "str"),
    ("version", "claude_version", "str"),
    ("workspace.project_dir", "project_dir", "str"),
    ("model.id", "model_id", "str"),
    ("model.display_name", "model_name", "str"),
    ("effort.level", "effort", "str"),
    ("context_window.context_window_size", "ctx_size", "int"),
    ("context_window.used_percentage", "ctx_pct", "num"),
    ("cost.total_cost_usd", "cc_cost_usd", "num"),
    ("cost.total_duration_ms", "duration_ms", "int"),
    ("cost.total_api_duration_ms", "api_duration_ms", "int"),
    ("cost.total_lines_added", "lines_added", "int"),
    ("cost.total_lines_removed", "lines_removed", "int"),
    ("rate_limits.five_hour.used_percentage", "five_hour_pct", "num"),
    ("rate_limits.five_hour.resets_at", "five_hour_resets_at", "int"),
    ("rate_limits.seven_day.used_percentage", "seven_day_pct", "num"),
    ("rate_limits.seven_day.resets_at", "seven_day_resets_at", "int"),
    ("session_name", "session_name", "str"),
    ("prompt_id", "prompt_id", "str"),
    ("workspace.current_dir", "workspace_current_dir", "str"),
    ("cwd", "payload_cwd", "str"),
    ("workspace.added_dirs", "added_dirs", "json"),
    ("thinking.enabled", "thinking_enabled", "bool"),
    ("fast_mode", "fast_mode", "bool"),
    ("output_style.name", "output_style", "str"),
    ("context_window.remaining_percentage", "ctx_remaining_pct", "num"),
    ("context_window.total_input_tokens", "ctx_input_tokens", "int"),
    ("context_window.total_output_tokens", "ctx_output_tokens", "int"),
    ("context_window.current_usage.input_tokens", "last_input_tokens", "int"),
    ("context_window.current_usage.output_tokens", "last_output_tokens", "int"),
    ("context_window.current_usage.cache_creation_input_tokens", "last_cache_creation_tokens", "int"),
    ("context_window.current_usage.cache_read_input_tokens", "last_cache_read_tokens", "int"),
    ("exceeds_200k_tokens", "exceeds_200k_tokens", "bool"),
    ("scratchpad_dir", "scratchpad_dir", "str"),
    ("workspace.repo.host", "repo_host", "str"),
    ("workspace.repo.owner", "repo_owner", "str"),
    ("workspace.repo.name", "repo_name", "str"),
    ("prompt_cache.warm", "prompt_cache_warm", "bool"),
    ("prompt_cache.caching_observed", "prompt_cache_caching_observed", "bool"),
    ("prompt_cache.ttl", "prompt_cache_ttl", "str"),
    ("prompt_cache.expires_at", "prompt_cache_expires_at", "epoch"),
    ("prompt_cache.requests", "prompt_cache_requests", "int"),
    ("prompt_cache.misses", "prompt_cache_misses", "int"),
    ("prompt_cache.expected_rebuilds", "prompt_cache_expected_rebuilds", "int"),
    ("prompt_cache.hit_ratio", "prompt_cache_hit_ratio", "num"),
    ("prompt_cache.cache_write_tokens", "prompt_cache_write_tokens", "int"),
    ("prompt_cache.miss_recache_tokens", "prompt_cache_miss_recache_tokens", "int"),
    ("prompt_cache.last_miss_at", "prompt_cache_last_miss_at", "epoch"),
    ("prompt_cache.recache_tokens_if_cold", "prompt_cache_recache_tokens_if_cold", "int"),
    ("prompt_cache.miss_causes", "prompt_cache_miss_causes", "json"),
    ("prompt_cache.last_miss_cause", "prompt_cache_last_miss_cause", "json"),
    ("workspace.git_worktree", "git_worktree", "str"),
    ("worktree.name", "worktree_name", "str"),
    ("worktree.path", "worktree_path", "str"),
    ("worktree.branch", "worktree_branch", "str"),
    ("worktree.original_cwd", "worktree_original_cwd", "str"),
    ("worktree.original_branch", "worktree_original_branch", "str"),
    ("pr.number", "pr_number", "int"),
    ("pr.url", "pr_url", "str"),
    ("pr.review_state", "pr_review_state", "str"),
    ("pr.kind", "pr_kind", "str"),
    ("agent.name", "agent_name", "str"),
    ("agent_type", "agent_type", "str"),
    ("rate_limits.spend_limit.used_percentage", "spend_limit_pct", "num"),
    ("rate_limits.spend_limit.resets_at", "spend_limit_resets_at", "int"),
    ("rate_limits.spend_limit.used_usd", "spend_limit_used_usd", "num"),
    ("rate_limits.spend_limit.limit_usd", "spend_limit_limit_usd", "num"),
    ("rate_limits.spend_limit.period", "spend_limit_period", "str"),
]
# Recognised and deliberately not kept. hook_event_name names the hook, the
# same on every render. vim.mode flips between NORMAL and INSERT as the person
# types, and each flip renders: a column for it would write a row per
# keystroke mode change, and it says nothing about usage.
IGNORED_LEAVES = frozenset({"hook_event_name", "vim.mode"})
_JSON_PATHS = tuple(f[0] for f in FIELDS if f[2] == "json")
_PATHS = frozenset(f[0] for f in FIELDS)
# The objects the known fields sit in. Claude Code sends some of them as null
# or {} when there is nothing in them yet -- context_window.current_usage is
# null before a session's first API response and again after /compact -- and
# that is the normal shape of the payload, so its fields are simply NULL.
_OBJECTS = frozenset(path.rsplit(".", i)[0]
                     for path in _PATHS | IGNORED_LEAVES
                     for i in range(1, path.count(".") + 1))

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


def to_epoch(v):
    """Epoch seconds from a number (seconds, or milliseconds when it is far too
    large to be seconds) or an ISO 8601 string with an offset; else None."""
    n = _num(v)
    if n is not None:
        return int(n / 1000) if n > 1e11 else int(n)
    if isinstance(v, str):
        import datetime as _dt
        s = v.strip()
        s = s[:-1] + "+00:00" if s.endswith(("Z", "z")) else s
        try:
            t = _dt.datetime.fromisoformat(s)
        except ValueError:
            return None
        return int(t.timestamp()) if t.tzinfo else None
    return None


def _convert(kind, v):
    """(value, ok). ok is False when v is present but not of the kind."""
    if v is None:
        return None, True
    if kind == "str":
        return (v or None, True) if isinstance(v, str) else (None, False)
    if kind == "int":
        n = _int(v)
        return n, n is not None
    if kind == "num":
        n = _num(v)
        return n, n is not None
    if kind == "bool":
        return (int(v), True) if isinstance(v, bool) else (None, False)
    if kind == "json":
        return json.dumps(v, ensure_ascii=False), True
    if kind == "epoch":
        n = to_epoch(v)
        return n, n is not None
    raise ValueError(kind)


def payload_leaves(p, prefix=""):
    """(dotted path, value) of every leaf. A list is a leaf; so are null and {}."""
    for k, v in p.items():
        path = prefix + k
        if isinstance(v, dict) and v:
            yield from payload_leaves(v, path + ".")
        else:
            yield path, v


def _known(path, v) -> bool:
    if path in _OBJECTS:
        # null or {} where an object belongs is an empty one; anything else
        # there is a type change.
        return v is None or v == {}
    return path in _PATHS or path in IGNORED_LEAVES or \
        any(path.startswith(j + ".") for j in _JSON_PATHS)


def row_from_payload(p: dict, now: dict = None, branch=None,
                     recorded_at: str = None, source: str = "statusline") -> dict:
    """The history row for one payload: every field it carries, plus the
    `ts now` reading and branch when the caller has them. The importer passes
    neither, and they stay NULL."""
    row = {n: None for n in NAMES}
    drift = []
    for path, col, kind in FIELDS:
        v = p
        for k in path.split("."):
            v = v.get(k) if isinstance(v, dict) else None
        row[col], ok = _convert(kind, v)
        if not ok:
            drift.append(path)

    if not row["session_id"] and row["transcript_path"]:
        # The transcript is named after the session.
        row["session_id"] = os.path.splitext(
            os.path.basename(row["transcript_path"]))[0] or None
    row["cwd"] = row["workspace_current_dir"] or row["payload_cwd"]
    if row["ctx_input_tokens"] is not None or row["ctx_output_tokens"] is not None:
        row["ctx_tokens"] = (row["ctx_input_tokens"] or 0) + (row["ctx_output_tokens"] or 0)
    # ctx_pct is what Claude Code reported, never derived: on a session's
    # first render it sends used_percentage null with 0 tokens, and a derived
    # 0.0 there would claim a measurement nobody made.

    be = now.get("breakeven_turns") if now else None
    row.update({
        "recorded_at": recorded_at or utc_now(),
        "git_branch": branch,
        "ts_context": _int(now.get("context")) if now else None,
        "ctx_growth_per_turn": _num(now.get("growth_per_turn")) if now else None,
        "clear_breakeven_turns": None if be is None or be == float("inf") else be,
        "ts_version": os.environ.get("TS_VERSION") or None,
        "rate_limits_observed_at": None,      # observed_at(), against the previous row
        "source": source,
    })
    unknown = sorted(set(drift) | {k for k, v in payload_leaves(p) if not _known(k, v)})
    if unknown:
        row["unknown_keys"] = json.dumps(unknown)
        row["raw_payload"] = json.dumps(p, ensure_ascii=False, sort_keys=True)
    return row


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

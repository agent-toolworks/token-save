#!/usr/bin/env python3
"""`ts statusline` — the whole statusline, and a history of what it showed.

Claude Code runs the statusline command on every render and hands it the
session JSON on stdin. This prints one line from it:

    ~/git/…/repo main | Opus 5.5 (high) | ctx 42k/200k 21% | 423.0K +530/t clear>1t | $1.23 | 5h 23% used(2h14m) · 7d 9% used(3d11h)

and records what it printed into the history database (history.py), because
the quota, Claude Code's cost figure and the context percentage reach nothing
else that keeps them (#41).

The `ts now` segment is computed in this process from the transcript, not by a
second `ts` call: one python start-up per render instead of two.

It runs inside a prompt, so the rules from `ts now --statusline` apply to the
whole command: it never writes to stderr, never exits non-zero, and bounds
every step that could wait -- stdin, git, the transcript read and the database
-- so a slow part drops its own segment instead of the line. The line is
printed and flushed before the row is written, so a database problem can cost
a row and never the line.

Usage: cmd_statusline.py [--explain | --schema]
"""
from __future__ import annotations

import datetime
import json
import os
import signal
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import history  # noqa: E402
import report as R  # noqa: E402

# Seconds. The wrapper bounds the whole command at 3; these keep each part well
# inside that, so the line still prints when one of them stalls.
STDIN_WAIT_S = 0.5
GIT_TIMEOUT_S = 0.5
TRANSCRIPT_BUDGET_S = 1.2

# Paths longer than this lose middle segments: "~/git/…/PROJ-388/api-gateway".
DIR_BUDGET = 28


# ---- reading the payload ---------------------------------------------------

def read_payload():
    """The session JSON on stdin, or None. Waits a beat, never forever: see
    cmd_now._session_from_stdin_json for the idle-pipe hang this avoids."""
    try:
        if sys.stdin.isatty():
            return None
        import select
        ready, _, _ = select.select([sys.stdin], [], [], STDIN_WAIT_S)
        if not ready:
            return None
        data = json.loads(sys.stdin.read() or "null")
    except Exception:
        return None
    return data if isinstance(data, dict) else None


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


def git_branch(cwd):
    # An empty cwd would ask git about this process's directory instead.
    if not cwd or not os.path.isdir(cwd):
        return None
    try:
        r = subprocess.run(["git", "-C", cwd, "rev-parse", "--abbrev-ref", "HEAD"],
                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                           stdin=subprocess.DEVNULL, timeout=GIT_TIMEOUT_S)
    except Exception:
        return None
    out = r.stdout.decode("utf-8", "replace").strip()
    return out if r.returncode == 0 and out else None


class _Budget(Exception):
    pass


def ts_now(transcript):
    """`ts now`'s reading of the transcript, or None.

    Bounded by an alarm: a transcript can be hundreds of megabytes, and the
    rest of the line must not wait on it.
    """
    if not transcript or not os.path.isfile(transcript):
        return None

    def expire(*_):
        raise _Budget()

    previous = None
    try:
        previous = signal.signal(signal.SIGALRM, expire)
        signal.setitimer(signal.ITIMER_REAL, TRANSCRIPT_BUDGET_S)
        import cmd_now
        from transcripts import parse
        sess = parse(transcript, usage_only=True)
        if not sess.turns:
            return None
        a = cmd_now.analyse(sess)
        a["segment"] = cmd_now.render_statusline(a)
        return a
    except BaseException:  # _Budget, or anything the parser trips on
        return None
    finally:
        try:
            signal.setitimer(signal.ITIMER_REAL, 0)
            if previous is not None:
                signal.signal(signal.SIGALRM, previous)
        except Exception:
            pass


def build_row(p: dict, now: dict, branch) -> dict:
    """The history row: everything the line shows, plus what identifies it."""
    cw = _get(p, "context_window") or {}
    ctx_size = _int(_get(cw, "context_window_size"))
    ctx_tokens = None
    if _num(_get(cw, "total_input_tokens")) is not None or \
       _num(_get(cw, "total_output_tokens")) is not None:
        ctx_tokens = (_int(_get(cw, "total_input_tokens")) or 0) + \
                     (_int(_get(cw, "total_output_tokens")) or 0)
    ctx_pct = _num(_get(cw, "used_percentage"))
    if ctx_pct is None and ctx_tokens is not None and ctx_size:
        ctx_pct = ctx_tokens * 100.0 / ctx_size

    transcript = _str(p.get("transcript_path"))
    session = _str(p.get("session_id"))
    if not session and transcript:
        # The transcript is named after the session.
        session = os.path.splitext(os.path.basename(transcript))[0] or None

    be = now.get("breakeven_turns") if now else None
    return {
        "recorded_at": datetime.datetime.now(datetime.timezone.utc)
                       .strftime("%Y-%m-%dT%H:%M:%SZ"),
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
    }


# ---- rendering -------------------------------------------------------------

def fmt_tokens(n: int) -> str:
    """15500 -> "16k", 1000000 -> "1M", 1500000 -> "1.5M"."""
    if n >= 1000000:
        tenths = (n + 50000) // 100000
        return "%dM" % (tenths // 10) if tenths % 10 == 0 \
            else "%d.%dM" % (tenths // 10, tenths % 10)
    if n >= 1000:
        return "%dk" % ((n + 500) // 1000)
    return "%d" % n


def fmt_reset(at, now_s=None) -> str:
    """"3d11h" / "3h12m" / "4m" until the epoch-seconds reset time."""
    if at is None:
        return ""
    import time
    delta = max(0, int(at - (now_s if now_s is not None else time.time())))
    d, h, m = delta // 86400, (delta % 86400) // 3600, (delta % 3600) // 60
    if d:
        return "%dd%02dh" % (d, h)
    if h:
        return "%dh%02dm" % (h, m)
    return "%dm" % m


def fmt_dir(path) -> str:
    """$HOME collapses to ~; anything still over DIR_BUDGET drops middle
    segments, keeping the root, its first segment and as many trailing ones as
    fit -- always at least the basename."""
    if not path:
        return ""
    home = os.environ.get("HOME")
    if home and (path == home or path.startswith(home.rstrip("/") + "/")):
        path = "~" + path[len(home.rstrip("/")):]
    if len(path) <= DIR_BUDGET:
        return path
    seg = path.split("/")
    # Fewer than two segments between root and basename: eliding only adds width.
    if len(seg) <= 3:
        return path
    head = seg[0] + "/" + seg[1]
    tail = ""
    for s in reversed(seg[2:]):
        cand = "/" + s + tail
        # +2 for the "/…" joining head to tail.
        if tail and len(head) + 2 + len(cand) > DIR_BUDGET:
            break
        tail = cand
    out = head + "/…" + tail
    return out if len(out) < len(path) else path


def _fill(pct):
    """Green while there is room, red as a window fills up."""
    if pct >= 80:
        return R.red
    if pct >= 50:
        return R.yellow
    return R.green


def _model_colour(name: str):
    """Cheap/fast -> capable/expensive."""
    n = name.lower()
    if "haiku" in n:
        return R.green
    if "sonnet" in n:
        return R.blue
    if "opus" in n:
        return R.magenta
    return str


def _context(row) -> str:
    size, used = row["ctx_size"], row["ctx_tokens"]
    if not size:
        return R.dim("ctx --")
    pct = int(round(row["ctx_pct"])) if row["ctx_pct"] is not None \
        else (used or 0) * 100 // size
    c = _fill(pct)
    return "%s %s%s %s" % (R.dim("ctx"), c(fmt_tokens(used or 0)),
                           R.dim("/" + fmt_tokens(size)), c("%d%%" % pct))


def _cost(usd) -> str:
    if usd is None:
        return R.dim("$--")
    return "$%.3f" % usd if 0 < usd < 0.01 else "$%.2f" % usd


def _quota(label, pct, resets_at) -> str:
    if pct is None:
        return R.dim("%s --" % label)
    pct = int(round(pct))
    out = "%s %s" % (R.dim(label), _fill(pct)("%d%% used" % pct))
    until = fmt_reset(resets_at)
    return out + R.dim("(%s)" % until) if until else out


def render(row: dict, now: dict) -> str:
    sep = " " + R.dim("|") + " "
    parts = []
    where = " ".join(x for x in (R.cyan(fmt_dir(row["cwd"])) if row["cwd"] else "",
                                 R.dim(row["git_branch"]) if row["git_branch"] else "") if x)
    if where:
        parts.append(where)
    model = row["model_name"] or "?"
    m = _model_colour(model)(model)
    if row["effort"]:
        e = {"low": R.dim, "medium": R.yellow}.get(row["effort"], R.red)
        m += " " + R.dim("(") + e(row["effort"]) + R.dim(")")
    parts.append(m)
    parts.append(_context(row))
    if now and now.get("segment"):
        parts.append(now["segment"])
    parts.append(_cost(row["cc_cost_usd"]))
    parts.append(_quota("5h", row["five_hour_pct"], row["five_hour_resets_at"])
                 + " " + R.dim("·") + " "
                 + _quota("7d", row["seven_day_pct"], row["seven_day_resets_at"]))
    return sep.join(parts)


# ---- main ------------------------------------------------------------------

def explain(p) -> int:
    """For a person debugging once: what was read, what would be recorded,
    where. Writes nothing."""
    if p is None:
        print("stdin:      no session JSON -- pipe in what Claude Code hands the statusline")
        print("history:    %s" % history.describe())
        return 0
    now = ts_now(_str(p.get("transcript_path")))
    row = build_row(p, now, git_branch(_str(_get(p, "workspace", "current_dir"))
                                       or _str(p.get("cwd"))))
    print("line:       %s" % render(row, now))
    if not now:
        print("ts now:     no segment -- transcript missing, empty, or slower than %.1fs"
              % TRANSCRIPT_BUDGET_S)
    print("history:    %s" % history.describe())
    if not row["session_id"]:
        print("            this payload has no session_id or transcript_path: not recorded")
    print("row:")
    for name in history.NAMES:
        print("  %-22s %s" % (name, row[name]))
    return 0


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    # A statusline is not a tty, and Claude Code renders its ANSI colour; the
    # report module's "only on a terminal" rule is for piping to a file.
    R._USE_COLOR = not os.environ.get("NO_COLOR")
    if argv[:1] == ["--schema"]:
        # For tools that read the history: no payload, nothing recorded.
        print(json.dumps(history.schema(), indent=2))
        return 0
    p = read_payload()
    if argv[:1] == ["--explain"]:
        return explain(p)
    if p is None:
        return 0
    row = now = None
    try:
        now = ts_now(_str(p.get("transcript_path")))
        row = build_row(p, now, git_branch(_str(_get(p, "workspace", "current_dir"))
                                           or _str(p.get("cwd"))))
        sys.stdout.write(render(row, now))
        sys.stdout.flush()
    except Exception:
        pass
    if row is not None:
        history.record(row)
    return 0


if __name__ == "__main__":
    try:
        code = main()
    except BaseException:
        code = 0
    # Silence even a failure to flush to a closed pipe at exit.
    try:
        sys.stdout.flush()
    except Exception:
        pass
    sys.stderr = open(os.devnull, "w")
    sys.exit(code)

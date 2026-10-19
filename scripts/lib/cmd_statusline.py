#!/usr/bin/env python3
"""`ts statusline` — the whole statusline, and a history of what it showed.

Claude Code runs the statusline command when the session changes (and every
`refreshInterval` seconds when that is set) and hands it the session JSON on
stdin. This prints one line from it:

    ~/git/…/repo main | 1a2b3c4d | Opus 5.5 (high) ✻ | ctx 21% free 158.0k | cache 97% msgs 4.2k warm 48m | 5h 23% used(2h14m) · 7d ~9% used(3d11h)

and records what it printed into the history database (history.py), because
the quota, Claude Code's cost figure and the context percentage reach nothing
else that keeps them (#41). The quota shown is the freshest reading across
sessions (limits.py), marked `~` when it is not this session's own.

The `ts now` reading (growth per turn, when clearing pays) is not shown, but
the history records it. It is computed in this process from the transcript, not
by a second `ts` call: one python start-up per render instead of two. It is
cached against the transcript's size and mtime, so an idle session re-rendering
on a timer does not parse its transcript again for the same answer.

It runs inside a prompt, so the rules from `ts now --statusline` apply to the
whole command: it never writes to stderr, never exits non-zero, and bounds
every step that could wait -- stdin, git, the transcript read and the database
-- so a slow part drops its own segment instead of the line. The line is
printed and flushed before the row is written, so a database problem can cost
a row and never the line.

Usage: cmd_statusline.py [--explain | --schema]
"""
from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import history  # noqa: E402
import limits  # noqa: E402
import report as R  # noqa: E402
from history import _get, _str  # noqa: E402

# Seconds. The wrapper bounds the whole command at 3; these keep each part well
# inside that, so the line still prints when one of them stalls.
STDIN_WAIT_S = 0.5
GIT_TIMEOUT_S = 0.5
TRANSCRIPT_BUDGET_S = 1.2

# Paths longer than this lose middle segments: "~/git/…/PROJ-388/api-gateway".
DIR_BUDGET = 28

# Prompt-cache warmth. The TTL is read from the transcript's newest cache
# write; the deepest such record across 20 real transcripts sat about 6 KB
# from the end, so a 256 KB tail is ample.
TAIL_BYTES = 256 * 1024
TTL_1H, TTL_5M = 3600, 300
WARM_HIDDEN_S = 60          # idle for less than this is mid-turn: say nothing

# `ts now` readings older than this are pruned from the cache.
CACHE_KEEP_S = 30 * 86400


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
        return cmd_now.analyse(sess)
    except BaseException:  # _Budget, or anything the parser trips on
        return None
    finally:
        try:
            signal.setitimer(signal.ITIMER_REAL, 0)
            if previous is not None:
                signal.signal(signal.SIGALRM, previous)
        except Exception:
            pass


def cache_ttl(transcript):
    """The prompt cache's TTL for this session, in seconds, or None.

    Read from the newest record that WROTE to the cache: a turn served
    entirely from cache reports both buckets as 0 and says nothing about the
    TTL, so it is skipped rather than read as 5m. Sessions normally get 1h and
    drop to 5m under overage, so assuming either is wrong some of the time.
    A write to both buckets counts as 5m: the shorter one is when the next
    turn starts paying for a cache write again.
    """
    try:
        with open(transcript, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - TAIL_BYTES))
            tail = fh.read()
    except OSError:
        return None
    lines = tail.split(b"\n")
    if size > TAIL_BYTES:
        lines = lines[1:]               # the first line is cut mid-record
    for line in reversed(lines):
        if b"ephemeral_" not in line:
            continue
        try:
            cc = _get(json.loads(line), "message", "usage", "cache_creation")
        except ValueError:
            continue
        if not isinstance(cc, dict):
            continue
        if (cc.get("ephemeral_5m_input_tokens") or 0) > 0:
            return TTL_5M
        if (cc.get("ephemeral_1h_input_tokens") or 0) > 0:
            return TTL_1H
    return None


def _state_dir():
    db = history.db_path()
    return None if db is None else os.path.dirname(db)


def _cache_file(transcript):
    d = _state_dir()
    if d is None:
        return None
    key = hashlib.sha1(transcript.encode("utf-8", "replace")).hexdigest()[:20]
    return os.path.join(d, "cache", key + ".json")


def _prune(directory, now_s):
    try:
        for name in os.listdir(directory):
            f = os.path.join(directory, name)
            if now_s - os.path.getmtime(f) > CACHE_KEEP_S:
                os.remove(f)
    except OSError:
        pass


def transcript_reading(transcript, write_cache: bool = True, need_ttl: bool = True):
    """(`ts now` reading, cache TTL, seconds idle) for the transcript.

    The first two depend only on the file's contents, so they are cached
    against its (size, mtime): a session re-rendering on a timer while it
    sits idle gets them for one stat and one small read. Idle time is the
    transcript's age, so it is measured on every render. need_ttl=False skips
    the tail read, for a payload that states the TTL itself.
    """
    if not transcript:
        return None, None, None
    try:
        st = os.stat(transcript)
    except OSError:
        return None, None, None
    idle = max(0.0, time.time() - st.st_mtime)
    key = [st.st_size, st.st_mtime_ns]
    cf = _cache_file(transcript)
    if cf:
        try:
            with open(cf, "rb") as fh:
                c = json.loads(fh.read().decode("utf-8"))
            if c.get("key") == key:
                if need_ttl and "ttl" not in c:
                    return c.get("now"), cache_ttl(transcript), idle
                return c.get("now"), c.get("ttl"), idle
        except (OSError, ValueError, AttributeError):
            pass
    now = ts_now(transcript)
    ttl = cache_ttl(transcript) if need_ttl else None
    # A missing reading can be a budget that ran out, which the next render
    # may not hit: only an answer is worth keeping.
    if cf and now is not None and write_cache:
        try:
            d = os.path.dirname(cf)
            fresh = not os.path.isdir(d)
            os.makedirs(d, mode=0o700, exist_ok=True)
            if fresh or not os.path.exists(cf):
                _prune(d, time.time())
            tmp = "%s.%d.tmp" % (cf, os.getpid())
            with open(tmp, "wb") as fh:
                entry = {"key": key, "now": now}
                if need_ttl:
                    entry["ttl"] = ttl
                fh.write(json.dumps(entry).encode("utf-8"))
            os.replace(tmp, cf)
        except Exception:
            pass
    return now, ttl, idle


def _ttl_seconds(v):
    """"5m" / "1h" / "300s" -> seconds, or None."""
    import re
    m = re.fullmatch(r"\s*(\d+)\s*([smh])\s*", v) if isinstance(v, str) else None
    return int(m.group(1)) * {"s": 1, "m": 60, "h": 3600}[m.group(2)] if m else None


def warmth(p, tail_ttl, idle, now_s=None):
    """(ttl, seconds left) for the cached prefix, or None to show nothing.

    Newer Claude Code states it in `prompt_cache`: `expires_at` is absolute,
    so the countdown is right on a timer re-render of an idle session with no
    idle-time arithmetic, and `warm: false` is COLD outright. Without them the
    TTL comes from the transcript tail (cache_ttl) and the countdown from the
    transcript's age, hidden for the first minute -- mid-turn.
    """
    now_s = time.time() if now_s is None else now_s
    pc = _get(p, "prompt_cache")
    pc = pc if isinstance(pc, dict) else {}
    ttl = _ttl_seconds(pc.get("ttl")) or tail_ttl
    if pc.get("warm") is False:
        return ttl, 0
    exp = history.to_epoch(pc.get("expires_at"))
    if exp is not None:
        return ttl, exp - now_s
    if ttl and idle is not None and idle >= WARM_HIDDEN_S:
        return ttl, ttl - idle
    return None


def payload_states_ttl(p) -> bool:
    pc = _get(p, "prompt_cache")
    return isinstance(pc, dict) and (_ttl_seconds(pc.get("ttl")) is not None
                                     or history.to_epoch(pc.get("expires_at")) is not None)


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


def fmt_tokens1(n: int) -> str:
    """One decimal, for counts read as an amount: 113600 -> "113.6k"."""
    if n >= 1000000:
        return "%.1fM" % (n / 1e6)
    if n >= 1000:
        return "%.1fk" % (n / 1e3)
    return "%d" % n


def fmt_pct(p) -> str:
    return "%d" % int(round(p)) if isinstance(p, (int, float)) else "?"


def fmt_reset(at, now_s=None) -> str:
    """"3d11h" / "3h12m" / "4m" until the epoch-seconds reset time."""
    if at is None:
        return ""
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
    """`ctx 21% free 158.0k`: the share used, and what is left in tokens."""
    size = row["ctx_size"]
    if not size:
        return R.dim("ctx --")
    used = row["ctx_input_tokens"] if row["ctx_input_tokens"] is not None \
        else row["ctx_tokens"]
    pct = int(round(row["ctx_pct"])) if row["ctx_pct"] is not None \
        else (used or 0) * 100 // size
    out = "%s %s" % (R.dim("ctx"), _fill(pct)("%d%%" % pct))
    if used is not None:
        free = max(0, size - used)
        c = R.red if free < size * 0.10 else R.yellow if free < size * 0.25 else R.green
        out += " %s %s" % (R.dim("free"), c(fmt_tokens1(free)))
    return out


def _request(row, warm) -> str:
    """The last request and the cache behind it: `cache 97% msgs 4.2k warm 48m`.

    The hit rate is of the last request alone, so a sudden drop -- a busted
    prefix -- shows on the turn it happens.
    """
    parts = []
    rd, cw, inp = (row["last_cache_read_tokens"], row["last_cache_creation_tokens"],
                   row["last_input_tokens"])
    total = (rd or 0) + (cw or 0) + (inp or 0)
    if rd is not None and total > 0:
        hit = rd * 100.0 / total
        c = R.green if hit >= 70 else R.yellow if hit >= 30 else R.red
        parts.append("%s %s" % (R.dim("cache"), c("%d%%" % int(hit))))
    if inp is not None:
        parts.append("%s %s" % (R.dim("msgs"), fmt_tokens1(inp)))
    if warm:
        ttl, left = warm
        if left <= 0:
            parts.append(R.red("COLD"))
        else:
            c = R.yellow if ttl and left <= ttl * 0.25 else R.green
            mins = int(left // 60)
            parts.append("%s %s" % (R.dim("warm"), c("%dm" % mins if mins else "<1m")))
    return " ".join(parts)


def _quota(label, pct, resets_at, borrowed=False) -> str:
    if pct is None:
        return R.dim("%s --" % label)
    pct = int(round(pct))
    mark = R.dim("~") if borrowed else ""
    value = R.red("FULL") if pct >= 100 else _fill(pct)("%d%% used" % pct)
    out = "%s %s%s" % (R.dim(label), mark, value)
    until = fmt_reset(resets_at)
    return out + R.dim("(%s)" % until) if until else out


def render(row: dict, warm=None, quota: dict = None) -> str:
    sep = " " + R.dim("|") + " "
    parts = []
    where = " ".join(x for x in (R.cyan(fmt_dir(row["cwd"])) if row["cwd"] else "",
                                 R.dim(row["git_branch"]) if row["git_branch"] else "") if x)
    if where:
        parts.append(where)
    if row["session_id"]:
        parts.append(R.dim(row["session_id"].split("-")[0]))
    model = row["model_name"] or "?"
    m = _model_colour(model)(model)
    if row["effort"]:
        e = {"low": R.dim, "medium": R.yellow}.get(row["effort"], R.red)
        m += " " + R.dim("(") + e(row["effort"]) + R.dim(")")
    if row["thinking_enabled"]:
        m += " " + R.magenta("✻")
    parts.append(m)
    parts.append(_context(row))
    req = _request(row, warm)
    if req:
        parts.append(req)
    if quota is None:
        quota = {w: (row[w + "_pct"], row[w + "_resets_at"], False)
                 for w, _ in limits.WINDOWS}
    parts.append((" " + R.dim("·") + " ").join(
        _quota(label, *quota[w]) for w, label in limits.WINDOWS))
    return sep.join(parts)


# ---- main ------------------------------------------------------------------

def collect(p, write_cache: bool = True):
    """Everything one render needs: (row, now, warm, quota)."""
    transcript = _str(p.get("transcript_path"))
    now, ttl, idle = transcript_reading(transcript, write_cache,
                                        need_ttl=not payload_states_ttl(p))
    row = history.row_from_payload(
        p, now, git_branch(_str(_get(p, "workspace", "current_dir")) or _str(p.get("cwd"))))
    row["rate_limits_observed_at"] = history.observed_at(
        row, history.previous(row["session_id"]))
    quota = limits.shown(row, limits.read())
    return row, now, warmth(p, ttl, idle), quota


def explain(p) -> int:
    """For a person debugging once: what was read, what would be recorded,
    where. Writes nothing, not even the `ts now` cache."""
    if p is None:
        print("stdin:      no session JSON -- pipe in what Claude Code hands the statusline")
        print("history:    %s" % history.describe())
        print("limits:     %s" % (limits.path() or "off"))
        return 0
    row, now, warm, quota = collect(p, write_cache=False)
    print("line:       %s" % render(row, warm, quota))
    if not now:
        print("ts now:     no reading to record -- transcript missing, empty, or slower than %.1fs"
              % TRANSCRIPT_BUDGET_S)
    if payload_states_ttl(p):
        src = "from the payload's prompt_cache"
    else:
        src = "from the transcript tail (the payload has no prompt_cache.ttl or expires_at)"
    print("warmth:     %s; %s" % (
        "ttl %ss, %ds left" % (warm[0] or "?", warm[1]) if warm else "not shown", src))
    print("history:    %s" % history.describe())
    if not row["session_id"]:
        print("            this payload has no session_id or transcript_path: not recorded")
    print("limits:     %s" % (limits.path() or "off"))
    snap = limits.read()
    for w, label in limits.WINDOWS:
        pct, _at, borrowed = quota[w]
        if borrowed:
            print("            %s shows %s%% from limits.json, observed %s by session %s,"
                  " not this payload's reading"
                  % (label, fmt_pct(pct), _get(snap, w, "observed_at"),
                     _get(snap, w, "session_id")))
    print("row:")
    for name in history.NAMES:
        print("  %-26s %s" % (name, row[name]))
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
    row = None
    try:
        row, now, warm, quota = collect(p)
        sys.stdout.write(render(row, warm, quota))
        sys.stdout.flush()
    except Exception:
        pass
    if row is not None:
        history.record(row)
        # Only this session's own reading, never the merged one it showed.
        limits.update(row)
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

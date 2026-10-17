#!/usr/bin/env python3
"""The freshest quota reading across sessions, kept in one small file.

`rate_limits` in the statusline payload comes from the session's own last API
response, and nothing refreshes it while the session sits idle. The quota is
the account's, though, so an idle window would show a number that can be far
behind what a busier session just saw. This keeps the newest observed reading
of each window in limits.json, next to the history, and the line shows that
one -- marked with a dim `~` -- when it beats the session's own.

"Newest" is by observation time (history.observed_at: when a session last saw
its own reading CHANGE), never by value. A max() across sessions is wrong: the
7d counter has been reset in place, dropping from 77% to 1% with the same
resets_at, and a max-merge held the old 77% for two days. Taking the newest
observation believes such a drop outright.

Only a session's own observations are ever written here. A value the line
borrowed from this file is never written back, or the merge would feed on its
own output.

The file is replaced atomically (temp file + os.replace): readers saw torn
JSON when renders overlapped and wrote in place. Two sessions writing at once
can lose one update; the next render of either puts it right.

`ts limits` prints it. docs/history-schema.md documents the format.
"""
from __future__ import annotations

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import history  # noqa: E402

WINDOWS = (("five_hour", "5h"), ("seven_day", "7d"))
FORMAT_VERSION = 1


def path():
    """limits.json beside the history, or None when recording is off."""
    db = history.db_path()
    return None if db is None else os.path.join(os.path.dirname(db), "limits.json")


def read(p: str = None) -> dict:
    p = p if p is not None else path()
    if p is None:
        return {}
    try:
        with open(p, "rb") as fh:
            data = json.loads(fh.read().decode("utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def own(row: dict) -> dict:
    """This session's reading of each window, as limits.json entries."""
    out = {}
    for w, _ in WINDOWS:
        pct = row.get(w + "_pct")
        if pct is None:
            continue
        out[w] = {"used_percentage": pct, "resets_at": row.get(w + "_resets_at"),
                  "observed_at": row.get("rate_limits_observed_at"),
                  "session_id": row.get("session_id")}
    return out


def _expired(e, now_s) -> bool:
    at = e.get("resets_at")
    return isinstance(at, (int, float)) and at <= now_s


def pick(a, b, now_s=None):
    """The better of two readings of one window; `a` wins ties.

    A window that has already reset says nothing about the current one. Then
    a known observation time beats an unknown one, and the later beats the
    earlier.
    """
    now_s = time.time() if now_s is None else now_s
    if not a or not b:
        return a or b
    if _expired(a, now_s) != _expired(b, now_s):
        return b if _expired(a, now_s) else a
    ta, tb = a.get("observed_at"), b.get("observed_at")
    if bool(ta) != bool(tb):
        return a if ta else b
    if ta and tb and tb > ta:
        return b
    return a


def shown(row: dict, snapshot: dict, now_s=None) -> dict:
    """What the line shows for each window: {window: (pct, resets_at, borrowed)}."""
    mine = own(row)
    out = {}
    for w, _ in WINDOWS:
        best = pick(mine.get(w), snapshot.get(w) if isinstance(snapshot.get(w), dict) else None,
                    now_s)
        if not best or best.get("used_percentage") is None:
            out[w] = (None, None, False)
            continue
        m = mine.get(w)
        borrowed = best is not m and (
            m is None or (m["used_percentage"], m["resets_at"])
            != (best.get("used_percentage"), best.get("resets_at")))
        out[w] = (best.get("used_percentage"), best.get("resets_at"), borrowed)
    return out


def update(row: dict, p: str = None, now_s=None) -> str:
    """Fold this session's own observed reading into limits.json.

    "written", "unchanged", "off" or "error: ...". Never raises: it runs after
    the line is printed, from inside a prompt.
    """
    p = p if p is not None else path()
    if p is None:
        return "off"
    if not row.get("rate_limits_observed_at"):
        return "unchanged"           # nothing observed: nothing to offer
    try:
        cur = read(p)
        new = dict(cur)
        changed = False
        for w, entry in own(row).items():
            best = pick(cur.get(w) if isinstance(cur.get(w), dict) else None, entry, now_s)
            if best is entry:
                new[w] = entry
                changed = True
        if not changed:
            return "unchanged"
        new["version"] = FORMAT_VERSION
        new["updated_at"] = history.utc_now()
        directory = os.path.dirname(p)
        os.makedirs(directory, mode=0o700, exist_ok=True)
        tmp = "%s.%d.tmp" % (p, os.getpid())
        old_umask = os.umask(0o077)
        try:
            with open(tmp, "wb") as fh:
                fh.write((json.dumps(new, indent=2, sort_keys=True) + "\n").encode("utf-8"))
        finally:
            os.umask(old_umask)
        os.replace(tmp, p)
        return "written"
    except Exception as e:
        return "error: %s" % e


def main(argv=None) -> int:
    import report as R
    argv = sys.argv[1:] if argv is None else argv
    p = path()
    data = read(p) if p else {}
    if "--json" in argv:
        from transcripts import tool_version
        out = {"tool_version": tool_version(), "path": p}
        out.update({w: data.get(w) for w, _ in WINDOWS})
        out["updated_at"] = data.get("updated_at")
        print(json.dumps(out, indent=2))
        return 0
    print(R.heading("QUOTA, FRESHEST READING ACROSS SESSIONS"))
    if p is None:
        print("  recording is off (TS_HISTORY=off): nothing is kept")
        return 0
    now_s = time.time()
    for w, label in WINDOWS:
        e = data.get(w)
        if not isinstance(e, dict):
            print(R.kv(label, R.dim("no observed reading yet")))
            continue
        import cmd_statusline as S
        until = S.fmt_reset(e.get("resets_at"), now_s)
        state = "window over, reset at %s" % e.get("resets_at") if _expired(e, now_s) \
            else "resets in %s" % until
        print(R.kv(label, "%s%% used" % S.fmt_pct(e.get("used_percentage")),
                   "%s; observed %s by %s" % (state, e.get("observed_at"),
                                              (e.get("session_id") or "?").split("-")[0])))
    print("\n  " + R.dim(p))
    return 0


if __name__ == "__main__":
    sys.exit(main())

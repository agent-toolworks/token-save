#!/usr/bin/env python3
"""Which Claude Code account a render belongs to (#51).

People run more than one account on one machine: a personal and a work
subscription, usually as two config folders (`CLAUDE_CONFIG_DIR=~/.claude-work
claude`), sometimes as `/login` to another account in the same folder. The
statusline payload names no account, and the 5h and 7d quota is the
account's, so without this a history two accounts write is one undivided pile
and limits.json lends one account's quota to the other.

The account is read from the folder's `.claude.json`, which holds the sign-in
(`oauthAccount`). It is the same file machine.py reads, found the same way:
inside CLAUDE_CONFIG_DIR when that is set, else ~/.claude.json, a sibling of
~/.claude rather than inside it.

Claude Code rewrites that file often and it runs to hundreds of KB, so the
answer is cached against its (size, mtime) beside the history, and the read is
bounded like every other step of a render: a missing, unreadable or slow file
costs the account columns, never the line.
"""
from __future__ import annotations

import json
import os
import signal

# Seconds. Parsing a 120 KB .claude.json takes a few milliseconds; this only
# runs out when the file is on something that stalls.
READ_BUDGET_S = 0.3
# Larger than any .claude.json seen; a file past this is not read at all.
MAX_BYTES = 32 * 1024 * 1024

# The history columns this fills, in COLUMNS order.
COLUMNS = ("config_dir", "account_uuid", "account_email", "org_uuid", "org_name")
_FROM = (("account_uuid", "accountUuid"), ("account_email", "emailAddress"),
         ("org_uuid", "organizationUuid"), ("org_name", "organizationName"))


class _Budget(Exception):
    pass


def config_dir() -> str:
    """The Claude Code folder in effect: CLAUDE_CONFIG_DIR, else ~/.claude."""
    cfg = os.environ.get("CLAUDE_CONFIG_DIR")
    return os.path.expanduser(cfg) if cfg else os.path.expanduser("~/.claude")


def claude_json(folder: str = None) -> str:
    """The `.claude.json` that holds ``folder``'s sign-in.

    With no folder, the one in effect, by machine.py's rule. A named folder
    keeps its own inside it; the default ~/.claude keeps it beside, at
    ~/.claude.json, unless a copy sits inside.
    """
    if folder is None:
        if os.environ.get("CLAUDE_CONFIG_DIR"):
            return os.path.join(config_dir(), ".claude.json")
        return os.path.expanduser("~/.claude.json")
    inside = os.path.join(folder, ".claude.json")
    default = os.path.expanduser("~/.claude")
    if not os.path.exists(inside) and \
            os.path.realpath(folder) == os.path.realpath(default):
        return os.path.expanduser("~/.claude.json")
    return inside


def _parse(path: str) -> dict:
    """The account fields from one .claude.json; {} when it has none."""
    with open(path, "rb") as fh:
        raw = fh.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        return {}
    data = json.loads(raw.decode("utf-8-sig", "replace"))
    oa = data.get("oauthAccount") if isinstance(data, dict) else None
    if not isinstance(oa, dict):
        return {}
    out = {}
    for col, key in _FROM:
        v = oa.get(key)
        out[col] = v if isinstance(v, str) and v else None
    return out if out.get("account_uuid") else {}


def _bounded_parse(path: str):
    """_parse() under an alarm, or None when it failed or ran out of time.
    Off the main thread (the hub's server, say) there is no alarm to set,
    and it reads unbounded."""
    def expire(*_):
        raise _Budget()

    previous = None
    try:
        try:
            previous = signal.signal(signal.SIGALRM, expire)
            signal.setitimer(signal.ITIMER_REAL, READ_BUDGET_S)
        except (ValueError, AttributeError, OSError):
            previous = None
        return _parse(path)
    except BaseException:
        return None
    finally:
        try:
            signal.setitimer(signal.ITIMER_REAL, 0)
            if previous is not None:
                signal.signal(signal.SIGALRM, previous)
        except Exception:
            pass


def read(folder: str = None, cache: str = None) -> dict:
    """The account signed in to ``folder`` (default: the one in effect), as
    {account_uuid, account_email, org_uuid, org_name}; {} when none is, or
    the file cannot be read.

    ``cache`` is a file to keep the answer in against the .claude.json's
    (path, size, mtime). Only an answer read in full is kept: a budget that
    ran out says nothing about the file.
    """
    path = claude_json(folder)
    try:
        st = os.stat(path)
    except OSError:
        return {}
    key = [path, st.st_size, st.st_mtime_ns]
    if cache:
        try:
            with open(cache, "rb") as fh:
                c = json.loads(fh.read().decode("utf-8"))
            if c.get("key") == key and isinstance(c.get("account"), dict):
                return c["account"]
        except (OSError, ValueError, AttributeError):
            pass
    got = _bounded_parse(path)
    if got is None:
        return {}
    if cache:
        try:
            os.makedirs(os.path.dirname(cache), mode=0o700, exist_ok=True)
            tmp = "%s.%d.tmp" % (cache, os.getpid())
            old_umask = os.umask(0o077)
            try:
                with open(tmp, "wb") as fh:
                    fh.write(json.dumps({"key": key, "account": got}).encode("utf-8"))
            finally:
                os.umask(old_umask)
            os.replace(tmp, cache)
        except Exception:
            pass
    return got


def columns(cache: str = None) -> dict:
    """The history row's account columns for a render: the folder in effect,
    and who is signed in to it (NULL when nobody is, by an account)."""
    acct = read(cache=cache)
    out = {"config_dir": config_dir()}
    for col, _ in _FROM:
        out[col] = acct.get(col)
    return out

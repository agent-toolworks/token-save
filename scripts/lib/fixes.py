#!/usr/bin/env python3
"""`ts fixes` — the few changes safe to make mechanically.

Most findings in advise.py are behavioural: no script can shorten your sessions
for you. The ones here edit a file, and every one of them:

  * shows the exact diff before touching anything (`--dry-run` is the default),
  * writes a timestamped backup next to the original,
  * is reversible with `ts fixes revert <id>`,
  * is idempotent — applying twice changes nothing the second time.

A fix that cannot meet all four does not belong here; it belongs in the
`actions` list of a Finding, where a human does it deliberately.

Usage: fixes.py list
       fixes.py show   <id>
       fixes.py apply  <id> [--yes]
       fixes.py revert <id>
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import report as R  # noqa: E402
from machine import CLAUDE_HOME  # noqa: E402

MARKER_BEGIN = "<!-- ts:begin "
MARKER_END = "<!-- ts:end "

TERSE_BLOCK = """\
## Response style

- Answer at the length the question deserves; no preamble, no recap of what I
  just showed you, no summary of what you are about to do.
- Report a diff or the changed lines, not whole rewritten files.
- When a command's output already answers the question, do not restate it.
"""


def _backup(path: str) -> str:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dst = "%s.ts-backup-%s" % (path, stamp)
    shutil.copy2(path, dst)
    return dst


def _write_error(path: str, exc: OSError) -> str:
    """A failure message in the house style, not a stack trace.

    Everything else here says what happened and what it means -- "does not
    parse — fix it by hand first", "no change: already applied". A read-only
    target got a Python traceback out of shutil instead, mid-write, which
    leaves a reader with no idea whether their config survived. So the message
    says the one thing that matters: nothing was modified.
    """
    detail = exc.strerror or str(exc)
    try:
        detail += " (mode %s)" % oct(os.stat(path).st_mode & 0o777)[2:]
    except OSError:
        pass
    return "cannot write %s: %s — nothing was modified" % (path, detail)


def _backup_and_write(path: str, text: str, append: bool = False):
    """Back up and write as one operation. (ok, backup_path_or_message).

    The backup used to be taken before a write that could still fail, so a
    read-only target left an orphan copy with nothing to pair with -- and each
    retry left another. Backups exist so a change can be undone; a directory
    of backups from operations that never happened makes the one that matters
    harder to find. A pre-flight writability check would race, so the backup
    is removed on failure instead.

    Failure can come from the backup as easily as from the write: copy2
    propagates a 444 mode to the copy, so a second attempt cannot overwrite
    it. Both are inside the guard.
    """
    backup = None
    try:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        if os.path.isfile(path):
            backup = _backup(path)
        if append:
            with open(path, "ab") as fh:
                fh.write(text.encode("utf-8"))
        else:
            _write(path, text)
        return True, backup
    except OSError as exc:
        if backup:
            try:
                os.remove(backup)
            except OSError:
                pass
        return False, _write_error(path, exc)


def _latest_backup(path: str):
    d = os.path.dirname(path) or "."
    base = os.path.basename(path) + ".ts-backup-"
    try:
        cands = sorted(n for n in os.listdir(d) if n.startswith(base))
    except OSError:
        return None
    return os.path.join(d, cands[-1]) if cands else None


def _read(path: str) -> str:
    # Bytes in, decoded explicitly. Text mode brought two platform-conditional
    # behaviours that only differ off POSIX, so neither CI runner could see
    # them: universal-newline translation (which turned a one-key change into
    # a whole-file diff on Windows, the failure in-place editing exists to
    # remove) and a locale-default encoding, which is cp1252 on Windows and
    # would mangle a UTF-8 config on the way through.
    #
    # Decoded as utf-8, NOT utf-8-sig: a BOM is content to preserve here, and
    # _split_bom holds it aside for parsing and puts it back on write.
    # machine.py reads the same files with utf-8-sig because it only parses
    # them and never writes them back.
    try:
        with open(path, "rb") as fh:
            return fh.read().decode("utf-8", errors="replace")
    except OSError:
        return ""


def _split_bom(text: str):
    """Separate a leading byte-order mark from the rest.

    A BOM is content to preserve, not noise to strip. Windows editors and some
    PowerShell redirections write one by default, and json.loads chokes on it,
    so it is held aside for parsing and put back on write. Stripping it would
    silently rewrite the file's first bytes -- the same class of unasked-for
    change as reindenting it, which is what this module exists not to do.
    """
    return ("\ufeff", text[1:]) if text.startswith("\ufeff") else ("", text)


def _newline(text: str) -> str:
    """The line ending this file already uses.

    Ties and files with no line ending at all go to "\n": there is no
    convention to preserve, so the platform-neutral one is used rather than
    guessing from the operating system, which would make the same file take
    different fixes on different machines.
    """
    crlf = text.count("\r\n")
    return "\r\n" if crlf > (text.count("\n") - crlf) else "\n"


def _write(path: str, text: str) -> None:
    """Write bytes through unchanged -- literally, now, rather than by argument.

    This used to be text mode with newline="", which is correct but
    platform-conditional: Python only translates when newline is None, and on
    POSIX os.linesep is already "\n", so dropping the argument was a no-op on
    both CI runners and doubled the CR of every line on Windows. A test that
    cannot fail on any platform CI runs is not evidence, so the dependency is
    removed rather than guarded.
    """
    with open(path, "wb") as fh:
        fh.write(text.encode("utf-8"))


# ---------------------------------------------------------------------------
# terse-output: append a response-style block to CLAUDE.md
# ---------------------------------------------------------------------------

def _terse_target() -> str:
    return os.path.join(CLAUDE_HOME, "CLAUDE.md")


def terse_show():
    path = _terse_target()
    cur = _read(path)
    applied = (MARKER_BEGIN + "terse-output") in cur
    block = "%sterse-output -->\n%s%sterse-output -->\n" % (
        MARKER_BEGIN, TERSE_BLOCK, MARKER_END)
    return {
        "id": "terse-output",
        "target": path,
        "applied": applied,
        "exists": os.path.isfile(path),
        "diff": "" if applied else "\n".join("+ " + l for l in block.splitlines()),
        "why": ("Output is billed at 5x input. A standing instruction costs a few "
                "dozen tokens in the preamble and trims every reply after it."),
    }


def terse_apply():
    path = _terse_target()
    cur = _read(path)
    if (MARKER_BEGIN + "terse-output") in cur:
        return False, "already applied"
    block = "\n%sterse-output -->\n%s%sterse-output -->\n" % (
        MARKER_BEGIN, TERSE_BLOCK, MARKER_END)
    # A file with no trailing newline must not acquire one, or apply+revert is
    # not a no-op and shows up as a diff. Terminate its last line (otherwise
    # the marker lands mid-sentence) but leave the block itself unterminated,
    # so the file's trailing-newline state still mirrors the original and
    # revert can put it back exactly.
    if cur and not cur.endswith("\n"):
        block = "\n" + block[:-1]
    # Because this fix APPENDS rather than rewrites, getting this wrong left
    # the original lines CRLF and only the new block LF -- one file with two
    # conventions, which is harder to notice than a clean conversion and is
    # what several linters and diff tools complain about.
    nl = _newline(cur)
    if nl != "\n":
        block = block.replace("\n", nl)
    ok, res = _backup_and_write(path, block, append=True)
    if not ok:
        return False, res
    return True, "appended to %s%s" % (
        path, " (backup: %s)" % res if res else "")


def terse_revert():
    path = _terse_target()
    cur = _read(path)
    begin = MARKER_BEGIN + "terse-output -->"
    end = MARKER_END + "terse-output -->"
    if begin not in cur:
        return False, "not applied"
    trailing = cur.endswith("\n")
    nl = _newline(cur)
    i = cur.index(begin)
    j = cur.index(end) + len(end)
    # Take back exactly what apply() wrote: the block's own terminator and the
    # blank-line separator in front of it -- one line ending each, whatever
    # this file's line ending is. Anything else in the file -- runs of blank
    # lines the author put there -- is not ours to normalise.
    if cur[j:j + len(nl)] == nl:
        j += len(nl)
    if i >= len(nl) and cur[i - len(nl):i] == nl:
        i -= len(nl)
    new = cur[:i] + cur[j:]
    if not trailing and not cur[j:]:
        new = new.rstrip("\r\n")   # apply() also had to terminate the last line
    ok, res = _backup_and_write(path, new)
    if not ok:
        return False, res
    return True, "removed the block from %s" % path

# ---------------------------------------------------------------------------
# tool-search: keep MCP tool deferral on behind a custom base URL
# ---------------------------------------------------------------------------

# settings.json is the user's own file: hand-written, often version-controlled,
# and frequently full of prose -- `permissions` rules and policy blocks get
# written in sentences, with typographic punctuation. Round-tripping it through
# json.dump() rewrites all of that: ensure_ascii escapes every em-dash and
# indent= re-indents every line, so a one-key change lands as a diff of lines
# nobody touched. That is worse than cosmetic. `ts fixes` is offered as the
# reviewable option -- backup, diff, revert -- and a large unexplained diff
# from an automated editor of your config teaches you to stop reading them.
#
# So the key is spliced in as text. json still does the parsing, both to
# validate the file and to locate the edit; bytes outside the edit are never
# re-serialised.

_DECODER = json.JSONDecoder()


def _skip_ws(s: str, i: int) -> int:
    while i < len(s) and s[i] in " \t\r\n":
        i += 1
    return i


def _members(s: str, start: int):
    """Walk the object at s[start], yielding (key, key_start, val_start, val_end).

    Offsets into s, so a caller can rewrite one member and leave every other
    byte of the file alone.
    """
    i = _skip_ws(s, start)
    if i >= len(s) or s[i] != "{":
        return
    i += 1
    while True:
        i = _skip_ws(s, i)
        if i >= len(s) or s[i] != '"':
            return
        key_start = i
        key, i = json.decoder.scanstring(s, i + 1)
        i = _skip_ws(s, _skip_ws(s, i) + 1)      # past the colon
        val_start = i
        _v, i = _DECODER.raw_decode(s, i)
        yield key, key_start, val_start, i
        i = _skip_ws(s, i)
        if i < len(s) and s[i] == ",":
            i += 1


def _find(s: str, start: int, key: str):
    for rec in _members(s, start):
        if rec[0] == key:
            return rec
    return None


def _line_indent(s: str, idx: int):
    """The whitespace before idx on its line, or None if idx is not the first
    thing on it -- i.e. the object is written inline and should stay inline."""
    bol = s.rfind("\n", 0, idx) + 1
    prefix = s[bol:idx]
    return prefix if prefix.strip() == "" else None


def _member_indent(s: str, obj_start: int):
    members = list(_members(s, obj_start))
    return _line_indent(s, members[0][1]) if members else None


def _indent_unit(s: str) -> str:
    """The file's own indent step, taken from its first indented line."""
    for line in s.splitlines():
        stripped = line.lstrip(" \t")
        if stripped and stripped != line:
            return line[:len(line) - len(stripped)]
    return "  "


def _insert_member(s: str, obj_start: int, key: str, value_src: str,
                   nl: str = "\n") -> str:
    """Splice `"key": value_src` in as the last member of an object.

    `nl` is the file's own line ending, so the inserted line matches the lines
    around it rather than introducing a second convention.
    """
    obj_start = _skip_ws(s, obj_start)
    _obj, obj_end = _DECODER.raw_decode(s, obj_start)
    close = s.rindex("}", obj_start, obj_end)
    members = list(_members(s, obj_start))
    entry = "%s: %s" % (json.dumps(key, ensure_ascii=False), value_src)
    if not members:
        return s[:obj_start + 1] + entry + s[close:]
    indent = _line_indent(s, members[0][1])
    end = members[-1][3]
    sep = ", " if indent is None else "," + nl + indent
    return s[:end] + sep + entry + s[end:]


def _remove_member(s: str, obj_start: int, key: str) -> str:
    """Cut one member out of an object, taking exactly one comma with it."""
    members = list(_members(s, obj_start))
    idx = next((n for n, m in enumerate(members) if m[0] == key), None)
    if idx is None:
        return s
    _k, key_start, _vs, val_end = members[idx]
    if len(members) == 1:
        obj_start = _skip_ws(s, obj_start)
        _obj, obj_end = _DECODER.raw_decode(s, obj_start)
        close = s.rindex("}", obj_start, obj_end)
        return s[:obj_start + 1] + s[close:]                 # -> {}
    if idx + 1 < len(members):
        return s[:key_start] + s[members[idx + 1][1]:]       # up to the next key
    return s[:members[idx - 1][3]] + s[val_end:]             # last: eat the comma before


def _settings_path() -> str:
    return os.path.join(CLAUDE_HOME, "settings.json")


def _load_settings(path: str):
    """(data, ok, error). `error` names what went wrong, for the message."""
    if not os.path.isfile(path):
        return {}, True, ""
    _bom, body = _split_bom(_read(path))
    if not body.strip():
        return {}, True, ""
    try:
        return json.loads(body), True, ""
    except Exception as exc:
        return None, False, str(exc)


def toolsearch_show():
    path = _settings_path()
    data, ok, err = _load_settings(path)
    if not ok:
        # Say what is wrong, not merely that something is. "fix it by hand
        # first" described no observable defect to anyone whose file was
        # rejected for an invisible reason.
        return {"id": "tool-search", "target": path, "applied": False,
                "exists": True, "diff": "",
                "why": "settings.json is not valid JSON; refusing to touch it.",
                "blocked": "settings.json does not parse (%s) — a byte-order "
                           "mark is handled, so this is a real syntax error"
                           % err}
    cur = (data.get("env") or {}).get("ENABLE_TOOL_SEARCH")
    applied = str(cur).lower() in ("true", "1", "on")
    return {
        "id": "tool-search",
        "target": path,
        "applied": applied,
        "exists": os.path.isfile(path),
        "diff": "" if applied else '  env.ENABLE_TOOL_SEARCH: %s -> "true"' % (
            json.dumps(cur) if cur is not None else "(unset)"),
        "why": ("A custom ANTHROPIC_BASE_URL turns off on-demand tool loading, "
                "which puts every MCP tool definition back into every preamble."),
    }


def toolsearch_apply():
    path = _settings_path()
    bom, raw = _split_bom(_read(path) if os.path.isfile(path) else "")
    data, ok, err = _load_settings(path)
    if not ok:
        return False, ("settings.json does not parse (%s) — refusing to "
                       "overwrite it" % err)
    env = data.get("env")
    if env is not None and not isinstance(env, dict):
        return False, '"env" is not an object — refusing to touch it'
    if str((env or {}).get("ENABLE_TOOL_SEARCH")).lower() in ("true", "1", "on"):
        return False, "already applied"

    unit = _indent_unit(raw) if raw.strip() else "  "
    nl = _newline(raw)

    if not raw.strip():
        new = ('{%(nl)s%(u)s"env": {%(nl)s%(u)s%(u)s'
               '"ENABLE_TOOL_SEARCH": "true"%(nl)s%(u)s}%(nl)s}%(nl)s'
               % {"nl": nl, "u": unit})
    else:
        top = _skip_ws(raw, 0)
        rec = _find(raw, top, "env")
        if rec is None:
            indent = _member_indent(raw, top)
            if indent is None:
                value_src = '{"ENABLE_TOOL_SEARCH": "true"}'
            else:
                value_src = '{%s%s%s"ENABLE_TOOL_SEARCH": "true"%s%s}' % (
                    nl, indent, unit, nl, indent)
            new = _insert_member(raw, top, "env", value_src, nl)
        else:
            inner = _find(raw, rec[2], "ENABLE_TOOL_SEARCH")
            if inner is None:
                new = _insert_member(raw, rec[2], "ENABLE_TOOL_SEARCH",
                                     '"true"', nl)
            else:
                new = raw[:inner[2]] + '"true"' + raw[inner[3]:]

    ok, res = _backup_and_write(path, bom + new)
    if not ok:
        return False, res
    return True, "set env.ENABLE_TOOL_SEARCH=true in %s%s" % (
        path, " (backup: %s)" % res if res else "")


def toolsearch_revert():
    path = _settings_path()
    bom, raw = _split_bom(_read(path) if os.path.isfile(path) else "")
    data, ok, err = _load_settings(path)
    if not ok:
        return False, "settings.json does not parse (%s)" % err
    env = data.get("env")
    if not isinstance(env, dict) or "ENABLE_TOOL_SEARCH" not in env:
        return False, "not applied"
    top = _skip_ws(raw, 0)
    if len(env) == 1:
        new = _remove_member(raw, top, "env")   # the key was all env held
    else:
        new = _remove_member(raw, _find(raw, top, "env")[2], "ENABLE_TOOL_SEARCH")
    ok, res = _backup_and_write(path, bom + new)
    if not ok:
        return False, res
    return True, "removed env.ENABLE_TOOL_SEARCH from %s" % path

FIXES = {
    "terse-output": (terse_show, terse_apply, terse_revert),
    "tool-search": (toolsearch_show, toolsearch_apply, toolsearch_revert),
}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="ts fixes", description=__doc__)
    ap.add_argument("action", choices=["list", "show", "apply", "revert"])
    ap.add_argument("id", nargs="?")
    ap.add_argument("--yes", action="store_true", help="apply without confirming")
    args = ap.parse_args(argv)

    if args.action == "list":
        print(R.heading("MECHANICAL FIXES"))
        rows = []
        for fid, (show, _a, _r) in sorted(FIXES.items()):
            info = show()
            rows.append([fid,
                         R.green("applied") if info["applied"] else R.dim("not applied"),
                         info["target"].replace(os.path.expanduser("~"), "~")])
        print(R.table(rows, ["id", "state", "target"], "lll"))
        print("\n  " + R.dim("everything else in `ts advise` is behavioural and "
                             "deliberately not automated"))
        return 0

    if not args.id or args.id not in FIXES:
        sys.stderr.write("unknown fix %r; try: ts fixes list\n" % (args.id,))
        return 2

    show, apply_, revert = FIXES[args.id]

    if args.action == "show":
        info = show()
        print(R.heading(args.id))
        print(R.kv("target", info["target"]))
        print(R.kv("state", "applied" if info["applied"] else "not applied"))
        print("\n  " + info["why"])
        if info.get("blocked"):
            print("\n  " + R.red("blocked: ") + info["blocked"])
        elif info["diff"]:
            print("\n" + info["diff"])
        return 0

    if args.action == "apply":
        info = show()
        if info.get("blocked"):
            sys.stderr.write("blocked: %s\n" % info["blocked"])
            return 1
        if not args.yes:
            print(R.heading("would apply: " + args.id))
            print(R.kv("target", info["target"]))
            print("\n" + (info["diff"] or "  (nothing to change)"))
            print("\n  " + R.dim("re-run with --yes to write it"))
            return 0
        ok, msg = apply_()
        print(("  " + R.green("applied: ") if ok else "  " + R.dim("no change: ")) + msg)
        return 0 if ok else 1

    ok, msg = revert()
    print(("  " + R.green("reverted: ") if ok else "  " + R.dim("no change: ")) + msg)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

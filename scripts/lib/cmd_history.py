#!/usr/bin/env python3
"""`ts history import` — bring an existing statusline history into history.db.

For moving off another recorder without losing what it kept. The input is
JSONL, one statusline payload per line with the time it was recorded:

    {"recorded_at": "2026-08-14T09:12:03+03:00", "payload": { ...the JSON Claude Code handed the statusline... }}

Every line goes through the same row-building and the same dedupe as a live
render, so the importer holds no column mapping of its own: any recorder that
kept the payload can export to this shape. The `ts now` columns and
git_branch stay NULL (nothing here can know them after the fact), and
`source` is "import" so the rows can be told apart.

recorded_at may carry any UTC offset and is stored in UTC. One without an
offset is refused rather than guessed at. Re-running is safe: a row whose
(session_id, recorded_at) is already there is skipped, so the import can be
repeated while a switch-over is in progress. Rows are stored to the second,
so two lines for one session within the same second keep the first.

Usage: cmd_history.py import FILE|- [--dry-run]
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import history  # noqa: E402
import report as R  # noqa: E402


def to_utc(stamp) -> str:
    """ISO 8601 with any offset -> "YYYY-MM-DDTHH:MM:SSZ". ValueError otherwise."""
    if not isinstance(stamp, str):
        raise ValueError("recorded_at is missing")
    s = stamp.strip()
    if s.endswith(("Z", "z")):
        s = s[:-1] + "+00:00"
    t = datetime.datetime.fromisoformat(s)
    if t.tzinfo is None:
        raise ValueError("recorded_at %r has no UTC offset" % stamp)
    return t.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def read_lines(fh):
    """(rows, problems): rows ready for history.import_rows, and one message
    per line that could not be used."""
    rows, problems = [], []
    for n, line in enumerate(fh, 1):
        line = line.strip()
        if not line:
            continue
        try:
            try:
                rec = json.loads(line)
            except ValueError as e:
                raise ValueError("not JSON (%s)" % e)
            if not isinstance(rec, dict) or not isinstance(rec.get("payload"), dict):
                raise ValueError("not an object with a \"payload\" object")
            row = history.row_from_payload(rec["payload"], recorded_at=to_utc(rec.get("recorded_at")),
                                           source="import")
            if not row["session_id"]:
                raise ValueError("payload has no session_id or transcript_path")
        except ValueError as e:
            problems.append("line %d: %s" % (n, e))
            continue
        rows.append(row)
    return rows, problems


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="ts history", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("action", choices=["import"])
    ap.add_argument("file", help="JSONL to import, or - for stdin")
    ap.add_argument("--dry-run", action="store_true",
                    help="report what would be written, and write nothing")
    args = ap.parse_args(argv)

    try:
        if args.file == "-":
            rows, problems = read_lines(sys.stdin)
        else:
            with open(args.file, encoding="utf-8") as fh:
                rows, problems = read_lines(fh)
    except OSError as e:
        sys.stderr.write("ts history: cannot read %s: %s\n" % (args.file, e.strerror or e))
        return 1
    try:
        counts = history.import_rows(rows, dry_run=args.dry_run)
    except Exception as e:
        sys.stderr.write("ts history: %s\n" % e)
        return 1

    print(R.heading("IMPORT%s" % (" (dry run: nothing written)" if args.dry_run else "")))
    print(R.kv("into", history.db_path()))
    print(R.kv("lines read", len(rows) + len(problems)))
    print(R.kv("rows written", counts["written"]))
    print(R.kv("already there", counts["already-there"], "same session and second"))
    print(R.kv("unchanged", counts["unchanged"], "repeats the session's previous row"))
    print(R.kv("refused", len(problems)))
    for msg in problems[:20]:
        print("    " + R.dim(msg))
    if len(problems) > 20:
        print("    " + R.dim("... and %d more" % (len(problems) - 20)))
    return 1 if problems and not rows else 0


if __name__ == "__main__":
    sys.exit(main())

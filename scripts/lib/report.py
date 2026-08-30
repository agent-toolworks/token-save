#!/usr/bin/env python3
"""Terminal formatting. Colour only when a human is looking at a real terminal.

Kept separate from the analysis so that piping `ts audit` into a file gives
plain text, and so that a future JSON emitter does not have to unpick ANSI
codes from numbers.
"""
from __future__ import annotations

import os
import sys

_USE_COLOR = (
    sys.stdout.isatty()
    and os.environ.get("TERM") not in (None, "", "dumb")
    and not os.environ.get("NO_COLOR")
)


def _c(code: str):
    def wrap(text) -> str:
        return "\033[%sm%s\033[0m" % (code, text) if _USE_COLOR else str(text)
    return wrap


dim = _c("2")
bold = _c("1")
red = _c("31")
green = _c("32")
yellow = _c("33")
blue = _c("34")
magenta = _c("35")
cyan = _c("36")


def rule(width: int = 74) -> str:
    return dim("-" * width)


def heading(text: str) -> str:
    return "\n" + bold(text) + "\n" + rule()


def kv(label: str, value, note: str = "", width: int = 30) -> str:
    line = "  %-*s %s" % (width, label, value)
    return line + ("  " + dim(note) if note else "")


def bar(fraction: float, width: int = 24, char: str = "#") -> str:
    """A fixed-width bar. Never longer than `width`, never negative."""
    n = max(0, min(width, int(round(fraction * width))))
    return char * n + dim("." * (width - n))


def table(rows: list, headers: list, aligns: str = None, indent: str = "  ") -> str:
    """Render rows as a fixed-width table. `aligns` is one char per column:
    'l' left, 'r' right. Numbers should be pre-formatted by the caller so the
    table never has to guess at units."""
    if not rows:
        return indent + dim("(nothing)")
    cols = len(headers)
    aligns = aligns or ("l" * cols)
    cells = [[str(c) for c in r] for r in rows]
    widths = [len(h) for h in headers]
    for r in cells:
        for i, c in enumerate(r[:cols]):
            widths[i] = max(widths[i], len(c))
    out = []
    head = indent + "  ".join(
        h.ljust(widths[i]) if aligns[i] == "l" else h.rjust(widths[i])
        for i, h in enumerate(headers))
    out.append(dim(head))
    for r in cells:
        out.append(indent + "  ".join(
            (r[i] if i < len(r) else "").ljust(widths[i]) if aligns[i] == "l"
            else (r[i] if i < len(r) else "").rjust(widths[i])
            for i in range(cols)))
    return "\n".join(out)


def severity(level: str) -> str:
    """Colour a severity word consistently everywhere it appears."""
    return {
        "high": red("HIGH"),
        "medium": yellow("MED "),
        "low": blue("LOW "),
        "none": dim("--  "),
    }.get(level, dim(level))


def confidence(label: str) -> str:
    """How much to trust a number. Mirrors `cs`'s evidence labelling: a reader
    should never have to guess whether a figure was measured or inferred."""
    return {
        "measured": green("measured"),
        "estimated": yellow("estimated"),
        "heuristic": dim("heuristic"),
    }.get(label, dim(label))

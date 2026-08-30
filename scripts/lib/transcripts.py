#!/usr/bin/env python3
"""Read Claude Code transcripts and account for what they cost.

Two kinds of number come out of here and they must never be confused:

  BILLED    — read verbatim from the ``usage`` block the provider wrote into
              every assistant record. Exact. Nothing is estimated.
  CONTENT   — the size of the text that accumulated in the conversation.
              Estimated, because the transcript stores text, not tokens.

The whole point of this tool is the RATIO between them, so the honest thing is
to keep the exact side exact and label the estimated side as estimated. Every
report prints which tokenizer produced the CONTENT column.

Estimator: ``len(text) / 3.6``. Calibrated against ``tiktoken`` cl100k_base over
a random sample of real transcript content — aggregate error +1.3%, which is
well inside the precision anyone acts on. When ``tiktoken`` is importable it is
used instead and the report says so. The divisor is deliberately a single
constant rather than a clever blend: blends scored WORSE here (-2.2% at best,
-19% at worst) because they overfit whitespace, and a constant is auditable.

Images are counted at Anthropic's documented (w*h)/750, capped at 1600, read
from the actual image header rather than assumed — a base64 payload is ~4x its
token cost, and counting the base64 as text overstates a screenshot by ~40x.
That mistake is the single easiest way to draw the wrong conclusion from a
transcript, so the sniffer is here rather than in a caller.
"""
from __future__ import annotations

import base64
import glob
import json
import os
import re
import struct
from dataclasses import dataclass, field

# --------------------------------------------------------------------------
# tokenizing
# --------------------------------------------------------------------------

_CHARS_PER_TOKEN = 3.6
_ENC = None
_ENC_NAME = "estimate(len/3.6)"

try:  # pragma: no cover - depends on the host having tiktoken
    import tiktoken as _tiktoken

    _ENC = _tiktoken.get_encoding("cl100k_base")
    _ENC_NAME = "tiktoken(cl100k_base)"
except Exception:
    _ENC = None


def tokenizer_name() -> str:
    """Which counter produced the CONTENT numbers. Printed in every report."""
    return _ENC_NAME


def toks(text) -> int:
    """Token count for a string. Estimated unless tiktoken is installed."""
    if not text:
        return 0
    if not isinstance(text, str):
        text = json.dumps(text, default=str)
    if _ENC is not None:
        try:
            return len(_ENC.encode(text, disallowed_special=()))
        except Exception:
            pass
    return int(len(text) / _CHARS_PER_TOKEN)


# --------------------------------------------------------------------------
# images
# --------------------------------------------------------------------------

_IMAGE_TOKEN_CAP = 1600
_IMAGE_TOKEN_FALLBACK = 800  # a screenshot-sized image when the header won't parse


def image_dimensions(raw: bytes):
    """(width, height) from a PNG/JPEG/WebP/GIF header, or None.

    Deliberately header-only: decoding the pixels would need a dependency, and
    every format below puts the dimensions in the first few dozen bytes.
    """
    try:
        if raw[:8] == b"\x89PNG\r\n\x1a\n":
            w, h = struct.unpack(">II", raw[16:24])
            return w, h
        if raw[:3] == b"\xff\xd8\xff":  # JPEG: walk the segment chain to a SOFn
            i = 2
            while i < len(raw) - 9:
                if raw[i] != 0xFF:
                    i += 1
                    continue
                marker = raw[i + 1]
                if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6,
                              0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                    h, w = struct.unpack(">HH", raw[i + 5:i + 9])
                    return w, h
                i += 2 + struct.unpack(">H", raw[i + 2:i + 4])[0]
        if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
            fmt = raw[12:16]
            if fmt == b"VP8X":
                return (int.from_bytes(raw[24:27], "little") + 1,
                        int.from_bytes(raw[27:30], "little") + 1)
            if fmt == b"VP8L":
                n = int.from_bytes(raw[21:25], "little")
                return (n & 0x3FFF) + 1, ((n >> 14) & 0x3FFF) + 1
            if fmt == b"VP8 ":
                return (struct.unpack("<H", raw[26:28])[0] & 0x3FFF,
                        struct.unpack("<H", raw[28:30])[0] & 0x3FFF)
        if raw[:6] in (b"GIF87a", b"GIF89a"):
            w, h = struct.unpack("<HH", raw[6:10])
            return w, h
    except Exception:
        pass
    return None


def image_tokens(b64_data: str) -> int:
    """Token cost of an inline image block, from its real pixel dimensions."""
    if not b64_data:
        return _IMAGE_TOKEN_FALLBACK
    head = b64_data[:4096]
    head += "=" * (-len(head) % 4)
    try:
        raw = base64.b64decode(head, validate=False)
    except Exception:
        return _IMAGE_TOKEN_FALLBACK
    wh = image_dimensions(raw)
    if not wh or not wh[0] or not wh[1]:
        return _IMAGE_TOKEN_FALLBACK
    return min(int(wh[0] * wh[1] / 750), _IMAGE_TOKEN_CAP)


# --------------------------------------------------------------------------
# the model
# --------------------------------------------------------------------------

# Anthropic price multipliers relative to one base input token. Cache reads are
# a tenth of an input token and cache writes a quarter more than one, so a token
# that merely SITS in context is ~12x cheaper per turn than one being written —
# which is exactly why persistence, not size, is what these reports rank by.
PRICE = {"cache_read": 0.10, "cache_write": 1.25, "input": 1.00, "output": 5.00}

# Bucket names are stable public API: fixes/ and the report both key off them.
BUCKET_TOOL_IN = "tool call inputs"
BUCKET_TOOL_TEXT = "tool results (text)"
BUCKET_TOOL_IMG = "tool results (images)"
BUCKET_ASSISTANT = "assistant text"
BUCKET_THINKING = "assistant thinking"
BUCKET_USER = "user prompts"
BUCKET_ATTACH = "attachments"
BUCKET_SYSTEM = "system records"


@dataclass
class Session:
    path: str
    project: str
    session_id: str
    turns: int = 0
    billed: dict = field(default_factory=lambda: dict(
        cache_read=0, cache_write=0, input=0, output=0))
    ctx_sizes: list = field(default_factory=list)   # per-turn context size, exact
    buckets: dict = field(default_factory=dict)     # bucket name -> tokens
    tool_out: dict = field(default_factory=dict)    # tool name -> result tokens
    tool_in: dict = field(default_factory=dict)     # tool name -> call-input tokens
    tool_calls: dict = field(default_factory=dict)  # tool name -> call count
    bash_out: list = field(default_factory=list)    # every Bash result size
    bash_cmds: list = field(default_factory=list)   # (tokens, command) per call
    reads: dict = field(default_factory=dict)       # file path -> [count, tokens]
    images: int = 0
    mtime: float = 0.0

    @property
    def content_total(self) -> int:
        return sum(self.buckets.values())

    @property
    def billed_total(self) -> int:
        return sum(self.billed.values())

    @property
    def cost_units(self) -> float:
        """Billed tokens re-weighted by what each kind actually costs."""
        return sum(self.billed[k] * PRICE[k] for k in self.billed)

    @property
    def floor(self) -> int:
        """The fixed preamble: system prompt + tool schemas + memory.

        Taken as the smallest context seen in the opening turns, which is the
        floor before any work has accumulated. Exact (it is a billed number),
        but only meaningful for sessions with a few turns to look at.
        """
        head = self.ctx_sizes[:5]
        return min(head) if head else 0

    @property
    def peak(self) -> int:
        return max(self.ctx_sizes) if self.ctx_sizes else 0

    @property
    def amplification(self) -> float:
        """How many times the average token of content was billed as a read."""
        c = self.content_total
        return (self.billed["cache_read"] / c) if c else 0.0


def _bump(d: dict, k, n) -> None:
    d[k] = d.get(k, 0) + n


def transcript_dir() -> str:
    return os.path.expanduser(
        os.environ.get("TS_TRANSCRIPT_DIR", "~/.claude/projects"))


def find_transcripts(root: str = None, project: str = None) -> list:
    root = root or transcript_dir()
    pat = os.path.join(root, project or "*", "*.jsonl")
    return sorted(glob.glob(pat))


def parse(path: str) -> Session:
    """Account for one transcript. Never raises on a malformed line."""
    s = Session(path=path,
                project=os.path.basename(os.path.dirname(path)),
                session_id=os.path.basename(path)[:-6])
    try:
        s.mtime = os.path.getmtime(path)
    except OSError:
        pass
    id2name = {}
    id2input = {}

    try:
        fh = open(path, "r", errors="replace")
    except OSError:
        return s

    with fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            kind = rec.get("type")

            if kind == "attachment":
                _bump(s.buckets, BUCKET_ATTACH, toks(json.dumps(rec.get("attachment"), default=str)))
                continue
            if kind == "system":
                _bump(s.buckets, BUCKET_SYSTEM, toks(json.dumps(rec.get("message") or rec, default=str)))
                continue
            if kind not in ("assistant", "user"):
                continue

            msg = rec.get("message") or {}
            content = msg.get("content")

            if kind == "assistant":
                usage = msg.get("usage") or {}
                if usage:
                    s.turns += 1
                    cr = usage.get("cache_read_input_tokens") or 0
                    cw = usage.get("cache_creation_input_tokens") or 0
                    ip = usage.get("input_tokens") or 0
                    s.billed["cache_read"] += cr
                    s.billed["cache_write"] += cw
                    s.billed["input"] += ip
                    s.billed["output"] += usage.get("output_tokens") or 0
                    ctx = cr + cw + ip
                    if ctx:
                        s.ctx_sizes.append(ctx)
                if isinstance(content, list):
                    for b in content:
                        if not isinstance(b, dict):
                            continue
                        t = b.get("type")
                        if t == "text":
                            _bump(s.buckets, BUCKET_ASSISTANT, toks(b.get("text")))
                        elif t == "thinking":
                            _bump(s.buckets, BUCKET_THINKING, toks(b.get("thinking")))
                        elif t == "tool_use":
                            name = b.get("name") or "?"
                            inp = b.get("input") or {}
                            id2name[b.get("id")] = name
                            id2input[b.get("id")] = inp
                            n = toks(json.dumps(inp, default=str))
                            _bump(s.buckets, BUCKET_TOOL_IN, n)
                            _bump(s.tool_in, name, n)
                            _bump(s.tool_calls, name, 1)
                            if name == "Bash":
                                s.bash_cmds.append((n, str(inp.get("command", ""))))
                continue

            # user record
            if isinstance(content, str):
                _bump(s.buckets, BUCKET_USER, toks(content))
                continue
            if not isinstance(content, list):
                continue
            for b in content:
                if not isinstance(b, dict):
                    continue
                t = b.get("type")
                if t == "text":
                    _bump(s.buckets, BUCKET_USER, toks(b.get("text")))
                elif t == "image":
                    n = image_tokens(((b.get("source") or {}).get("data")) or "")
                    _bump(s.buckets, BUCKET_USER, n)
                    s.images += 1
                elif t == "tool_result":
                    tuid = b.get("tool_use_id")
                    name = id2name.get(tuid, "?unknown")
                    cc = b.get("content")
                    text_n = 0
                    img_n = 0
                    if isinstance(cc, str):
                        text_n = toks(cc)
                    elif isinstance(cc, list):
                        for blk in cc:
                            if not isinstance(blk, dict):
                                continue
                            if blk.get("type") == "text":
                                text_n += toks(blk.get("text"))
                            elif blk.get("type") == "image":
                                img_n += image_tokens(((blk.get("source") or {}).get("data")) or "")
                                s.images += 1
                    elif cc is not None:
                        text_n = toks(json.dumps(cc, default=str))
                    if text_n:
                        _bump(s.buckets, BUCKET_TOOL_TEXT, text_n)
                    if img_n:
                        _bump(s.buckets, BUCKET_TOOL_IMG, img_n)
                    _bump(s.tool_out, name, text_n + img_n)
                    if name == "Bash":
                        s.bash_out.append(text_n)
                    elif name in ("Read", "read"):
                        fp = str((id2input.get(tuid) or {}).get("file_path", "?"))
                        cur = s.reads.get(fp) or [0, 0]
                        s.reads[fp] = [cur[0] + 1, cur[1] + text_n + img_n]
    return s


@dataclass
class Fleet:
    """Every session found, plus the totals the reports and detectors read."""
    sessions: list = field(default_factory=list)

    @classmethod
    def load(cls, root: str = None, project: str = None, limit: int = None) -> "Fleet":
        paths = find_transcripts(root, project)
        if limit:
            paths = sorted(paths, key=lambda p: -os.path.getmtime(p))[:limit]
        return cls(sessions=[parse(p) for p in paths])

    # -- aggregates ---------------------------------------------------------
    def billed(self) -> dict:
        out = dict(cache_read=0, cache_write=0, input=0, output=0)
        for s in self.sessions:
            for k in out:
                out[k] += s.billed[k]
        return out

    def cost_units(self) -> float:
        b = self.billed()
        return sum(b[k] * PRICE[k] for k in b)

    def buckets(self) -> dict:
        out = {}
        for s in self.sessions:
            for k, v in s.buckets.items():
                _bump(out, k, v)
        return out

    def content_total(self) -> int:
        return sum(self.buckets().values())

    def amplification(self) -> float:
        c = self.content_total()
        return (self.billed()["cache_read"] / c) if c else 0.0

    def turns(self) -> int:
        return sum(s.turns for s in self.sessions)

    def substantive(self, min_turns: int = 5) -> list:
        """Sessions long enough for turn-shape statistics to mean anything."""
        return [s for s in self.sessions if s.turns >= min_turns]

    def merged(self, attr: str) -> dict:
        out = {}
        for s in self.sessions:
            for k, v in getattr(s, attr).items():
                if isinstance(v, list):
                    cur = out.get(k) or [0, 0]
                    out[k] = [cur[0] + v[0], cur[1] + v[1]]
                else:
                    _bump(out, k, v)
        return out

    def bash_out(self) -> list:
        out = []
        for s in self.sessions:
            out.extend(s.bash_out)
        return out

    def bash_cmds(self) -> list:
        out = []
        for s in self.sessions:
            out.extend(s.bash_cmds)
        return out


# --------------------------------------------------------------------------
# small shared helpers
# --------------------------------------------------------------------------

def pct(n, d) -> float:
    return (100.0 * n / d) if d else 0.0


def quantile(sorted_values: list, q: float):
    if not sorted_values:
        return 0
    i = int(q * (len(sorted_values) - 1))
    return sorted_values[i]


def human(n) -> str:
    n = float(n)
    for unit, div in (("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if abs(n) >= div:
            return "%.1f%s" % (n / div, unit)
    return "%d" % n


_PROG = re.compile(r"^(?:cd\s+\S+\s*(?:&&|;)\s*)*([\w.\-/]+)")


def command_program(cmd: str) -> str:
    """The program a shell command actually runs, past any cd prefixes."""
    m = _PROG.match((cmd or "").strip())
    return m.group(1).rsplit("/", 1)[-1] if m else "?"

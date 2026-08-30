#!/usr/bin/env python3
"""`ts advise` — the catalogue of fixes, and which ones apply to THIS machine.

Every entry is a detector, not a tip. A detector reads the measurements and
either fires with a number attached or stays silent. That is the whole design:
the same catalogue tells one person "install a Bash-output compressor" and the
next person "a compressor is worthless here, your outputs are 115 tokens" —
because it read their transcripts before opening its mouth.

Savings are expressed in COST UNITS (tokens x price relative to one input
token) and as a share of measured spend, so a fix that removes a few tokens
from every one of a thousand turns ranks above one that removes many tokens
once. Each carries a confidence label:

The label qualifies the SAVING, not the evidence. Evidence is measured or it
is not reported; a saving is always a projection, so no saving is ever labelled
"measured" — an earlier cut did exactly that for three detectors whose numbers
multiplied exact billed figures by an invented constant, which is the failure
this tool was built to catch someone else committing.

    derived    — the projection follows from this machine's own measured shape
    estimated  — measured inputs, but a stated assumption about what you change
    heuristic  — the direction is right, the magnitude is not

Every non-derived Finding must carry an `assumption` naming what was assumed.

Adding a detector: write a function taking (fleet, mach) and returning a
Finding or None, then list it in DETECTORS. Keep the thresholds visible in the
function rather than in a config file — a reader should be able to see why it
fired without going somewhere else.
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from transcripts import PRICE, cd_shape, pct, quantile  # noqa: E402


def to_spend(fleet, share_pct: float) -> float:
    """Convert a share of CONTENT (or of cache reads) into a share of spend.

    Content does not cost anything by existing — it costs by being re-read.
    So a slice of content maps onto the bill through the cache-read line, and
    a slice of cache reads maps through the same factor. Both callers want the
    identical conversion, which is why there is one function rather than two
    hand-inlined constants that could drift apart.
    """
    cost = fleet.cost_units()
    if not cost:
        return 0.0
    read_cost = fleet.billed()["cache_read"] * PRICE["cache_read"]
    return share_pct * (read_cost / cost)


@dataclass
class Finding:
    id: str
    title: str
    severity: str            # high | medium | low
    confidence: str          # measured | estimated | heuristic
    saving_pct: float        # share of measured spend, 0 when unquantifiable
    evidence: list = field(default_factory=list)
    actions: list = field(default_factory=list)
    fix: str = None          # id understood by fixes.py, when auto-appliable
    # What had to be assumed to turn measured facts into a saving. Printed
    # next to the number. A projection with an unstated assumption is the
    # thing this tool exists to complain about, so it may not be omitted.
    assumption: str = ""

    @property
    def rank(self) -> tuple:
        return (-self.saving_pct, self.id)


def severity_for(saving_pct: float) -> str:
    """Severity IS the estimated impact.

    An earlier cut let each detector declare its own severity, which produced
    a report where a LOW worth 8% of spend sorted below a MEDIUM worth 2.4%.
    A reader cannot act on a ranking that disagrees with its own numbers, so
    the two are now the same thing and a detector cannot set them apart.
    """
    if saving_pct >= 8:
        return "high"
    if saving_pct >= 2.5:
        return "medium"
    return "low"


# ---------------------------------------------------------------------------
# detectors
# ---------------------------------------------------------------------------

def d_preamble(fleet, mach):
    """The fixed prompt is re-read on every single turn, so it is the one place
    where a one-off edit is multiplied by every turn you will ever run."""
    subs = fleet.substantive()
    floors = sorted(s.floor for s in subs if s.floor)
    if not floors:
        return None
    med = quantile(floors, 0.5)
    reads = fleet.billed()["cache_read"]
    if not reads:
        return None
    cost = med * fleet.turns()
    share = pct(cost, reads)
    if share < 4 and med < 20000:
        return None
    sev = "high" if share >= 12 or med >= 35000 else "medium" if share >= 6 else "low"

    ev = ["fixed preamble is {:,} tokens (median across {} sessions)".format(med, len(floors)),
          "re-read on every one of {:,} turns = {:,} cache-read tokens".format(
              fleet.turns(), cost),
          "that is {:.1f}% of all cache reads you were billed for".format(share)]
    mem = mach["memory"]
    if mem["total"]:
        ev.append("memory/instruction files total ~{:,} tokens:".format(mem["total"]))
        for p, t in mem["files"][:4]:
            ev.append("    {:>7,}  {}".format(t, p.replace(os.path.expanduser("~"), "~")))
    sk = mach["skills"]
    if sk["count"]:
        ev.append("{} installed skills; descriptions load up front".format(sk["count"]))

    # Only the read-multiplied share is recoverable, and only partly: a trim of
    # a third of the preamble is an aggressive but achievable target.
    return Finding(
        id="preamble",
        title="Fixed preamble is re-read every turn",
        severity=sev, confidence="estimated",
        assumption="a third of the preamble is removable",
        saving_pct=to_spend(fleet, share) * 0.33,
        evidence=ev,
        actions=[
            "Audit the files above: `ts doctor --memory` lists them by size.",
            "Move rarely-needed detail out of CLAUDE.md into a skill or a doc "
            "the agent can read on demand. Instructions that fire on 5% of "
            "turns should not be in 100% of them.",
            "Trim skill descriptions to their trigger phrases.",
        ])


def d_session_length(fleet, mach):
    """Cost grows with the square of session length: each new turn re-reads
    everything before it. This is the single largest lever on most machines."""
    subs = fleet.substantive()
    if len(subs) < 3:
        return None
    turns = sorted(s.turns for s in subs)
    peaks = sorted(s.peak for s in subs)
    med_t, p90_t = quantile(turns, 0.5), quantile(turns, 0.9)
    med_p = quantile(peaks, 0.5)
    if med_t < 80 and med_p < 120000:
        return None
    sev = "high" if (p90_t >= 400 or med_p >= 250000) else "medium"

    growth = []
    for s in sorted(subs, key=lambda s: -s.peak)[:3]:
        if s.floor:
            growth.append("    {:>5} turns   {:>7,} -> {:>8,}   ({:.0f}x)".format(
                s.turns, s.floor, s.peak, s.peak / s.floor))

    ev = ["median session: {:,} turns, peak context {:,} tokens".format(med_t, med_p),
          "p90 session: {:,} turns".format(p90_t),
          "worst sessions (turns, preamble -> peak):"] + growth
    ev.append("cost of a session grows ~quadratically in its turn count, "
              "because every turn re-reads all prior turns")

    # Derive the saving from each heavy session's OWN shape rather than a flat
    # multiplier. A session of N turns whose context runs from F to P costs
    # about N*(F+P)/2 in reads. Split it in two at the midpoint and each half
    # restarts at F and ends at M = (F+P)/2, for a total of N*(F+M)/2. So
    #
    #     saving fraction = (P - M) / (F + P) = (P - F) / (2 * (F + P))
    #
    # which is measured per session, not guessed. A comment here previously
    # claimed "roughly three quarters" while the code applied 0.35; neither
    # was derived from anything.
    heavy = sorted(subs, key=lambda s: -s.cost_units)[:max(1, len(subs) // 4)]
    heavy_cost = sum(s.cost_units for s in heavy)
    heavy_share = pct(heavy_cost, fleet.cost_units())
    saved = 0.0
    for h in heavy:
        F, P = float(h.floor or 0), float(h.peak or 0)
        if F > 0 and P > F:
            saved += h.cost_units * ((P - F) / (2.0 * (F + P)))
    derived_pct = pct(saved, fleet.cost_units())
    ev.append("splitting the heaviest %d session(s) in half would save about "
              "%.0f%% of total spend, from their own measured shape"
              % (len(heavy), derived_pct))
    return Finding(
        id="session-length",
        title="Long sessions dominate spend",
        severity=sev, confidence="derived",
        assumption="each heavy session split once at its midpoint",
        saving_pct=derived_pct,
        evidence=ev,
        actions=[
            "Clear between unrelated tasks. A fresh session restarts at the "
            "preamble; a continued one pays for everything before it, forever.",
            "Use `/compact` at natural boundaries rather than letting context "
            "run to the limit.",
            "Push exploratory reading into subagents — their transcript never "
            "enters the main thread's permanent history. Note this moves cost "
            "rather than removing it: the subagent's own turns are billed, and "
            "`subagent-cost` reports what they came to.",
        ])


def d_bash_chatter(fleet, mach):
    """Many small Bash calls. Compressors cannot help; batching can, and it
    cuts BOTH the command text and the turn count."""
    outs = sorted(fleet.bash_out())
    cmds = fleet.bash_cmds()
    if len(outs) < 100:
        return None
    med = quantile(outs, 0.5)
    p90 = quantile(outs, 0.9)
    cmd_tok = sum(c[0] for c in cmds)
    out_tok = sum(outs)
    combined = cmd_tok + out_tok
    share = pct(combined, fleet.content_total())
    if share < 15 or p90 > 5000:
        return None   # d_bash_bulk owns the fat-output case

    # How much of the command text is pure ceremony: cd prefixes and absolute
    # path repetition. Measured, not assumed.
    cd_tok = sum(n for n, c in cmds if c.strip().startswith("cd "))
    shapes = [cd_shape(c) for _n, c in cmds]
    n_chained = shapes.count("chained")
    n_standalone = shapes.count("standalone")
    # Both shapes present in quantity = a machine mid-migration from the
    # chained form to the standalone one, which is what a machine paying an
    # approval prompt per chained call does. Recommending it batch them back
    # is recommending the regression.
    converting = n_chained >= 20 and n_standalone >= 0.2 * n_chained
    ev = ["{:,} Bash calls: {:,} tokens of command text + {:,} of output "
          "= {:.1f}% of all content".format(len(outs), cmd_tok, out_tok, share),
          "output size: median {:,}, p90 {:,}, max {:,}".format(
              med, p90, outs[-1] if outs else 0),
          "the commands cost {:.0f}% of what their output costs — "
          "no compressor touches that half".format(pct(cmd_tok, out_tok))]
    if cd_tok:
        ev.append("{:,} tokens are in calls that begin with a `cd` prefix "
                  "({:,} chained, {:,} standalone)".format(
                      cd_tok, n_chained, n_standalone))
    return Finding(
        id="bash-chatter",
        title="Bash cost is call volume, not output size",
        severity="high" if share >= 35 else "medium",
        confidence="estimated",
        assumption="a quarter of Bash calls batchable, 30% off those",
        saving_pct=to_spend(fleet, share) * 0.25 * 0.30,
        evidence=ev,
        actions=[
            "Batch related commands into one call (`a && b && c`) — it halves "
            "the turn count as well as the ceremony.",
            # The measurement above stands either way; only the recommendation
            # changes. Silence would throw away a true number the reader wants.
            ("{:,} of those tokens are `cd` ceremony — but you already write "
             "{:,} standalone `cd` calls against {:,} chained ones, so you are "
             "converting that shape deliberately. On a machine that pays an "
             "approval prompt per chained call, standalone is the cheaper "
             "shape and batching it back would be a bad trade."
             .format(cd_tok, n_standalone, n_chained)) if converting else
            ("{:,} tokens are `cd` ceremony that a scoped flag (`git -C`, "
             "`make -C`) removes without a second call.".format(cd_tok)),
            "Do NOT install an output compressor for this profile: at a median "
            "of {:,} tokens there is nothing in the output to compress.".format(med),
            "Prefer Read/Grep over `cat`/`sed -n` where you only need content: "
            "same tokens, but they are excluded from any future compression "
            "and are cheaper to batch.",
        ])


def d_bash_bulk(fleet, mach):
    """The opposite profile: fat Bash outputs, where a compressor genuinely
    pays. Fires only when the distribution supports it."""
    outs = sorted(fleet.bash_out())
    if len(outs) < 30:
        return None
    p90 = quantile(outs, 0.9)
    if p90 < 5000:
        return None
    total = sum(outs)
    top = sorted(outs, reverse=True)
    n = max(1, len(top) // 10)
    conc = pct(sum(top[:n]), total)
    share = pct(total, fleet.content_total())
    rtk = mach["tools"].get("rtk", {}).get("path")
    ev = ["Bash output p90 is {:,} tokens (max {:,}) — there is real bulk here"
          .format(p90, outs[-1]),
          "the top 10% of calls carry {:.0f}% of Bash output".format(conc),
          "Bash output is {:.1f}% of all content".format(share)]
    ev.append("rtk is " + ("installed at " + rtk if rtk else "NOT installed"))
    return Finding(
        id="bash-bulk",
        title="Fat Bash output — a compressor would pay here",
        severity="high" if share >= 30 else "medium",
        confidence="estimated",
        assumption="half the output compressible, 30% off it",
        saving_pct=to_spend(fleet, share) * 0.5 * 0.30,
        evidence=ev,
        actions=[
            "Install rtk (github.com/rtk-ai/rtk): a PreToolUse hook that "
            "compresses Bash output before it reaches context. It preserves "
            "errors, diffs and stack traces by design.",
            "Re-run `ts audit` a week later and compare the Bash block — this "
            "tool measures whether it actually helped.",
        ] if not rtk else [
            "rtk is already installed. Confirm the hook is registered: "
            "`ts doctor --hooks`.",
        ])


def d_output_verbosity(fleet, mach):
    """Output is billed at 5x input, so it punches far above its token count."""
    b = fleet.billed()
    cost = fleet.cost_units()
    if not cost:
        return None
    out_share = pct(b["output"] * 5.0, cost)
    if out_share < 10:
        return None
    turns = fleet.turns() or 1
    per_turn = b["output"] / turns
    return Finding(
        id="output-verbosity",
        title="Output tokens are a large share of spend",
        severity="high" if out_share >= 20 else "medium",
        confidence="estimated",
        assumption="a fifth off output length",
        saving_pct=out_share * 0.20,
        evidence=[
            "{:,} output tokens = {:.1f}% of cost-weighted spend "
            "(output is billed at 5x input)".format(b["output"], out_share),
            "average {:,.0f} output tokens per turn".format(per_turn),
        ],
        actions=[
            "Add a terseness instruction to CLAUDE.md — `ts fixes apply "
            "terse-output` writes one (with a backup, and a revert).",
            "Ask for diffs rather than whole rewritten files.",
        ],
        fix="terse-output")


def d_mcp_schema(fleet, mach):
    """MCP tool definitions sit in the preamble of every turn unless deferred."""
    mcp = mach["mcp"]
    if not mcp.get("readable"):
        return None
    n_global = len(mcp["global"])
    n_proj = sum(len(v) for v in mcp["projects"].values())
    called = fleet.mcp_servers_called()
    # Configured is a subset: plugin-provided servers never appear there.
    total = max(n_global + n_proj, len(called))
    if total == 0:
        return None
    env = mach["env"]
    tool_search = env.get("ENABLE_TOOL_SEARCH")
    base_url = env.get("ANTHROPIC_BASE_URL")
    # Tool search is on by default in current Claude Code, but a custom base
    # URL turns it off, which is exactly the case a proxy user lands in.
    at_risk = bool(base_url) and tool_search in (None, "", "false", "0", "off")
    if not at_risk and total <= 3:
        return None
    ev = ["{} MCP server(s) configured ({} global, {} project-scoped); "
          "{} actually called".format(
              n_global + n_proj, n_global, n_proj, len(called))]
    if len(called) > n_global + n_proj:
        ev.append("{} more were called than are configured — plugin-provided "
                  "servers do not appear in the config".format(
                      len(called) - (n_global + n_proj)))
    for proj, names in list(mcp["projects"].items())[:4]:
        ev.append("    {}: {}".format(
            os.path.basename(proj.rstrip("/")), ", ".join(names)))
    ev.append("a single MCP tool definition typically costs 200-800 tokens, "
              "paid on every turn unless deferred")
    if at_risk:
        ev.append("ANTHROPIC_BASE_URL is set and ENABLE_TOOL_SEARCH is not — "
                  "in that combination tool definitions are NOT deferred")
    return Finding(
        id="mcp-schema",
        title=("Tool deferral is off behind a custom base URL" if at_risk
               else "MCP tool schemas sit in every preamble"),
        severity="high" if at_risk else "low",
        confidence="heuristic",
        saving_pct=6.0 if at_risk else 1.0,
        evidence=ev,
        actions=([
            "Set ENABLE_TOOL_SEARCH=true — `ts fixes apply tool-search` writes "
            "it into settings.json.",
        ] if at_risk else [
            "Remove MCP servers you do not use from ~/.claude.json; each one's "
            "definitions are loaded before deferral can help.",
        ]),
        fix="tool-search" if at_risk else None)


def d_repeat_reads(fleet, mach):
    """Re-reading a file you already have in context pays twice for it."""
    reads = fleet.merged("reads")
    repeats = [(p, c, t) for p, (c, t) in reads.items() if c >= 4 and t > 3000]
    if not repeats:
        return None
    repeats.sort(key=lambda r: -r[2])
    wasted = sum(t - (t // c) for _p, c, t in repeats)
    share = pct(wasted, fleet.content_total())
    if share < 1:
        return None
    ev = ["{} file(s) read 4+ times; ~{:,} tokens are redundant copies".format(
        len(repeats), wasted)]
    for p, c, t in repeats[:5]:
        ev.append("    {:>3}x  {:>7,} tok  {}".format(
            c, t, p.replace(os.path.expanduser("~"), "~")))
    return Finding(
        id="repeat-reads",
        title="The same files are read many times per session",
        severity="medium" if share >= 3 else "low",
        confidence="estimated",
        saving_pct=to_spend(fleet, share),
        evidence=ev,
        actions=[
            "Read once with a wider range instead of several narrow ranges.",
            "For files re-read across sessions, put the stable part in memory "
            "rather than re-reading it each time.",
        ])


def d_images(fleet, mach):
    """Screenshots are cheap per image but permanent, and they are rarely
    needed after the turn that looked at them."""
    buckets = fleet.buckets()
    img = buckets.get("tool results (images)", 0)
    n = sum(s.images for s in fleet.sessions)
    if not img or n < 10:
        return None
    share = pct(img, fleet.content_total())
    if share < 2:
        return None
    return Finding(
        id="images",
        title="Screenshots accumulate in context",
        severity="low",
        confidence="estimated",
        saving_pct=to_spend(fleet, share) * 0.5,
        evidence=[
            "{} images totalling ~{:,} tokens ({:.1f}% of content)".format(
                n, img, share),
            "an image is billed at (width x height)/750 on every subsequent turn",
        ],
        actions=[
            "Resize before capture where you only need to confirm a layout.",
            "Clear after a browser-heavy stretch rather than carrying the "
            "screenshots into unrelated work.",
        ])


def d_thinking(fleet, mach):
    buckets = fleet.buckets()
    th = buckets.get("assistant thinking", 0)
    if not th:
        return None
    share = pct(th, fleet.content_total())
    if share < 8:
        return None
    return Finding(
        id="thinking",
        title="Extended thinking is a large share of content",
        severity="low", confidence="estimated",
        saving_pct=to_spend(fleet, share),
        evidence=["thinking blocks are ~{:,} tokens ({:.1f}% of content)".format(
            th, share)],
        actions=[
            "Lower MAX_THINKING_TOKENS for routine work, or select a lower "
            "effort level when the task does not need deep reasoning.",
        ])


def d_attachments(fleet, mach):
    buckets = fleet.buckets()
    at = buckets.get("attachments", 0)
    if not at:
        return None
    share = pct(at, fleet.content_total())
    if share < 8:
        return None
    return Finding(
        id="attachments",
        title="File injections are a large share of content",
        severity="low", confidence="estimated",
        saving_pct=to_spend(fleet, share),
        evidence=["attachments/injections ~{:,} tokens ({:.1f}% of content)".format(
            at, share),
            "these are files pulled in automatically (@-mentions, IDE "
            "selections, directory listings), not tool results"],
        actions=[
            "Reference paths rather than @-mentioning whole files when the "
            "agent can read the part it needs.",
        ])


def d_subagent_cost(fleet, mach):
    """Subagent turns are billed like any other, and are easy to treat as free.

    Delegation genuinely helps the MAIN thread: the subagent's transcript never
    enters it, so it does not inflate the context every later turn re-reads.
    But the subagent runs its own context with its own preamble, and that is
    billed. Until the recursive-discovery fix this tool could not see any of
    it, which meant `session-length` recommended delegating while being
    structurally blind to what delegating cost.
    """
    subs = fleet.subagents()
    if not subs:
        return None
    share = fleet.subagent_cost_share()
    if share < 5:
        return None
    turns = sum(s.turns for s in subs)
    floors = sorted(s.floor for s in subs if s.floor)
    sub_turns = sorted(x.turns for x in subs)
    ev = ["{:,} subagent transcript(s), {:,} turns, {:.1f}% of cost-weighted "
          "spend".format(len(subs), turns, share)]
    if floors:
        ev.append("each carries its own preamble; median {:,} tokens, paid "
                  "again per subagent".format(quantile(floors, 0.5)))

    # Two machines can show the same inherited-preamble share for opposite
    # reasons, and they need opposite advice. Many tiny subagents means the
    # preamble is being paid too often; few long ones means the preamble is
    # simply large. Reported profile: median 39 turns per subagent, 2% at or
    # under 5 -- "batch your small questions" targeted a population that
    # barely existed there.
    short = [t for t in sub_turns if t <= 5]
    short_share = pct(len(short), len(sub_turns))
    inherited = sum((x.floor or 0) * x.turns for x in subs)
    sub_ctx = sum(sum(x.ctx_sizes) for x in subs)
    inherited_share = pct(inherited, sub_ctx)

    ev.append("median {:,} turns per subagent (p90 {:,}); {:.0f}% run 5 turns "
              "or fewer".format(quantile(sub_turns, 0.5),
                                quantile(sub_turns, 0.9), short_share))
    if inherited_share:
        ev.append("the inherited preamble is {:.0f}% of all subagent context"
                  .format(inherited_share))
    ev.append("delegation keeps work out of the MAIN thread's history, which "
              "is real — but the subagent's own turns are billed")

    actions = ["Delegate for context hygiene, not as a saving — measure both "
               "sides before assuming a subagent is cheaper than doing it "
               "inline."]
    if short_share >= 15:
        actions.append(
            "{:.0f}% of your subagents run 5 turns or fewer, and each pays a "
            "full preamble to answer briefly. Batch small questions into one "
            "agent, or ask inline.".format(short_share))
    elif inherited_share >= 20:
        actions.append(
            "Your subagents are not short (median {:,} turns), so batching "
            "them is not the lever. The preamble each one inherits is — it is "
            "{:.0f}% of all subagent context. See `preamble`."
            .format(quantile(sub_turns, 0.5), inherited_share))
    return Finding(
        id="subagent-cost",
        title="Subagent traffic is a large share of spend",
        severity="medium", confidence="derived",
        saving_pct=0.0,
        evidence=ev,
        actions=actions)


DETECTORS = [
    d_preamble,
    d_session_length,
    d_subagent_cost,
    d_bash_chatter,
    d_bash_bulk,
    d_output_verbosity,
    d_mcp_schema,
    d_repeat_reads,
    d_images,
    d_thinking,
    d_attachments,
]

CATALOGUE = {
    "preamble": "Fixed preamble re-read every turn",
    "session-length": "Long sessions dominate spend",
    "subagent-cost": "Subagent traffic is a large share of spend",
    "bash-chatter": "Bash cost is call volume, not output size",
    "bash-bulk": "Fat Bash output — a compressor would pay",
    "output-verbosity": "Output tokens are a large share of spend",
    "mcp-schema": "MCP tool schemas / deferral",
    "repeat-reads": "Same files read many times",
    "images": "Screenshots accumulate in context",
    "thinking": "Extended thinking is a large share of content",
    "attachments": "File injections are a large share of content",
}


def run(fleet, mach) -> list:
    """Every detector that fires, ranked by severity then estimated saving."""
    found = []
    for det in DETECTORS:
        try:
            f = det(fleet, mach)
        except Exception as exc:  # a broken detector must not kill the report
            sys.stderr.write("warning: detector %s failed: %s\n"
                             % (getattr(det, "__name__", "?"), exc))
            continue
        if f:
            # Detectors set a severity for readability while being written;
            # the report only ever shows the one derived from the number, so
            # the two can never drift apart in front of a reader.
            f.severity = severity_for(f.saving_pct)
            found.append(f)
    found.sort(key=lambda f: f.rank)
    return found

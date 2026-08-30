# token-save-fleet

Measure what your AI coding sessions actually cost, then get advice derived
from *your* numbers instead of someone else's.

On the first real machine this was run against, **98% of every token billed was
a cache read** — the same content, re-read on the next turn, and the next. Each
token of content created was billed **646 times**.

That number is the whole point. Most token-saving advice assumes the cost is
the size of what you send. It usually is not. The cost is how long it stays.

```
cost of a token  ~=  its size  x  the number of turns that re-read it
```

## Why

The tools in this space compress tool output. That is a reasonable thing to do
and it is measurably the wrong priority on many machines:

- A compressor cannot touch the **fixed preamble** — system prompt, tool
  schemas, memory files — which is re-read on *every* turn. On the machine
  above that was 10.3% of all cache reads, from 24,781 tokens of text.
- A compressor cannot touch **command text**. On that machine the Bash commands
  cost 79% of what their output cost. Half the surface, invisible to every tool
  that only looks at results.
- A compressor cannot shorten a **260-turn session** whose context grew from
  27K to 716K, and session length is superlinear: every turn re-reads all the
  turns before it.
- And where outputs are small — a median of 113 tokens — there is simply
  nothing in them to compress.

None of that is knowable without measuring. So this measures first.

## Quick start

```sh
scripts/bootstrap --check    # what is present; installs nothing
scripts/ts audit             # where the tokens went
scripts/ts advise            # what to do about it, ranked
scripts/verify               # 12 checks against fixtures with known answers
```

`python3` (3.9+) is the only hard requirement. `tiktoken` is optional — without
it, content sizes use `len/3.6`, which lands within **1.5%** of the real
tokenizer in aggregate. Every report names which counter produced its numbers.

Nothing is sent anywhere. There is no network code in this repository.

## Commands

| Question | Command |
|---|---|
| Where did my tokens go? | `ts audit` |
| ...one project only | `ts audit --project '*myrepo*'` |
| ...heaviest sessions | `ts audit --sessions` |
| What is installed and configured here? | `ts doctor` |
| What fills my preamble? | `ts doctor --memory` |
| What should I change? | `ts advise` |
| ...and what did *not* apply | `ts advise --all` |
| What can be changed mechanically? | `ts fixes list` |
| Preview / apply / undo one | `ts fixes show\|apply\|revert <id>` |

`--json` on `audit`, `advise` and `doctor` for machine-readable output.

## The catalogue gives different machines opposite advice

This is the design, not a side effect. Two detectors:

- **`bash-bulk`** fires when Bash output has real bulk (p90 above 5,000
  tokens). It tells you to install a compressor such as
  [rtk](https://github.com/rtk-ai/rtk).
- **`bash-chatter`** fires when the cost is call *volume* with small outputs.
  It tells you, in as many words, **not** to install one — and points at
  batching, which cuts the command text and the turn count too.

They are mutually exclusive, and `scripts/verify` proves it against two
deliberately opposite fixture fleets. A tool that recommends the same thing to
everyone has not read anything.

Full catalogue: `preamble`, `session-length`, `bash-chatter`, `bash-bulk`,
`output-verbosity`, `mcp-schema`, `repeat-reads`, `images`, `thinking`,
`attachments`.

## Two kinds of number, never mixed

```
BILLED    exact — read verbatim from the usage records the provider wrote
          into your transcript. Nothing is estimated.

CONTENT   estimated — the transcript stores text, not tokens.
```

The headline metric is the ratio between them, so the exact side is kept exact
and the estimated side is labelled. Every finding also carries a confidence:

| label | meaning |
|---|---|
| `measured` | arithmetic on exact billed numbers |
| `estimated` | arithmetic on estimated content sizes |
| `heuristic` | the direction is right, the magnitude is not |

Severity is **derived from** estimated impact rather than declared separately,
so the ranking can never disagree with its own numbers.

## What it will not do

- **It will not shorten your sessions for you.** The largest finding on most
  machines is behavioural, and `ts fixes` deliberately contains only changes
  that are mechanical, reversible, and idempotent. Everything else is advice
  with the evidence attached.
- **It will not add up its own percentages.** Findings overlap. The report
  prints the largest single item and says so.
- **It will not tell you a compressor is worth installing** unless your
  distribution says it is.
- **It does not estimate money.** Prices change and vary by plan; it reports
  *cost units* (tokens re-weighted by what each kind costs relative to one
  input token), which is what actually ranks the findings.

## Fixes that are safe to script

| id | what it does |
|---|---|
| `terse-output` | appends a response-style block to `CLAUDE.md` |
| `tool-search` | sets `ENABLE_TOOL_SEARCH=true` when a custom base URL has disabled tool deferral |

Every one shows a diff first (`--dry-run` is the default), writes a timestamped
backup, is idempotent, and reverts. A fix that cannot meet all four does not
belong there.

## Installing as a Claude Code plugin

```
/plugin marketplace add <owner>/token-save-fleet
/plugin install token-save
```

This registers the `token-save` skill, which teaches the agent to measure
before it advises — the failure mode being that a model answers "how do I
reduce tokens?" from general knowledge, which is close to a coin flip.

## Verifying it

```sh
scripts/verify
```

Twelve checks in three groups: arithmetic against hand-derived expectations in
`fixtures/EXPECTATIONS.json` (not recorded from program output — a self-test
that records its own output tests nothing), routing across the two opposite
fleets, and safety, including that an unparseable `settings.json` is refused
rather than overwritten.

## Licence

Apache-2.0. See [LICENSE](LICENSE).

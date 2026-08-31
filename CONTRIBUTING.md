# Contributing

The useful contribution here is usually **a new detector**, because a detector
encodes a cost pattern somebody actually hit. If your machine wastes tokens in
a way this tool cannot see, that gap is the bug.

## Reporting

One issue per thing, please — items get closed and prioritised independently.
Two templates:

- **Bug** — a number or behaviour is wrong.
- **A detector is wrong for my machine** — the measurement may be right and the
  advice still wrong. This is the most valuable report this project takes, and
  it has its own template because it is not quite a bug. See below.

Attach `ts share --show` where you can. It is shape only — distributions,
counts, which detectors fired, no paths or contents — and cross-machine
comparison is the single thing that has found every real bug here so far.

## Why "the advice is wrong for me" is a first-class report

The design claim is that the same catalogue gives different machines different,
sometimes opposite, advice. Every time that has failed, it failed on an axis
invisible from a transcript:

- Batching Bash calls saves tokens, and costs an approval prompt per chained
  call on a machine with a prefix-matched allowlist. `bash-chatter` recommended
  the regression.
- "Batch small questions into fewer subagents" targeted a population that did
  not exist on a machine whose subagents ran 39 turns each.

Naming that axis is the contribution. Fixing it usually means a **gate**, and a
gate needs a signal visible in the transcripts — see the failure mode below
before proposing one.

## Ground rules

1. **Measure, then advise.** A detector reads numbers and either fires with a
   figure attached or stays silent. "Consider being more concise" is not a
   detector; "your output is 13.3% of cost-weighted spend at 897 tokens per
   turn" is.
2. **Never mix exact and estimated numbers.** Billed figures come from the
   provider's usage records and are exact. Content sizes are estimated. A
   report that blurs the two is worse than no report — see the `confidence`
   labels in `advise.py`.
3. **Silence is a valid result.** A detector that fires on every machine is
   decoration. `bash-chatter` and `bash-bulk` are the model: mutually
   exclusive, and one of them tells you *not* to install something. Where a
   detector is genuinely universal because the thing it measures is universal,
   report the margin and let the reader see that — do not raise the threshold
   until it goes quiet, which suppresses a true finding to flatter a rule.
4. **Degrade, never crash.** A malformed transcript, an unreadable config, a
   missing tool: report and continue. `scripts/verify` checks this.

## Adding a detector

In `scripts/lib/advise.py`:

```python
def d_my_thing(fleet, mach):
    """One line on why this costs money."""
    ...
    if <not worth mentioning>:
        return None
    return Finding(
        id="my-thing",
        title="...",
        severity="medium",          # overwritten by severity_for(); see below
        confidence="measured",      # measured | estimated | heuristic
        saving_pct=to_spend(fleet, share_of_content),
        gate=Gate("any", [          # what had to be true, and by how much
            Cond("share of content", share_of_content, 8, unit="%"),
        ]),
        evidence=[...],             # the numbers that made it fire
        actions=[...],              # what a human should do
        fix=None,                   # id in fixes.py, if scriptable
    )
```

Then add it to `DETECTORS` and to `CATALOGUE` (the catalogue is what
`ts advise --all` lists as "did not fire", so an omission there makes the tool
look like it has fewer opinions than it does).

Notes:

- `saving_pct` is a share of **measured spend**. Use `to_spend()` to convert a
  share of content or of cache reads; do not hand-inline the factor.
- `severity` is recomputed from `saving_pct` by `severity_for()`. The field on
  the dataclass exists for readability while writing; the report never shows a
  severity that disagrees with the number beside it.
- Keep thresholds as literals in the function body. A reader should see why it
  fired without opening a config file.
- Declare the same thresholds in a `gate`. Firing is not one bit of
  information: a machine sitting at 1.05x a threshold and one sitting at 3.7x
  are different facts, and until the gate was reported they were printed
  identically. `Gate("any", ...)` fires when one condition holds and takes the
  margin from the one you are furthest past; `Gate("all", ...)` needs every
  condition and takes it from the one you are closest to losing. Use
  `mode="at_most"` for a ceiling and `mode="flag"` for a boolean, which has no
  margin and must not pretend to one.
- `verify` fails if something fires with no gate, or with a margin below 1.
  That check cannot see a bound inflated past what the fixtures reach — see
  the note beside it — so the literal in the `Cond` must be the literal in the
  `if`, not an approximation of it.

## Adding a fix

`scripts/lib/fixes.py`. A fix must be all four of:

- **previewable** — `--dry-run` is the default and shows a diff,
- **backed up** — a timestamped copy beside the original,
- **idempotent** — applying twice changes nothing the second time,
- **reversible** — `ts fixes revert <id>` restores the prior state.

If it cannot be all four, it belongs in a Finding's `actions` list, where a
human does it deliberately.

## Tests

```sh
scripts/verify          # must be green before a PR
scripts/verify --keep   # keeps the generated fixtures for inspection
```

Expected values live in `fixtures/EXPECTATIONS.json` and are **derived by
hand**, with the arithmetic written into `_derivation`. Do not paste in
whatever the program printed — that only proves the program is deterministic.

If you add a detector, add a fixture profile that makes it fire. This is
enforced, not requested: `verify` compares `advise.CATALOGUE` against the
profiles that fired and fails on anything covered by neither a fixture nor the
explicit exemption list. Add your profile to `fixtures/build-detector-fixtures`
and its name to the loop in section 4 of `scripts/verify`.

The one standing exemption is `mcp-schema`, which keys off machine
configuration rather than a transcript, so no fixture fleet can reach it. If
your detector needs an exemption too, say why in the same place — an exemption
a reader can see is a different thing from a gap nobody noticed.

## A failure mode worth naming

Twice now a check has passed while testing nothing, and both times it looked
exactly like a check that worked:

- A fixture directory named after the thing being detected (`subagents/`),
  asserted with an absolute-path substring test — so the assertion matched
  every file, including ones it never really classified. Compare **relative to
  the fixture root**, not against the absolute path.
- A gate meant to discriminate between two machines that fired on both, because
  the classifier behind it counted every multi-line script opening with `cd` as
  a standalone `cd`. It looked like a working signal until it was measured on
  a machine it was supposed to stay silent on.

A sensor that silently matches everything reads identically to one that works.
When you add a gate, assert **both** branches — the case that fires and the
case that must not — and where possible check it against a real profile that
should give the opposite answer.

## Style

Match the surrounding code. Comments explain *why*, especially where a
threshold or a constant was chosen — the calibration note at the top of
`transcripts.py` is the standard to aim for.

# Fixtures

Two transcript fleets, built to be opposites.

| fleet | shape | must fire | must stay silent |
|---|---|---|---|
| `chatter` | 3 sessions x 120 Bash calls, ~100-token outputs | `bash-chatter` | `bash-bulk` |
| `bulk` | 3 sessions x 12 Bash calls, ~20,000-token outputs | `bash-bulk` | `bash-chatter` |

They exist to test the central claim of this tool: that the same catalogue
gives different machines different — sometimes opposite — advice. If both
fleets produced the same findings, the claim would be false.

## Generated, not committed

`build-fixtures` writes them; `.gitignore` excludes the output. A reader can
see exactly what produced the expected numbers instead of trusting an opaque
blob.

```sh
fixtures/build-fixtures            # into fixtures/{chatter,bulk}/
fixtures/build-fixtures --out DIR  # somewhere else (verify uses a temp dir)
```

## Defined in tokens, not characters

`text_of(n)` measures with the counter that is actually installed and trims to
hit `n` exactly. An earlier version assumed 3.6 characters per token, which
made every content-derived expectation hold under the estimator and fail under
`tiktoken` — a 30% gap in the amplification check. The CI matrix runs both
paths for that reason.

## Expectations are hand-derived

`EXPECTATIONS.json` carries a `_derivation` field showing the arithmetic behind
every number. Nothing in it was copied from program output: a self-test that
records what the code printed only proves the code is deterministic.

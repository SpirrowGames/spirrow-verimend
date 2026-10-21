# Golden set (T05)

Labelled documents for measuring the reconciler, as described in docs/design.md §8.
The loader, scorer and oracle are in `src/verimend/eval/golden.py`. The tests that
check this data are in `tests/test_golden.py`.

## Where the facts come from

Every fact whose `source_ref` names a commit was extracted by verimend's own M1
extractors (`mcp_schema`, `config_keys`, `entrypoints`, `ports`) from:

- **SpirrowGames/spirrow-magickit @ `2ad86bf830cd2b09ee590daf7ad5bd06ec045f23`**
  (committed 2026-10-21)

The facts are frozen. Re-extracting from a newer magickit would move the facts out
from under the labels, so a refresh has to update the labels in the same change.

A few facts were made up to set traps that the real snapshot has no example of. Their
`source_ref` reads `SpirrowGames/spirrow-magickit@golden-injected:...`:

- tools with `*args` / `**kwargs` (`run_workflow`, `notify`)
- a tool name defined twice (`search`)

Each `doc.md` was written for this set. The drift in it is deliberate.

## Layout

```
eval/golden/<case-id>/
  doc.md      the document; anchors are 1-based inclusive line ranges into it
  facts.json  [{key, source_kind, source_ref, content}]
  gold.json   {claims, non_claims, injected_invalid}
```

Each entry in `claims` has the following fields:

| field | meaning |
|---|---|
| `id` | unique within the case |
| `anchor` | `[start, end]` lines of `doc.md` |
| `claim_type` | one of the 8 values of `claim.claim_type` |
| `subject` | the subject an ideal extractor would emit, validated by `verimend.claims`. Required for `port` / `parameter` / `command`. `null` for every other type, because those have no subject model yet; the model arrives with its checker. |
| `final` | `verified` / `stale` / `unverifiable`: the true answer against the frozen facts. Used to score the whole pipeline (extractor included). |
| `deterministic` | `verified` / `stale` / `deferred`: what the M2a reconciler should answer when it is given the correct subject. Used to score oracle mode. It is `deferred` whenever no checker compares the claim (another type, or a non-`current` polarity). |
| `evidence_keys` | required when either label is `stale`. These are the `facts.json` keys that contradict the claim. |
| `note` | what was planted |

- `non_claims` is `[{anchor, reason}]`. It lists sentences that must not become claims: opinions, advice and plans.
- `injected_invalid` is `[{anchor, claim_type, raw_subject_json, note}]`. Each entry is a raw `subject_json` that is broken or violates its schema. Oracle mode is the only thing that reads these. Each one must come back `unverifiable` and be counted in `invalid_subject`.

Case ids begin with `ctl-` (no planted drift), `trap-` (traps the deterministic stage
must not call stale), or `stale-` (planted drift).

## Scoring

`score(case, predictions, layer)` takes a layer, `"deterministic"` or `"final"`, and
compares the predictions with that layer's labels.

- **Matching.** A prediction can pair with a gold claim only when both have the same
  `claim_type` and their anchors overlap. Pairs are taken greedily, largest overlap
  first, and each side is used at most once. A prediction left without a partner
  counts as an extra extraction. If it also overlaps a `non_claims` anchor, it counts
  as a non-claim hit too.
- **Stale precision / recall.** These are the primary metrics (§8 sets the bar at 90%).
  A predicted `stale` counts as correct only when it names at least one of the gold
  `evidence_keys`. Stale with the wrong evidence counts as a false positive.
- **Missing verdicts.** A prediction without a verdict counts as `deferred`.

`run_oracle(case)` loads the facts and the gold subjects into an in-memory database,
along with the `injected_invalid` claims. It then runs `reconcile()` and scores the
result on the deterministic layer.

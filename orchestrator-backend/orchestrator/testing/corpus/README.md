# Refactor-mutation corpus

Each case is `cases/<name>/base/` (the baseline repo) · `expect.toml` ·
`variants/<m>/` (hand-written variant repos; when a variant contains `before/`
and `after/`, `before/` is the baseline).

What is under test is the **identity matcher** (`analyzer.history.match`). The
harness depends on `NodeObs` alone: `(node_type, qualified_name, file_path,
struct_sig, dataflow_sig, dataflow_trivial)`.

`expect.toml`:

- `[case] symbols` — the qualified names that take part in identity, **always
  written as they appear on the baseline side**; the harness reconciles them
  against `Pair.prev.qualified_name`.
- `[generated]` / `[variants]` — name → `{ identity = preserved|broken|ambiguous,
  semantic_diff = none|callers|expected, broken = [...], added = [...],
  via = qualname|struct_sig|dataflow_sig, symbols = [...] }`, where the optional
  `symbols` overrides which symbols take part in identity for that variant.

`via` applies to **every** symbol in that variant's `symbols` set, so only
symbols whose qualified name actually changes belong in the set — narrow it
with `symbols = [...]` when it does not.

Two `must_not` rules are built in: `preserved` implies no `node_removed` and no
`identity_ambiguous`; `none` implies no `node_changed`.

The expected-failure case (`swap_two_similar` → `ambiguous`) must stay: it pins
a boundary we know about.

`generated` variants are produced by the stdlib mutators in `mutate.GENERATED`
against a temporary copy of `base`. `variants` are hand-written directories.
The harness extracts from `base` twice and compares the two results; a
non-deterministic extractor is reported as `determinism`.

## Structure-signature (`struct_sig`) rules

What the corpus expects of "the same node" rests on the two `struct_sig` rules
in spec §2.3 (settled in the phase 1 review; the spec is in sync):

- Parameter and return annotations do not take part in the signature. Type
  names change whenever a class is renamed, and type information is carried by
  the dataflow signature instead.
- A class node recursively α-normalises the parameters and local names of its
  nested methods.

The `class_methods/rename_class` variant depends on both. It renames `Trainer`
to `Estimator` and, with it, the string return annotation on `fit` from
`"Trainer"` to `"Estimator"`. Under these two rules `fit`'s structure signature
does not change, so the expectation is `preserved` with
`semantic_diff = "none"` (via `struct_sig`). **The corpus is not to be changed
to accommodate a different answer.**

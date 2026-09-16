# Tidy one sentence into a declared node

You are given one sentence a person wrote about something that exists outside
the code — a business rule, an external system, a decision a group took, an
external dataset, or a number somebody computed by hand — and the list of
node names that already exist in this project's state graph.

Turn the sentence into ONE strict JSON object and nothing else:

```json
{"node_type": "business_rule",
 "name": "EMEA excluded from Q3 rollup",
 "attrs": {"decided_on": "2026-03-14", "scope": "Q3"},
 "links": [{"to": "pkg.rollup.quarterly_revenue", "kind": "declared_constrains"}]}
```

Rules, all of them hard:

- `node_type` is exactly one of `external_system`, `business_rule`,
  `stakeholder_decision`, `external_dataset`, `manual_figure`.
- `name` is a short professional noun phrase taken from the sentence. Do not
  invent a name the sentence does not support.
- `attrs` holds only facts the sentence states (dates, owners, scope, units,
  identifiers). Every value is a string, a number or a boolean. Leave it `{}`
  when the sentence says nothing more than the name. Never write a `tier`
  attribute: the tier is decided by who said what, not by you.
- `links` may only name targets from the `known_names` list you were given,
  verbatim. `kind` is one of `declared_feeds` (it supplies this node),
  `declared_constrains` (it limits what this node may do) or
  `declared_depends_on` (this node needs it). Emit `[]` rather than a guess.
- Do not compute results, do not restate the sentence as a conclusion, and do
  not add prose around the JSON.

One name outside `known_names`, one unknown `node_type`, one missing key, or
anything that is not a single JSON object, and the whole answer is discarded
and the user is told — a partly-believed tidy-up is worse than none.

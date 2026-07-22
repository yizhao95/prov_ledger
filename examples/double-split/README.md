# double-split — the leak you can only see from above

Three files, each **locally correct**, that together leak test data into
training — and the project-state-graph view where the problem becomes
visible.

## The story

1. `data_cleaning.py` — cleans the raw events and does ONE
   `train_test_split`, persisting `train.parquet` / `test.parquet`. From
   here on those two files are two halves of one partition.
2. `training.py` — receives the two halves, but nothing about two
   DataFrames says "already split". So it concats them back together
   (`combine_halves`) and **splits again**. Inside this file the code is
   textbook: fit on `X_train`, validate on `X_val`.
3. `evaluate.py` — evaluates on `data_cleaning`'s `test.parquet`,
   believing it is untouched holdout. But after the second split, a
   fraction of those rows were inside the model's training half. The
   holdout score is optimistically wrong.

No test fails. Every file reviews clean in isolation. The failure only
exists at the **composition** level — which is exactly what a project-wide
state graph is for:

- the ML overlay models every `train_test_split` as `split` nodes
  (train/test roles) anchored to the function that produced them, so
  **two split pairs** show up where there should be one;
- the second pair's `trains` edges point at the model — you can read
  "the model was trained from the *second* split" straight off the graph.

Static analysis can't prove the leak end-to-end (the `test.parquet`
round-trip through disk is invisible to the AST) — the graph's job here is
to make the *smell* visible so a human asks the question.

## Build the graph

```bash
cd skills/project-state-graph/scripts
python -m analyzer ../../../examples/double-split \
    --project double-split --db-path /tmp/double-split.db
python visualize.py /tmp/double-split.db /tmp/double-split.html --level full
# open /tmp/double-split.html, keep node types: function/split/model/file,
# edge types: downstream_data_feed/produces/splits_into/trains/defines
```

The example code is synthetic and illustrative (it is analyzed statically,
never executed); the failure mechanism — an upstream partition silently
re-split downstream — is a reconstruction of a real-world failure class.

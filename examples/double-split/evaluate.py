"""Stage 3 — the "held-out" evaluation that isn't.

This file evaluates on data_cleaning's `test.parquet`, believing it is
untouched holdout data. But training.py re-split the concatenated data,
so a fraction of these rows were inside the model's training half.
The reported score is optimistically wrong — and every file, read
alone, looks correct.
"""
import joblib
import pandas as pd

from training import build_features


def load_holdout(name: str) -> pd.DataFrame:
    return pd.read_parquet(name)


def main() -> None:
    model = joblib.load("model.joblib")
    test_df = load_holdout("test.parquet")
    holdout = build_features(test_df)
    holdout_score = model.score(
        holdout.drop(columns=["amount"]), holdout["amount"])
    print(f"holdout R^2 = {holdout_score:.3f}   # partially seen in training")


if __name__ == "__main__":
    main()

"""Stage 2 — feature engineering + training… and the SECOND split.

The bug: this file receives two halves of an existing partition, but
nothing about the two DataFrames says so. So it concats them back
together and splits again — locally this looks completely correct
(fit on X_train, validate on X_val, textbook). The damage is invisible
from inside this file: rows from data_cleaning's `test.parquet` are now
inside THIS file's training half.
"""
import pandas as pd
from sklearn.ensemble import GradientBoostingRegressor
from sklearn.model_selection import train_test_split


def load_split(name: str) -> pd.DataFrame:
    return pd.read_parquet(name)


def combine_halves(train_df: pd.DataFrame, test_df: pd.DataFrame) -> pd.DataFrame:
    # "more data is better" — the partition the upstream split created
    # is silently un-done right here:
    return pd.concat([train_df, test_df], ignore_index=True)


def build_features(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out["dow"] = out["ts"].dt.dayofweek
    out["amount_log"] = out["amount"].clip(lower=0.01).apply("log")
    return out


def main() -> None:
    train_df = load_split("train.parquet")
    test_df = load_split("test.parquet")

    all_df = combine_halves(train_df, test_df)
    feats = build_features(all_df)
    X_train, X_val = train_test_split(feats, test_size=0.25, random_state=7)

    model = GradientBoostingRegressor(random_state=7)
    model.fit(X_train.drop(columns=["amount"]), X_train["amount"])
    val_score = model.score(X_val.drop(columns=["amount"]), X_val["amount"])
    print(f"validation R^2 = {val_score:.3f}")

    import joblib
    joblib.dump(model, "model.joblib")


if __name__ == "__main__":
    main()

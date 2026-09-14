"""A small estimator wrapper: fit -> predict."""
import pandas as pd
from sklearn.linear_model import LogisticRegression


class Estimator:
    def __init__(self, C: float = 1.0):
        self.model = LogisticRegression(C=C)

    def fit(self, X_train: pd.DataFrame, y_train: pd.Series) -> "Estimator":
        # fit in place, return self for chaining
        self.model.fit(X_train, y_train)
        return self

    def predict(self, X: pd.DataFrame) -> pd.Series:
        return pd.Series(self.model.predict(X), index=X.index)


def main(df: pd.DataFrame):
    X_train = df.drop(columns=["label"])
    y_train = df["label"]
    trainer = Estimator().fit(X_train, y_train)
    return trainer.predict(X_train)


def entry(df: pd.DataFrame):
    return main(df)

"""A small estimator wrapper: fit -> predict (entry point removed)."""
import pandas as pd
from sklearn.linear_model import LogisticRegression


class Trainer:
    def __init__(self, C: float = 1.0):
        self.model = LogisticRegression(C=C)

    def fit(self, X_train: pd.DataFrame, y_train: pd.Series) -> "Trainer":
        # fit in place, return self for chaining
        self.model.fit(X_train, y_train)
        return self

    def predict(self, X: pd.DataFrame) -> pd.Series:
        return pd.Series(self.model.predict(X), index=X.index)


def entry(df: pd.DataFrame):
    return main(df)

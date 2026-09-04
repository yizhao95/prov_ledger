"""A small estimator wrapper: fit -> predict (predict is now module-level)."""
import pandas as pd
from sklearn.linear_model import LogisticRegression


class Trainer:
    def __init__(self, C: float = 1.0):
        self.model = LogisticRegression(C=C)

    def fit(self, X_train: pd.DataFrame, y_train: pd.Series) -> "Trainer":
        # fit in place, return self for chaining
        self.model.fit(X_train, y_train)
        return self


def predict(model, X: pd.DataFrame) -> pd.Series:
    return pd.Series(model.predict(X), index=X.index)


def main(df: pd.DataFrame):
    X_train = df.drop(columns=["label"])
    y_train = df["label"]
    trainer = Trainer().fit(X_train, y_train)
    return predict(trainer.model, X_train)

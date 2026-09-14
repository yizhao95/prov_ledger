"""A small estimator wrapper plus two body-identical scoring methods."""
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

    def eval_x(self, X: pd.DataFrame, y: pd.Series) -> float:
        return float(self.model.score(X, y))

    def eval_y(self, X: pd.DataFrame, y: pd.Series) -> float:
        return float(self.model.score(X, y))


def main(df: pd.DataFrame):
    X_train = df.drop(columns=["label"])
    y_train = df["label"]
    trainer = Trainer().fit(X_train, y_train)
    a = trainer.eval_x(X_train, y_train)
    b = trainer.eval_y(X_train, y_train)
    return trainer.predict(X_train), a, b

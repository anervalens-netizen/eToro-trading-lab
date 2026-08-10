from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence


@dataclass(frozen=True)
class LinearProbabilityModelV2:
    feature_names: tuple[str, ...]
    coefficients: tuple[float, ...]
    intercept: float
    ridge_lambda: float

    def probability(self, features: Sequence[float]) -> float:
        if len(features) != len(self.coefficients):
            raise ValueError("feature vector length mismatch")
        z = self.intercept + sum(weight * value for weight, value in zip(self.coefficients, features, strict=True))
        z = max(-35.0, min(35.0, z))
        return 1.0 / (1.0 + math.exp(-z))


class RidgeLogisticBaselineV2:
    """Small dependency-free statistical baseline for AI ablation."""

    def fit(
        self,
        x: Sequence[Sequence[float]],
        y: Sequence[int],
        *,
        feature_names: Sequence[str] | None = None,
        ridge_lambda: float = 1.0,
        learning_rate: float = 0.05,
        iterations: int = 500,
    ) -> LinearProbabilityModelV2:
        if not x or len(x) != len(y) or any(value not in {0, 1} for value in y):
            raise ValueError("training data are invalid")
        width = len(x[0])
        if width < 1 or any(len(row) != width for row in x):
            raise ValueError("training matrix is ragged")
        if ridge_lambda < 0 or learning_rate <= 0 or iterations < 10:
            raise ValueError("optimizer configuration is invalid")
        weights = [0.0] * width
        intercept = 0.0
        n = float(len(x))
        for _ in range(iterations):
            grad_w = [0.0] * width
            grad_b = 0.0
            for row, target in zip(x, y, strict=True):
                z = intercept + sum(w * value for w, value in zip(weights, row, strict=True))
                z = max(-35.0, min(35.0, z))
                p = 1.0 / (1.0 + math.exp(-z))
                error = p - target
                grad_b += error
                for index, value in enumerate(row):
                    grad_w[index] += error * value
            intercept -= learning_rate * grad_b / n
            for index in range(width):
                weights[index] -= learning_rate * (grad_w[index] / n + ridge_lambda * weights[index] / n)
        names = tuple(feature_names or (f"x{index}" for index in range(width)))
        if len(names) != width:
            raise ValueError("feature_names length mismatch")
        return LinearProbabilityModelV2(names, tuple(weights), intercept, ridge_lambda)

from __future__ import annotations

import itertools
import json
import math
import random
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from statistics import NormalDist, mean, pstdev
from typing import Sequence


@dataclass(frozen=True)
class StatisticalSummary:
    observations: int
    mean_return: float
    volatility: float
    sharpe: float
    skewness: float
    kurtosis: float


def summary(returns: Sequence[float], periods_per_year: int = 252) -> StatisticalSummary:
    values = [float(value) for value in returns]
    if len(values) < 3:
        raise ValueError("at least three returns are required")
    mu = mean(values)
    sigma = pstdev(values)
    if sigma <= 0:
        sharpe = 0.0
        skew = 0.0
        kurt = 3.0
    else:
        centered = [(value - mu) / sigma for value in values]
        skew = mean([value**3 for value in centered])
        kurt = mean([value**4 for value in centered])
        sharpe = math.sqrt(periods_per_year) * mu / sigma
    return StatisticalSummary(len(values), mu, sigma, sharpe, skew, kurt)


def probabilistic_sharpe_ratio(
    observed_sharpe: float,
    benchmark_sharpe: float,
    observations: int,
    skewness: float,
    kurtosis: float,
) -> float:
    if observations < 3:
        raise ValueError("PSR requires at least three observations")
    denominator = math.sqrt(
        max(
            1e-12,
            1.0 - skewness * observed_sharpe + ((kurtosis - 1.0) / 4.0) * observed_sharpe**2,
        )
    )
    z = (observed_sharpe - benchmark_sharpe) * math.sqrt(observations - 1) / denominator
    return NormalDist().cdf(z)


def expected_max_sharpe(trial_sharpes: Sequence[float]) -> float:
    values = [float(value) for value in trial_sharpes]
    if len(values) <= 1:
        return 0.0
    sigma = pstdev(values)
    if sigma == 0:
        return values[0]
    n = len(values)
    gamma = 0.5772156649015329
    normal = NormalDist()
    z1 = normal.inv_cdf(max(1e-12, min(1 - 1e-12, 1 - 1 / n)))
    z2 = normal.inv_cdf(max(1e-12, min(1 - 1e-12, 1 - 1 / (n * math.e))))
    return sigma * ((1 - gamma) * z1 + gamma * z2)


def deflated_sharpe_ratio(
    returns: Sequence[float],
    trial_sharpes: Sequence[float],
    periods_per_year: int = 252,
) -> float:
    stats = summary(returns, periods_per_year)
    threshold = expected_max_sharpe(trial_sharpes)
    return probabilistic_sharpe_ratio(
        stats.sharpe,
        threshold,
        stats.observations,
        stats.skewness,
        stats.kurtosis,
    )


def probability_backtest_overfitting(
    strategy_returns: Sequence[Sequence[float]],
    *,
    slices: int = 8,
    max_combinations: int = 5000,
) -> float:
    """CSCV-style PBO estimate from a matrix of strategy x chronological returns."""
    matrix = [list(map(float, row)) for row in strategy_returns]
    if len(matrix) < 2:
        raise ValueError("PBO requires multiple strategy/parameter trials")
    length = len(matrix[0])
    if any(len(row) != length for row in matrix) or length < slices or slices < 4 or slices % 2:
        raise ValueError("PBO matrix/slices are invalid")
    boundaries = [round(i * length / slices) for i in range(slices + 1)]
    partitions = [list(range(boundaries[i], boundaries[i + 1])) for i in range(slices)]
    combinations = list(itertools.combinations(range(slices), slices // 2))
    if len(combinations) > max_combinations:
        step = max(1, len(combinations) // max_combinations)
        combinations = combinations[::step][:max_combinations]
    logits: list[float] = []
    for train_parts in combinations:
        train_set = set(train_parts)
        train_idx = [idx for part in train_parts for idx in partitions[part]]
        test_idx = [idx for part in range(slices) if part not in train_set for idx in partitions[part]]
        train_scores = [mean([row[idx] for idx in train_idx]) for row in matrix]
        best = max(range(len(matrix)), key=lambda index: train_scores[index])
        test_scores = [mean([row[idx] for idx in test_idx]) for row in matrix]
        ordered = sorted(range(len(matrix)), key=lambda index: test_scores[index])
        rank = ordered.index(best) + 1
        omega = rank / (len(matrix) + 1)
        logits.append(math.log(omega / (1 - omega)))
    return sum(value <= 0 for value in logits) / len(logits) if logits else 1.0


def white_reality_check_pvalue(
    strategy_returns: Sequence[Sequence[float]],
    benchmark_returns: Sequence[float],
    *,
    bootstrap_samples: int = 1000,
    block_size: int = 5,
    seed: int = 7,
) -> float:
    """Deterministic circular-block bootstrap Reality-Check style p-value."""
    benchmark = list(map(float, benchmark_returns))
    matrix = [list(map(float, row)) for row in strategy_returns]
    if not matrix or len(benchmark) < 10 or any(len(row) != len(benchmark) for row in matrix):
        raise ValueError("Reality Check return matrix is invalid")
    if bootstrap_samples < 100 or block_size < 1:
        raise ValueError("bootstrap configuration is too small")
    differential = [[row[i] - benchmark[i] for i in range(len(benchmark))] for row in matrix]
    centered = [[value - mean(row) for value in row] for row in differential]
    observed = max(math.sqrt(len(benchmark)) * mean(row) for row in differential)
    rng = random.Random(seed)
    exceed = 0
    n = len(benchmark)
    for _ in range(bootstrap_samples):
        indices: list[int] = []
        while len(indices) < n:
            start = rng.randrange(n)
            indices.extend((start + offset) % n for offset in range(block_size))
        indices = indices[:n]
        statistic = max(
            math.sqrt(n) * mean([row[index] for index in indices]) for row in centered
        )
        exceed += statistic >= observed
    return (exceed + 1) / (bootstrap_samples + 1)


class ResearchRegistry:
    """Append-oriented experiment registry with one-way untouched-test locking."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path)
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS hypotheses(
              hypothesis_id TEXT PRIMARY KEY, title TEXT NOT NULL, body_json TEXT NOT NULL,
              created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS data_snapshots(
              snapshot_id TEXT PRIMARY KEY, manifest_hash TEXT NOT NULL, metadata_json TEXT NOT NULL,
              created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS experiments(
              experiment_id TEXT PRIMARY KEY, hypothesis_id TEXT NOT NULL,
              data_snapshot_id TEXT NOT NULL, code_sha TEXT NOT NULL, config_hash TEXT NOT NULL,
              created_at TEXT NOT NULL,
              FOREIGN KEY(hypothesis_id) REFERENCES hypotheses(hypothesis_id),
              FOREIGN KEY(data_snapshot_id) REFERENCES data_snapshots(snapshot_id)
            );
            CREATE TABLE IF NOT EXISTS parameter_trials(
              trial_id TEXT PRIMARY KEY, experiment_id TEXT NOT NULL, parameters_json TEXT NOT NULL,
              result_json TEXT NOT NULL, created_at TEXT NOT NULL,
              FOREIGN KEY(experiment_id) REFERENCES experiments(experiment_id)
            );
            CREATE TABLE IF NOT EXISTS statistical_tests(
              test_id TEXT PRIMARY KEY, experiment_id TEXT NOT NULL, test_name TEXT NOT NULL,
              result_json TEXT NOT NULL, created_at TEXT NOT NULL,
              FOREIGN KEY(experiment_id) REFERENCES experiments(experiment_id)
            );
            CREATE TABLE IF NOT EXISTS untouched_sets(
              split_id TEXT PRIMARY KEY, data_snapshot_id TEXT NOT NULL, definition_json TEXT NOT NULL,
              consumed_by_experiment_id TEXT, consumed_at TEXT
            );
            CREATE TABLE IF NOT EXISTS promotion_decisions(
              decision_id TEXT PRIMARY KEY, experiment_id TEXT NOT NULL, decision TEXT NOT NULL,
              evidence_json TEXT NOT NULL, created_at TEXT NOT NULL,
              FOREIGN KEY(experiment_id) REFERENCES experiments(experiment_id)
            );
            """
        )
        self.db.commit()

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _json(value: object) -> str:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)

    def register_hypothesis(self, hypothesis_id: str, title: str, body: object) -> bool:
        cur = self.db.execute(
            "INSERT OR IGNORE INTO hypotheses VALUES(?,?,?,?)",
            (hypothesis_id, title, self._json(body), self._now()),
        )
        self.db.commit()
        return cur.rowcount == 1

    def register_data_snapshot(self, snapshot_id: str, manifest_hash: str, metadata: object) -> bool:
        cur = self.db.execute(
            "INSERT OR IGNORE INTO data_snapshots VALUES(?,?,?,?)",
            (snapshot_id, manifest_hash, self._json(metadata), self._now()),
        )
        self.db.commit()
        return cur.rowcount == 1

    def register_experiment(
        self,
        experiment_id: str,
        hypothesis_id: str,
        data_snapshot_id: str,
        code_sha: str,
        config_hash: str,
    ) -> bool:
        cur = self.db.execute(
            "INSERT OR IGNORE INTO experiments VALUES(?,?,?,?,?,?)",
            (experiment_id, hypothesis_id, data_snapshot_id, code_sha, config_hash, self._now()),
        )
        self.db.commit()
        return cur.rowcount == 1

    def record_trial(self, trial_id: str, experiment_id: str, parameters: object, result: object) -> bool:
        cur = self.db.execute(
            "INSERT OR IGNORE INTO parameter_trials VALUES(?,?,?,?,?)",
            (trial_id, experiment_id, self._json(parameters), self._json(result), self._now()),
        )
        self.db.commit()
        return cur.rowcount == 1

    def lock_untouched_set(self, split_id: str, data_snapshot_id: str, definition: object) -> bool:
        cur = self.db.execute(
            "INSERT OR IGNORE INTO untouched_sets(split_id,data_snapshot_id,definition_json) VALUES(?,?,?)",
            (split_id, data_snapshot_id, self._json(definition)),
        )
        self.db.commit()
        return cur.rowcount == 1

    def consume_untouched_set(self, split_id: str, experiment_id: str) -> None:
        cur = self.db.execute(
            """UPDATE untouched_sets SET consumed_by_experiment_id=?,consumed_at=?
               WHERE split_id=? AND consumed_by_experiment_id IS NULL""",
            (experiment_id, self._now(), split_id),
        )
        self.db.commit()
        if cur.rowcount != 1:
            raise PermissionError("untouched set is missing or was already consumed")

    def trial_count(self, experiment_id: str) -> int:
        return int(
            self.db.execute(
                "SELECT COUNT(*) FROM parameter_trials WHERE experiment_id=?", (experiment_id,)
            ).fetchone()[0]
        )

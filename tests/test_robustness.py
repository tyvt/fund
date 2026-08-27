from __future__ import annotations

import numpy as np
import pandas as pd

from scripts.monte_carlo_robustness import (
    block_bootstrap_returns,
    rolling_window_backtest,
    simulate_sparse_targets,
)
from scripts.run_ablation import calculate_yearly_performance


def test_sparse_target_simulator_handles_weight_drift_and_rebalance_costs():
    dates = pd.bdate_range("2020-01-01", periods=8)
    close = pd.DataFrame(
        {"A": np.linspace(10.0, 12.0, len(dates)), "B": np.full(len(dates), 10.0)},
        index=dates,
    )
    events = [dates[1], dates[5]]
    targets = pd.DataFrame([[1.0, 0.0], [0.0, 1.0]], index=events, columns=["A", "B"])
    nav = simulate_sparse_targets(
        close,
        targets,
        events,
        commission=0.001,
        stamp_duty=0.001,
        slippage=0.0,
    )
    assert len(nav) == len(dates)
    assert nav.iloc[-1] > 1.0
    assert nav.notna().all()


def test_block_bootstrap_and_rolling_windows_are_deterministic():
    dates = pd.bdate_range("2015-01-01", "2020-12-31")
    nav = pd.Series((1.0004 ** np.arange(len(dates))), index=dates)
    events = list(dates[::21])
    first = block_bootstrap_returns(nav, events, n_iter=20, seed=7)
    second = block_bootstrap_returns(nav, events, n_iter=20, seed=7)
    np.testing.assert_allclose(first, second)
    rolling = rolling_window_backtest(nav, window_years=3)
    assert len(rolling) == 4
    assert rolling["annual_return"].gt(0.0).all()


def test_yearly_performance_uses_baseline0_and_candidate_counts():
    dates = pd.bdate_range("2019-01-01", "2020-12-31")
    navs = pd.DataFrame(
        {
            "full": 100.0 * 1.0005 ** np.arange(len(dates)),
            "baseline0": 100.0 * 1.0002 ** np.arange(len(dates)),
        },
        index=dates,
    )
    counts = {date.date().isoformat(): 100 for date in dates[::21]}
    metadata = {
        "candidate_stage_counts": {"final": counts},
        "turnover_by_date": {date: 0.2 for date in counts},
    }
    frame, acceptance = calculate_yearly_performance(navs, metadata)
    assert len(frame) == 2
    assert frame["beat_baseline0"].all()
    assert frame["avg_candidates"].eq(100.0).all()
    assert acceptance["year_count"] == 2

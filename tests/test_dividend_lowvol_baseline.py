from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from scripts.baseline_validation import _fixed_weight_path
from vbt.strategies.dividend_lowvol_baseline import (
    capped_proportional_weights,
    second_friday,
)


def test_second_friday_matches_official_review_calendar() -> None:
    assert second_friday(2024) == pd.Timestamp("2024-12-13")
    assert second_friday(2025) == pd.Timestamp("2025-12-12")


def test_capped_weights_are_fully_invested_and_strictly_capped() -> None:
    values = pd.Series([100.0, 9.0, 8.0, 7.0, 6.0, 5.0, 4.0, 3.0])
    weights = capped_proportional_weights(values, cap=0.15)
    assert weights.sum() == pytest.approx(1.0)
    assert weights.max() <= 0.15 + 1e-12
    assert weights.idxmax() == 0


def test_capped_weights_reject_infeasible_holding_count() -> None:
    with pytest.raises(ValueError, match="无法满足满仓"):
        capped_proportional_weights(pd.Series([1.0] * 6), cap=0.15)


def test_validation_segment_is_buy_and_hold_not_daily_rebalanced() -> None:
    dates = pd.date_range("2024-01-01", periods=3, freq="D")
    returns = pd.DataFrame(
        {"A": [0.10, -0.10, 0.10], "B": [-0.10, 0.10, -0.10]}, index=dates
    )
    path = _fixed_weight_path(
        returns,
        [(dates[0], pd.Series({"A": 0.5, "B": 0.5}))],
    )
    # A daily-rebalanced 50/50 portfolio would be flat every day.  Buy-and-hold
    # drifts after day one and therefore has a non-zero second-day return.
    assert path[1] != pytest.approx(0.0)
    assert np.isfinite(path).all()

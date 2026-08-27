from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from scripts.factor_quantile_diagnosis import (
    assign_quantile_returns,
    diagnose_concentration,
    summarize_quantiles,
)
from scripts.run_cost_ablation import _cost_backtest


def test_quantile_returns_preserve_low_to_high_factor_order():
    rows = []
    for month in range(1, 25):
        day = pd.Timestamp("2020-01-31") + pd.offsets.MonthEnd(month - 1)
        month_noise = (month % 4 - 1.5) * 0.001
        for value in range(1, 101):
            rows.append(
                {
                    "trade_date": day,
                    "symbol": f"{value:06d}",
                    "factor_value": float(value),
                    "forward_return": value / 10_000.0 + month_noise,
                }
            )
    portfolios = assign_quantile_returns(pd.DataFrame(rows), 5)
    summary, diagnostics = summarize_quantiles(portfolios, 5)
    annual = summary.set_index("quantile")["annual_return"]
    assert annual.is_monotonic_increasing
    assert diagnostics["best_quantile"] == "Q5"


def test_factor_direction_treats_low_volatility_q1_as_preferred():
    diagnostics = {
        "best_quantile": "Q1",
        "statistically_distinct_5pct_normal_approx": True,
        "meaningful_difference": True,
    }
    conclusion = diagnose_concentration("volatility_60d", diagnostics)
    assert "最低波动率 Q1" in conclusion
    assert "因子方向有效" in conclusion


def test_cost_variant_requires_all_three_cost_components():
    config = {
        "backtest": {"commission": 0.1, "slippage": 0.2, "stamp_duty": 0.3},
        "full_no_cost": {"commission": 0.0, "slippage": 0.0, "stamp_duty": 0.0},
    }
    assert _cost_backtest(config, "full_no_cost")["stamp_duty"] == pytest.approx(0.0)
    with pytest.raises(ValueError, match="stamp_duty"):
        _cost_backtest(
            {
                **config,
                "broken": {"commission": 0.0, "slippage": 0.0},
            },
            "broken",
        )

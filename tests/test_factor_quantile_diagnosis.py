from __future__ import annotations

import numpy as np
import pandas as pd
from scripts.factor_quantile_diagnosis import (
    assign_quantile_returns,
    diagnose_concentration,
    summarize_quantiles,
)


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

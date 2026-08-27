from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from scripts.backtest_volatility_quantiles import (
    QUANTILES,
    assign_quintiles,
    monthly_portfolio_returns,
    simulate_costed_returns,
)


def _observations(months: int = 3) -> pd.DataFrame:
    rows = []
    for month, day in enumerate(pd.date_range("2024-01-31", periods=months, freq="ME")):
        for value in range(1, 101):
            rows.append(
                {
                    "trade_date": day,
                    "symbol": f"{value:06d}",
                    "factor_value": float(value),
                    "forward_return": value / 10_000.0 + month / 100_000.0,
                }
            )
    return pd.DataFrame(rows)


def test_quintiles_are_equal_sized_and_ordered_low_to_high():
    assigned = assign_quintiles(_observations())
    counts = assigned.groupby(["trade_date", "quantile"]).size().unstack()
    assert list(counts.columns) == list(QUANTILES)
    assert counts.eq(20).all().all()
    means = assigned.groupby("quantile", observed=True)["factor_value"].mean()
    assert means.is_monotonic_increasing


def test_zero_cost_ledger_matches_gross_compounding():
    assigned = assign_quintiles(_observations())
    monthly = monthly_portfolio_returns(assigned).set_index("trade_date")
    net, _ = simulate_costed_returns(
        assigned,
        "Q3",
        commission=0.0,
        slippage=0.0,
        stamp_duty=0.0,
    )
    assert np.prod(1.0 + net) == pytest.approx(np.prod(1.0 + monthly["Q3"]))


def test_positive_costs_reduce_portfolio_value():
    assigned = assign_quintiles(_observations())
    gross, _ = simulate_costed_returns(
        assigned,
        "Q3",
        commission=0.0,
        slippage=0.0,
        stamp_duty=0.0,
    )
    costed, turnover = simulate_costed_returns(
        assigned,
        "Q3",
        commission=0.0003,
        slippage=0.001,
        stamp_duty=0.001,
    )
    assert np.prod(1.0 + costed) < np.prod(1.0 + gross)
    assert turnover > 0

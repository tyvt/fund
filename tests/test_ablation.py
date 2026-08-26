from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from scripts.run_ablation import calculate_attribution
from vbt.adapters.data_loader import VBTData
from vbt.engine import VBTEngine
from vbt.strategies.ablation import AblationStrategy, annual_rebalance_pairs
from vbt.strategies.base import BaseStrategy


def _matrix(values, dates, columns):
    return pd.DataFrame(values, index=pd.DatetimeIndex(dates), columns=columns, dtype="float32")


def _synthetic_data() -> VBTData:
    dates = pd.to_datetime(
        [
            "2020-01-14",
            "2020-01-15",
            "2020-01-16",
            "2021-01-14",
            "2021-01-15",
            "2021-01-18",
        ]
    )
    columns = pd.Index(["000001", "000002"])
    shape = (len(dates), len(columns))
    dividend = np.array(
        [
            [0.04, 0.03],
            [0.06, 0.04],  # 2020 signal: first stock wins
            [0.01, 0.20],  # execution-day flip must not leak into selection
            [0.05, 0.04],
            [0.07, 0.04],  # 2021 signal: first stock wins again
            [0.01, 0.20],
        ]
    )
    reports = pd.DataFrame(
        {
            "code": ["000001", "000002"],
            "report_year": [2019, 2019],
            "roe_pct": [12.0, 4.0],
            "debt_ratio_pct": [40.0, 40.0],
            "available_date": pd.to_datetime(["2020-04-30", "2020-04-30"]),
        }
    )
    return VBTData(
        matrices={
            "close": _matrix(np.full(shape, 10.0), dates, columns),
            "amount": _matrix(np.full(shape, 20_000_000.0), dates, columns),
            "float_mv": _matrix(np.full(shape, 1_000_000_000.0), dates, columns),
            "total_mv": _matrix(np.full(shape, 2_000_000_000.0), dates, columns),
            "is_st": _matrix(np.zeros(shape), dates, columns),
            "listed_date": pd.Series(pd.to_datetime(["2010-01-01", "2010-01-01"]), index=columns),
            "dividend_yield": _matrix(dividend, dates, columns),
            "volatility_60d": _matrix(np.full(shape, 0.10), dates, columns),
            "beta_300": _matrix(np.full(shape, 0.60), dates, columns),
            "roe_volatility": _matrix(np.full(shape, 5.0), dates, columns),
            "industry": pd.Series(["银行", "银行"], index=columns),
            "absolute_financials": reports,
        },
        metadata={"start_date": "2020-01-14", "end_date": "2021-01-18"},
    )


def test_annual_rebalance_uses_previous_trading_day():
    index = pd.to_datetime(["2020-01-14", "2020-01-15", "2020-01-16"])
    assert annual_rebalance_pairs(index) == [(pd.Timestamp("2020-01-15"), pd.Timestamp("2020-01-16"))]


def test_baseline2_uses_prior_day_factor_not_execution_day():
    targets, metadata = AblationStrategy("baseline2", {"top_n": 1}).generate_signals(
        _synthetic_data()
    )
    assert targets.loc["2020-01-16", "000001"] == pytest.approx(1.0)
    assert targets.loc["2020-01-16", "000002"] == pytest.approx(0.0)
    assert metadata["signal_dates"][0] == "2020-01-15"


def test_full_uses_absolute_financials_only_after_disclosure_date():
    params = {
        "top_n": 1,
        "max_single_weight": 1.0,
        "industry_single_max": 1.0,
        "small_cap_weight_max": 1.0,
    }
    targets, metadata = AblationStrategy("full", params).generate_signals(_synthetic_data())
    assert targets.loc["2020-01-16"].fillna(0).sum() == pytest.approx(0.0)
    assert targets.loc["2021-01-18", "000001"] == pytest.approx(1.0)
    assert targets.loc["2021-01-18", "000002"] == pytest.approx(0.0)
    assert metadata["point_in_time"] is True


def test_attribution_is_normalized_to_excess_return_and_closes():
    metrics = {
        "baseline0": {"annual_return": 0.04},
        "baseline1": {"annual_return": 0.06},
        "baseline2": {"annual_return": 0.07},
        "full": {"annual_return": 0.08},
    }
    result = calculate_attribution(metrics)
    assert result["excess_return"] == pytest.approx(0.04)
    assert sum(result["contributions"].values()) == pytest.approx(0.04)
    assert sum(result["percentages"].values()) == pytest.approx(100.0)
    assert result["closure_error"] == pytest.approx(0.0)


class _TwoStepStrategy(BaseStrategy):
    def __init__(self):
        super().__init__({"alignment_mode": False})

    def generate_signals(self, data):
        dates = data["close"].index
        weights = pd.DataFrame(np.nan, index=dates, columns=["A", "B"])
        weights.loc[dates[0]] = [1.0, 0.0]
        weights.loc[dates[1]] = [0.0, 1.0]
        return weights, {"turnover": 1.0}


def test_matrix_engine_applies_stamp_duty_only_when_selling():
    dates = pd.date_range("2024-01-02", periods=3, freq="D")
    data = SimpleNamespace(
        metadata={"start_date": "2024-01-02", "end_date": "2024-01-04"},
        __getitem__=None,
    )
    matrix_data = VBTData(
        matrices={"close": pd.DataFrame({"A": [10.0, 10.0, 10.0], "B": [10.0, 10.0, 10.0]}, index=dates)},
        metadata=data.metadata,
    )
    without_tax = VBTEngine(
        data=matrix_data,
        strategy=_TwoStepStrategy(),
        initial_capital=100.0,
        commission=0.0,
        stamp_duty_before=0.0,
        stamp_duty_after=0.0,
    ).run()
    with_tax = VBTEngine(
        data=matrix_data,
        strategy=_TwoStepStrategy(),
        initial_capital=100.0,
        commission=0.0,
        stamp_duty_before=0.10,
        stamp_duty_after=0.10,
    ).run()
    assert without_tax.nav.iloc[-1] == pytest.approx(100.0)
    assert with_tax.nav.iloc[-1] < without_tax.nav.iloc[-1]
    assert with_tax.metadata["stamp_duty_model"] == "sell_only_two_pass"

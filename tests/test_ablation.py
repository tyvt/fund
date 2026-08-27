from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from scripts.run_ablation import calculate_attribution, experiment_params
from vbt.adapters.data_loader import VBTData
from vbt.engine import VBTEngine
from vbt.strategies.ablation import (
    AblationStrategy,
    annual_rebalance_pairs,
    rebalance_pairs,
)
from vbt.strategies.base import BaseStrategy
from vbt.strategies.dividend_lowvol import (
    apply_rebalance_buffer,
    build_rebalance_hold_eligible,
)
from vbt.strategies.signal_generators import (
    apply_percentile_filters,
    compute_fusion_score,
    filter_volatility_band,
    filter_volatility_top,
    select_by_fusion_score,
    select_stocks_with_fallback,
)


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


def test_month_end_signal_executes_on_next_trading_day():
    index = pd.to_datetime(
        ["2020-01-30", "2020-01-31", "2020-02-03", "2020-02-28", "2020-03-02"]
    )
    assert rebalance_pairs(index, freq="M", day=-1) == [
        (pd.Timestamp("2020-01-31"), pd.Timestamp("2020-02-03")),
        (pd.Timestamp("2020-02-28"), pd.Timestamp("2020-03-02")),
    ]


def test_baseline2_uses_prior_day_factor_not_execution_day():
    targets, metadata = AblationStrategy("baseline2", {"top_n": 1}).generate_signals(
        _synthetic_data()
    )
    assert targets.loc["2020-01-16", "000001"] == pytest.approx(1.0)
    assert targets.loc["2020-01-16", "000002"] == pytest.approx(0.0)
    assert metadata["signal_dates"][0] == "2020-01-15"


def test_dividend_yield_only_selects_top30_after_hard_constraints():
    dates = pd.to_datetime(["2020-01-30", "2020-01-31", "2020-02-03"])
    columns = pd.Index([f"{index:06d}" for index in range(1, 41)])
    shape = (len(dates), len(columns))
    data = VBTData(
        matrices={
            "close": _matrix(np.full(shape, 10.0), dates, columns),
            "amount": _matrix(np.full(shape, 20_000_000.0), dates, columns),
            "float_mv": _matrix(np.full(shape, 1_000_000_000.0), dates, columns),
            "is_st": _matrix(np.zeros(shape), dates, columns),
            "listed_date": pd.Series(pd.Timestamp("2010-01-01"), index=columns),
            "dividend_yield": _matrix(
                np.tile(np.arange(1.0, 41.0), (len(dates), 1)), dates, columns
            ),
        },
        metadata={"start_date": "2020-01-30", "end_date": "2020-02-03"},
    )
    targets, metadata = AblationStrategy(
        "dividend_yield_only", {"top_n": 30, "rebalance_freq": "M"}
    ).generate_signals(data)
    selected = targets.loc["2020-02-03"].dropna().gt(0)
    assert int(selected.sum()) == 30
    assert not selected.loc["000001"]
    assert selected.loc["000040"]
    assert metadata["strategy"] == "dividend_yield_only"


def test_full_fallback_remains_invested_before_financial_disclosure():
    params = {
        "top_n": 1,
        "min_holdings": 1,
        "max_single_weight": 1.0,
        "industry_single_max": 1.0,
        "small_cap_weight_max": 1.0,
    }
    targets, metadata = AblationStrategy("full", params).generate_signals(_synthetic_data())
    assert targets.loc["2020-01-16"].fillna(0).sum() == pytest.approx(1.0)
    assert targets.loc["2021-01-18", "000001"] == pytest.approx(1.0)
    assert targets.loc["2021-01-18", "000002"] == pytest.approx(0.0)
    assert metadata["fallback_tiers"]["2020-01-16"] == "percentile_relaxed"
    assert metadata["point_in_time"] is True


def test_percentile_filters_are_one_sided_for_roe_and_debt():
    frame = pd.DataFrame([[1.0, 2.0, 3.0, 4.0, 5.0]], columns=list("ABCDE"))
    roe = apply_percentile_filters(frame, lower_pct=0.20, upper_pct=None)
    debt = apply_percentile_filters(frame, lower_pct=None, upper_pct=0.80)
    assert int(roe.sum(axis=1).iloc[0]) == 4
    assert int(debt.sum(axis=1).iloc[0]) == 4


def test_volatility_band_excludes_lowest_and_highest_quintiles():
    day = pd.Timestamp("2024-01-31")
    volatility = pd.DataFrame(
        [np.arange(1.0, 11.0)], index=[day], columns=[f"S{i}" for i in range(10)]
    )
    mask = filter_volatility_band(volatility)
    assert mask.loc[day].tolist() == [False, False, True, True, True, True, True, True, False, False]


def test_volatility_top_selects_exactly_the_ten_lowest_names():
    day = pd.Timestamp("2024-01-31")
    columns = [f"S{i:02d}" for i in range(15)]
    volatility = pd.DataFrame(
        [[5.0, 1.0, 3.0, np.nan, 2.0, 4.0, *range(6, 15)]],
        index=[day],
        columns=columns,
    )
    mask = filter_volatility_top(volatility, top_n=10)
    expected = volatility.loc[day].dropna().nsmallest(10).index
    assert int(mask.loc[day].sum()) == 10
    assert set(mask.columns[mask.loc[day]]) == set(expected)


def test_top_buffer_retention_requires_both_volatility_and_dividend_top30():
    day = pd.Timestamp("2024-01-31")
    columns = pd.Index([f"S{i:02d}" for i in range(40)])
    volatility = pd.DataFrame([np.arange(40.0)], index=[day], columns=columns)
    dividend = pd.DataFrame([np.arange(40.0)], index=[day], columns=columns)
    hard = pd.DataFrame(True, index=[day], columns=columns)
    qualified = build_rebalance_hold_eligible(
        volatility,
        dividend,
        hard_eligible=hard,
        volatility_mode="top",
        volatility_top_n=10,
        dividend_top_n=10,
        hold_threshold_multiplier=3,
    )
    # Lowest-volatility Top30 is S00-S29; highest-dividend Top30 is S10-S39.
    assert set(qualified.columns[qualified.loc[day]]) == set(columns[10:30])


def test_rollback_experiment_reads_nested_overrides():
    config = {
        "rollback_top_buffer": {
            "name": "rollback_top_buffer",
            "extends": "full",
            "overrides": {
                "volatility_filter": {"mode": "top", "top_n": 10},
                "rebalance_buffer": {"enabled": True, "max_sell_per_month": 3},
            },
        }
    }
    overrides = experiment_params(config, "rollback_top_buffer")
    assert overrides["volatility_filter"] == {"mode": "top", "top_n": 10}
    assert overrides["rebalance_buffer"]["max_sell_per_month"] == 3


def test_rebalance_buffer_enforces_absolute_sell_cap_and_defers_the_rest():
    current = [
        {"symbol": f"OLD{i}", "score": 1.0 - i / 10, "score_decay": i}
        for i in range(6)
    ]
    candidates = [
        {"symbol": f"NEW{i}", "score": 2.0 - i / 10, "qualified": True}
        for i in range(6)
    ]
    final, trades = apply_rebalance_buffer(
        current, candidates, max_sell=3, target_count=6
    )
    assert len(trades["sell"]) == 3
    assert len(trades["buy"]) == 3
    assert len(final) == 6
    assert {_item["symbol"] for _item in trades["sell"]} == {"OLD3", "OLD4", "OLD5"}


def test_rebalance_buffer_does_not_rebuy_a_name_sold_in_the_same_period():
    current = [
        {"symbol": "A", "score": 1.0, "score_decay": 1.0},
        {"symbol": "B", "score": 0.9, "score_decay": 0.5},
    ]
    candidates = [
        {"symbol": "A", "score": 2.0, "qualified": False},
        {"symbol": "C", "score": 1.8, "qualified": True},
    ]
    final, trades = apply_rebalance_buffer(
        current, candidates, max_sell=1, target_count=2
    )
    sold = {_item["symbol"] for _item in trades["sell"]}
    bought = {_item["symbol"] for _item in trades["buy"]}
    assert sold == {"A"}
    assert bought == {"C"}
    assert sold.isdisjoint(bought)
    assert {_item["symbol"] for _item in final} == {"B", "C"}


def test_full_band_fallback_never_reintroduces_extreme_volatility():
    dates = pd.to_datetime(["2020-01-30", "2020-01-31", "2020-02-03"])
    columns = pd.Index([f"S{index:02d}" for index in range(20)])
    shape = (len(dates), len(columns))
    data = VBTData(
        matrices={
            "close": _matrix(np.full(shape, 10.0), dates, columns),
            "amount": _matrix(np.full(shape, 20_000_000.0), dates, columns),
            "float_mv": _matrix(np.full(shape, 1_000_000_000.0), dates, columns),
            "total_mv": _matrix(np.full(shape, 2_000_000_000.0), dates, columns),
            "is_st": _matrix(np.zeros(shape), dates, columns),
            "listed_date": pd.Series(pd.Timestamp("2010-01-01"), index=columns),
            "dividend_yield": _matrix(np.full(shape, 0.04), dates, columns),
            "volatility_60d": _matrix(
                np.tile(np.arange(1.0, 21.0), (len(dates), 1)), dates, columns
            ),
            "beta_300": _matrix(np.full(shape, 0.60), dates, columns),
            "roe_volatility": _matrix(np.full(shape, 5.0), dates, columns),
            "industry": pd.Series({column: f"I{index}" for index, column in enumerate(columns)}),
            "absolute_financials": pd.DataFrame(),
        },
        metadata={"start_date": "2020-01-30", "end_date": "2020-02-03"},
    )
    params = {
        "rebalance_freq": "M",
        "top_n": 10,
        "min_holdings": 10,
        "max_single_weight": 0.10,
        "industry_single_max": 1.0,
        "small_cap_weight_max": 1.0,
        "volatility_filter": {
            "mode": "band",
            "lower_quantile": 0.20,
            "upper_quantile": 0.80,
        },
    }
    targets, metadata = AblationStrategy("full", params).generate_signals(data)
    selected = targets.loc["2020-02-03"].fillna(0).gt(0)
    assert selected.loc[columns[:4]].sum() == 0
    assert selected.loc[columns[16:]].sum() == 0
    assert int(selected.sum()) == 10
    assert metadata["volatility_filter_mode"] == "band"


def test_full_investment_expands_past_ten_without_breaking_caps():
    columns = pd.Index([f"S{index:02d}" for index in range(30)])
    day = pd.Timestamp("2024-02-01")
    factor = pd.DataFrame(
        [np.linspace(0.06, 0.03, len(columns))], index=[day], columns=columns
    )
    eligible = pd.DataFrame(True, index=[day], columns=columns)
    industries = pd.Series(
        {column: f"industry_{index % 6}" for index, column in enumerate(columns)}
    )
    _, weights, _ = select_stocks_with_fallback(
        factor,
        primary_eligible=eligible,
        relaxed_eligible=eligible,
        hard_eligible=eligible,
        top_n=10,
        min_holdings=10,
        fallback_top_n=100,
        min_investment_pct=1.0,
        max_single_weight=0.08,
        industries=industries,
        industry_max=0.20,
    )
    assert weights.loc[day].sum() == pytest.approx(1.0, abs=1e-8)
    assert weights.loc[day].max() <= 0.08 + 1e-10
    assert int(weights.loc[day].gt(1e-12).sum()) >= 13
    for industry in industries.unique():
        members = industries[industries.eq(industry)].index
        assert weights.loc[day, members].sum() <= 0.20 + 1e-10


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


def test_fusion_score_rewards_high_dividend_and_low_volatility():
    frame = pd.DataFrame(
        {
            "dividend_yield": [0.02, 0.04, 0.06],
            "volatility_60d": [0.30, 0.20, 0.10],
        },
        index=list("ABC"),
    )
    score = compute_fusion_score(frame)
    assert score.loc["C"] > score.loc["B"] > score.loc["A"]


def test_matrix_fusion_selection_respects_candidate_width():
    day = pd.Timestamp("2024-01-31")
    columns = [f"S{index}" for index in range(8)]
    dividend = pd.DataFrame([np.arange(8.0)], index=[day], columns=columns)
    volatility = pd.DataFrame([np.arange(8.0, 0.0, -1.0)], index=[day], columns=columns)
    score = compute_fusion_score(dividend, volatility)
    selected = select_by_fusion_score(score, top_n=3)
    assert int(selected.loc[day].sum()) == 3
    assert set(selected.columns[selected.loc[day]]) == {"S5", "S6", "S7"}


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

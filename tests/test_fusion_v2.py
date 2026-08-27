from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from scripts.compute_factor_t_stats import (
    newey_west_standard_error,
    parse_period,
    summarize_ic_series,
)
from scripts.run_ablation import fusion_cli_overrides
from scripts.update_orthogonality_report import update_report_text
from scripts.audit_period_metrics import calculate_period_metrics
from scripts.compute_holding_matrix import build_holding_matrix
from scripts.factor_builder_extended import (
    compute_fcf_ev,
    compute_pe_industry_quantile,
    compute_reversal,
)
from vbt.strategies.capital_deployment import deploy_new_capital
from vbt.strategies.dividend_lowvol import (
    apply_hold_bonus,
    build_cost_aware_selection,
    should_trade,
)
from vbt.strategies.signal_generators import compute_fusion_score


def test_generic_fusion_respects_direction_and_positional_api():
    date = pd.Timestamp("2024-01-31")
    factors = {
        "quality": pd.DataFrame([[1.0, 2.0, 3.0]], index=[date], columns=list("ABC")),
        "risk": pd.DataFrame([[3.0, 2.0, 1.0]], index=[date], columns=list("ABC")),
    }
    score = compute_fusion_score(
        factors, {"quality": 0.5, "risk": 0.5}, ["quality", "risk"],
        directions={"quality": 1, "risk": -1},
    )
    assert score.loc[date, "C"] > score.loc[date, "B"] > score.loc[date, "A"]


def test_pe_non_positive_is_neutral_l3():
    frame = pd.DataFrame(
        {
            "trade_date": pd.to_datetime(["2024-01-31"] * 3),
            "symbol": ["A", "B", "C"],
            "industry": ["I", "I", "I"],
            "pe_ttm": [-10.0, 0.0, np.nan],
        }
    )
    assert compute_pe_industry_quantile(frame).tolist() == [0.5, 0.5, 0.5]


def test_fcf_ev_and_reversal_formulas():
    frame = pd.DataFrame(
        {
            "net_operate_cash_flow": [120.0],
            "capital_expenditure": [-20.0],
            "total_mv": [800.0],
            "total_liability": [300.0],
            "cash": [100.0],
        }
    )
    assert compute_fcf_ev(frame).iloc[0] == pytest.approx(0.10)
    prices = pd.DataFrame(
        {"symbol": ["A", "A", "A"], "close": [10.0, 11.0, 12.1]}
    )
    assert compute_reversal(prices, 2).iloc[-1] == pytest.approx(-0.21)


def test_hold_bonus_cost_hurdle_and_stateful_selection():
    score = pd.Series({"A": 0.50, "B": 0.55})
    assert apply_hold_bonus(score, ["A"], 0.10).loc["A"] == pytest.approx(0.55)
    assert should_trade(0.50, 0.52, 0.01)
    assert not should_trade(0.50, 0.505, 0.01)
    matrix = pd.DataFrame(
        [[0.9, 0.8, 0.7], [0.85, 0.84, 0.86]],
        index=pd.to_datetime(["2024-01-31", "2024-02-29"]),
        columns=list("ABC"),
    )
    selected, _ = build_cost_aware_selection(
        matrix, hard_eligible=matrix.notna(), candidate_n=3, top_n=2,
        hold_bonus=0.10, cost_threshold=0.01,
    )
    assert selected.sum(axis=1).tolist() == [2, 2]
    assert set(selected.columns[selected.iloc[1]]) == {"A", "B"}


def test_capital_deployment_prioritizes_incumbent_and_obeys_daily_cap():
    result = deploy_new_capital(
        {"A": 50_000.0},
        [
            {"symbol": "A", "score": 1.0, "target_weight": 0.5},
            {"symbol": "B", "score": 0.9, "target_weight": 0.5},
        ],
        100_000.0,
    )
    assert result["invested"] == pytest.approx(30_000.0)
    assert [order["symbol"] for order in result["orders"]] == ["A", "B"]
    waiting = deploy_new_capital({}, ["A"], 100_000.0, market_overvalued=True)
    assert waiting["invested"] == 0.0
    assert waiting["cash_remaining"] == 100_000.0


def test_t_stat_period_parser_and_iid_standard_error():
    start, end = parse_period("2015-2019")
    assert (start.isoformat(), end.isoformat()) == ("2015-01-01", "2019-12-31")
    values = np.array([-1.0, 0.0, 1.0])
    standard_error, lag = newey_west_standard_error(values, lag=0)
    assert lag == 0
    assert standard_error == pytest.approx(np.std(values, ddof=0) / np.sqrt(3))


def test_t_stat_gate_is_inclusive_at_two():
    # This construction has a population-standard-error t statistic of 2.
    third = (7.0 - 2.0 * np.sqrt(6.0)) / 5.0
    result = summarize_ic_series(pd.Series([0.0, 1.0, third]), lag=0)
    assert result["t_statistic"] == pytest.approx(2.0)
    assert result["gate_2_pass"] is True


def test_fusion_cli_equal_weights_only_selected_factors():
    result = fusion_cli_overrides(
        factors="volatility_60d,fcf_ev,reversal_10d",
        equal_weight=True,
    )
    assert result["fusion_factors"] == ["volatility_60d", "fcf_ev", "reversal_10d"]
    assert result["fusion_min_valid_factors"] == 3
    assert all(
        value == pytest.approx(1 / 3)
        for value in result["fusion_weights"].values()
    )


def test_default_main_config_contains_only_t_gate_passers():
    result = fusion_cli_overrides(factors=None, equal_weight=True)
    assert result["fusion_factors"] == ["volatility_60d", "fcf_ev", "reversal_10d"]


def test_orthogonality_fix_replaces_false_no_conflict_claim():
    source = (
        "# report\n\n- 最终因子：`A`\n\n"
        "诊断通过口径：IC均值≥0.010、IC_IR≥0.15、纯化IC均值≥0.010。\n\n"
        "## 高相关冲突\n\n- 无 |r| > 0.7 的冲突对。\n\n## 下一节\n"
    )
    fixed = update_report_text(
        source,
        [("reversal_5d", "reversal_10d", 0.7124)],
        threshold=0.70,
    )
    assert "无 |r|" not in fixed
    assert "判定阈值：**|r| ≥ 0.70**" in fixed
    assert "⚠️ 高相关冲突对（竞争上岗已裁决）" in fixed
    assert "r=0.7124" in fixed
    assert "正交性阶段候选因子（非主配置裁决）" in fixed
    assert "主配置唯一准入闸门" in fixed


def test_period_metrics_use_period_start_as_new_base():
    dates = pd.bdate_range("2020-01-01", periods=253)
    nav = pd.Series(100.0 * 1.10 ** (np.arange(253) / 252.0), index=dates)
    result = calculate_period_metrics(nav)
    assert result["annual_return"] == pytest.approx(0.10)
    assert result["max_drawdown"] == pytest.approx(0.0)


def test_holding_matrix_coverage_uses_board_lot_budget():
    prices = pd.Series({"A": 10.0, "B": 20.0, "C": 60.0, "D": 80.0})
    frame = build_holding_matrix(
        prices,
        [10_000.0],
        target_holdings=4,
        lot_size=100,
        buy_cost_rate=0.0,
    )
    assert frame.iloc[0]["affordable_target_stocks"] == 2
    assert frame.iloc[0]["coverage"] == pytest.approx(0.5)
    assert frame.iloc[0]["recommended_holdings"] == 2

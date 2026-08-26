from pathlib import Path
from types import SimpleNamespace

import nbformat
import numpy as np
import pandas as pd
import pytest

from vbt.config import load_backtest_config, load_strategy_config
from vbt.engine.engine import BacktestResults
from vbt.engine.parameter_scan import ParameterScan
from vbt.strategies.signal_generators import apply_industry_cap, compute_weight_by_factor


ROOT = Path(__file__).resolve().parents[1]


def test_config_defaults_are_alignment_safe():
    strategy = load_strategy_config()
    backtest = load_backtest_config()
    assert strategy["alignment_mode"] is True
    assert strategy["alignment_overrides"] == {}
    assert backtest["price_source"] == "stock_daily"
    assert Path(backtest["baseline_path"]).name.startswith("rqalpha_parquet_10y_")


def test_weight_cap_never_allocates_to_unselected_stocks():
    factor = pd.DataFrame([[4.0, 3.0, 2.0]], columns=["A", "B", "C"])
    selected = pd.DataFrame([[True, True, False]], columns=factor.columns)
    weights = compute_weight_by_factor(factor, selected=selected, max_weight=0.4)
    assert weights.loc[0, "C"] == 0.0
    assert weights.loc[0].max() <= 0.4 + 1e-12
    assert weights.loc[0].sum() == pytest.approx(0.8)


def test_industry_cap_leaves_excess_as_cash():
    weights = pd.DataFrame([[0.4, 0.4, 0.2]], columns=["A", "B", "C"])
    capped = apply_industry_cap(
        weights,
        {"A": "能源", "B": "能源", "C": "消费"},
        max_weight=0.5,
    )
    assert capped.loc[0, ["A", "B"]].sum() == pytest.approx(0.5)
    assert capped.loc[0].sum() == pytest.approx(0.7)


def test_result_return_uses_configured_initial_capital():
    result = BacktestResults(
        portfolio=None,
        nav=pd.Series([99_980.0, 110_000.0], index=pd.date_range("2024-01-01", periods=2)),
        positions=pd.DataFrame(),
        shares=pd.DataFrame(),
        trades=pd.DataFrame(),
        holdings=pd.DataFrame(),
        stock_summary=pd.DataFrame(),
        dividend_taxes=pd.DataFrame(),
        metadata={"initial_capital": 100_000.0},
    )
    assert result.total_return == pytest.approx(0.10)


def test_sparse_scan_values_constant_share_intervals():
    dates = pd.date_range("2024-01-01", periods=3)
    engine = SimpleNamespace(
        data={"close": pd.DataFrame({"A": [10.0, 11.0, 12.0]}, index=dates)},
        initial_capital=100.0,
        commission=0.0,
        slippage=0.0,
    )
    targets = pd.DataFrame({"A": [0.5, np.nan, np.nan]}, index=dates)
    metrics = ParameterScan(engine=engine, param_grid={})._simulate_targets(targets)
    assert metrics["total_return"] == pytest.approx(0.10)
    assert metrics["turnover"] == pytest.approx(50.0 / 105.0)
    assert metrics["backend"] == "sparse_interval"


def test_all_workbench_notebooks_have_valid_python_cells():
    paths = sorted((ROOT / "notebooks").rglob("*.ipynb"))
    assert len(paths) == 8
    for path in paths:
        notebook = nbformat.read(path, as_version=4)
        assert notebook.cells
        for index, cell in enumerate(notebook.cells):
            if cell.cell_type == "code":
                compile(cell.source, f"{path}:cell-{index}", "exec")

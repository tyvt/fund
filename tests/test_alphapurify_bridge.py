import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from alphapurify_bridge.adapters import FactorDataCache, SnapshotAdapter
from alphapurify_bridge.diagnostics import DiagnosisRunner
from alphapurify_bridge.diagnostics.metrics import compute_ic, compute_quantile_return
from alphapurify_bridge.diagnostics.official import official_version, run_official_diagnostics
from alphapurify_bridge.filters import ThresholdFilter
from alphapurify_bridge.io import write_approved_factors
from alphapurify_bridge.reporting import DiagnosisReporter
from alphapurify_bridge.utils import PERF_STAGE_NAMES, PerformanceLog, StageTimer
from scripts.run_factor_diagnosis import _fast_config
from vbt.config import load_approved_factors


def _adapter_fixture(tmp_path: Path) -> SnapshotAdapter:
    snapshots = tmp_path / "snapshots"
    stock = tmp_path / "stock_daily" / "year=2024"
    calendar = tmp_path / "calendar.parquet"
    snapshots.mkdir()
    stock.mkdir(parents=True)
    (snapshots / "manifest.json").write_text(json.dumps({"factors": ["alpha"]}), encoding="utf-8")
    for trade_date, values in (
        ("2024-01-02", [1.0, 2.0]),
        ("2024-01-03", [1.5, 2.5]),
        ("2024-01-04", [2.0, 3.0]),
    ):
        target = snapshots / f"trade_date={trade_date}"
        target.mkdir()
        pd.DataFrame({"symbol": ["000001", "000002"], "alpha": values}).to_parquet(target / "factors.parquet", index=False)
    prices = pd.DataFrame(
        {
            "trade_date": ["2024-01-02", "2024-01-02", "2024-01-02", "2024-01-03", "2024-01-03", "2024-01-04", "2024-01-04"],
            "symbol": ["000001", "000001", "000002", "000001", "000002", "000001", "000002"],
            "close": [10.0, 10.0, 20.0, 11.0, 18.0, 12.1, 16.2],
        }
    )
    prices.to_parquet(stock / "part.parquet", index=False)
    pd.DataFrame({"trade_date": ["2024-01-02", "2024-01-03", "2024-01-04"]}).to_parquet(calendar, index=False)
    return SnapshotAdapter(snapshots, tmp_path / "stock_daily", calendar_path=calendar)


def test_forward_return_has_correct_direction_and_deduplicates(tmp_path):
    frame = _adapter_fixture(tmp_path).load_factor("alpha", "2024-01-02", "2024-01-03", horizon=1)
    first = frame[(frame["trade_date"] == pd.Timestamp("2024-01-02")) & (frame["symbol"] == "000001")].iloc[0]
    assert first["forward_return"] == pytest.approx(0.10)
    assert len(frame) == 4
    assert list(frame.columns) == ["trade_date", "symbol", "factor_value", "forward_return"]


def test_negative_direction_orients_ic_positive():
    frame = pd.DataFrame(
        {
            "trade_date": np.repeat(pd.date_range("2024-01-01", periods=4), 5),
            "factor_value": np.tile(np.arange(5.0), 4),
            "forward_return": np.tile(-np.arange(5.0), 4),
        }
    )
    assert compute_ic(frame, direction=-1, min_observations=5).mean() == pytest.approx(1.0)


def test_quantile_monotonicity_uses_oriented_values():
    frame = pd.DataFrame(
        {
            "trade_date": np.repeat(pd.date_range("2024-01-01", periods=4), 100),
            "factor_value": np.tile(np.arange(100.0), 4),
            "forward_return": np.tile(np.arange(100.0) / 10_000, 4),
        }
    )
    result = compute_quantile_return(frame, n_quantiles=5, rebalance_freq="D")
    assert result["monotonicity"] is True
    assert result["monotonicity_rank_corr"] == pytest.approx(1.0)


def test_decay_is_warning_and_does_not_fail_factor():
    result = ThresholdFilter(
        {"ic_mean_min": 0.015, "ic_ir_min": 0.3, "spread_return_min": 0.02, "max_ic_decay": 0.5}
    ).evaluate(
        {
            "ic_mean": 0.02,
            "ic_ir": 0.5,
            "spread_return": 0.03,
            "quantile_monotonicity": True,
            "ic_decay": {"horizon_1": 0.0, "horizon_5": 0.75},
        }
    )
    assert result["status"] == "PASS"
    assert result["checks"]["ic_decay"]["status"] == "WARNING"


def test_vectorbt_reads_approved_factor_artifact(tmp_path):
    target = tmp_path / "approved.json"
    write_approved_factors(
        [{"factor_name": "alpha", "status": "PASS", "data_version": "v", "alphapurify_version": "1.0.6"}],
        target,
    )
    assert load_approved_factors(target, required=True) == ["alpha"]


def test_official_alphapurify_adapter_smoke():
    rng = np.random.default_rng(11)
    dates = pd.bdate_range("2024-01-02", periods=45)
    rows = []
    for symbol in range(12):
        close = 10.0
        for trade_date in dates:
            factor = rng.normal()
            close *= 1.0 + rng.normal(scale=0.01)
            rows.append((trade_date, f"{symbol:06d}", close, factor))
    frame = pd.DataFrame(rows, columns=["trade_date", "symbol", "close", "factor"])
    result = run_official_diagnostics(
        frame,
        factor_col="factor",
        horizons=[1],
        n_quantiles=5,
        rebalance_freq="M",
        ic_method="spearman",
        rolling_window=5,
        max_workers=1,
    )
    assert official_version() == "1.0.6"
    assert "horizon_1" in result["ic_mean"]


def test_factor_data_cache_is_bounded_lru():
    cache = FactorDataCache[int](max_entries=2)
    cache.set("a", 1)
    cache.set("b", 2)
    assert cache.get("a") == 1
    cache.set("c", 3)
    assert cache.get("b") is None
    assert cache.get("a") == 1
    assert cache.get("c") == 3


def test_factor_data_cache_supports_factor_date_keys():
    cache = FactorDataCache[dict](max_entries=2)
    cache.set_factor_data("alpha", "2024-01-01", "2024-01-31", {"rows": 10}, (1, 5), 10)
    assert cache.get_factor_data("alpha", "2024-01-01", "2024-01-31", (1, 5), 10) == {"rows": 10}
    assert cache.get_factor_data("alpha", "2024-02-01", "2024-02-29", (1, 5), 10) is None


def test_stage_timer_and_performance_log_have_complete_stages(tmp_path):
    timer = StageTimer()
    with timer.measure("metrics"):
        pass
    target = tmp_path / "perf.log"
    PerformanceLog(target).factor("alpha", stages=timer.stages, total=0.1, rows=10, dates=2)
    payload = json.loads(target.read_text(encoding="utf-8"))
    assert tuple(payload["stages"]) == PERF_STAGE_NAMES
    assert payload["stages"]["metrics"] >= 0.0


def test_fast_config_does_not_mutate_full_configuration():
    original = {
        "diagnosis": {"horizons": [1, 5, 10, 20, 40], "primary_horizon": 1, "n_quantiles": 10},
        "output": {},
    }
    fast = _fast_config(original)
    assert original["diagnosis"]["n_quantiles"] == 10
    assert original["diagnosis"]["horizons"] == [1, 5, 10, 20, 40]
    assert fast["diagnosis"]["n_quantiles"] == 5
    assert fast["diagnosis"]["horizons"] == [1, 5, 20]


def test_report_formats_reuse_chart_generation(tmp_path, monkeypatch):
    reporter = DiagnosisReporter(tmp_path)
    calls = []
    monkeypatch.setattr(reporter, "_generate_charts", lambda _result: calls.append(1) or {})
    result = {
        "factor_name": "alpha",
        "direction": 1,
        "sample_count": 1,
        "checks": {},
        "ic_by_horizon": {},
        "ic_decay": {},
        "status": "PASS",
    }
    paths = reporter.generate_factor_reports(result, ("md", "html"))
    assert len(calls) == 1
    assert len(paths) == 2 and all(path.is_file() for path in paths)


def test_runner_profile_reports_cache_hit_without_changing_values(tmp_path):
    adapter = _adapter_fixture(tmp_path)
    config = {
        "diagnosis": {
            "start_date": "2024-01-02",
            "end_date": "2024-01-03",
            "horizons": [1],
            "primary_horizon": 1,
            "n_quantiles": 5,
            "rebalance_freq": "D",
            "min_cross_section": 2,
        }
    }
    registry = {"factors": {"alpha": {"direction": 1, "version": "test"}}}
    runner = DiagnosisRunner(config, adapter=adapter, registry=registry)
    first = runner.diagnose_factor("alpha", official=False, profile=True)
    second = runner.diagnose_factor("alpha", official=False, profile=True)
    assert first["ic_mean"] == pytest.approx(second["ic_mean"])
    assert runner.last_profiles["alpha"]["stages"]["data_load"] == 0.0
    assert tuple(runner.last_profiles["alpha"]["stages"]) == PERF_STAGE_NAMES

"""Optional audit adapter for the upstream AlphaPurify FactorAnalyzer."""

from __future__ import annotations

import gc
import importlib
import os
import shutil
import tempfile
from typing import Any, Sequence

import pandas as pd


def official_version() -> str | None:
    try:
        import alphapurify

        return str(alphapurify.__version__)
    except ImportError:
        return None


def run_official_diagnostics(
    frame: pd.DataFrame,
    *,
    factor_col: str,
    horizons: Sequence[int],
    n_quantiles: int,
    rebalance_freq: str,
    ic_method: str,
    rolling_window: int,
    max_workers: int = 1,
) -> dict[str, Any]:
    """Run upstream AlphaPurify and normalize its public result attributes."""

    try:
        from alphapurify import AnalysisConfig, FactorAnalyzer, ResearchConfig
    except ImportError as exc:
        raise RuntimeError(
            "缺少官方 alphapurify；请在 .venv-vectorbt 中安装 alphapurify==1.0.6"
        ) from exc
    required = ["trade_date", "symbol", "close", factor_col]
    panel = frame.loc[:, required].dropna().copy()
    panel["trade_date"] = pd.to_datetime(panel["trade_date"])
    analyzer = FactorAnalyzer(
        base_df=panel,
        trade_date_col="trade_date",
        symbol_col="symbol",
        price_col="close",
        factor_name=factor_col,
        research_cfg=ResearchConfig(
            rebalance_periods=(str(rebalance_freq).upper(),),
            return_horizons=tuple(int(value) for value in horizons),
            horizon_rolling_period=int(rolling_window),
            bins=int(n_quantiles),
        ),
        analysis_cfg=AnalysisConfig(
            rank_ic=str(ic_method).lower() == "spearman",
            max_workers=max(1, int(max_workers)),
        ),
    )
    # AlphaPurify 1.0.6 keeps an Arrow mapping alive until after its temporary
    # directory context exits on Windows. Suppress that premature cleanup once,
    # release the upstream worker frame, then remove the recorded directories.
    created_temp_dirs: list[str] = []
    factor_module = importlib.import_module("alphapurify.FactorAnalyzer")
    original_temporary_directory = tempfile.TemporaryDirectory
    factor_module._worker_df = None

    class _WindowsTemporaryDirectory(original_temporary_directory):
        def __init__(self, *args, **kwargs):
            kwargs["ignore_cleanup_errors"] = True
            super().__init__(*args, **kwargs)
            created_temp_dirs.append(self.name)

    if os.name == "nt":
        tempfile.TemporaryDirectory = _WindowsTemporaryDirectory
    try:
        analyzer.run()
    finally:
        tempfile.TemporaryDirectory = original_temporary_directory
        factor_module._worker_df = None
        gc.collect()
        if os.name == "nt":
            for directory in created_temp_dirs:
                shutil.rmtree(directory, ignore_errors=True)
    ic_key = "rank_ic" if str(ic_method).lower() == "spearman" else "ic"
    ic_series: dict[str, list[dict[str, Any]]] = {}
    for horizon, values in analyzer.ics_dict.items():
        ic_series[f"horizon_{horizon}"] = [
            {"trade_date": pd.Timestamp(row.trade_date).date().isoformat(), "value": float(getattr(row, ic_key))}
            for row in values.loc[:, ["trade_date", ic_key]].itertuples(index=False)
        ]
    return {
        "version": official_version(),
        "ic_mean": {f"horizon_{key}": float(value) for key, value in analyzer.mean_ics_dict.items()},
        "ic_series": ic_series,
        "ic_stats": analyzer.ic_stats_panel.to_dict(orient="records"),
        "long_short_stats": analyzer.ls_stats_panel.to_dict(orient="records"),
    }


__all__ = ["official_version", "run_official_diagnostics"]

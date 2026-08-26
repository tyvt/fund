"""Orchestrate factor loading, metrics, thresholds, and optional official audit."""

from __future__ import annotations

from datetime import datetime, timezone
import logging
from pathlib import Path
import time
from typing import Any, Mapping, Sequence

import numpy as np
import pandas as pd

from alphapurify_bridge.adapters import SnapshotAdapter
from alphapurify_bridge.config import (
    DEFAULT_CONFIG_PATH,
    DEFAULT_REGISTRY_PATH,
    data_version,
    deep_merge,
    load_diagnosis_config,
    load_factor_registry,
)
from alphapurify_bridge.diagnostics.metrics import (
    compute_factor_returns,
    compute_histogram,
    compute_ic,
    compute_ir,
    compute_quantile_return,
)
from alphapurify_bridge.diagnostics.official import official_version, run_official_diagnostics
from alphapurify_bridge.filters import ThresholdFilter
from alphapurify_bridge.utils.profiler import merge_stages


logger = logging.getLogger(__name__)


def _points(series: pd.Series) -> list[dict[str, Any]]:
    return [
        {"trade_date": pd.Timestamp(index).date().isoformat(), "value": float(value)}
        for index, value in series.dropna().items()
    ]


def _annualized(values: pd.Series, periods_per_year: int) -> float:
    clean = pd.to_numeric(values, errors="coerce").replace([np.inf, -np.inf], np.nan).dropna()
    if clean.empty:
        return float("nan")
    gross = float((1.0 + clean).prod())
    return gross ** (float(periods_per_year) / len(clean)) - 1.0 if gross > 0 else float("nan")


def _quantile_summary(
    frame: pd.DataFrame,
    n_quantiles: int,
    rebalance_freq: str,
    monotonicity_threshold: float,
) -> dict[str, Any]:
    if frame.empty:
        return {"annualized_returns": [], "spread_return": float("nan"), "monotonicity": False, "monotonicity_rank_corr": float("nan"), "curve": [], "spread_curve": [], "rebalance_observations": 0}
    returns = frame.pivot(index="trade_date", columns="quantile", values="forward_return").reindex(columns=range(1, n_quantiles + 1)).sort_index()
    returns.index = pd.to_datetime(returns.index)
    periods_per_year = {"M": 12, "Q": 4, "D": 252}[str(rebalance_freq).upper()]
    annualized = returns.apply(lambda values: _annualized(values, periods_per_year), axis=0)
    curve = (1.0 + returns.fillna(0.0)).cumprod()
    spread = returns[n_quantiles] - returns[1]
    spread_curve = (1.0 + spread.fillna(0.0)).cumprod()
    finite = annualized.dropna()
    corr = float(pd.Series(finite.values).corr(pd.Series(finite.index, dtype=float), method="spearman")) if len(finite) >= 3 else float("nan")
    return {
        "annualized_returns": [None if pd.isna(value) else float(value) for value in annualized],
        "spread_return": _annualized(spread, periods_per_year),
        "monotonicity": bool(np.isfinite(corr) and corr >= monotonicity_threshold),
        "monotonicity_rank_corr": corr,
        "curve": [{"trade_date": index.date().isoformat(), **{f"q{int(column)}": float(value) for column, value in row.items()}} for index, row in curve.iterrows()],
        "spread_curve": [{"trade_date": index.date().isoformat(), "value": float(value)} for index, value in spread_curve.items()],
        "rebalance_observations": int(len(returns)),
    }


class DiagnosisRunner:
    """Run deterministic diagnostics with an optional upstream AlphaPurify audit."""

    def __init__(
        self,
        config: Mapping[str, Any] | str | Path | None = None,
        *,
        adapter: SnapshotAdapter | None = None,
        registry: Mapping[str, Any] | str | Path | None = None,
    ):
        if config is None or isinstance(config, (str, Path)):
            self.config = load_diagnosis_config(config or DEFAULT_CONFIG_PATH)
        else:
            self.config = deep_merge(load_diagnosis_config(), config)
        if registry is None or isinstance(registry, (str, Path)):
            self.registry = load_factor_registry(registry or DEFAULT_REGISTRY_PATH)
        else:
            self.registry = dict(registry)
        self.adapter = adapter or SnapshotAdapter()
        self.filter = ThresholdFilter(self.config["thresholds"])
        self.last_profiles: dict[str, dict[str, Any]] = {}
        self.last_batch_profile: dict[str, Any] = {}
        self._last_official_seconds = 0.0

    @property
    def factor_names(self) -> list[str]:
        return list(self.registry["factors"])

    def _factor_metadata(self, factor_name: str) -> dict[str, Any]:
        try:
            return dict(self.registry["factors"][factor_name])
        except KeyError as exc:
            raise ValueError(f"因子未注册：{factor_name}") from exc

    def _primary_horizon(self, factor_name: str) -> int:
        diagnosis = self.config["diagnosis"]
        metadata = self._factor_metadata(factor_name)
        primary = int(
            metadata.get(
                "primary_horizon",
                diagnosis.get("primary_horizon", diagnosis["horizons"][0]),
            )
        )
        if primary not in diagnosis["horizons"]:
            raise ValueError(
                f"因子 {factor_name} 的 primary_horizon={primary} 不在 horizons 中"
            )
        return primary

    @staticmethod
    def _apply_direction(factor_values: Any, direction: int) -> Any:
        """Orient factor values so larger values consistently mean better."""

        if int(direction) not in {-1, 1}:
            raise ValueError("direction 必须为 1 或 -1")
        if isinstance(factor_values, pd.Series):
            assert not factor_values.attrs.get("_alphapurify_direction_applied", False), (
                "因子方向已应用，禁止重复翻转"
            )
            oriented = factor_values.copy()
            if int(direction) == -1:
                oriented = -oriented
            oriented.attrs["_alphapurify_direction_applied"] = True
            oriented.attrs["_alphapurify_direction"] = int(direction)
            return oriented
        return -factor_values if int(direction) == -1 else factor_values

    @staticmethod
    def _log_metric_ranges(
        factor_name: str,
        primary_horizon: int,
        ic_values: pd.Series,
        quantile_values: pd.Series,
    ) -> None:
        ic_clean = pd.to_numeric(ic_values, errors="coerce").dropna()
        quantile_clean = pd.to_numeric(quantile_values, errors="coerce").dropna()
        ic_min = float(ic_clean.min()) if not ic_clean.empty else float("nan")
        ic_max = float(ic_clean.max()) if not ic_clean.empty else float("nan")
        quantile_min = (
            float(quantile_clean.min()) if not quantile_clean.empty else float("nan")
        )
        quantile_max = (
            float(quantile_clean.max()) if not quantile_clean.empty else float("nan")
        )
        logger.info(
            "%s %d日 IC 计算使用的定向因子值范围: [%.6f, %.6f]",
            factor_name,
            primary_horizon,
            ic_min,
            ic_max,
        )
        logger.info(
            "%s %d日 分层使用的定向因子值范围: [%.6f, %.6f]",
            factor_name,
            primary_horizon,
            quantile_min,
            quantile_max,
        )

    def diagnose_factor(
        self,
        factor_name: str,
        start_date: str | None = None,
        end_date: str | None = None,
        *,
        official: bool | None = None,
        profile: bool = False,
    ) -> dict[str, Any]:
        total_started = time.perf_counter()
        diagnosis = self.config["diagnosis"]
        start = start_date or diagnosis.get("start_date")
        end = end_date if end_date is not None else diagnosis.get("end_date")
        horizons = list(diagnosis["horizons"])
        official_cfg = diagnosis.get("official_validation", {}) or {}
        use_official = bool(official_cfg.get("enabled", False)) if official is None else bool(official)
        if use_official:
            load_started = time.perf_counter()
            frames = {
                horizon: self.adapter.load_factor(factor_name, start, end, horizon=horizon, include_close=True)
                for horizon in horizons
            }
            load_elapsed = time.perf_counter() - load_started
            diagnose_started = time.perf_counter()
            result = self._diagnose_loaded(factor_name, frames, start, end, official=True)
            diagnose_elapsed = time.perf_counter() - diagnose_started
            if profile:
                stages = merge_stages(
                    {
                        "data_load": load_elapsed,
                        "alphapurify": self._last_official_seconds,
                        "metrics": max(0.0, diagnose_elapsed - self._last_official_seconds),
                    }
                )
                self.last_profiles[factor_name] = {
                    "stages": stages,
                    "total": time.perf_counter() - total_started,
                    "rows": int(result.get("sample_count", 0)),
                    "symbols": None,
                    "dates": int(result.get("cross_section_count", 0)),
                }
            return result
        metadata = self._factor_metadata(factor_name)
        primary = self._primary_horizon(factor_name)
        aggregates = self.adapter.aggregate_factor_diagnostics(
            factor_name,
            start,
            end,
            horizons=horizons,
            direction=int(metadata.get("direction", 1)),
            ic_method=str(diagnosis.get("ic_method", "spearman")),
            primary_horizon=primary,
            n_quantiles=int(diagnosis.get("n_quantiles", 10)),
            rebalance_freq=str(diagnosis.get("rebalance_freq", "M")),
            min_observations=int(diagnosis.get("min_cross_section", 20)),
        )
        metrics_started = time.perf_counter()
        result = self._diagnose_aggregated(factor_name, aggregates, start, end)
        finalize_elapsed = time.perf_counter() - metrics_started
        if profile:
            adapter_profile = self.adapter.last_profile or {}
            stages = merge_stages(adapter_profile.get("stages"), {"metrics": finalize_elapsed})
            self.last_profiles[factor_name] = {
                "stages": stages,
                "total": time.perf_counter() - total_started,
                "rows": int(result.get("sample_count", 0)),
                "symbols": None,
                "dates": int(result.get("cross_section_count", 0)),
            }
        return result

    def _diagnose_aggregated(
        self,
        factor_name: str,
        aggregates: Mapping[str, Any],
        start: str | None,
        end: str | None,
    ) -> dict[str, Any]:
        diagnosis = self.config["diagnosis"]
        metadata = self._factor_metadata(factor_name)
        horizons = list(diagnosis["horizons"])
        primary = self._primary_horizon(factor_name)
        ic_frame = aggregates["ic"]
        ic_series = {
            horizon: pd.Series(
                pd.to_numeric(ic_frame[f"ic_{horizon}"], errors="coerce").to_numpy(),
                index=pd.to_datetime(ic_frame["trade_date"]),
                name="ic",
            ).dropna()
            for horizon in horizons
        }
        ic_values = {f"horizon_{horizon}": float(ic_series[horizon].mean()) if not ic_series[horizon].empty else float("nan") for horizon in horizons}
        base_ic = ic_values[f"horizon_{primary}"]
        decay = {
            f"horizon_{horizon}": (
                0.0 if horizon == primary else max(0.0, 1.0 - abs(ic_values[f"horizon_{horizon}"]) / abs(base_ic))
            )
            if np.isfinite(base_ic) and base_ic != 0 and np.isfinite(ic_values[f"horizon_{horizon}"])
            else float("nan")
            for horizon in horizons
        }
        quantiles = _quantile_summary(
            aggregates["quantile"],
            int(diagnosis.get("n_quantiles", 10)),
            str(diagnosis.get("rebalance_freq", "M")),
            float(diagnosis.get("monotonicity_min_rank_corr", 0.8)),
        )
        factor_return_frame = aggregates["factor_return"]
        histogram = aggregates["histogram"]
        edges = histogram.get("edges", []) if isinstance(histogram, Mapping) else []
        oriented_range = pd.Series(
            [edges[0], edges[-1]] if len(edges) >= 2 else [], dtype=float
        )
        self._log_metric_ranges(
            factor_name, primary, oriented_range, oriented_range
        )
        factor_returns = pd.Series(
            pd.to_numeric(factor_return_frame["factor_return"], errors="coerce").to_numpy(),
            index=pd.to_datetime(factor_return_frame["trade_date"]),
            name="factor_return",
        ).dropna()
        result: dict[str, Any] = {
            "factor_name": factor_name,
            "display_name": metadata.get("display_name", factor_name),
            "category": metadata.get("category", "unknown"),
            "factor_version": metadata.get("version", "unknown"),
            "direction": int(metadata.get("direction", 1)),
            "primary_horizon": primary,
            "start_date": str(start) if start else str(ic_frame["trade_date"].min()),
            "end_date": str(end) if end else str(ic_frame["trade_date"].max()),
            "sample_count": int(aggregates["sample_count"]),
            "cross_section_count": int(aggregates["cross_section_count"]),
            "ic_method": str(diagnosis.get("ic_method", "spearman")),
            "ic_mean": base_ic,
            "ic_ir": compute_ir(ic_series[primary]),
            "ic_by_horizon": ic_values,
            "ic_decay": decay,
            "quantile_returns": quantiles["annualized_returns"],
            "spread_return": quantiles["spread_return"],
            "quantile_monotonicity": quantiles["monotonicity"],
            "monotonicity_rank_corr": quantiles["monotonicity_rank_corr"],
            "factor_return_mean": float(factor_returns.mean()) if not factor_returns.empty else float("nan"),
            "timestamp": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "data_version": data_version(self.adapter.snapshot_path),
            "alphapurify_version": official_version(),
            "details": {
                "ic_series": {f"horizon_{horizon}": _points(ic_series[horizon]) for horizon in horizons},
                "quantile_curve": quantiles["curve"],
                "spread_curve": quantiles["spread_curve"],
                "factor_return_series": _points(factor_returns),
                "factor_distribution": histogram,
                "rebalance_observations": quantiles["rebalance_observations"],
            },
        }
        result.update(self.filter.evaluate(result))
        return result

    def _diagnose_loaded(
        self,
        factor_name: str,
        frames: Mapping[int, pd.DataFrame],
        start: str | None,
        end: str | None,
        *,
        official: bool | None,
    ) -> dict[str, Any]:
        diagnosis = self.config["diagnosis"]
        metadata = self._factor_metadata(factor_name)
        direction = int(metadata.get("direction", 1))
        horizons = list(diagnosis["horizons"])
        primary = self._primary_horizon(factor_name)
        if primary not in frames:
            raise ValueError(f"primary_horizon={primary} 不在 horizons 中")
        raw_primary_values = pd.to_numeric(
            frames[primary]["factor_value"], errors="coerce"
        )
        oriented_frames: dict[int, pd.DataFrame] = {}
        for horizon, frame in frames.items():
            oriented = frame.copy()
            oriented["factor_value"] = self._apply_direction(
                pd.to_numeric(oriented["factor_value"], errors="coerce"), direction
            )
            oriented_frames[horizon] = oriented
        frames = oriented_frames
        method = str(diagnosis.get("ic_method", "spearman"))
        minimum = int(diagnosis.get("min_cross_section", 20))
        ic_values: dict[str, float] = {}
        ic_curves: dict[str, list[dict[str, Any]]] = {}
        ic_series_by_horizon: dict[int, pd.Series] = {}
        for horizon in horizons:
            ic = compute_ic(
                frames[horizon],
                method=method,
                direction=1,
                min_observations=minimum,
            )
            ic_series_by_horizon[horizon] = ic
            ic_values[f"horizon_{horizon}"] = float(ic.mean()) if not ic.empty else float("nan")
            ic_curves[f"horizon_{horizon}"] = _points(ic)
        base_ic = ic_values[f"horizon_{primary}"]
        decay: dict[str, float] = {}
        for horizon in horizons:
            value = ic_values[f"horizon_{horizon}"]
            if horizon == primary:
                decay[f"horizon_{horizon}"] = 0.0
            elif np.isfinite(base_ic) and base_ic != 0 and np.isfinite(value):
                decay[f"horizon_{horizon}"] = max(0.0, 1.0 - abs(value) / abs(base_ic))
            else:
                decay[f"horizon_{horizon}"] = float("nan")
        primary_frame = frames[primary]
        self._log_metric_ranges(
            factor_name,
            primary,
            primary_frame["factor_value"],
            primary_frame["factor_value"],
        )
        logger.info(
            "%s 原始因子值范围: [%.6f, %.6f]；direction=%d",
            factor_name,
            float(raw_primary_values.min()),
            float(raw_primary_values.max()),
            direction,
        )
        quantiles = compute_quantile_return(
            primary_frame,
            n_quantiles=int(diagnosis.get("n_quantiles", 10)),
            direction=1,
            rebalance_freq=str(diagnosis.get("rebalance_freq", "M")),
            monotonicity_min_rank_corr=float(
                diagnosis.get("monotonicity_min_rank_corr", 0.80)
            ),
        )
        factor_returns = compute_factor_returns(
            primary_frame,
            direction=1,
            min_observations=minimum,
        )
        valid_sample = primary_frame[["factor_value", "forward_return"]].dropna()
        result: dict[str, Any] = {
            "factor_name": factor_name,
            "display_name": metadata.get("display_name", factor_name),
            "category": metadata.get("category", "unknown"),
            "factor_version": metadata.get("version", "unknown"),
            "direction": direction,
            "primary_horizon": primary,
            "start_date": str(start) if start else str(primary_frame["trade_date"].min().date()),
            "end_date": str(end) if end else str(primary_frame["trade_date"].max().date()),
            "sample_count": int(len(valid_sample)),
            "cross_section_count": int(primary_frame["trade_date"].nunique()),
            "ic_method": method,
            "ic_mean": base_ic,
            "ic_ir": compute_ir(ic_series_by_horizon[primary]),
            "ic_by_horizon": ic_values,
            "ic_decay": decay,
            "quantile_returns": quantiles["annualized_returns"],
            "spread_return": quantiles["spread_return"],
            "quantile_monotonicity": quantiles["monotonicity"],
            "monotonicity_rank_corr": quantiles["monotonicity_rank_corr"],
            "factor_return_mean": float(factor_returns.mean()) if not factor_returns.empty else float("nan"),
            "timestamp": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "data_version": data_version(self.adapter.snapshot_path),
            "alphapurify_version": official_version(),
            "details": {
                "ic_series": ic_curves,
                "quantile_curve": quantiles["curve"],
                "spread_curve": quantiles["spread_curve"],
                "factor_return_series": _points(factor_returns),
                "factor_distribution": compute_histogram(valid_sample["factor_value"]),
                "rebalance_observations": quantiles.get("rebalance_observations", 0),
            },
        }
        evaluation = self.filter.evaluate(result)
        result.update(evaluation)
        official_cfg = diagnosis.get("official_validation", {}) or {}
        use_official = bool(official_cfg.get("enabled", False)) if official is None else bool(official)
        if use_official:
            audit = primary_frame.copy()
            audit["oriented_factor"] = audit["factor_value"]
            official_started = time.perf_counter()
            result["official_validation"] = run_official_diagnostics(
                audit,
                factor_col="oriented_factor",
                horizons=horizons,
                n_quantiles=int(diagnosis.get("n_quantiles", 10)),
                rebalance_freq=str(diagnosis.get("rebalance_freq", "M")),
                ic_method=method,
                rolling_window=int(diagnosis.get("decay_window", 12)),
                max_workers=int(official_cfg.get("max_workers", 1)),
            )
            self._last_official_seconds = time.perf_counter() - official_started
        else:
            self._last_official_seconds = 0.0
        return result

    def diagnose_factors(
        self,
        factor_names: Sequence[str] | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        *,
        official: bool | None = None,
        profile: bool = False,
    ) -> list[dict[str, Any]]:
        total_started = time.perf_counter()
        if profile:
            self.last_profiles = {}
            self.last_batch_profile = {}
        names = list(dict.fromkeys(factor_names or self.factor_names))
        for name in names:
            self._factor_metadata(name)
        diagnosis = self.config["diagnosis"]
        start = start_date or diagnosis.get("start_date")
        end = end_date if end_date is not None else diagnosis.get("end_date")
        official_cfg = diagnosis.get("official_validation", {}) or {}
        use_official = bool(official_cfg.get("enabled", False)) if official is None else bool(official)
        primary_horizons = {name: self._primary_horizon(name) for name in names}
        shared_primary = set(primary_horizons.values())
        if not use_official and len(names) > 1 and len(shared_primary) == 1:
            aggregates = self.adapter.aggregate_factors_diagnostics(
                names,
                start,
                end,
                horizons=diagnosis["horizons"],
                directions={name: int(self._factor_metadata(name).get("direction", 1)) for name in names},
                ic_method=str(diagnosis.get("ic_method", "spearman")),
                primary_horizon=next(iter(shared_primary)),
                n_quantiles=int(diagnosis.get("n_quantiles", 10)),
                rebalance_freq=str(diagnosis.get("rebalance_freq", "M")),
                min_observations=int(diagnosis.get("min_cross_section", 20)),
            )
            adapter_profile = self.adapter.last_profile or {}
            per_adapter = adapter_profile.get("per_factor", {}) or {}
            shared = merge_stages(adapter_profile.get("stages"))
            computed_factors = set(adapter_profile.get("computed_factors", names) or [])
            shared_divisor = max(1, len(computed_factors))
            results: list[dict[str, Any]] = []
            finalize_total = 0.0
            for name in names:
                factor_started = time.perf_counter()
                result = self._diagnose_aggregated(name, aggregates[name], start, end)
                finalize_elapsed = time.perf_counter() - factor_started
                finalize_total += finalize_elapsed
                results.append(result)
                if profile:
                    stages = merge_stages(
                        per_adapter.get(name),
                        {
                            "data_load": shared["data_load"] / shared_divisor if name in computed_factors else 0.0,
                            "data_prep": shared["data_prep"] / shared_divisor if name in computed_factors else 0.0,
                            "factor_extract": shared["factor_extract"] / shared_divisor if name in computed_factors else 0.0,
                            "metrics": finalize_elapsed,
                        },
                    )
                    self.last_profiles[name] = {
                        "stages": stages,
                        "total": sum(stages.values()),
                        "rows": int(result.get("sample_count", 0)),
                        "symbols": None,
                        "dates": int(result.get("cross_section_count", 0)),
                    }
            if profile:
                batch_stages = merge_stages(shared, {"metrics": finalize_total})
                self.last_batch_profile = {
                    "stages": batch_stages,
                    "total": time.perf_counter() - total_started,
                    "per_factor": {
                        name: float(self.last_profiles[name]["total"])
                        for name in names
                    },
                }
            return results
        results = [
            self.diagnose_factor(name, start, end, official=official, profile=profile)
            for name in names
        ]
        if profile:
            self.last_batch_profile = {
                "stages": merge_stages(*(self.last_profiles[name]["stages"] for name in names)),
                "total": time.perf_counter() - total_started,
                "per_factor": {name: float(self.last_profiles[name]["total"]) for name in names},
            }
        return results


__all__ = ["DiagnosisRunner"]

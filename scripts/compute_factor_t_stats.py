#!/usr/bin/env python
"""Compute the locked development-period purified-IC t-statistic gate."""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from datetime import date
from pathlib import Path
from typing import Sequence

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.factor_orthogonality import (
    CANDIDATES,
    compute_purified_ic_series,
    load_factor_panel,
    load_forward_returns,
)


DEFAULT_FACTORS = (
    "dividend_yield",
    "volatility_60d",
    "roe_ttm",
    "fcf_ev",
    "pe_industry_quantile",
    "reversal_10d",
)
DEFAULT_OUTPUT = ROOT / "output" / "t_stats" / "factor_t_stats.csv"


def parse_period(value: str) -> tuple[date, date]:
    """Parse YYYY-YYYY into an inclusive calendar-date interval."""

    match = re.fullmatch(r"\s*(\d{4})\s*-\s*(\d{4})\s*", value)
    if not match:
        raise argparse.ArgumentTypeError("期间必须为 YYYY-YYYY")
    start_year, end_year = (int(part) for part in match.groups())
    if end_year < start_year:
        raise argparse.ArgumentTypeError("结束年份不得早于开始年份")
    return date(start_year, 1, 1), date(end_year, 12, 31)


def automatic_newey_west_lag(sample_size: int) -> int:
    """Newey-West's common data-dependent Bartlett bandwidth."""

    if sample_size < 2:
        return 0
    return min(sample_size - 1, int(math.floor(4.0 * (sample_size / 100.0) ** (2.0 / 9.0))))


def newey_west_standard_error(values: Sequence[float], lag: int | None = None) -> tuple[float, int]:
    """Return the HAC standard error of a sample mean and the lag used."""

    sample = np.asarray(values, dtype=float)
    sample = sample[np.isfinite(sample)]
    size = len(sample)
    if size < 2:
        return float("nan"), 0
    bandwidth = automatic_newey_west_lag(size) if lag is None else int(lag)
    if bandwidth < 0 or bandwidth >= size:
        raise ValueError(f"Newey-West lag 必须位于 [0, {size - 1}]")
    centered = sample - float(sample.mean())
    long_run_variance = float(np.dot(centered, centered) / size)
    for offset in range(1, bandwidth + 1):
        covariance = float(np.dot(centered[offset:], centered[:-offset]) / size)
        bartlett_weight = 1.0 - offset / (bandwidth + 1.0)
        long_run_variance += 2.0 * bartlett_weight * covariance
    # Tiny negative values may occur through floating-point cancellation.
    long_run_variance = max(long_run_variance, 0.0)
    return float(math.sqrt(long_run_variance / size)), bandwidth


def summarize_ic_series(series: pd.Series, *, lag: int | None = None) -> dict[str, object]:
    clean = pd.to_numeric(series, errors="coerce").dropna().astype(float)
    mean = float(clean.mean()) if len(clean) else float("nan")
    standard_error, bandwidth = newey_west_standard_error(clean.to_numpy(), lag=lag)
    if math.isfinite(standard_error) and standard_error > 0.0:
        statistic = mean / standard_error
    else:
        statistic = float("nan")
    return {
        "purified_ic_mean": mean,
        "standard_error": standard_error,
        "t_statistic": float(statistic),
        "sample_size": int(len(clean)),
        "newey_west_lag": int(bandwidth),
        "gate_2_pass": bool(math.isfinite(statistic) and statistic >= 2.0),
    }


def compute_factor_t_stats(
    factors: Sequence[str],
    start: date,
    end: date,
    *,
    lag: int | None = None,
) -> pd.DataFrame:
    selected = tuple(dict.fromkeys(str(factor).strip() for factor in factors if str(factor).strip()))
    if not selected:
        raise ValueError("至少需要一个因子")
    unknown = set(selected) - set(DEFAULT_FACTORS)
    if unknown:
        raise ValueError(f"非主配置候选因子：{', '.join(sorted(unknown))}")
    panel = load_factor_panel(start, end)
    forward = load_forward_returns(start, end)
    rows = []
    # Purification always uses the locked six-factor candidate set, even when
    # the CLI requests only the three disputed rows for display.
    for factor in selected:
        series = compute_purified_ic_series(
            panel,
            forward,
            factor,
            factors=DEFAULT_FACTORS,
        )
        rows.append({"factor": factor, **summarize_ic_series(series, lag=lag)})
    return pd.DataFrame(rows)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="开发期纯化 IC t 统计量（闸门②）")
    parser.add_argument("--factors", default=",".join(DEFAULT_FACTORS))
    parser.add_argument("--train-period", type=parse_period, default=parse_period("2015-2019"))
    parser.add_argument("--newey-west-lag", type=int, default=None)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    factors = [value.strip() for value in args.factors.split(",") if value.strip()]
    start, end = args.train_period
    frame = compute_factor_t_stats(
        factors,
        start,
        end,
        lag=args.newey_west_lag,
    )
    output = args.output if args.output.is_absolute() else ROOT / args.output
    output.parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(output, index=False, encoding="utf-8-sig")
    metadata = {
        "train_period": f"{start.year}-{end.year}",
        "gate": "purified IC Newey-West t-statistic >= 2",
        "purification_factors": list(DEFAULT_FACTORS),
        "rows": frame.to_dict(orient="records"),
    }
    output.with_suffix(".json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(frame.to_string(index=False))
    print(f"t 统计量输出：{output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

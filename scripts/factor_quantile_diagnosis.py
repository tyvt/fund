#!/usr/bin/env python
"""Diagnose factor returns on non-overlapping monthly point-in-time cross sections."""

from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path
from typing import Any

import duckdb
import matplotlib.pyplot as plt
from matplotlib import font_manager
import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from data.parquet_loader import load_trade_calendar


SNAPSHOT_ROOT = ROOT / "data" / "parquet" / "factors" / "snapshots"
QFQ_ROOT = ROOT / "data" / "parquet" / "stock_daily_qfq"
SUPPORTED_FACTORS = ("dividend_yield", "volatility_60d")
PREFERRED_EXTREME = {"dividend_yield": "Q5", "volatility_60d": "Q1"}


def _snapshot_entries(start: str, end: str) -> list[tuple[pd.Timestamp, Path]]:
    start_ts, end_ts = pd.Timestamp(start), pd.Timestamp(end)
    entries: list[tuple[pd.Timestamp, Path]] = []
    for directory in SNAPSHOT_ROOT.glob("trade_date=????-??-??"):
        path = directory / "factors.parquet"
        if not path.is_file():
            continue
        try:
            day = pd.Timestamp(directory.name.removeprefix("trade_date="))
        except ValueError:
            continue
        if start_ts <= day <= end_ts:
            entries.append((day, path))
    if not entries:
        raise FileNotFoundError(f"指定区间没有因子快照：{SNAPSHOT_ROOT}")
    frame = pd.DataFrame(entries, columns=["trade_date", "path"])
    frame["month"] = frame["trade_date"].dt.to_period("M")
    month_end = frame.sort_values("trade_date").groupby("month", sort=True).tail(1)
    return list(month_end[["trade_date", "path"]].itertuples(index=False, name=None))


def build_monthly_schedule(
    start: str, end: str, *, horizon: int = 20
) -> tuple[pd.DataFrame, list[Path]]:
    """Map month-end signals to next-session entry and a 20-session exit."""
    if horizon < 1:
        raise ValueError("horizon 必须大于 0")
    entries = _snapshot_entries(start, end)
    calendar_end = (pd.Timestamp(end) + pd.Timedelta(days=horizon * 3 + 15)).date().isoformat()
    calendar = pd.DatetimeIndex(
        pd.to_datetime(load_trade_calendar(start, calendar_end).index)
    ).sort_values()
    positions = pd.Series(np.arange(len(calendar)), index=calendar)
    rows: list[dict[str, pd.Timestamp]] = []
    paths: list[Path] = []
    for signal_date, path in entries:
        if signal_date not in positions.index:
            continue
        entry_pos = int(positions.loc[signal_date]) + 1
        exit_pos = entry_pos + horizon
        if exit_pos >= len(calendar):
            continue
        rows.append(
            {
                "trade_date": signal_date,
                "entry_date": calendar[entry_pos],
                "exit_date": calendar[exit_pos],
            }
        )
        paths.append(path)
    if not rows:
        raise ValueError("没有同时具备下一交易日和完整持有期的月末截面")
    return pd.DataFrame(rows), paths


def load_forward_returns(
    factor_name: str,
    start: str,
    end: str,
    *,
    horizon: int = 20,
    threads: int = 4,
) -> pd.DataFrame:
    if factor_name not in SUPPORTED_FACTORS:
        raise ValueError(f"不支持的因子：{factor_name}")
    schedule, snapshot_paths = build_monthly_schedule(start, end, horizon=horizon)
    snapshot_source = ", ".join(
        "'" + path.resolve().as_posix().replace("'", "''") + "'"
        for path in snapshot_paths
    )
    qfq_glob = (QFQ_ROOT / "year=*" / "part_*.parquet").resolve().as_posix()
    price_start = pd.Timestamp(schedule["entry_date"].min()).date().isoformat()
    price_end = pd.Timestamp(schedule["exit_date"].max()).date().isoformat()
    query = f"""
        SELECT
            s.trade_date::DATE AS trade_date,
            s.symbol::VARCHAR AS symbol,
            s."{factor_name}"::DOUBLE AS factor_value,
            p0.close::DOUBLE AS entry_close,
            p1.close::DOUBLE AS exit_close,
            p1.close::DOUBLE / p0.close::DOUBLE - 1.0 AS forward_return
        FROM read_parquet(
            [{snapshot_source}], hive_partitioning=true, union_by_name=true
        ) AS s
        INNER JOIN schedule AS d
            ON s.trade_date::DATE = d.trade_date
        INNER JOIN read_parquet('{qfq_glob}', hive_partitioning=true) AS p0
            ON s.symbol::VARCHAR = p0.symbol::VARCHAR
           AND p0.trade_date::DATE = d.entry_date
           AND p0.trade_date::DATE BETWEEN DATE '{price_start}' AND DATE '{price_end}'
        INNER JOIN read_parquet('{qfq_glob}', hive_partitioning=true) AS p1
            ON s.symbol::VARCHAR = p1.symbol::VARCHAR
           AND p1.trade_date::DATE = d.exit_date
           AND p1.trade_date::DATE BETWEEN DATE '{price_start}' AND DATE '{price_end}'
        WHERE s."{factor_name}" IS NOT NULL
          AND isfinite(s."{factor_name}"::DOUBLE)
          AND p0.close::DOUBLE > 0
          AND p1.close::DOUBLE > 0
        ORDER BY trade_date, symbol
    """
    sql_schedule = schedule.copy()
    for column in sql_schedule.columns:
        sql_schedule[column] = pd.to_datetime(sql_schedule[column]).dt.date
    with duckdb.connect() as con:
        con.execute(f"SET threads TO {max(1, int(threads))}")
        con.register("schedule", sql_schedule)
        result = con.execute(query).fetch_df()
    result["trade_date"] = pd.to_datetime(result["trade_date"])
    return result


def assign_quantile_returns(
    observations: pd.DataFrame, n_quantiles: int = 5
) -> pd.DataFrame:
    """Build equal-weight quantile portfolio returns for every monthly cross section."""
    if n_quantiles < 2:
        raise ValueError("quantiles 必须至少为 2")
    labels = [f"Q{index}" for index in range(1, n_quantiles + 1)]
    rows: list[dict[str, Any]] = []
    for day, daily in observations.groupby("trade_date", sort=True):
        usable = daily.dropna(subset=["factor_value", "forward_return"]).copy()
        if len(usable) < n_quantiles:
            continue
        # Stable first-rank makes large tied blocks (notably zero dividends)
        # deterministic while retaining five portfolios of near-equal size.
        percentile = usable["factor_value"].rank(method="first", pct=True)
        bucket = np.ceil(percentile * n_quantiles).clip(1, n_quantiles).astype(int)
        usable["quantile"] = bucket.map(lambda value: f"Q{value}")
        grouped = usable.groupby("quantile", observed=True)["forward_return"].mean()
        counts = usable.groupby("quantile", observed=True).size()
        if any(label not in grouped for label in labels):
            continue
        row: dict[str, Any] = {"date": pd.Timestamp(day)}
        row.update({label: float(grouped[label]) for label in labels})
        row.update({f"{label}_count": int(counts[label]) for label in labels})
        rows.append(row)
    if not rows:
        raise ValueError("没有可计算的完整因子分位截面")
    return pd.DataFrame(rows).sort_values("date").reset_index(drop=True)


def summarize_quantiles(
    quantile_returns: pd.DataFrame, n_quantiles: int = 5
) -> tuple[pd.DataFrame, dict[str, Any]]:
    labels = [f"Q{index}" for index in range(1, n_quantiles + 1)]
    rows: list[dict[str, Any]] = []
    for label in labels:
        returns = pd.to_numeric(quantile_returns[label], errors="coerce").dropna()
        wealth = pd.concat([pd.Series([1.0]), (1.0 + returns).cumprod()], ignore_index=True)
        annual = float(wealth.iloc[-1] ** (12.0 / len(returns)) - 1.0)
        rows.append(
            {
                "quantile": label,
                "monthly_mean_return": float(returns.mean()),
                "annual_return": annual,
                "win_rate": float(returns.gt(0).mean()),
                "max_drawdown": float((wealth / wealth.cummax() - 1.0).min()),
                "periods": int(len(returns)),
                "average_stocks": float(quantile_returns[f"{label}_count"].mean()),
            }
        )
    summary = pd.DataFrame(rows)
    q3_annual = float(summary.loc[summary["quantile"].eq("Q3"), "annual_return"].iloc[0])
    summary["annual_return_vs_q3"] = summary["annual_return"] - q3_annual
    best = str(summary.loc[summary["annual_return"].idxmax(), "quantile"])
    worst = str(summary.loc[summary["annual_return"].idxmin(), "quantile"])
    spread = float(summary["annual_return"].max() - summary["annual_return"].min())
    paired = quantile_returns[[best, worst]].dropna()
    difference = paired[best] - paired[worst]
    std = float(difference.std(ddof=1)) if len(difference) > 1 else float("nan")
    t_stat = (
        float(difference.mean() / (std / math.sqrt(len(difference))))
        if len(difference) > 1 and std > 0
        else float("nan")
    )
    diagnostics = {
        "best_quantile": best,
        "worst_quantile": worst,
        "best_minus_worst_annual_return": spread,
        "best_minus_worst_t_stat": t_stat,
        "statistically_distinct_5pct_normal_approx": bool(
            np.isfinite(t_stat) and abs(t_stat) >= 1.96
        ),
        "economically_distinct_1_5pct": bool(spread >= 0.015),
        "meaningful_difference": bool(
            spread >= 0.015 or (np.isfinite(t_stat) and abs(t_stat) >= 1.96)
        ),
    }
    return summary, diagnostics


def diagnose_concentration(factor_name: str, diagnostics: dict[str, Any]) -> str:
    best = str(diagnostics["best_quantile"])
    meaningful = bool(diagnostics.get("meaningful_difference", True))
    if not meaningful:
        return "各分位既无明显经济差异，也未达到 5% 近似显著水平，暂不能认定因子有效。"
    if best == PREFERRED_EXTREME[factor_name]:
        direction = "最高股息率 Q5" if factor_name == "dividend_yield" else "最低波动率 Q1"
        return f"Alpha 集中在{direction}，因子方向有效，可保留极端分位策略。"
    middle = {"Q3", "Q4"} if factor_name == "dividend_yield" else {"Q2", "Q3"}
    if best in middle:
        return f"Alpha 集中在中间分位 {best}，因子有效但极端 Top10 可能选错，需验证 Top30。"
    return f"最佳分位为 {best}，与预期方向相反，需要重新设计该因子的选股方向。"


def _pct(value: float) -> str:
    return "—" if not np.isfinite(value) else f"{value:.2%}"


def plot_quantile_curves(
    quantile_returns: pd.DataFrame,
    factor_name: str,
    output_path: Path,
    n_quantiles: int = 5,
) -> None:
    for path in (Path("C:/Windows/Fonts/msyh.ttc"), Path("C:/Windows/Fonts/simhei.ttf")):
        if path.is_file():
            font_manager.fontManager.addfont(str(path))
            plt.rcParams["font.sans-serif"] = [font_manager.FontProperties(fname=str(path)).get_name()]
            break
    plt.rcParams["axes.unicode_minus"] = False
    fig, ax = plt.subplots(figsize=(12, 6))
    for index in range(1, n_quantiles + 1):
        label = f"Q{index}"
        wealth = (1.0 + quantile_returns[label]).cumprod()
        ax.plot(quantile_returns["date"], wealth, label=label, linewidth=1.6)
    ax.set_title(f"{factor_name} 月末分位累计收益（次日执行，持有 20 个交易日）")
    ax.set_xlabel("信号日期")
    ax.set_ylabel("累计净值")
    ax.legend(ncol=n_quantiles)
    ax.grid(True, alpha=0.3)
    fig.tight_layout()
    fig.savefig(output_path, dpi=160)
    plt.close(fig)


def run_diagnosis(
    factor_name: str,
    *,
    n_quantiles: int = 5,
    start: str = "2015-01-01",
    end: str = "2024-12-31",
    horizon: int = 20,
    output_dir: str | Path = ROOT / "output" / "quantile",
) -> dict[str, Any]:
    output = Path(output_dir)
    if not output.is_absolute():
        output = ROOT / output
    output.mkdir(parents=True, exist_ok=True)
    print(f"加载 {factor_name} 月末截面与前复权未来 {horizon} 日收益 ...", flush=True)
    observations = load_forward_returns(factor_name, start, end, horizon=horizon)
    quantile_returns = assign_quantile_returns(observations, n_quantiles)
    summary, diagnostics = summarize_quantiles(quantile_returns, n_quantiles)
    conclusion = diagnose_concentration(factor_name, diagnostics)
    summary.insert(0, "factor", factor_name)
    summary.to_csv(
        output / f"{factor_name}_quantile_summary.csv", index=False, encoding="utf-8-sig"
    )
    quantile_returns.to_csv(
        output / f"{factor_name}_quantile_returns.csv", index=False, encoding="utf-8-sig"
    )
    plot_quantile_curves(
        quantile_returns,
        factor_name,
        output / f"{factor_name}_quantile_curves.png",
        n_quantiles,
    )
    lines = [
        f"# {factor_name} 因子分位收益诊断",
        "",
        f"> {start} ~ {end}；每月末形成截面，下一交易日按前复权收盘价买入，持有 {horizon} 个交易日。",
        "",
        "| 分位 | 月均收益 | 年化收益 | 胜率 | 最大回撤 | 相对 Q3 年化差 | 平均股票数 |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for row in summary.itertuples(index=False):
        lines.append(
            f"| {row.quantile} | {_pct(row.monthly_mean_return)} | {_pct(row.annual_return)} | "
            f"{_pct(row.win_rate)} | {_pct(row.max_drawdown)} | "
            f"{row.annual_return_vs_q3:+.2%} | {row.average_stocks:.1f} |"
        )
    lines.extend(
        [
            "",
            f"最佳分位：**{diagnostics['best_quantile']}**；最差分位：**{diagnostics['worst_quantile']}**；"
            f"年化差：**{diagnostics['best_minus_worst_annual_return']:+.2%}**；"
            f"配对月度收益 t 值：**{diagnostics['best_minus_worst_t_stat']:.2f}**。",
            "",
            f"**结论：{conclusion}**",
            "",
            f"![分位累计收益]({factor_name}_quantile_curves.png)",
            "",
        ]
    )
    (output / f"{factor_name}_quantile_report.md").write_text(
        "\n".join(lines), encoding="utf-8"
    )
    print(summary.to_string(index=False))
    print(f"结论：{conclusion}")
    return {
        "factor": factor_name,
        "summary": summary,
        "diagnostics": diagnostics,
        "conclusion": conclusion,
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="因子月末五分位收益诊断")
    parser.add_argument("--factor", required=True, choices=SUPPORTED_FACTORS)
    parser.add_argument("--quantiles", type=int, default=5)
    parser.add_argument("--start", default="2015-01-01")
    parser.add_argument("--end", default="2024-12-31")
    parser.add_argument("--horizon", type=int, default=20)
    parser.add_argument("--output", default="output/quantile")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    from market_data import configure_stdout_utf8

    configure_stdout_utf8()
    args = parse_args(argv)
    run_diagnosis(
        args.factor,
        n_quantiles=args.quantiles,
        start=args.start,
        end=args.end,
        horizon=args.horizon,
        output_dir=args.output,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

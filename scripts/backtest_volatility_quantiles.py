#!/usr/bin/env python
"""Backtest unconstrained volatility quintiles on a true monthly schedule.

The factor diagnosis uses a fixed 20-session forward return.  This script keeps
the same point-in-time month-end signal and next-session execution convention,
but holds each cohort until the following monthly execution date.  That makes
the result a continuous, non-overlapping monthly rebalance backtest.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any

import duckdb
import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from data.parquet_loader import load_trade_calendar


SNAPSHOT_ROOT = ROOT / "data" / "parquet" / "factors" / "snapshots"
QFQ_ROOT = ROOT / "data" / "parquet" / "stock_daily_qfq"
QUANTILES = tuple(f"Q{number}" for number in range(1, 6))


def monthly_snapshot_entries(start: str, end: str) -> list[tuple[pd.Timestamp, Path]]:
    """Return the final available point-in-time snapshot in each month."""
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


def build_rebalance_schedule(start: str, end: str) -> pd.DataFrame:
    """Build signal, entry and next-month exit dates for every signal cohort."""
    end_ts = pd.Timestamp(end)
    next_month_end = (end_ts.to_period("M") + 1).end_time.normalize()
    entries = monthly_snapshot_entries(start, next_month_end.date().isoformat())
    calendar_end = (next_month_end + pd.Timedelta(days=15)).date().isoformat()
    calendar = pd.DatetimeIndex(
        pd.to_datetime(load_trade_calendar(start, calendar_end).index)
    ).sort_values()
    positions = pd.Series(np.arange(len(calendar)), index=calendar)
    executable: list[dict[str, Any]] = []
    for signal_date, path in entries:
        if signal_date not in positions.index:
            continue
        location = int(positions.loc[signal_date]) + 1
        if location >= len(calendar):
            continue
        executable.append(
            {
                "trade_date": pd.Timestamp(signal_date),
                "entry_date": pd.Timestamp(calendar[location]),
                "path": path,
            }
        )
    rows: list[dict[str, Any]] = []
    for current, following in zip(executable, executable[1:]):
        if current["trade_date"] > end_ts:
            break
        rows.append({**current, "exit_date": following["entry_date"]})
    schedule = pd.DataFrame(rows)
    if schedule.empty or schedule["trade_date"].max() < end_ts.to_period("M").start_time:
        raise ValueError("没有形成覆盖结束月份的完整月度调仓区间")
    return schedule


def load_monthly_observations(
    schedule: pd.DataFrame, *, threads: int = 4
) -> pd.DataFrame:
    """Load factor values and next-rebalance returns without future-price filtering."""
    snapshot_source = ", ".join(
        "'" + Path(path).resolve().as_posix().replace("'", "''") + "'"
        for path in schedule["path"]
    )
    qfq_glob = (QFQ_ROOT / "year=*" / "part_*.parquet").resolve().as_posix()
    price_start = pd.Timestamp(schedule["entry_date"].min()).date().isoformat()
    price_end = pd.Timestamp(schedule["exit_date"].max()).date().isoformat()
    sql_schedule = schedule.drop(columns="path").copy()
    for column in sql_schedule.columns:
        sql_schedule[column] = pd.to_datetime(sql_schedule[column]).dt.date
    query = f"""
        WITH base AS (
            SELECT
                s.trade_date::DATE AS trade_date,
                s.symbol::VARCHAR AS symbol,
                s.volatility_60d::DOUBLE AS factor_value,
                d.entry_date,
                d.exit_date,
                p0.close::DOUBLE AS entry_close
            FROM read_parquet(
                [{snapshot_source}], hive_partitioning=true, union_by_name=true
            ) AS s
            INNER JOIN schedule AS d
                ON s.trade_date::DATE = d.trade_date
            INNER JOIN read_parquet('{qfq_glob}', hive_partitioning=true) AS p0
                ON s.symbol::VARCHAR = p0.symbol::VARCHAR
               AND p0.trade_date::DATE = d.entry_date
            WHERE s.volatility_60d IS NOT NULL
              AND isfinite(s.volatility_60d::DOUBLE)
              AND p0.close::DOUBLE > 0
        ), prices AS (
            SELECT
                symbol::VARCHAR AS symbol,
                trade_date::DATE AS price_date,
                close::DOUBLE AS price_close
            FROM read_parquet('{qfq_glob}', hive_partitioning=true)
            WHERE trade_date::DATE BETWEEN DATE '{price_start}' AND DATE '{price_end}'
              AND close::DOUBLE > 0
        )
        SELECT
            b.trade_date,
            b.symbol,
            b.factor_value,
            p.price_close / b.entry_close - 1.0 AS forward_return
        FROM base AS b
        ASOF LEFT JOIN prices AS p
            ON b.symbol = p.symbol
           AND b.exit_date >= p.price_date
        WHERE p.price_date >= b.entry_date
        ORDER BY b.trade_date, b.symbol
    """
    with duckdb.connect() as connection:
        connection.execute(f"SET threads TO {max(1, int(threads))}")
        connection.register("schedule", sql_schedule)
        observations = connection.execute(query).fetch_df()
    observations["trade_date"] = pd.to_datetime(observations["trade_date"])
    return observations


def assign_quintiles(observations: pd.DataFrame) -> pd.DataFrame:
    """Assign deterministic, near-equal quintiles from low to high volatility."""
    required = {"trade_date", "symbol", "factor_value", "forward_return"}
    missing = required - set(observations.columns)
    if missing:
        raise KeyError(f"分位数据缺少字段：{', '.join(sorted(missing))}")
    frame = observations.dropna(subset=list(required)).copy()
    frame = frame.sort_values(["trade_date", "symbol"]).reset_index(drop=True)
    percentile = frame.groupby("trade_date", sort=False)["factor_value"].rank(
        method="first", pct=True
    )
    number = np.ceil(percentile * 5).clip(1, 5).astype(int)
    frame["quantile"] = "Q" + number.astype(str)
    counts = frame.groupby(["trade_date", "quantile"], observed=True).size().unstack()
    if any(label not in counts or counts[label].eq(0).any() for label in QUANTILES):
        raise ValueError("至少一个月度截面无法形成完整五分位")
    return frame


def monthly_portfolio_returns(assigned: pd.DataFrame) -> pd.DataFrame:
    returns = assigned.pivot_table(
        index="trade_date",
        columns="quantile",
        values="forward_return",
        aggfunc="mean",
        observed=True,
    ).reindex(columns=QUANTILES)
    counts = assigned.pivot_table(
        index="trade_date",
        columns="quantile",
        values="symbol",
        aggfunc="count",
        observed=True,
    ).reindex(columns=QUANTILES)
    counts.columns = [f"{label}_count" for label in counts.columns]
    return returns.join(counts).reset_index()


def _after_cost_value(
    current_values: dict[str, float],
    members: list[str],
    *,
    buy_rate: float,
    sell_rate: float,
    available_value: float | None = None,
) -> tuple[float, float, float]:
    """Solve the fully invested post-trade value after asymmetric costs."""
    pre_value = float(
        sum(current_values.values()) if available_value is None else available_value
    )
    if pre_value <= 0 or not members:
        return pre_value, 0.0, 0.0

    def amounts(post_value: float) -> tuple[float, float]:
        target = post_value / len(members)
        member_set = set(members)
        buys = sum(max(target - current_values.get(symbol, 0.0), 0.0) for symbol in members)
        sells = sum(
            max(value - (target if symbol in member_set else 0.0), 0.0)
            for symbol, value in current_values.items()
        )
        return buys, sells

    low, high = 0.0, pre_value
    for _ in range(80):
        middle = (low + high) / 2.0
        buys, sells = amounts(middle)
        implied = pre_value - buy_rate * buys - sell_rate * sells
        if middle <= implied:
            low = middle
        else:
            high = middle
    post_value = (low + high) / 2.0
    buys, sells = amounts(post_value)
    return post_value, buys, sells


def simulate_costed_returns(
    assigned: pd.DataFrame,
    quantile: str,
    *,
    commission: float,
    slippage: float,
    stamp_duty: float,
) -> tuple[pd.Series, float]:
    """Run a fractional-share endpoint ledger with the configured trading costs."""
    selected = assigned[assigned["quantile"].eq(quantile)].copy()
    buy_rate = float(commission) + float(slippage)
    sell_rate = buy_rate + float(stamp_duty)
    values: dict[str, float] = {}
    nav_before = 1.0
    net_returns: dict[pd.Timestamp, float] = {}
    recurring_turnover: list[float] = []
    for index, (day, cohort) in enumerate(selected.groupby("trade_date", sort=True)):
        members = cohort["symbol"].astype(str).tolist()
        pre_trade = float(sum(values.values())) if values else nav_before
        post_trade, buys, sells = _after_cost_value(
            values,
            members,
            buy_rate=buy_rate,
            sell_rate=sell_rate,
            available_value=pre_trade,
        )
        if index > 0 and pre_trade > 0:
            recurring_turnover.append(0.5 * (buys + sells) / pre_trade)
        target = post_trade / len(members)
        period_returns = dict(
            zip(cohort["symbol"].astype(str), cohort["forward_return"].astype(float))
        )
        values = {
            symbol: target * (1.0 + period_returns[symbol]) for symbol in members
        }
        nav_after = float(sum(values.values()))
        net_returns[pd.Timestamp(day)] = nav_after / nav_before - 1.0
        nav_before = nav_after
    average_turnover = float(np.mean(recurring_turnover)) if recurring_turnover else 0.0
    return pd.Series(net_returns, name=quantile).sort_index(), average_turnover


def _annual_return(returns: pd.Series) -> float:
    return float((1.0 + returns.astype(float)).prod() ** (12.0 / len(returns)) - 1.0)


def _max_drawdown(returns: pd.Series) -> float:
    wealth = pd.concat(
        [pd.Series([1.0]), (1.0 + returns.astype(float)).cumprod()], ignore_index=True
    )
    return float((wealth / wealth.cummax() - 1.0).min())


def summarize(
    assigned: pd.DataFrame,
    monthly: pd.DataFrame,
    *,
    commission: float,
    slippage: float,
    stamp_duty: float,
) -> tuple[pd.DataFrame, dict[str, Any], pd.DataFrame]:
    rows: list[dict[str, Any]] = []
    net_frame = pd.DataFrame(index=pd.DatetimeIndex(monthly["trade_date"]))
    for label in QUANTILES:
        gross = monthly.set_index("trade_date")[label].astype(float)
        net, turnover = simulate_costed_returns(
            assigned,
            label,
            commission=commission,
            slippage=slippage,
            stamp_duty=stamp_duty,
        )
        net_frame[label] = net.reindex(net_frame.index)
        rows.append(
            {
                "quantile": label,
                "gross_annual_return": _annual_return(gross),
                "costed_annual_return": _annual_return(net),
                "gross_monthly_mean": float(gross.mean()),
                "gross_win_rate": float(gross.gt(0).mean()),
                "gross_max_drawdown": _max_drawdown(gross),
                "periods": int(len(gross)),
                "average_stocks": float(monthly[f"{label}_count"].mean()),
                "average_one_way_turnover": turnover,
            }
        )
    summary = pd.DataFrame(rows)
    indexed = summary.set_index("quantile")
    q3_minus_q1 = monthly["Q3"].astype(float) - monthly["Q1"].astype(float)
    standard_error = float(q3_minus_q1.std(ddof=1) / math.sqrt(len(q3_minus_q1)))
    diagnostics = {
        "gross_q3_minus_q1_annual_return": float(
            indexed.loc["Q3", "gross_annual_return"]
            - indexed.loc["Q1", "gross_annual_return"]
        ),
        "costed_q3_minus_q1_annual_return": float(
            indexed.loc["Q3", "costed_annual_return"]
            - indexed.loc["Q1", "costed_annual_return"]
        ),
        "gross_q3_above_q1": bool(
            indexed.loc["Q3", "gross_annual_return"]
            > indexed.loc["Q1", "gross_annual_return"]
        ),
        "costed_q3_above_q1": bool(
            indexed.loc["Q3", "costed_annual_return"]
            > indexed.loc["Q1", "costed_annual_return"]
        ),
        "q3_minus_q1_monthly_t_stat": float(q3_minus_q1.mean() / standard_error)
        if standard_error > 0
        else float("nan"),
        "q3_outperformance_month_rate": float(q3_minus_q1.gt(0).mean()),
    }
    return summary, diagnostics, net_frame.reset_index(names="trade_date")


def _pct(value: float) -> str:
    return f"{value:.2%}" if np.isfinite(value) else "—"


def write_outputs(
    output: Path,
    schedule: pd.DataFrame,
    monthly: pd.DataFrame,
    net_monthly: pd.DataFrame,
    summary: pd.DataFrame,
    diagnostics: dict[str, Any],
    *,
    start: str,
    end: str,
    target_annual_return: float,
    commission: float,
    slippage: float,
    stamp_duty: float,
) -> None:
    output.mkdir(parents=True, exist_ok=True)
    summary.to_csv(output / "summary.csv", index=False, encoding="utf-8-sig")
    monthly.to_csv(output / "gross_monthly_returns.csv", index=False, encoding="utf-8-sig")
    net_monthly.to_csv(
        output / "costed_monthly_returns.csv", index=False, encoding="utf-8-sig"
    )
    schedule.drop(columns="path").to_csv(
        output / "rebalance_schedule.csv", index=False, encoding="utf-8-sig"
    )
    indexed = summary.set_index("quantile")
    q3_gross = float(indexed.loc["Q3", "gross_annual_return"])
    diagnostics = {
        **diagnostics,
        "target_annual_return": float(target_annual_return),
        "q3_target_difference": q3_gross - float(target_annual_return),
        "q3_13_85_reproduced_at_2dp": round(q3_gross * 100.0, 2)
        == round(float(target_annual_return) * 100.0, 2),
        "signal_start": start,
        "signal_end": end,
        "first_entry": str(pd.Timestamp(schedule["entry_date"].min()).date()),
        "last_exit": str(pd.Timestamp(schedule["exit_date"].max()).date()),
        "costs": {
            "commission": commission,
            "slippage": slippage,
            "stamp_duty": stamp_duty,
        },
    }
    (output / "diagnostics.json").write_text(
        json.dumps(diagnostics, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    lines = [
        "# volatility_60d 无约束月度分位回测",
        "",
        f"> 信号区间 {start} ~ {end}；月末点时因子，下一交易日执行，持有至下一次月度执行日。",
        "> 仅要求 volatility_60d 有效且执行日有价格；不使用 ST、上市时间、流动性、市值、股息率、Beta、财务、行业或权重上限等策略约束。",
        "",
        "| 分位 | 毛年化 | 含成本年化 | 月均毛收益 | 胜率 | 毛回撤 | 平均股票数 | 单边换手 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in summary.itertuples(index=False):
        lines.append(
            f"| {row.quantile} | {_pct(row.gross_annual_return)} | "
            f"{_pct(row.costed_annual_return)} | {_pct(row.gross_monthly_mean)} | "
            f"{_pct(row.gross_win_rate)} | {_pct(row.gross_max_drawdown)} | "
            f"{row.average_stocks:.1f} | {_pct(row.average_one_way_turnover)} |"
        )
    target_status = "可复现" if diagnostics["q3_13_85_reproduced_at_2dp"] else "不可复现"
    lines.extend(
        [
            "",
            f"- Q3 毛年化为 **{q3_gross:.2%}**，目标 13.85% **{target_status}**，差值 **{diagnostics['q3_target_difference']:+.2%}**。",
            f"- 毛收益 Q3−Q1 为 **{diagnostics['gross_q3_minus_q1_annual_return']:+.2%}**；配对月收益 t 值 **{diagnostics['q3_minus_q1_monthly_t_stat']:.2f}**。",
            f"- 含当前成本后 Q3−Q1 为 **{diagnostics['costed_q3_minus_q1_annual_return']:+.2%}**。成本参数：佣金 {commission:.2%}、滑点 {slippage:.2%}、卖出印花税 {stamp_duty:.2%}。",
            "",
            "固定 20 个交易日的诊断收益与严格月调仓收益不是同一时间区间：前者会在相邻月份产生空档或重叠，后者使用相邻两次调仓日，因而不能期待相同年化结果。",
            "",
        ]
    )
    (output / "report.md").write_text("\n".join(lines), encoding="utf-8")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="volatility_60d 无约束月度五分位回测")
    parser.add_argument("--start", default="2015-01-01")
    parser.add_argument("--end", default="2024-12-31")
    parser.add_argument("--output", default="output/volatility_quantile_backtest")
    parser.add_argument("--threads", type=int, default=4)
    parser.add_argument("--target-annual-return", type=float, default=0.1385)
    parser.add_argument("--commission", type=float, default=0.0003)
    parser.add_argument("--slippage", type=float, default=0.001)
    parser.add_argument("--stamp-duty", type=float, default=0.001)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    schedule = build_rebalance_schedule(args.start, args.end)
    print(f"加载 {len(schedule)} 个相邻月度持有区间...", flush=True)
    observations = load_monthly_observations(schedule, threads=args.threads)
    assigned = assign_quintiles(observations)
    monthly = monthly_portfolio_returns(assigned)
    summary, diagnostics, net_monthly = summarize(
        assigned,
        monthly,
        commission=args.commission,
        slippage=args.slippage,
        stamp_duty=args.stamp_duty,
    )
    output = Path(args.output)
    if not output.is_absolute():
        output = ROOT / output
    write_outputs(
        output,
        schedule,
        monthly,
        net_monthly,
        summary,
        diagnostics,
        start=args.start,
        end=args.end,
        target_annual_return=args.target_annual_return,
        commission=args.commission,
        slippage=args.slippage,
        stamp_duty=args.stamp_duty,
    )
    print(summary.to_string(index=False))
    print(json.dumps(diagnostics, ensure_ascii=False, indent=2))
    print(f"结果已写入 {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

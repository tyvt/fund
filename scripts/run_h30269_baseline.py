#!/usr/bin/env python
"""Run the official-rule H30269 replication baseline and write audit artifacts."""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Mapping

import duckdb
import matplotlib.pyplot as plt
from matplotlib import font_manager
import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]


def _metric(series: pd.Series) -> dict[str, float]:
    nav = series.astype(float).dropna()
    returns = nav.pct_change().dropna()
    years = (nav.index[-1] - nav.index[0]).days / 365.2425 if len(nav) > 1 else 0.0
    total = float(nav.iloc[-1] / nav.iloc[0] - 1.0) if len(nav) else float("nan")
    annual = float((nav.iloc[-1] / nav.iloc[0]) ** (1.0 / years) - 1.0) if years > 0 else 0.0
    drawdown = float((nav / nav.cummax() - 1.0).min()) if len(nav) else float("nan")
    sharpe = (
        float(returns.mean() / returns.std(ddof=1) * math.sqrt(252.0))
        if len(returns) > 1 and returns.std(ddof=1) > 0
        else float("nan")
    )
    return {
        "total_return": total,
        "annual_return": annual,
        "max_drawdown": drawdown,
        "sharpe_ratio": sharpe,
        "observations": int(len(nav)),
    }


def load_adjusted_close(symbols: list[str], start: str, end: str) -> pd.DataFrame:
    if not symbols:
        raise ValueError("没有入选证券")
    escaped = ",".join("'" + value.replace("'", "''") + "'" for value in symbols)
    qfq = (ROOT / "data/parquet/stock_daily_qfq/year=*/*.parquet").as_posix()
    raw = (ROOT / "data/parquet/stock_daily/year=*/*.parquet").as_posix()
    query = f"""
    WITH q AS (
      SELECT try_cast(trade_date AS DATE) trade_date, symbol, max(close) AS close_px
      FROM read_parquet('{qfq}', hive_partitioning=true, union_by_name=true)
      WHERE try_cast(trade_date AS DATE) BETWEEN DATE '{start}' - INTERVAL 5 DAY
        AND DATE '{end}' AND symbol IN ({escaped}) GROUP BY 1,2
    ), r AS (
      SELECT try_cast(trade_date AS DATE) trade_date, symbol, max(close) AS close_px
      FROM read_parquet('{raw}', hive_partitioning=true, union_by_name=true)
      WHERE try_cast(trade_date AS DATE) BETWEEN DATE '{start}' - INTERVAL 5 DAY
        AND DATE '{end}' AND symbol IN ({escaped}) GROUP BY 1,2
    )
    SELECT r.trade_date, r.symbol, coalesce(q.close_px, r.close_px) AS close_px
    FROM r LEFT JOIN q USING(trade_date, symbol) ORDER BY 1,2
    """
    with duckdb.connect() as con:
        frame = con.execute(query).fetch_df()
    close = frame.pivot(index="trade_date", columns="symbol", values="close_px").sort_index()
    close.index = pd.DatetimeIndex(pd.to_datetime(close.index))
    return close.ffill()


def load_raw_prices(symbols: list[str], dates: list[pd.Timestamp]) -> pd.DataFrame:
    if not symbols or not dates:
        return pd.DataFrame()
    escaped = ",".join("'" + value.replace("'", "''") + "'" for value in symbols)
    start, end = min(dates).date().isoformat(), max(dates).date().isoformat()
    raw = (ROOT / "data/parquet/stock_daily/year=*/*.parquet").as_posix()
    with duckdb.connect() as con:
        frame = con.execute(
            f"""SELECT try_cast(trade_date AS DATE) trade_date, symbol, max(close) AS close_px
            FROM read_parquet('{raw}', hive_partitioning=true, union_by_name=true)
            WHERE try_cast(trade_date AS DATE) BETWEEN DATE '{start}' AND DATE '{end}'
              AND symbol IN ({escaped}) GROUP BY 1,2"""
        ).fetch_df()
    result = frame.pivot(index="trade_date", columns="symbol", values="close_px")
    result.index = pd.DatetimeIndex(pd.to_datetime(result.index))
    return result


def simulate_portfolio(
    close: pd.DataFrame,
    targets: Mapping[pd.Timestamp, pd.Series],
    *,
    commission: float,
    stamp_duty: float,
    slippage: float,
    initial_capital: float = 100000.0,
    min_commission: float = 5.0,
) -> tuple[pd.Series, dict[str, float]]:
    """Drift weights daily and rebalance at official effective dates."""
    returns = close.pct_change(fill_method=None).replace([np.inf, -np.inf], np.nan).fillna(0.0)
    events = sorted((pd.Timestamp(day), weights.astype(float)) for day, weights in targets.items())
    if not events:
        raise ValueError("没有调样权重")
    latest = max((item for item in events if item[0] <= close.index[0]), default=events[0], key=lambda x: x[0])
    current = latest[1].reindex(close.columns).fillna(0.0)
    current = current / current.sum()
    nav = pd.Series(index=close.index, dtype=float, name="strategy_nav")
    nav.iloc[0] = 1.0
    turnovers: dict[str, float] = {}
    event_map = {day: weights for day, weights in events if day >= close.index[0]}
    for position in range(1, len(close.index)):
        day = close.index[position]
        value = float(nav.iloc[position - 1])
        target = event_map.get(day)
        if target is not None:
            target = target.reindex(close.columns).fillna(0.0)
            target /= float(target.sum())
            delta = target - current
            traded = delta.abs()[delta.abs().gt(1e-12)]
            one_way = float(traded.sum() * 0.5)
            portfolio_value = max(value * float(initial_capital), 1e-9)
            # Commission and slippage apply to buys and sells; stamp duty to sells.
            commission_cash = sum(
                max(float(min_commission), commission * float(weight) * portfolio_value)
                for weight in traded
            )
            cost = (
                commission_cash / portfolio_value
                + float(traded.sum()) * slippage
                + float((-delta[delta.lt(0)]).sum()) * stamp_duty
            )
            value *= max(0.0, 1.0 - cost)
            turnovers[day.date().isoformat()] = one_way
            current = target
        daily = returns.iloc[position]
        portfolio_return = float((current * daily).sum())
        value *= 1.0 + portfolio_return
        nav.iloc[position] = value
        grown = current * (1.0 + daily)
        total = float(grown.sum())
        current = grown / total if total > 0 else current
    return nav, turnovers


def load_benchmark(code: str) -> pd.Series:
    files = list((ROOT / "cache/cn").glob(f"index_perf_{code}_*.csv"))
    if not files:
        raise FileNotFoundError(f"缺少基准缓存：{code}")
    best = max(files, key=lambda path: (pd.read_csv(path, usecols=["tradeDate"])["tradeDate"].max(), path.stat().st_size))
    frame = pd.read_csv(best)
    date_column = "date" if "date" in frame else "tradeDate"
    series = pd.Series(
        pd.to_numeric(frame["close"], errors="coerce").to_numpy(),
        index=pd.to_datetime(frame[date_column], errors="coerce"),
        name=code,
    ).dropna()
    return series[~series.index.duplicated(keep="last")].sort_index()


def _configure_font() -> None:
    for path in (Path("C:/Windows/Fonts/msyh.ttc"), Path("C:/Windows/Fonts/simhei.ttf")):
        if path.is_file():
            font_manager.fontManager.addfont(str(path))
            plt.rcParams["font.sans-serif"] = [font_manager.FontProperties(fname=str(path)).get_name()]
            break
    plt.rcParams["axes.unicode_minus"] = False


def write_comparison_plot(frame: pd.DataFrame, metrics: Mapping[str, Mapping[str, float]], output: Path) -> None:
    _configure_font()
    normalized = frame.div(frame.iloc[0])
    labels = {
        "strategy_nav": "官方规则复现策略",
        "H20269": "H20269 全收益指数",
        "H30269": "H30269 价格指数",
    }
    fig, ax = plt.subplots(figsize=(13, 7))
    normalized.rename(columns=labels).plot(ax=ax, linewidth=1.6)
    ax.set_title("中证红利低波动指数：复现策略与官方基准")
    ax.set_xlabel("日期")
    ax.set_ylabel("净值（起点=1）")
    ax.grid(alpha=0.25)
    annotation = "\n".join(
        f"{labels[key]}：年化 {metrics[key]['annual_return']:.2%}，回撤 {metrics[key]['max_drawdown']:.2%}，夏普 {metrics[key]['sharpe_ratio']:.2f}"
        for key in ("strategy_nav", "H20269", "H30269")
    )
    ax.text(0.01, 0.99, annotation, transform=ax.transAxes, va="top", fontsize=9,
            bbox={"facecolor": "white", "alpha": 0.78, "edgecolor": "#bbbbbb"})
    fig.tight_layout()
    output.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output, dpi=180)
    plt.close(fig)


def run_baseline(config: Mapping[str, Any], *, tag: str = "baseline_h30269", verbose: bool = False) -> dict[str, Any]:
    from vbt.strategies.dividend_lowvol_baseline import DividendLowVolBaseline

    params = dict(config["baseline"])
    backtest = dict(config.get("backtest") or {})
    start = str(backtest.get("start_date", "2015-01-01"))
    end = str(backtest.get("end_date", "2026-07-28"))
    output_dir = ROOT / str((config.get("output") or {}).get("directory", "output/baseline"))
    output_dir.mkdir(parents=True, exist_ok=True)

    strategy = DividendLowVolBaseline(params)
    history = strategy.build_history(pd.Timestamp(start).year - 1, pd.Timestamp(end).year)
    history = [snapshot for snapshot in history if snapshot.effective_date <= pd.Timestamp(end)]
    symbols = sorted({symbol for snapshot in history for symbol in snapshot.final_selection})
    close = load_adjusted_close(symbols, start, end)
    h20269, h30269 = load_benchmark("H20269"), load_benchmark("H30269")
    common_start = max(pd.Timestamp(start), close.index.min(), h20269.index.min(), h30269.index.min())
    common_end = min(pd.Timestamp(end), close.index.max(), h20269.index.max(), h30269.index.max())
    close = close.loc[common_start:common_end]
    targets = {snapshot.effective_date: snapshot.weights for snapshot in history}
    nav, turnover = simulate_portfolio(
        close,
        targets,
        commission=float(backtest.get("commission", 0.0003)),
        stamp_duty=float(backtest.get("stamp_duty", 0.001)),
        slippage=float(backtest.get("slippage", 0.001)),
        initial_capital=float(backtest.get("initial_capital", 100000.0)),
        min_commission=float(backtest.get("min_commission", 5.0)),
    )
    frame = pd.concat(
        [nav, h20269.rename("H20269"), h30269.rename("H30269")], axis=1, join="inner"
    ).dropna()
    frame = frame.loc[common_start:common_end]
    frame["strategy_nav"] /= float(frame["strategy_nav"].iloc[0])
    metrics = {column: _metric(frame[column]) for column in frame.columns}
    annual_turnover = float(sum(turnover.values()) / max((common_end - common_start).days / 365.2425, 1e-9))
    metrics["strategy_nav"]["turnover"] = annual_turnover

    snapshots_payload: list[dict[str, Any]] = []
    stage_rows: list[dict[str, Any]] = []
    holding_rows: list[dict[str, Any]] = []
    for snapshot in history:
        item = {
            "signal_date": snapshot.signal_date.date().isoformat(),
            "effective_date": snapshot.effective_date.date().isoformat(),
            "pure_selection": list(snapshot.pure_selection),
            "final_selection": list(snapshot.final_selection),
            "weights": {str(k): float(v) for k, v in snapshot.weights.items()},
            "stage_counts": snapshot.stage_counts,
            "buffer_dependency": snapshot.buffer_dependency,
            "exceptions": list(snapshot.exceptions),
        }
        snapshots_payload.append(item)
        stage_rows.append({"effective_date": item["effective_date"], **snapshot.stage_counts,
                           "buffer_dependency": snapshot.buffer_dependency})
        for symbol, weight in snapshot.weights.items():
            holding_rows.append({"effective_date": item["effective_date"], "symbol": symbol, "weight": weight})

    payload: dict[str, Any] = {
        "tag": tag,
        "methodology": {
            "index": "H30269",
            "total_return_index": "H20269",
            "official_methodology_checked_on": "2026-08-27",
            "official_methodology_version": "V1.1 (2020-12 update; current official URL checked)",
            "official_methodology_url": "https://oss-ch.csindex.com.cn/static/html/csindex/public/uploads/indices/detail/files/zh_CN/H30269_Index_Methodology_cn.pdf",
            "dps_growth_translation": "DPS(t) / DPS(t-2) - 1; require all three fiscal years positive",
            "after_tax_factor": float(params.get("after_tax_factor", 0.90)),
            "known_deviations": [
                "中证全指历史逐日成分不可得，使用沪深A股、非ST、在市证券构造PIT代理空间。",
                "官网未公布每股股利增长率公式，采用三年年度DPS首尾增长率直译。",
                "税后股息统一按90%折算；实际指数供应商税务细节不可从公开方案还原。",
                "前复权价格近似含分红再投资；不复刻指数除数及临时公司行动处理。",
            ],
            "phase_7b_preregistered": ["申万一级行业内股息率排名"],
        },
        "period": {"start": frame.index.min().date().isoformat(), "end": frame.index.max().date().isoformat()},
        "metrics": metrics,
        "annual_one_way_turnover": annual_turnover,
        "turnover_by_date": turnover,
        "snapshots": snapshots_payload,
        "acceptance": {
            "annual_return_pass": metrics["strategy_nav"]["annual_return"] >= 0.10,
            "max_drawdown_pass": metrics["strategy_nav"]["max_drawdown"] >= -0.40,
            "sharpe_pass": metrics["strategy_nav"]["sharpe_ratio"] >= 0.55,
            "annual_return_gap_vs_total_return": metrics["strategy_nav"]["annual_return"] - metrics["H20269"]["annual_return"],
            "within_2pp_of_total_return": abs(metrics["strategy_nav"]["annual_return"] - metrics["H20269"]["annual_return"]) <= 0.02,
        },
    }
    metrics_path = output_dir / f"metrics_{tag}.json"
    nav_path = output_dir / f"nav_{tag}.csv"
    metrics_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    frame.to_csv(nav_path, encoding="utf-8-sig", index_label="date")
    pd.DataFrame(stage_rows).to_csv(output_dir / "candidate_stage_counts.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame(holding_rows).to_csv(output_dir / "holdings_by_rebalance.csv", index=False, encoding="utf-8-sig")
    (output_dir / "selection_audit.json").write_text(
        json.dumps(snapshots_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (output_dir / "edge_case_exceptions.json").write_text(
        json.dumps(
            {
                "policy_exclusions": [
                    {
                        "case": "送股/转增股本",
                        "treatment": "不计入现金分红；仅 dividend_cash_before_tax > 0 的除权事件参与连续分红和DPS计算",
                    },
                    {
                        "case": "配股",
                        "treatment": "不计入现金分红；前复权行情负责价格连续性近似",
                    },
                    {
                        "case": "B转A或非标准代码迁移",
                        "treatment": "仅纳入证券元数据中交易所为 XSHG/XSHE 且代码为六位数字的A股记录；无法建立可靠PIT映射的迁移记录排除",
                    },
                    {
                        "case": "退市/合并/分拆",
                        "treatment": "调样时剔除已退市证券；区间内临时替代及指数除数修正无法由公开数据完整复刻，列为已知偏差",
                    },
                ],
                "review_buffer_exceptions": [
                    {"effective_date": item["effective_date"], **exception}
                    for item in snapshots_payload for exception in item["exceptions"]
                ],
            }, ensure_ascii=False, indent=2
        ) + "\n", encoding="utf-8"
    )
    write_comparison_plot(frame, metrics, output_dir / "vs_benchmark.png")
    write_report(payload, output_dir / "baseline_report.md")
    if verbose:
        print(pd.DataFrame(metrics).T.to_string())
    print(json.dumps({"tag": tag, "metrics": metrics["strategy_nav"], "acceptance": payload["acceptance"]}, ensure_ascii=False, indent=2))
    return payload


def write_report(payload: Mapping[str, Any], output: Path) -> None:
    metrics = payload["metrics"]
    acceptance = payload["acceptance"]
    rows = []
    labels = {"strategy_nav": "复现策略", "H20269": "H20269 全收益", "H30269": "H30269 价格"}
    for key in ("strategy_nav", "H20269", "H30269"):
        value = metrics[key]
        rows.append(
            f"| {labels[key]} | {value['annual_return']:.2%} | {value['max_drawdown']:.2%} | {value['sharpe_ratio']:.2f} | {value['total_return']:.2%} |"
        )
    methodology = payload["methodology"]
    lines = [
        "# H30269 官方规则复现基础层报告",
        "",
        f"回测区间：{payload['period']['start']} 至 {payload['period']['end']}。官网编制方案核对日期：**{methodology['official_methodology_checked_on']}**。",
        "",
        "## 三线对账表",
        "",
        "| 序列 | 年化收益 | 最大回撤 | 夏普 | 累计收益 |",
        "|---|---:|---:|---:|---:|",
        *rows,
        "",
        f"复现策略相对 H20269 年化偏差：**{acceptance['annual_return_gap_vs_total_return']:+.2%}**；±2pp 对账：**{'PASS' if acceptance['within_2pp_of_total_return'] else 'FAIL'}**。",
        "",
        f"年均单边换手：**{payload['annual_one_way_turnover']:.2%}**。收益/回撤/夏普硬门槛："
        f"{'PASS' if acceptance['annual_return_pass'] else 'FAIL'} / {'PASS' if acceptance['max_drawdown_pass'] else 'FAIL'} / {'PASS' if acceptance['sharpe_pass'] else 'FAIL'}。",
        "",
        "![三线净值对比](vs_benchmark.png)",
        "",
        "## 冻结编制规则",
        "",
        "- 中证全指代理空间；过去一年日均总市值、成交金额均居前 80%。",
        "- 三个完整自然年逐年现金分红，以除权日为 PIT 生效口径。",
        "- 剔除过去一年红利支付率为负或位于最高 5%，并剔除三年每股股利增长非正证券。",
        "- 三年平均税后现金股息率前 75，再按一年年化日收益波动率取最低 50。",
        "- 股息率加权，单股上限 15%；每年 12 月第二个星期五的下一交易日生效。",
        "- 原样本执行 0.5% 股息率、全市场市值/成交额前 90% 保留门槛，通常最多替换 20%。",
        "",
        "## 股息税专项",
        "",
        f"选样股息统一按税前现金股利的 {methodology['after_tax_factor']:.0%} 折算。该常数不改变横截面排序，但影响 0.5% 保留门槛。策略收益使用前复权价格近似现金分红再投资；因此可与 H20269 全收益指数对账，但不等同于复刻指数公司的逐笔税务与除数维护。",
        "",
        "## 偏差归因与例外",
        "",
        *[f"- {item}" for item in methodology["known_deviations"]],
        "",
        "每股股利增长率公式采用 `DPS(t) / DPS(t-2) - 1`。这是对官网模糊字段的量化直译，不是优化参数。送转、配股不计入现金分红；B 转 A 等未进入沪深 A 股代理空间的记录不纳入。原样本缓冲例外逐期记录在 `edge_case_exceptions.json`。",
        "",
        "## 数据与审计文件",
        "",
        "- 官方方案：" + methodology["official_methodology_url"],
        "- `selection_audit.json`：逐年纯选样、缓冲后样本、权重和阶段计数。",
        "- `candidate_stage_counts.csv`：候选池健康度。",
        "- `holdings_by_rebalance.csv`：逐调样持仓权重。",
        "",
        "## Phase 7-B 预注册",
        "",
        "- 行业内股息率排名（申万一级）仅列为候选 Alpha，不进入本次基线。",
        "",
    ]
    output.write_text("\n".join(lines), encoding="utf-8")


__all__ = [
    "load_adjusted_close",
    "load_benchmark",
    "run_baseline",
    "simulate_portfolio",
    "write_comparison_plot",
]

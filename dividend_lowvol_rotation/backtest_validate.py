# -*- coding: utf-8 -*-
"""红利低波轮动：走步前向分析（WFA）+ 蒙特卡洛检验。"""

from __future__ import annotations

import argparse
import json
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from html import escape
from pathlib import Path

import numpy as np
import pandas as pd

from dividend_lowvol_rotation.backtest import (
    BacktestContext,
    _rebalance_dates,
    _trading_calendar,
    default_start_years,
    prepare_backtest_context,
    run_backtest,
)
from dividend_lowvol_rotation.config import (
    BACKTEST_INITIAL_CAPITAL,
    BACKTEST_OUTPUT_DIR,
    BACKTEST_PREFETCH_SIZE,
    BACKTEST_REBALANCE_DAYS,
    BACKTEST_YEARS,
    TOP_N_BUY,
    resolve_sell_rank,
)
from dividend_lowvol_rotation.strategy_params import StrategyParams
from config import get_dividend_total_return_code
from market_data import configure_stdout_utf8, get_index_perf_history

DEFAULT_MC_PERMUTATIONS = 200
DEFAULT_WFA_FREQ = "year"
FAST_MC_PERMUTATIONS = 100
DEFAULT_OPTIMIZE_JSON = BACKTEST_OUTPUT_DIR / "optimize.json"


def load_optimal_params(path: Path | str | None = None) -> StrategyParams:
    """从 optimize.json 读取综合最优参数。"""
    path = Path(path) if path else DEFAULT_OPTIMIZE_JSON
    if not path.exists():
        raise FileNotFoundError(f"未找到优化结果：{path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    trials = data.get("grid_results", []) + data.get("bayes_results", [])
    if not trials:
        raise ValueError(f"{path} 中无试验结果")
    best = max(trials, key=lambda t: t["metrics"]["score"])
    allowed = {f.name for f in StrategyParams.__dataclass_fields__.values()}
    clean = {k: v for k, v in best["params"].items() if k in allowed}
    return StrategyParams(**clean)


def _resolve_run_args(
    top_n: int,
    sell_rank: int | None,
    rebalance_days: int,
    strategy_params: StrategyParams | None,
) -> tuple[int, int, int, StrategyParams]:
    sp = strategy_params or StrategyParams()
    tn = sp.resolved_top_n(top_n)
    rb = sp.resolved_rebalance_days(rebalance_days)
    sr = sp.resolved_sell_rank(tn) if sell_rank is None else sell_rank
    return tn, sr, rb, sp


@dataclass
class WfaWindowResult:
    label: str
    start: str
    end: str
    strategy_return_pct: float | None
    hold_return_pct: float | None
    return_edge_pct: float | None
    strategy_trades: int
    note: str = ""


@dataclass
class WfaSummary:
    windows: int
    windows_active: int
    strategy_wins: int
    mean_edge_pct: float | None
    stitched_strategy_pct: float | None
    stitched_hold_pct: float | None
    full_strategy_pct: float | None
    full_hold_pct: float | None


@dataclass
class MonteCarloResult:
    actual_return_pct: float | None
    actual_cagr_pct: float | None
    perm_mean_pct: float | None
    perm_std_pct: float | None
    perm_median_pct: float | None
    perm_min_pct: float | None
    perm_max_pct: float | None
    percentile: float | None
    p_value: float | None
    significant_5pct: bool | None
    permutations: int
    method: str


@dataclass
class BenchmarkWindowResult:
    label: str
    start: str
    end: str
    strategy_return_pct: float | None
    index_return_pct: float | None
    hold_return_pct: float | None
    strategy_edge_pct: float | None
    note: str = ""


@dataclass
class BenchmarkSummary:
    windows: int
    windows_active: int
    strategy_wins_vs_index: int
    mean_edge_vs_index_pct: float | None
    stitched_strategy_pct: float | None
    stitched_index_pct: float | None
    full_strategy_pct: float | None
    full_index_pct: float | None
    full_hold_pct: float | None
    index_cagr_pct: float | None
    strategy_cagr_pct: float | None


def _index_name(index_code: str) -> str:
    names = {"H30269": "中证红利低波动", "H20269": "中证红利低波动全收益"}
    return names.get(index_code, index_code)


def load_index_benchmark_nav(
    index_code: str,
    start: str,
    end: str | None,
    initial_capital: float,
) -> tuple[pd.DataFrame, str]:
    """加载指数基准净值（红利指数用全收益 H20269，含分红再投资）。"""
    tr_code = get_dividend_total_return_code(index_code) or index_code
    end_s = end or pd.Timestamp.today().strftime("%Y-%m-%d")
    start_fmt = pd.Timestamp(start).strftime("%Y%m%d")
    end_fmt = pd.Timestamp(end_s).strftime("%Y%m%d")
    hist = get_index_perf_history(tr_code, start_fmt, end_fmt)
    if hist is None or hist.empty:
        return pd.DataFrame(), tr_code
    hist = hist.copy()
    hist["date"] = pd.to_datetime(hist["date"])
    hist = hist.sort_values("date").dropna(subset=["close"])
    if hist.empty:
        return pd.DataFrame(), tr_code
    base_close = float(hist["close"].iloc[0])
    nav = initial_capital * hist["close"].astype(float) / base_close
    out = pd.DataFrame(
        {
            "date": hist["date"].dt.strftime("%Y-%m-%d"),
            "nav": nav.round(2),
            "close": hist["close"].astype(float).values,
        }
    )
    return out, tr_code


def _nav_cagr_pct(nav_df: pd.DataFrame, initial_capital: float) -> float | None:
    if nav_df.empty:
        return None
    v1 = float(nav_df["nav"].iloc[-1])
    t0 = pd.Timestamp(nav_df["date"].iloc[0])
    t1 = pd.Timestamp(nav_df["date"].iloc[-1])
    years = max((t1 - t0).days / 365.25, 1 / 365)
    total_ret = v1 / initial_capital - 1
    return float(((1 + total_ret) ** (1 / years) - 1) * 100)


def run_benchmark_compare(
    *,
    index_code: str = "H30269",
    start: str,
    end: str | None,
    top_n: int = TOP_N_BUY,
    sell_rank: int | None = None,
    rebalance_days: int = BACKTEST_REBALANCE_DAYS,
    initial_capital: float = BACKTEST_INITIAL_CAPITAL,
    prefetch_size: int = BACKTEST_PREFETCH_SIZE,
    freq: str = DEFAULT_WFA_FREQ,
    verbose: bool = True,
    ctx: BacktestContext | None = None,
    strategy_params: StrategyParams | None = None,
) -> tuple[list[BenchmarkWindowResult], BenchmarkSummary, dict, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    top_n, sell_rank, rebalance_days, sp = _resolve_run_args(
        top_n, sell_rank, rebalance_days, strategy_params
    )
    end = end or pd.Timestamp.today().strftime("%Y-%m-%d")

    if ctx is None:
        if verbose:
            print("预加载数据…")
        ctx = prepare_backtest_context(
            start, end, prefetch_size=prefetch_size, rebalance_days=rebalance_days, verbose=verbose
        )
    elif verbose:
        print("复用已加载数据…")

    if verbose:
        print("运行轮动策略…")
    strat_nav, _, _, _, strat_meta, _ = run_backtest(
        start=start,
        end=end,
        top_n=top_n,
        sell_rank=sell_rank,
        rebalance_days=rebalance_days,
        initial_capital=initial_capital,
        prefetch_size=prefetch_size,
        ctx=ctx,
        record_details=False,
        verbose=False,
        strategy_params=sp,
    )
    if verbose:
        print("运行买入持有对照…")
    hold_nav, _, _, _, hold_meta, _ = run_backtest(
        start=start,
        end=end,
        top_n=top_n,
        sell_rank=sell_rank,
        rebalance_days=rebalance_days,
        initial_capital=initial_capital,
        prefetch_size=prefetch_size,
        hold_only=True,
        ctx=ctx,
        record_details=False,
        verbose=False,
        strategy_params=sp,
    )
    if verbose:
        print(f"加载指数 {index_code}（全收益）…")
    index_nav, tr_code = load_index_benchmark_nav(index_code, start, end, initial_capital)

    windows: list[BenchmarkWindowResult] = []
    for label, w_start, w_end in iter_wfa_windows(start, end, freq=freq):
        s_ret = _window_return(strat_nav, w_start, w_end, initial_capital)
        i_ret = _window_return(index_nav, w_start, w_end, initial_capital)
        h_ret = _window_return(hold_nav, w_start, w_end, initial_capital)
        edge = (s_ret - i_ret) if s_ret is not None and i_ret is not None else None
        note = ""
        if s_ret is None and i_ret is None:
            note = "无数据"
        windows.append(
            BenchmarkWindowResult(
                label=label,
                start=w_start,
                end=w_end,
                strategy_return_pct=s_ret,
                index_return_pct=i_ret,
                hold_return_pct=h_ret,
                strategy_edge_pct=edge,
                note=note,
            )
        )

    active = [w for w in windows if w.strategy_return_pct is not None and w.index_return_pct is not None]
    edges = [w.strategy_edge_pct for w in active if w.strategy_edge_pct is not None]
    wins = sum(1 for w in active if w.strategy_edge_pct is not None and w.strategy_edge_pct > 0)
    full_index_pct = None
    if not index_nav.empty:
        full_index_pct = (float(index_nav["nav"].iloc[-1]) / initial_capital - 1) * 100

    summary = BenchmarkSummary(
        windows=len(windows),
        windows_active=len(active),
        strategy_wins_vs_index=wins,
        mean_edge_vs_index_pct=(sum(edges) / len(edges)) if edges else None,
        stitched_strategy_pct=_stitched_return(active, "strategy_return_pct"),
        stitched_index_pct=_stitched_return(active, "index_return_pct"),
        full_strategy_pct=strat_meta.get("total_return_pct"),
        full_index_pct=full_index_pct,
        full_hold_pct=hold_meta.get("total_return_pct"),
        index_cagr_pct=_nav_cagr_pct(index_nav, initial_capital),
        strategy_cagr_pct=strat_meta.get("cagr_pct"),
    )
    meta = {
        "start": start,
        "end": end,
        "freq": freq,
        "top_n": top_n,
        "sell_rank": sell_rank,
        "initial_capital": initial_capital,
        "rebalance_days": rebalance_days,
        "index_code": index_code,
        "index_tr_code": tr_code,
        "index_name": _index_name(index_code),
        "index_tr_name": _index_name(tr_code),
        "strategy_params": sp.to_dict(),
        "strategy_params_summary": sp.summary(),
    }
    return windows, summary, meta, strat_nav, index_nav, hold_nav


def _resolve_end(end: str | None) -> pd.Timestamp:
    return pd.Timestamp(end) if end else pd.Timestamp.today().normalize()


def iter_annual_windows(start: str, end: str | None):
    start_ts = pd.Timestamp(start)
    end_ts = _resolve_end(end)
    for year in range(start_ts.year, end_ts.year + 1):
        w_start = max(start_ts, pd.Timestamp(f"{year}-01-01"))
        w_end = min(end_ts, pd.Timestamp(f"{year}-12-31"))
        if w_start > w_end:
            continue
        yield str(year), w_start.strftime("%Y-%m-%d"), w_end.strftime("%Y-%m-%d")


def iter_period_windows(start: str, end: str | None, months: int):
    start_ts = pd.Timestamp(start)
    end_ts = _resolve_end(end)
    cursor = start_ts
    while cursor <= end_ts:
        w_end = min(cursor + pd.DateOffset(months=months) - pd.Timedelta(days=1), end_ts)
        if cursor > w_end:
            break
        label = f"{cursor.strftime('%Y-%m')}_{w_end.strftime('%Y-%m')}"
        yield label, cursor.strftime("%Y-%m-%d"), w_end.strftime("%Y-%m-%d")
        cursor = w_end + pd.Timedelta(days=1)


def iter_wfa_windows(start: str, end: str | None, *, freq: str = DEFAULT_WFA_FREQ):
    if freq == "year":
        yield from iter_annual_windows(start, end)
    elif freq == "half":
        yield from iter_period_windows(start, end, 6)
    elif freq == "quarter":
        yield from iter_period_windows(start, end, 3)
    else:
        raise ValueError(f"未知频率: {freq}")


def _window_return(
    nav_df: pd.DataFrame,
    w_start: str,
    w_end: str,
    initial_capital: float,
) -> float | None:
    if nav_df.empty:
        return None
    before = nav_df[nav_df["date"] < w_start]
    inside = nav_df[(nav_df["date"] >= w_start) & (nav_df["date"] <= w_end)]
    if inside.empty:
        return None
    v0 = float(before["nav"].iloc[-1]) if not before.empty else float(initial_capital)
    v1 = float(inside["nav"].iloc[-1])
    if v0 <= 0:
        return None
    return (v1 / v0 - 1) * 100


def _count_trades(trades_df: pd.DataFrame, w_start: str, w_end: str) -> int:
    if trades_df.empty:
        return 0
    sub = trades_df[(trades_df["date"] >= w_start) & (trades_df["date"] <= w_end)]
    return len(sub)


def _stitched_return(windows: list[WfaWindowResult], attr: str) -> float | None:
    growth = 1.0
    used = 0
    for w in windows:
        val = getattr(w, attr)
        if val is None:
            continue
        growth *= 1.0 + val / 100.0
        used += 1
    if used == 0:
        return None
    return (growth - 1.0) * 100.0


def run_wfa(
    *,
    start: str,
    end: str | None,
    top_n: int = TOP_N_BUY,
    sell_rank: int | None = None,
    rebalance_days: int = BACKTEST_REBALANCE_DAYS,
    initial_capital: float = BACKTEST_INITIAL_CAPITAL,
    prefetch_size: int = BACKTEST_PREFETCH_SIZE,
    freq: str = DEFAULT_WFA_FREQ,
    verbose: bool = True,
    ctx: BacktestContext | None = None,
    strategy_params: StrategyParams | None = None,
) -> tuple[list[WfaWindowResult], WfaSummary, dict, pd.DataFrame, pd.DataFrame]:
    top_n, sell_rank, rebalance_days, sp = _resolve_run_args(
        top_n, sell_rank, rebalance_days, strategy_params
    )
    end = end or pd.Timestamp.today().strftime("%Y-%m-%d")

    if ctx is None:
        if verbose:
            print("预加载数据（仅一次）…")
        ctx = prepare_backtest_context(
            start, end, prefetch_size=prefetch_size, rebalance_days=rebalance_days, verbose=verbose
        )
    elif verbose:
        print("复用已加载数据…")

    if verbose:
        print("运行策略…")
    strat_nav, strat_trades, _, _, strat_meta, _ = run_backtest(
        start=start,
        end=end,
        top_n=top_n,
        sell_rank=sell_rank,
        rebalance_days=rebalance_days,
        initial_capital=initial_capital,
        prefetch_size=prefetch_size,
        ctx=ctx,
        record_details=False,
        verbose=False,
        strategy_params=sp,
    )
    if verbose:
        print("运行买入持有对照…")
    hold_nav, hold_trades, _, _, hold_meta, _ = run_backtest(
        start=start,
        end=end,
        top_n=top_n,
        sell_rank=sell_rank,
        rebalance_days=rebalance_days,
        initial_capital=initial_capital,
        prefetch_size=prefetch_size,
        hold_only=True,
        ctx=ctx,
        record_details=False,
        verbose=False,
        strategy_params=sp,
    )

    windows: list[WfaWindowResult] = []
    for label, w_start, w_end in iter_wfa_windows(start, end, freq=freq):
        s_ret = _window_return(strat_nav, w_start, w_end, initial_capital)
        h_ret = _window_return(hold_nav, w_start, w_end, initial_capital)
        edge = (s_ret - h_ret) if s_ret is not None and h_ret is not None else None
        note = ""
        if s_ret is None and h_ret is None:
            note = "无数据"
        windows.append(
            WfaWindowResult(
                label=label,
                start=w_start,
                end=w_end,
                strategy_return_pct=s_ret,
                hold_return_pct=h_ret,
                return_edge_pct=edge,
                strategy_trades=_count_trades(strat_trades, w_start, w_end),
                note=note,
            )
        )

    active = [w for w in windows if w.strategy_return_pct is not None]
    edges = [w.return_edge_pct for w in active if w.return_edge_pct is not None]
    wins = sum(1 for w in active if w.return_edge_pct is not None and w.return_edge_pct > 0)
    summary = WfaSummary(
        windows=len(windows),
        windows_active=len(active),
        strategy_wins=wins,
        mean_edge_pct=(sum(edges) / len(edges)) if edges else None,
        stitched_strategy_pct=_stitched_return(active, "strategy_return_pct"),
        stitched_hold_pct=_stitched_return(active, "hold_return_pct"),
        full_strategy_pct=strat_meta.get("total_return_pct"),
        full_hold_pct=hold_meta.get("total_return_pct"),
    )
    meta = {
        "start": start,
        "end": end,
        "freq": freq,
        "top_n": top_n,
        "sell_rank": sell_rank,
        "initial_capital": initial_capital,
        "rebalance_days": rebalance_days,
        "strategy_params": sp.to_dict(),
        "strategy_params_summary": sp.summary(),
    }
    return windows, summary, meta, strat_nav, hold_nav


def _permute_rebalance_dates(
    calendar: list[pd.Timestamp],
    reb_dates: list[pd.Timestamp],
    rng: np.random.Generator,
) -> list[pd.Timestamp]:
    n = len(reb_dates)
    if n <= 1 or len(calendar) < n:
        return reb_dates
    chosen = sorted(rng.choice(calendar, size=n, replace=False).tolist())
    if calendar and chosen[-1] != calendar[-1]:
        chosen[-1] = calendar[-1]
    return chosen


def run_monte_carlo_rebalance(
    *,
    start: str,
    end: str | None,
    top_n: int = TOP_N_BUY,
    sell_rank: int | None = None,
    rebalance_days: int = BACKTEST_REBALANCE_DAYS,
    initial_capital: float = BACKTEST_INITIAL_CAPITAL,
    prefetch_size: int = BACKTEST_PREFETCH_SIZE,
    permutations: int = DEFAULT_MC_PERMUTATIONS,
    seed: int = 42,
    verbose: bool = True,
    ctx: BacktestContext | None = None,
) -> tuple[MonteCarloResult, list[float], dict]:
    """置换调仓日：保持调仓次数，随机映射到交易日。"""
    sell_rank = resolve_sell_rank(top_n, sell_rank)
    end = end or pd.Timestamp.today().strftime("%Y-%m-%d")
    calendar = _trading_calendar(start, end)
    base_reb = _rebalance_dates(calendar, rebalance_days)

    if ctx is None:
        if verbose:
            print("预加载数据（仅一次）…")
        ctx = prepare_backtest_context(
            start, end, prefetch_size=prefetch_size, reb_dates=base_reb, verbose=verbose
        )
    elif verbose:
        print("复用已加载数据…")

    if verbose:
        print("运行真实策略…")
    _, _, _, _, actual_meta, _ = run_backtest(
        start=start,
        end=end,
        top_n=top_n,
        sell_rank=sell_rank,
        rebalance_days=rebalance_days,
        initial_capital=initial_capital,
        prefetch_size=prefetch_size,
        reb_dates_override=base_reb,
        ctx=ctx,
        record_details=False,
        verbose=False,
    )
    actual_ret = actual_meta.get("total_return_pct")

    rng = np.random.default_rng(seed)
    perm_returns: list[float] = []
    if verbose:
        print(f"蒙特卡洛置换调仓日 × {permutations}（复用缓存）…")
    for i in range(permutations):
        perm_dates = _permute_rebalance_dates(calendar, base_reb, rng)
        _, _, _, _, meta, _ = run_backtest(
            start=start,
            end=end,
            top_n=top_n,
            sell_rank=sell_rank,
            rebalance_days=rebalance_days,
            initial_capital=initial_capital,
            prefetch_size=prefetch_size,
            reb_dates_override=perm_dates,
            ctx=ctx,
            record_details=False,
            verbose=False,
        )
        r = meta.get("total_return_pct")
        if r is not None:
            perm_returns.append(float(r))
        if verbose and (i + 1) % 50 == 0:
            print(f"  {i + 1}/{permutations}")

    return _summarize_mc(actual_ret, actual_meta.get("cagr_pct"), perm_returns, permutations, "rebalance"), perm_returns, {
        "start": start,
        "end": end,
        "permutations": permutations,
        "method": "rebalance",
    }


def run_monte_carlo_bootstrap(
    *,
    start: str,
    end: str | None,
    top_n: int = TOP_N_BUY,
    sell_rank: int | None = None,
    rebalance_days: int = BACKTEST_REBALANCE_DAYS,
    initial_capital: float = BACKTEST_INITIAL_CAPITAL,
    prefetch_size: int = BACKTEST_PREFETCH_SIZE,
    permutations: int = DEFAULT_MC_PERMUTATIONS,
    seed: int = 42,
    verbose: bool = True,
    ctx: BacktestContext | None = None,
    strategy_params: StrategyParams | None = None,
) -> tuple[MonteCarloResult, list[float], dict]:
    """对调仓期收益率自助抽样，估计收益分布。"""
    top_n, sell_rank, rebalance_days, sp = _resolve_run_args(
        top_n, sell_rank, rebalance_days, strategy_params
    )
    end = end or pd.Timestamp.today().strftime("%Y-%m-%d")

    if ctx is None:
        if verbose:
            print("预加载并运行真实策略…")
        ctx = prepare_backtest_context(
            start, end, prefetch_size=prefetch_size, rebalance_days=rebalance_days, verbose=verbose
        )
    elif verbose:
        print("复用已加载数据…")
    nav_df, _, _, _, actual_meta, _ = run_backtest(
        start=start,
        end=end,
        top_n=top_n,
        sell_rank=sell_rank,
        rebalance_days=rebalance_days,
        initial_capital=initial_capital,
        prefetch_size=prefetch_size,
        ctx=ctx,
        record_details=False,
        verbose=False,
        strategy_params=sp,
    )
    actual_ret = actual_meta.get("total_return_pct")
    rets = nav_df["nav"].pct_change().dropna().values
    if len(rets) < 2:
        return _summarize_mc(actual_ret, actual_meta.get("cagr_pct"), [], permutations, "bootstrap"), [], {
            "start": start,
            "end": end,
            "permutations": permutations,
            "method": "bootstrap",
            "strategy_params": sp.to_dict(),
            "strategy_params_summary": sp.summary(),
        }

    rng = np.random.default_rng(seed)
    perm_returns: list[float] = []
    if verbose:
        print(f"蒙特卡洛自助抽样 × {permutations}…")
    for _ in range(permutations):
        sampled = rng.choice(rets, size=len(rets), replace=True)
        terminal = initial_capital * float(np.prod(1 + sampled))
        perm_returns.append((terminal / initial_capital - 1) * 100)

    return _summarize_mc(actual_ret, actual_meta.get("cagr_pct"), perm_returns, permutations, "bootstrap"), perm_returns, {
        "start": start,
        "end": end,
        "permutations": permutations,
        "method": "bootstrap",
        "strategy_params": sp.to_dict(),
        "strategy_params_summary": sp.summary(),
    }


def _summarize_mc(
    actual: float | None,
    actual_cagr: float | None,
    perm_returns: list[float],
    n_perm: int,
    method: str,
) -> MonteCarloResult:
    if not perm_returns or actual is None:
        return MonteCarloResult(
            actual_return_pct=actual,
            actual_cagr_pct=actual_cagr,
            perm_mean_pct=None,
            perm_std_pct=None,
            perm_median_pct=None,
            perm_min_pct=None,
            perm_max_pct=None,
            percentile=None,
            p_value=None,
            significant_5pct=None,
            permutations=n_perm,
            method=method,
        )
    arr = np.array(perm_returns)
    below = int(np.sum(arr <= actual))
    percentile = below / len(arr) * 100
    p_value = float(np.mean(arr >= actual))
    return MonteCarloResult(
        actual_return_pct=actual,
        actual_cagr_pct=actual_cagr,
        perm_mean_pct=float(arr.mean()),
        perm_std_pct=float(arr.std()),
        perm_median_pct=float(np.median(arr)),
        perm_min_pct=float(arr.min()),
        perm_max_pct=float(arr.max()),
        percentile=percentile,
        p_value=p_value,
        significant_5pct=p_value < 0.05,
        permutations=n_perm,
        method=method,
    )


def _fmt_pct(v: float | None, digits: int = 2) -> str:
    if v is None:
        return "—"
    return f"{v:+.{digits}f}%"


def format_benchmark_markdown(
    windows: list[BenchmarkWindowResult],
    summary: BenchmarkSummary,
    meta: dict,
) -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    n = summary.windows_active
    win_rate = (
        f"{summary.strategy_wins_vs_index}/{n}（{summary.strategy_wins_vs_index / n * 100:.0f}%）"
        if n
        else "—"
    )
    idx_label = f"{meta['index_name']}（{meta['index_code']}→{meta['index_tr_code']} 全收益）"
    lines = [
        "# 指数对比 — 红利低波轮动 vs H30269",
        "",
        f"> 生成时间：{now}  ",
        f"> 区间：{meta['start']} ~ {meta['end']}  ",
        f"> 基准：**{idx_label}**（含分红再投资）  ",
        f"> 策略：持仓 {meta['top_n']} 只，跌出前 {meta['sell_rank']} 卖（含分红个税）",
    ]
    if meta.get("strategy_params_summary"):
        lines.append(f"> 参数：**{meta['strategy_params_summary']}**")
    lines.extend([
        "",
        "## 汇总",
        "",
        f"- 有效窗口：**{n}** / {summary.windows}",
        f"- 轮动胜率（收益高于指数）：**{win_rate}**",
        f"- 平均超额（轮动−指数）：**{_fmt_pct(summary.mean_edge_vs_index_pct)}**",
        "",
        "| 口径 | 总收益 | 年化 |",
        "|------|--------|------|",
        f"| 轮动策略 | {_fmt_pct(summary.full_strategy_pct)} | {_fmt_pct(summary.strategy_cagr_pct)} |",
        f"| {meta['index_name']} | {_fmt_pct(summary.full_index_pct)} | {_fmt_pct(summary.index_cagr_pct)} |",
        f"| 选股买入持有 | {_fmt_pct(summary.full_hold_pct)} | — |",
        "",
        f"- 轮动 vs 指数超额：**{_fmt_pct((summary.full_strategy_pct or 0) - (summary.full_index_pct or 0))}**",
        f"- 拼接 OOS：轮动 {_fmt_pct(summary.stitched_strategy_pct)} · 指数 {_fmt_pct(summary.stitched_index_pct)}",
        "",
        "## 分窗口",
        "",
        "| 窗口 | 区间 | 轮动 | 指数 | 买入持有 | 超额(轮动−指数) | 备注 |",
        "|------|------|------|------|----------|-----------------|------|",
    ])
    for w in windows:
        win = ""
        if w.strategy_edge_pct is not None:
            win = "轮动更优" if w.strategy_edge_pct > 0 else "指数更优"
        lines.append(
            f"| {w.label} | {w.start}~{w.end} | {_fmt_pct(w.strategy_return_pct)} | "
            f"{_fmt_pct(w.index_return_pct)} | {_fmt_pct(w.hold_return_pct)} | "
            f"{_fmt_pct(w.strategy_edge_pct)} | {w.note or win} |"
        )
    lines.extend(["", "图表见 `benchmark.html`。", ""])
    return "\n".join(lines)


def render_benchmark_html(
    windows: list[BenchmarkWindowResult],
    summary: BenchmarkSummary,
    meta: dict,
    strat_nav: pd.DataFrame,
    index_nav: pd.DataFrame,
) -> str:
    labels = [w.label for w in windows]
    strat = [w.strategy_return_pct for w in windows]
    index_ret = [w.index_return_pct for w in windows]
    hold = [w.hold_return_pct for w in windows]
    nav_dates = strat_nav["date"].astype(str).tolist() if not strat_nav.empty else []
    strat_vals = [float(v) for v in strat_nav["nav"].tolist()] if not strat_nav.empty else []
    index_vals = []
    if not index_nav.empty and nav_dates:
        idx = index_nav.copy()
        idx["date"] = idx["date"].astype(str)
        index_vals = [
            float(idx.loc[idx["date"] == d, "nav"].iloc[0]) if d in idx["date"].values else None
            for d in nav_dates
        ]
    data = json.dumps(
        {
            "labels": labels,
            "strat": strat,
            "index": index_ret,
            "hold": hold,
            "meta": meta,
            "summary": asdict(summary),
            "nav": {"dates": nav_dates, "strat": strat_vals, "index": index_vals},
        },
        ensure_ascii=False,
    )
    edge = (summary.full_strategy_pct or 0) - (summary.full_index_pct or 0)
    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8"><title>指数对比 H30269</title>
<script src="https://cdn.jsdelivr.net/npm/echarts@5/dist/echarts.min.js"></script>
<style>
body{{font-family:sans-serif;margin:20px;background:#f5f6f8}}
.chart{{height:400px;background:#fff;border-radius:8px;padding:12px;margin-top:16px}}
</style></head><body>
<h1>轮动策略 vs {escape(meta['index_name'])}（{escape(meta['index_tr_code'])}）</h1>
<p>{escape(meta['start'])} ~ {escape(meta['end'])} · 轮动 {_fmt_pct(summary.full_strategy_pct)} · 指数 {_fmt_pct(summary.full_index_pct)} · 超额 {_fmt_pct(edge)}</p>
<div id="navChart" class="chart"></div>
<div id="barChart" class="chart"></div>
<script>
const D={data};
const navC=echarts.init(document.getElementById('navChart'));
navC.setOption({{
  title:{{text:'净值曲线',left:'center',textStyle:{{fontSize:14}}}},
  tooltip:{{trigger:'axis'}},
  legend:{{data:['轮动策略','指数全收益']}},
  xAxis:{{type:'category',data:D.nav.dates}},
  yAxis:{{type:'value',scale:true,name:'净值(元)'}},
  series:[
    {{type:'line',name:'轮动策略',data:D.nav.strat,smooth:true,lineStyle:{{width:2}},itemStyle:{{color:'#1677ff'}}}},
    {{type:'line',name:'指数全收益',data:D.nav.index,smooth:true,lineStyle:{{width:2}},itemStyle:{{color:'#fa8c16'}}}}
  ]
}});
const barC=echarts.init(document.getElementById('barChart'));
barC.setOption({{
  title:{{text:'分窗口收益率',left:'center',textStyle:{{fontSize:14}}}},
  tooltip:{{trigger:'axis'}},
  legend:{{data:['轮动','指数','买入持有']}},
  xAxis:{{type:'category',data:D.labels}},
  yAxis:{{type:'value',name:'收益率%'}},
  series:[
    {{type:'bar',name:'轮动',data:D.strat,itemStyle:{{color:'#1677ff'}}}},
    {{type:'bar',name:'指数',data:D.index,itemStyle:{{color:'#fa8c16'}}}},
    {{type:'bar',name:'买入持有',data:D.hold,itemStyle:{{color:'#91caff'}}}}
  ]
}});
</script></body></html>"""


def save_benchmark_outputs(
    out_dir: Path,
    windows: list[BenchmarkWindowResult],
    summary: BenchmarkSummary,
    meta: dict,
    strat_nav: pd.DataFrame,
    index_nav: pd.DataFrame,
    *,
    stem: str = "benchmark",
) -> dict[str, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = {"md": out_dir / f"{stem}.md", "html": out_dir / f"{stem}.html"}
    paths["md"].write_text(format_benchmark_markdown(windows, summary, meta), encoding="utf-8")
    paths["html"].write_text(
        render_benchmark_html(windows, summary, meta, strat_nav, index_nav), encoding="utf-8"
    )
    return paths


def format_wfa_markdown(windows: list[WfaWindowResult], summary: WfaSummary, meta: dict) -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    n = summary.windows_active
    win_rate = f"{summary.strategy_wins}/{n}（{summary.strategy_wins / n * 100:.0f}%）" if n else "—"
    lines = [
        "# 走步前向分析（WFA）— 红利低波轮动",
        "",
        f"> 生成时间：{now}  ",
        f"> 区间：{meta['start']} ~ {meta['end']}  ",
        f"> 切分：按 **{meta['freq']}**  ",
        f"> 持仓 {meta['top_n']} 只，跌出前 {meta['sell_rank']} 卖",
    ]
    if meta.get("strategy_params_summary"):
        lines.append(f"> 参数：**{meta['strategy_params_summary']}**")
    lines.extend([
        "",
        "## 方法",
        "",
        "策略参数固定，不做样本内优化。先连续运行全区间回测，再在各 OOS 窗口切片统计收益：",
        "",
        "- **轮动策略**：缓冲带智能调仓",
        "- **买入持有**：期初建仓后不再调仓（对照）",
        "",
        "## 汇总",
        "",
        f"- 有效窗口：**{n}** / {summary.windows}",
        f"- 轮动胜率（收益高于持有）：**{win_rate}**",
        f"- 平均利差（轮动−持有）：**{_fmt_pct(summary.mean_edge_pct)}**",
        f"- 拼接 OOS 收益：轮动 **{_fmt_pct(summary.stitched_strategy_pct)}**，持有 **{_fmt_pct(summary.stitched_hold_pct)}**",
        f"- 全区间：轮动 **{_fmt_pct(summary.full_strategy_pct)}**，持有 **{_fmt_pct(summary.full_hold_pct)}**",
        "",
        "## 分窗口",
        "",
        "| 窗口 | 区间 | 轮动收益 | 持有收益 | 利差 | 成交笔数 | 备注 |",
        "|------|------|----------|----------|------|----------|------|",
    ])
    for w in windows:
        win = "轮动更优" if w.return_edge_pct and w.return_edge_pct > 0 else (
            "持有更优" if w.return_edge_pct and w.return_edge_pct < 0 else ""
        )
        lines.append(
            f"| {w.label} | {w.start}~{w.end} | {_fmt_pct(w.strategy_return_pct)} | "
            f"{_fmt_pct(w.hold_return_pct)} | {_fmt_pct(w.return_edge_pct)} | "
            f"{w.strategy_trades} | {w.note or win} |"
        )
    lines.extend(["", "图表见 `wfa.html`。", ""])
    return "\n".join(lines)


def format_mc_markdown(mc: MonteCarloResult, meta: dict) -> str:
    method = "调仓日置换" if mc.method == "rebalance" else "收益自助抽样"
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    sig = "是" if mc.significant_5pct else ("否" if mc.significant_5pct is not None else "—")
    return "\n".join([
        f"# 蒙特卡洛检验 — 红利低波轮动（{method}）",
        "",
        f"> 生成时间：{now}  ",
        f"> 区间：{meta['start']} ~ {meta['end']}  ",
        f"> 置换/抽样次数：**{mc.permutations}**",
        "",
        "## 结果",
        "",
        f"- 真实总收益：**{_fmt_pct(mc.actual_return_pct)}**（年化 {_fmt_pct(mc.actual_cagr_pct)}）",
        f"- 随机分布均值：**{_fmt_pct(mc.perm_mean_pct)}**（标准差 {_fmt_pct(mc.perm_std_pct)}）",
        f"- 随机分布中位数：**{_fmt_pct(mc.perm_median_pct)}**",
        f"- 随机范围：**{_fmt_pct(mc.perm_min_pct)}** ~ **{_fmt_pct(mc.perm_max_pct)}**",
        f"- 真实收益百分位：**{mc.percentile:.1f}%**" if mc.percentile is not None else "- 真实收益百分位：—",
        f"- p 值（随机 ≥ 真实）：**{mc.p_value:.4f}**" if mc.p_value is not None else "- p 值：—",
        f"- 5% 显著优于运气：**{sig}**",
        "",
        "若 p < 0.05，说明策略收益显著高于随机调仓/抽样，不太像纯运气。",
        "",
        "图表见 `monte_carlo.html`。",
        "",
    ])


def render_wfa_html(windows: list[WfaWindowResult], summary: WfaSummary, meta: dict) -> str:
    labels = [w.label for w in windows]
    strat = [w.strategy_return_pct for w in windows]
    hold = [w.hold_return_pct for w in windows]
    data = json.dumps({"labels": labels, "strat": strat, "hold": hold, "meta": meta, "summary": asdict(summary)}, ensure_ascii=False)
    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8"><title>WFA 红利低波</title>
<script src="https://cdn.jsdelivr.net/npm/echarts@5/dist/echarts.min.js"></script>
<style>body{{font-family:sans-serif;margin:20px;background:#f5f6f8}} .chart{{height:420px;background:#fff;border-radius:8px;padding:12px;margin-top:16px}}</style>
</head><body>
<h1>走步前向分析 — 红利低波轮动</h1>
<p>{escape(meta['start'])} ~ {escape(meta['end'])} · 轮动全区间 {_fmt_pct(summary.full_strategy_pct)} · 持有 {_fmt_pct(summary.full_hold_pct)}</p>
<div id="chart" class="chart"></div>
<script>
const D={data};
const c=echarts.init(document.getElementById('chart'));
c.setOption({{
  tooltip:{{trigger:'axis'}},
  legend:{{data:['轮动','买入持有']}},
  xAxis:{{type:'category',data:D.labels}},
  yAxis:{{type:'value',name:'收益率%'}},
  series:[
    {{type:'bar',name:'轮动',data:D.strat,itemStyle:{{color:'#1677ff'}}}},
    {{type:'bar',name:'买入持有',data:D.hold,itemStyle:{{color:'#91caff'}}}}
  ]
}});
</script></body></html>"""


def render_mc_html(mc: MonteCarloResult, perm_returns: list[float], meta: dict) -> str:
    title = "调仓日置换" if mc.method == "rebalance" else "收益自助抽样"
    p_str = f"{mc.p_value:.4f}" if mc.p_value is not None else "—"
    pct_str = f"{mc.percentile:.1f}%" if mc.percentile is not None else "—"
    data = json.dumps({
        "perm": perm_returns,
        "actual": mc.actual_return_pct,
        "meta": meta,
        "mc": asdict(mc),
    }, ensure_ascii=False)
    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8"><title>蒙特卡洛 — 红利低波</title>
<script src="https://cdn.jsdelivr.net/npm/echarts@5/dist/echarts.min.js"></script>
<style>body{{font-family:sans-serif;margin:20px;background:#f5f6f8}} .chart{{height:420px;background:#fff;border-radius:8px;padding:12px;margin-top:16px}}</style>
</head><body>
<h1>蒙特卡洛检验 — {escape(title)}</h1>
<p>真实收益 {_fmt_pct(mc.actual_return_pct)} · p={p_str} · 百分位 {pct_str}</p>
<div id="chart" class="chart"></div>
<script>
const D={data};
const c=echarts.init(document.getElementById('chart'));
const bins=30;
const arr=D.perm.filter(x=>x!=null);
const min=Math.min(...arr), max=Math.max(...arr);
const step=(max-min)/bins||1;
const hist=new Array(bins).fill(0);
arr.forEach(v=>{{const i=Math.min(bins-1,Math.floor((v-min)/step));hist[i]++}});
const labels=hist.map((_,i)=>(min+i*step).toFixed(1)+'%');
c.setOption({{
  tooltip:{{}},
  xAxis:{{type:'category',data:labels,axisLabel:{{interval:Math.floor(bins/8)}}}},
  yAxis:{{type:'value',name:'频次'}},
  series:[{{type:'bar',data:hist,itemStyle:{{color:'#91caff'}}}}],
  markLine:{{data:[{{xAxis:labels[Math.min(bins-1,Math.max(0,Math.floor((D.actual-min)/step)))],name:'真实',lineStyle:{{color:'#cf1322',width:2}}}}]}}
}});
</script></body></html>"""


def save_wfa_outputs(
    out_dir: Path, windows, summary, meta, *, stem: str = "wfa"
) -> dict[str, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = {"md": out_dir / f"{stem}.md", "html": out_dir / f"{stem}.html"}
    paths["md"].write_text(format_wfa_markdown(windows, summary, meta), encoding="utf-8")
    paths["html"].write_text(render_wfa_html(windows, summary, meta), encoding="utf-8")
    return paths


def save_mc_outputs(
    out_dir: Path,
    mc: MonteCarloResult,
    perm_returns: list[float],
    meta: dict,
    *,
    stem: str = "monte_carlo",
) -> dict[str, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = {"md": out_dir / f"{stem}.md", "html": out_dir / f"{stem}.html"}
    paths["md"].write_text(format_mc_markdown(mc, meta), encoding="utf-8")
    paths["html"].write_text(render_mc_html(mc, perm_returns, meta), encoding="utf-8")
    return paths


def main(argv: list[str] | None = None) -> int:
    configure_stdout_utf8()
    parser = argparse.ArgumentParser(description="红利低波 WFA + 蒙特卡洛检验")
    parser.add_argument("--wfa", action="store_true", help="运行走步前向分析")
    parser.add_argument("--monte-carlo", action="store_true", help="运行蒙特卡洛")
    parser.add_argument("--mc-method", choices=["rebalance", "bootstrap", "both"], default="bootstrap")
    parser.add_argument("--permutations", type=int, default=DEFAULT_MC_PERMUTATIONS)
    parser.add_argument("--freq", choices=["year", "half", "quarter"], default=DEFAULT_WFA_FREQ)
    parser.add_argument("--start", default=None)
    parser.add_argument("--years", type=int, default=BACKTEST_YEARS)
    parser.add_argument("--end", default=None)
    parser.add_argument("--top", type=int, default=TOP_N_BUY)
    parser.add_argument("--sell-rank", type=int, default=None)
    parser.add_argument("--rebalance-days", type=int, default=BACKTEST_REBALANCE_DAYS)
    parser.add_argument("--capital", type=float, default=BACKTEST_INITIAL_CAPITAL)
    parser.add_argument("--prefetch", type=int, default=BACKTEST_PREFETCH_SIZE)
    parser.add_argument("--fast", action="store_true", help="快速模式：prefetch=80，MC 100 次")
    parser.add_argument("--benchmark", default=None, metavar="CODE", help="对比指数（如 H30269）")
    parser.add_argument(
        "--optimized",
        action="store_true",
        help="使用 optimize.json 中的最优参数",
    )
    parser.add_argument(
        "--params-json",
        default=None,
        help="自定义参数 JSON（optimize.json 格式）",
    )
    parser.add_argument("-o", "--output-dir", default=None)
    args = parser.parse_args(argv)

    if args.fast:
        if args.prefetch == BACKTEST_PREFETCH_SIZE:
            args.prefetch = 80
        if args.permutations == DEFAULT_MC_PERMUTATIONS:
            args.permutations = FAST_MC_PERMUTATIONS

    if not args.wfa and not args.monte_carlo and not args.benchmark:
        args.wfa = True
        args.monte_carlo = True

    start = args.start or default_start_years(args.years)
    out_dir = Path(args.output_dir) if args.output_dir else BACKTEST_OUTPUT_DIR
    output_stem = ""

    strategy_params: StrategyParams | None = None
    if args.optimized or args.params_json:
        strategy_params = load_optimal_params(args.params_json)
        output_stem = "optimized_"
        print(f"已加载最优参数：{strategy_params.summary()}")

    top_n = strategy_params.resolved_top_n(args.top) if strategy_params else args.top
    rebalance_days = (
        strategy_params.resolved_rebalance_days(args.rebalance_days)
        if strategy_params
        else args.rebalance_days
    )
    sell_rank = (
        strategy_params.resolved_sell_rank(top_n)
        if strategy_params and args.sell_rank is None
        else resolve_sell_rank(top_n, args.sell_rank)
    )
    t0 = time.time()

    shared_ctx = None
    need_ctx = args.wfa or args.monte_carlo or args.benchmark
    if need_ctx:
        print("预加载数据…")
        shared_ctx = prepare_backtest_context(
            start,
            args.end,
            prefetch_size=args.prefetch,
            rebalance_days=rebalance_days,
            verbose=True,
        )

    if args.wfa:
        print(f"WFA {start} ~ {args.end or '今'}（{args.freq}）…")
        windows, summary, meta, _, _ = run_wfa(
            start=start,
            end=args.end,
            top_n=top_n,
            sell_rank=sell_rank,
            rebalance_days=rebalance_days,
            initial_capital=args.capital,
            prefetch_size=args.prefetch,
            freq=args.freq,
            ctx=shared_ctx,
            strategy_params=strategy_params,
        )
        paths = save_wfa_outputs(out_dir, windows, summary, meta, stem=f"{output_stem}wfa")
        print(format_wfa_markdown(windows, summary, meta))
        print(f"已写入 {paths['md']} / {paths['html']}")

    if args.monte_carlo:
        methods = ["rebalance", "bootstrap"] if args.mc_method == "both" else [args.mc_method]
        for method in methods:
            print(f"\n蒙特卡洛（{method}）…")
            if method == "rebalance":
                mc, perm, meta = run_monte_carlo_rebalance(
                    start=start,
                    end=args.end,
                    top_n=args.top,
                    sell_rank=sell_rank,
                    rebalance_days=args.rebalance_days,
                    initial_capital=args.capital,
                    prefetch_size=args.prefetch,
                    permutations=args.permutations,
                    ctx=shared_ctx,
                )
            else:
                mc, perm, meta = run_monte_carlo_bootstrap(
                    start=start,
                    end=args.end,
                    top_n=top_n,
                    sell_rank=sell_rank,
                    rebalance_days=rebalance_days,
                    initial_capital=args.capital,
                    prefetch_size=args.prefetch,
                    permutations=args.permutations,
                    ctx=shared_ctx,
                    strategy_params=strategy_params,
                )
            stem = f"{output_stem}monte_carlo" if len(methods) == 1 else f"{output_stem}monte_carlo_{method}"
            paths = save_mc_outputs(out_dir, mc, perm, meta, stem=stem)
            print(format_mc_markdown(mc, meta))
            print(f"已写入 {paths['md']} / {paths['html']}")

    if args.benchmark:
        print(f"\n指数对比 {args.benchmark} {start} ~ {args.end or '今'}…")
        b_windows, b_summary, b_meta, strat_nav, index_nav, _ = run_benchmark_compare(
            index_code=args.benchmark,
            start=start,
            end=args.end,
            top_n=top_n,
            sell_rank=sell_rank,
            rebalance_days=rebalance_days,
            initial_capital=args.capital,
            prefetch_size=args.prefetch,
            freq=args.freq,
            ctx=shared_ctx,
            verbose=True,
            strategy_params=strategy_params,
        )
        b_paths = save_benchmark_outputs(
            out_dir, b_windows, b_summary, b_meta, strat_nav, index_nav, stem=f"{output_stem}benchmark"
        )
        print(format_benchmark_markdown(b_windows, b_summary, b_meta))
        print(f"已写入 {b_paths['md']} / {b_paths['html']}")

    print(f"\n总耗时 {time.time() - t0:.0f} 秒")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

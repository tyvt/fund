"""蒙特卡洛置换检验：随机打乱买入日，判断回测收益是否显著优于运气。

原理
----
保持与真实策略相同的买入次数与每次买入金额，将买入日随机映射到区间内
任意交易日（不放回抽样），重复 N 次（默认 200），得到「纯运气」下的
收益率分布。若真实收益率显著高于该分布，说明择时可能并非偶然。

模式
----
- buy：仅买入持有（与 inception_present.md 一致）
- trade：完整波段策略，打乱买入日后重跑止盈/卖点（与 trade_inception_present.md 一致）
- rotation：智能轮动组合（共享资金池 + 轮动门控，与 trade_inception_present.md 一致）

用法
----
    python monte_carlo_permutation.py
    python monte_carlo_permutation.py --mode trade
    python monte_carlo_permutation.py --mode rotation
    python monte_carlo_permutation.py --mode all
    python backtest_wfa.py
    python monte_carlo_permutation.py --mode trade --index 000852 --permutations 500
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass
from datetime import datetime

import numpy as np
import pandas as pd

from backtest_buy_signals import (
    BacktestRange,
    EXCLUDED_RANKING_NOTE,
    _attach_dt,
    _cooldown_amount_scale,
    _filter_by_range,
    _index_simulate_amount,
    _iter_backtest_configs,
    _resolve_buy_mask,
    _resolve_row_buy_amount,
    default_backtest_range,
    format_backtest_range_label,
    get_panels,
    is_ranking_excluded,
)
from config import BACKTEST_OUTPUT_DIR, get_backtest_buy_amount, resolve_backtest_amounts
from market_data import configure_stdout_utf8

DEFAULT_PERMUTATIONS = 200
DEFAULT_OUTPUT_STEM = "monte_carlo_permutation"
TRADE_OUTPUT_STEM = "monte_carlo_trade_permutation"
ROTATION_OUTPUT_STEM = "monte_carlo_rotation_permutation"
MODE_LABELS = {
    "buy": "仅买入持有",
    "trade": "波段买卖（含止盈/卖点）",
    "rotation": "智能轮动组合（共享资金池）",
}


@dataclass
class PermutationResult:
    code: str
    name: str
    buy_days: int
    total_days: int
    invested: float
    actual_return_pct: float | None
    perm_mean: float | None
    perm_std: float | None
    perm_min: float | None
    perm_max: float | None
    perm_median: float | None
    percentile: float | None
    p_value: float | None
    significant_5pct: bool | None
    permutations: int
    note: str = ""


def _resolve_val_col(cfg) -> str:
    panel = cfg["panel"]
    price_col = cfg.get("price_col", "close")
    if cfg.get("valuation_price_col"):
        return cfg["valuation_price_col"]
    if panel is not None and "total_return_close" in panel.columns:
        return "total_return_close"
    return price_col


def _resolve_date_range(cfg=None, date_range: BacktestRange | None = None) -> BacktestRange:
    if date_range is not None:
        return date_range
    if cfg is None:
        return default_backtest_range()
    return BacktestRange(
        start=cfg.get("start_date"),
        end=cfg.get("end_date"),
        label=f"{cfg.get('start_date')}_{cfg.get('end_date') or 'present'}",
    )


def _filter_sample(panel, cfg, date_range: BacktestRange | None = None):
    date_col = cfg.get("date_col", "date")
    price_col = cfg.get("price_col", "close")
    val_col = _resolve_val_col(cfg)

    if panel is None or panel.empty:
        return None, val_col

    priced = _attach_dt(panel, date_col).dropna(subset=[price_col, val_col])
    if priced.empty:
        return None, val_col

    dr = _resolve_date_range(cfg, date_range)
    if cfg.get("start_date") is not None or cfg.get("end_date") is not None:
        from backtest_trade_signals import _filter_panel

        sample = _filter_panel(
            priced,
            cfg.get("start_date"),
            cfg.get("end_date"),
            date_col=date_col,
        ).sort_values("_dt")
    else:
        sample = _filter_by_range(priced, dr, date_col).sort_values("_dt")

    if sample.empty:
        return None, val_col
    return sample, val_col


def _resolve_buy_amounts(buy_sample, sim_amt, scale: float) -> list[float]:
    if callable(sim_amt):
        return [
            _resolve_row_buy_amount(sim_amt, row, True) * scale
            for _, row in buy_sample.iterrows()
        ]
    base = float(sim_amt) * scale
    return [base] * len(buy_sample)


def _compute_return_pct(
    val_prices: np.ndarray,
    buy_indices: np.ndarray,
    buy_amounts: np.ndarray,
    latest_price: float,
) -> float | None:
    if len(buy_indices) == 0:
        return None
    prices_at_buy = val_prices[buy_indices]
    if np.any(prices_at_buy <= 0):
        return None
    units = np.sum(buy_amounts / prices_at_buy)
    invested = float(np.sum(buy_amounts))
    if invested <= 0:
        return None
    market_value = units * latest_price
    return (market_value - invested) / invested * 100


def _trade_return_pct(stats: dict | None, has_sell: bool) -> float | None:
    if not stats:
        return None
    if has_sell:
        return stats.get("return_pct")
    return stats.get("buy_only_return_pct")


def _make_permuted_buy_fns(sample, buy_amounts: np.ndarray, perm_indices: np.ndarray):
    sorted_perm = sorted(int(i) for i in perm_indices)
    amount_by_date: dict[str, float] = {}
    perm_dates: set[str] = set()
    for pos, day_idx in enumerate(sorted_perm):
        day = pd.Timestamp(sample.iloc[day_idx]["_dt"]).strftime("%Y-%m-%d")
        perm_dates.add(day)
        amount_by_date[day] = float(buy_amounts[pos])

    def buy_fn(row):
        dt = row.get("_dt")
        if dt is None:
            dt = pd.Timestamp(row.get("date"))
        return pd.Timestamp(dt).strftime("%Y-%m-%d") in perm_dates

    def amount_fn(row):
        dt = row.get("_dt")
        if dt is None:
            dt = pd.Timestamp(row.get("date"))
        return amount_by_date.get(pd.Timestamp(dt).strftime("%Y-%m-%d"), 0.0)

    return buy_fn, amount_fn


def _simulate_trade_stats(cfg, buy_fn, amount, *, apply_cooldown: bool):
    from backtest_trade_signals import _run_index_trades, simulate_trades
    from sell_trailing import simulate_trades_trailing

    code = cfg["code"]
    if apply_cooldown:
        return _run_index_trades(
            cfg["panel"],
            code,
            cfg["start_date"],
            cfg["end_date"],
            amount,
            buy_fn,
            cfg["sell_fn"],
            cfg["has_sell"],
            trailing_cfg=cfg.get("trailing_cfg"),
            valuation_sell_fn=cfg.get("valuation_sell_fn"),
            date_col=cfg.get("date_col", "date"),
            valuation_price_col=cfg.get("valuation_price_col"),
        )

    if cfg["has_sell"] and cfg.get("trailing_cfg") is not None:
        return simulate_trades_trailing(
            cfg["panel"],
            cfg["start_date"],
            cfg["end_date"],
            amount=amount,
            date_col=cfg.get("date_col", "date"),
            buy_fn=buy_fn,
            valuation_sell_fn=cfg.get("valuation_sell_fn"),
            trailing_cfg=cfg.get("trailing_cfg"),
            valuation_price_col=cfg.get("valuation_price_col"),
            index_code=None,
        )
    return simulate_trades(
        cfg["panel"],
        cfg["start_date"],
        cfg["end_date"],
        amount=amount,
        buy_fn=buy_fn,
        sell_fn=cfg["sell_fn"],
        has_sell=cfg["has_sell"],
        date_col=cfg.get("date_col", "date"),
        valuation_price_col=cfg.get("valuation_price_col"),
        index_code=None,
    )


def _extract_buy_plan(panel, cfg, amounts, date_range: BacktestRange | None = None):
    """提取真实策略的买入计划：估值价、买入下标、每次金额。"""
    code = cfg["code"]
    buy_fn = cfg["buy_fn"]
    buy_mask_fn = cfg.get("buy_mask_fn")

    sample, val_col = _filter_sample(panel, cfg, date_range)
    if sample is None:
        return None

    buy_mask = _resolve_buy_mask(
        panel, sample, buy_fn, buy_mask_fn, index_code=code
    )
    buy_sample = sample.loc[buy_mask]
    if buy_sample.empty:
        return None

    dr = _resolve_date_range(cfg, date_range)
    date_col = cfg.get("date_col", "date")
    sim_amt = cfg.get("sim_amt")
    if sim_amt is None:
        sim_amt = _index_simulate_amount(code, amounts, panel, dr, buy_fn, date_col)
    if sim_amt == 0:
        return None

    scale = _cooldown_amount_scale(
        panel, sample, buy_fn=buy_fn, buy_mask_fn=buy_mask_fn, index_code=code
    )
    buy_amounts = np.asarray(_resolve_buy_amounts(buy_sample, sim_amt, scale), dtype=float)

    val_prices = sample[val_col].astype(float).to_numpy()
    latest_price = float(val_prices[-1])
    buy_indices = np.flatnonzero(buy_mask.to_numpy())
    invested = float(buy_amounts.sum())

    return {
        "code": code,
        "name": cfg["name"],
        "sample": sample,
        "val_prices": val_prices,
        "buy_indices": buy_indices,
        "buy_amounts": buy_amounts,
        "latest_price": latest_price,
        "buy_days": len(buy_indices),
        "total_days": len(sample),
        "invested": invested,
        "note": cfg.get("note", ""),
    }


def _run_buy_permutations(
    val_prices: np.ndarray,
    buy_indices: np.ndarray,
    buy_amounts: np.ndarray,
    latest_price: float,
    n_perm: int,
    rng: np.random.Generator,
) -> np.ndarray:
    n_days = len(val_prices)
    n_buys = len(buy_indices)
    if n_buys == 0 or n_days < n_buys:
        return np.array([], dtype=float)

    results = np.empty(n_perm, dtype=float)
    for i in range(n_perm):
        perm_idx = np.sort(rng.choice(n_days, size=n_buys, replace=False))
        ret = _compute_return_pct(val_prices, perm_idx, buy_amounts, latest_price)
        results[i] = np.nan if ret is None else ret
    return results[np.isfinite(results)]


def _run_trade_permutations(plan, cfg, n_perm: int, rng: np.random.Generator) -> np.ndarray:
    sample = plan["sample"]
    n_days = len(sample)
    n_buys = len(plan["buy_indices"])
    if n_buys == 0 or n_days < n_buys:
        return np.array([], dtype=float)

    results = np.empty(n_perm, dtype=float)
    for i in range(n_perm):
        perm_idx = np.sort(rng.choice(n_days, size=n_buys, replace=False))
        buy_fn, amount_fn = _make_permuted_buy_fns(sample, plan["buy_amounts"], perm_idx)
        stats = _simulate_trade_stats(cfg, buy_fn, amount_fn, apply_cooldown=False)
        ret = _trade_return_pct(stats, cfg["has_sell"])
        results[i] = np.nan if ret is None else ret
    return results[np.isfinite(results)]


def _percentile_in_distribution(actual: float, samples: np.ndarray) -> float:
    if samples.size == 0:
        return None
    below = int(np.sum(samples < actual))
    equal = int(np.sum(samples == actual))
    return (below + 0.5 * equal) / samples.size * 100


def _p_value_greater_or_equal(actual: float, samples: np.ndarray) -> float:
    if samples.size == 0:
        return None
    count = int(np.sum(samples >= actual))
    return (count + 1) / (samples.size + 1)


def _build_permutation_result(
    plan: dict,
    actual: float | None,
    perm_returns: np.ndarray,
    n_perm: int,
) -> PermutationResult:
    if actual is None or perm_returns.size == 0:
        return PermutationResult(
            code=plan["code"],
            name=plan["name"],
            buy_days=plan["buy_days"],
            total_days=plan["total_days"],
            invested=plan["invested"],
            actual_return_pct=actual,
            perm_mean=None,
            perm_std=None,
            perm_min=None,
            perm_max=None,
            perm_median=None,
            percentile=None,
            p_value=None,
            significant_5pct=None,
            permutations=n_perm,
            note=plan["note"],
        )

    p_value = _p_value_greater_or_equal(actual, perm_returns)
    return PermutationResult(
        code=plan["code"],
        name=plan["name"],
        buy_days=plan["buy_days"],
        total_days=plan["total_days"],
        invested=plan["invested"],
        actual_return_pct=actual,
        perm_mean=float(np.mean(perm_returns)),
        perm_std=float(np.std(perm_returns, ddof=1)) if perm_returns.size > 1 else 0.0,
        perm_min=float(np.min(perm_returns)),
        perm_max=float(np.max(perm_returns)),
        perm_median=float(np.median(perm_returns)),
        percentile=_percentile_in_distribution(actual, perm_returns),
        p_value=p_value,
        significant_5pct=p_value < 0.05 if p_value is not None else None,
        permutations=int(perm_returns.size),
        note=plan["note"],
    )


def _excluded_result(code, name, n_perm: int) -> PermutationResult:
    return PermutationResult(
        code=code,
        name=name,
        buy_days=0,
        total_days=0,
        invested=0.0,
        actual_return_pct=None,
        perm_mean=None,
        perm_std=None,
        perm_min=None,
        perm_max=None,
        perm_median=None,
        percentile=None,
        p_value=None,
        significant_5pct=None,
        permutations=n_perm,
        note=EXCLUDED_RANKING_NOTE,
    )


def _empty_buy_result(plan: dict, n_perm: int) -> PermutationResult:
    return PermutationResult(
        code=plan["code"],
        name=plan["name"],
        buy_days=0,
        total_days=plan.get("total_days", 0),
        invested=0.0,
        actual_return_pct=None,
        perm_mean=None,
        perm_std=None,
        perm_min=None,
        perm_max=None,
        perm_median=None,
        percentile=None,
        p_value=None,
        significant_5pct=None,
        permutations=n_perm,
        note="区间内无买入信号",
    )


def run_index_permutation(
    panel,
    date_range,
    cfg,
    amounts,
    n_perm: int,
    rng: np.random.Generator,
) -> PermutationResult | None:
    code = cfg["code"]
    if amounts and is_ranking_excluded(code, amounts):
        return _excluded_result(code, cfg["name"], n_perm)

    plan = _extract_buy_plan(panel, cfg, amounts, date_range)
    if plan is None:
        return None
    if plan["buy_days"] == 0:
        return _empty_buy_result(plan, n_perm)

    actual = _compute_return_pct(
        plan["val_prices"],
        plan["buy_indices"],
        plan["buy_amounts"],
        plan["latest_price"],
    )
    perm_returns = _run_buy_permutations(
        plan["val_prices"],
        plan["buy_indices"],
        plan["buy_amounts"],
        plan["latest_price"],
        n_perm,
        rng,
    )
    return _build_permutation_result(plan, actual, perm_returns, n_perm)


def run_index_trade_permutation(
    cfg,
    amounts,
    n_perm: int,
    rng: np.random.Generator,
) -> PermutationResult | None:
    code = cfg["code"]
    if amounts and is_ranking_excluded(code, amounts):
        return _excluded_result(code, cfg["name"], n_perm)

    plan = _extract_buy_plan(cfg["panel"], cfg, amounts)
    if plan is None:
        return None
    if plan["buy_days"] == 0:
        return _empty_buy_result(plan, n_perm)

    stats = _simulate_trade_stats(
        cfg, cfg["buy_fn"], cfg["sim_amt"], apply_cooldown=True
    )
    actual = _trade_return_pct(stats, cfg["has_sell"])
    perm_returns = _run_trade_permutations(plan, cfg, n_perm, rng)
    return _build_permutation_result(plan, actual, perm_returns, n_perm)


def _iter_trade_configs(panels, amounts, start_date, end_date):
    from backtest_trade_signals import (
        _cn_broad_signal_fns,
        _cn_broad_trailing_cfg,
        _cn_broad_valuation_sell_fn,
        _cyb_signal_fns,
        _dividend_trailing_cfg,
        _dividend_valuation_sell_fn,
        _resolve_trade_amount,
        _us_trailing_cfg,
        _us_valuation_sell_fn,
        US_INDEX_NOTES,
    )
    from backtest_buy_signals import CN_BROAD_BACKTEST_INDICES, US_INDEX_META, _us_buy_snapshot
    from config import (
        CYB_INDEX,
        INDICES,
        US_INDEX_KEYS,
        cn_broad_sell_enabled,
        dividend_sell_enabled,
        us_index_sell_enabled,
    )
    from dividend_data import is_buy_signal_row

    for item in INDICES:
        code = item["code"]
        panel = panels.dividend_panel(code)
        amt = get_backtest_buy_amount(code, amounts)
        buy_fn = lambda r, c=code: is_buy_signal_row(r, c)
        sell_on = dividend_sell_enabled(code)
        sim_amt = (
            _resolve_trade_amount(code, amt, amounts, panel, start_date, end_date, buy_fn)
            if amt > 0
            else 0
        )
        yield {
            "code": code,
            "name": item["name"],
            "panel": panel,
            "buy_fn": buy_fn,
            "buy_mask_fn": None,
            "sell_fn": None,
            "has_sell": sell_on,
            "trailing_cfg": _dividend_trailing_cfg(code) if sell_on else None,
            "valuation_sell_fn": _dividend_valuation_sell_fn(code) if sell_on else None,
            "valuation_price_col": "total_return_close",
            "date_col": "date",
            "price_col": "close",
            "start_date": start_date,
            "end_date": end_date,
            "sim_amt": sim_amt,
            "amount_raw": amt,
            "note": "日频，每交易日评估",
        }

    for item in CN_BROAD_BACKTEST_INDICES:
        code = item["code"]
        panel = panels.cn_broad_panel(code)
        amt = get_backtest_buy_amount(code, amounts)
        sell_on = cn_broad_sell_enabled(code)
        trail_cfg = _cn_broad_trailing_cfg(code) if sell_on else None
        buy_fn, sell_fn = _cn_broad_signal_fns(code, buy_only=not sell_on)
        sim_amt = (
            _resolve_trade_amount(code, amt, amounts, panel, start_date, end_date, buy_fn)
            if amt > 0
            else 0
        )
        yield {
            "code": code,
            "name": item["name"],
            "panel": panel,
            "buy_fn": buy_fn,
            "buy_mask_fn": None,
            "sell_fn": sell_fn,
            "has_sell": sell_on,
            "trailing_cfg": trail_cfg,
            "valuation_sell_fn": _cn_broad_valuation_sell_fn(code) if sell_on else None,
            "valuation_price_col": None,
            "date_col": "date",
            "price_col": "close",
            "start_date": start_date,
            "end_date": end_date,
            "sim_amt": sim_amt,
            "amount_raw": amt,
            "note": "日频，每交易日评估",
        }

    cyb_panel = panels.cyb_panel()
    cyb_code = CYB_INDEX["code"]
    cyb_amt = get_backtest_buy_amount(cyb_code, amounts)
    cyb_buy, cyb_sell = _cyb_signal_fns(buy_only=True)
    sim_amt = (
        _resolve_trade_amount(
            cyb_code, cyb_amt, amounts, cyb_panel, start_date, end_date, cyb_buy
        )
        if cyb_amt > 0
        else 0
    )
    yield {
        "code": cyb_code,
        "name": CYB_INDEX["name"],
        "panel": cyb_panel,
        "buy_fn": cyb_buy,
        "buy_mask_fn": None,
        "sell_fn": cyb_sell,
        "has_sell": False,
        "trailing_cfg": None,
        "valuation_sell_fn": None,
        "valuation_price_col": None,
        "date_col": "date",
        "price_col": "close",
        "start_date": start_date,
        "end_date": end_date,
        "sim_amt": sim_amt,
        "amount_raw": cyb_amt,
        "note": "日频，每交易日评估",
    }

    for key in US_INDEX_KEYS:
        daily, growth = panels.us_index_panel(key)
        meta = US_INDEX_META[key]
        code = meta["code"]
        amt = get_backtest_buy_amount(code, amounts)
        buy_fn = lambda r, k=key, g=growth: _us_buy_snapshot(k, r, g)
        sell_on = us_index_sell_enabled(key)
        sim_amt = (
            _resolve_trade_amount(code, amt, amounts, daily, start_date, end_date, buy_fn)
            if amt > 0
            else 0
        )
        yield {
            "code": code,
            "name": meta["name"],
            "panel": daily,
            "buy_fn": buy_fn,
            "buy_mask_fn": None,
            "sell_fn": None,
            "has_sell": sell_on,
            "trailing_cfg": _us_trailing_cfg(key) if sell_on else None,
            "valuation_sell_fn": _us_valuation_sell_fn(key, growth) if sell_on else None,
            "valuation_price_col": None,
            "date_col": "date",
            "price_col": "close",
            "start_date": start_date,
            "end_date": end_date,
            "sim_amt": sim_amt,
            "amount_raw": amt,
            "note": US_INDEX_NOTES[key],
        }


def _run_portfolio_rotation_permutations(
    indices,
    plans: dict[str, dict],
    n_perm: int,
    rng: np.random.Generator,
) -> np.ndarray:
    from backtest_rotation import (
        apply_rotation_buy_permutation,
        clone_rotation_indices,
        simulate_portfolio,
    )

    results = np.empty(n_perm, dtype=float)
    for i in range(n_perm):
        trial = clone_rotation_indices(indices)
        perm_by_code = {}
        for code, plan in plans.items():
            n_days = plan["n_days"]
            n_buys = plan["buy_count"]
            if n_buys <= 0 or n_days < n_buys:
                perm_by_code[code] = []
            else:
                perm_by_code[code] = rng.choice(n_days, size=n_buys, replace=False)
        apply_rotation_buy_permutation(trial, plans, perm_by_code)
        stats = simulate_portfolio(
            trial, "rotation", use_pool=True, rotation_gate=True
        )
        ret = stats.return_pct
        results[i] = np.nan if ret is None else ret
    return results[np.isfinite(results)]


def run_portfolio_rotation_permutation(
    panels,
    amounts,
    start_date: str,
    end_date: str | None,
    n_perm: int,
    rng: np.random.Generator,
) -> list[PermutationResult]:
    from backtest_rotation import (
        clone_rotation_indices,
        extract_rotation_buy_plans,
        prepare_rotation_indices,
        simulate_portfolio,
    )

    base_indices = prepare_rotation_indices(panels, amounts, start_date, end_date)
    if not base_indices:
        return []

    plans = extract_rotation_buy_plans(base_indices)
    total_buys = sum(p["buy_count"] for p in plans.values())
    total_days = max(p["n_days"] for p in plans.values())
    total_invested = sum(
        float(b["amount"]) for p in plans.values() for b in p["buys"]
    )

    actual_stats = simulate_portfolio(
        clone_rotation_indices(base_indices),
        "rotation",
        use_pool=True,
        rotation_gate=True,
    )
    actual = actual_stats.return_pct
    perm_returns = _run_portfolio_rotation_permutations(
        base_indices, plans, n_perm, rng
    )
    result = _build_permutation_result(
        {
            "code": "PORTFOLIO",
            "name": "智能轮动组合",
            "buy_days": total_buys,
            "total_days": total_days,
            "invested": total_invested,
            "note": (
                f"净投入 {actual_stats.total_new_money:,.0f} 元；"
                f"池复用 {actual_stats.pool_reused:,.0f} 元"
            ),
        },
        actual,
        perm_returns,
        n_perm,
    )
    return [result]


def run_all_permutations(
    date_range,
    amounts=None,
    panels=None,
    n_perm: int = DEFAULT_PERMUTATIONS,
    index_codes: list[str] | None = None,
    seed: int | None = None,
    mode: str = "buy",
    trade_start: str | None = None,
    trade_end: str | None = None,
) -> list[PermutationResult]:
    panels = panels or get_panels()
    rng = np.random.default_rng(seed)
    codes = {c.upper() for c in index_codes} if index_codes else None
    results: list[PermutationResult] = []

    if mode == "buy":
        for cfg in _iter_backtest_configs(panels):
            if codes and cfg["code"].upper() not in codes:
                continue
            if amounts and get_backtest_buy_amount(cfg["code"], amounts) <= 0:
                if not is_ranking_excluded(cfg["code"], amounts):
                    continue
            item = run_index_permutation(
                cfg["panel"], date_range, cfg, amounts, n_perm, rng
            )
            if item is not None:
                results.append(item)
        return results

    if mode == "rotation":
        if index_codes:
            print("rotation 模式为组合级检验，--index 参数将被忽略")
        return run_portfolio_rotation_permutation(
            panels,
            amounts,
            trade_start or "2015-01-01",
            trade_end,
            n_perm,
            rng,
        )

    for cfg in _iter_trade_configs(panels, amounts, trade_start, trade_end):
        if codes and cfg["code"].upper() not in codes:
            continue
        if amounts and cfg.get("amount_raw", 0) <= 0:
            if not is_ranking_excluded(cfg["code"], amounts):
                continue
        item = run_index_trade_permutation(cfg, amounts, n_perm, rng)
        if item is not None:
            results.append(item)
    return results


def _fmt_pct(value: float | None, digits: int = 1) -> str:
    if value is None:
        return "—"
    return f"{value:.{digits}f}%"


def _fmt_p(value: float | None) -> str:
    if value is None:
        return "—"
    if value < 0.001:
        return "<0.001"
    return f"{value:.3f}"


def _interpretation(result: PermutationResult) -> str:
    if result.note == EXCLUDED_RANKING_NOTE:
        return "未参与额度分配"
    if result.buy_days == 0:
        return "无买入"
    if result.p_value is None:
        return "—"
    if result.significant_5pct:
        return "显著优于随机（p<0.05）"
    if result.p_value < 0.1:
        return "略优于随机（p<0.10）"
    if result.percentile is not None and result.percentile >= 50:
        return "优于随机中位数"
    return "未显著优于随机"


def print_results_table(results: list[PermutationResult], n_perm: int, mode: str):
    print(f"\n蒙特卡洛置换检验（{MODE_LABELS[mode]}，打乱买入日 × {n_perm} 次）")
    print(
        f"{'指数':<14} {'代码':<8} {'买入':>5} {'真实收益':>9} "
        f"{'随机均值':>9} {'随机中位':>9} {'分位':>6} {'p值':>7} 结论"
    )
    print("-" * 88)
    for r in results:
        print(
            f"{r.name:<14} {r.code:<8} {r.buy_days:>5} "
            f"{_fmt_pct(r.actual_return_pct):>9} "
            f"{_fmt_pct(r.perm_mean):>9} "
            f"{_fmt_pct(r.perm_median):>9} "
            f"{_fmt_pct(r.percentile, 0):>6} "
            f"{_fmt_p(r.p_value):>7} "
            f"{_interpretation(r)}"
        )


def format_markdown(
    results: list[PermutationResult],
    date_range,
    amounts,
    n_perm: int,
    seed: int | None,
    mode: str,
    range_label: str,
) -> str:
    from config import format_backtest_amount_note

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if mode == "trade":
        strategy_note = (
            "打乱买入日后，**完整重跑**分批止盈与估值卖点逻辑"
            "（各指数独立，与旧版 `trade_inception_present.md` 一致）。"
        )
        title = "# 蒙特卡洛置换检验（波段买卖）"
        reproduce_cmd = "python monte_carlo_permutation.py --mode trade"
    elif mode == "rotation":
        strategy_note = (
            "打乱各指数买入日后，**完整重跑**智能轮动组合模拟"
            "（共享资金池 + 轮动门控，与 `trade_inception_present.md` 一致）。"
            "买入次数与每次金额序列不变，仅改变择时。"
        )
        title = "# 蒙特卡洛置换检验（智能轮动组合）"
        reproduce_cmd = "python monte_carlo_permutation.py --mode rotation"
    else:
        strategy_note = (
            "本检验仅验证**买入择时**（与 `inception_present.md` 一致，不含卖出逻辑）。"
        )
        title = "# 蒙特卡洛置换检验（仅买入持有）"
        reproduce_cmd = "python monte_carlo_permutation.py --mode buy"

    lines = [
        title,
        "",
        f"> 生成时间：{now}  ",
        f"> 模式：{MODE_LABELS[mode]}  ",
        f"> 区间：{range_label}  ",
        f"> 置换次数：{n_perm}  ",
        f"> 随机种子：{seed if seed is not None else '（未固定）'}  ",
        f"> 买入金额：{format_backtest_amount_note(amounts)}  ",
        "",
        "## 方法说明",
        "",
        "在回测区间内，保持与真实策略**相同的买入次数与每次买入金额**，",
        "将买入日随机映射到任意交易日（不放回抽样），重复上述过程得到",
        "「纯运气」下的收益率分布。",
        "",
        "- **p 值**：随机置换收益 ≥ 真实收益的概率（越小越不像运气）",
        "- **分位**：真实收益在随机分布中的百分位（越高越好）",
        "- **显著**：p < 0.05 时认为策略显著优于随机买入",
        "",
        strategy_note,
        "",
        "## 汇总",
        "",
        "| 指数 | 代码 | 买入次 | 真实收益 | 随机均值 | 随机中位 | 随机区间 | 分位 | p值 | 结论 |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
    ]

    for r in results:
        perm_range = "—"
        if r.perm_min is not None and r.perm_max is not None:
            perm_range = f"{r.perm_min:.1f}% ~ {r.perm_max:.1f}%"
        lines.append(
            f"| {r.name} | {r.code} | {r.buy_days} | "
            f"{_fmt_pct(r.actual_return_pct)} | {_fmt_pct(r.perm_mean)} | "
            f"{_fmt_pct(r.perm_median)} | {perm_range} | "
            f"{_fmt_pct(r.percentile, 0)} | {_fmt_p(r.p_value)} | "
            f"{_interpretation(r)} |"
        )

    lines.extend([
        "",
        "## 如何解读",
        "",
        "| p 值 | 含义 |",
        "| ---: | --- |",
        "| < 0.05 | 真实收益显著高于随机，策略可能有效 |",
        "| 0.05 ~ 0.10 | 有一定优势，但证据较弱 |",
        "| > 0.10 | 与随机买入差异不大，收益可能主要来自市场β或买入频率 |",
        "",
        "## 复现命令",
        "",
        "```bash",
        reproduce_cmd,
        "```",
        "",
    ])
    return "\n".join(lines)


def save_results(
    results: list[PermutationResult],
    date_range,
    amounts,
    n_perm: int,
    seed: int | None,
    mode: str,
    range_label: str,
    stem: str,
) -> tuple[str, str]:
    BACKTEST_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    md_path = BACKTEST_OUTPUT_DIR / f"{stem}.md"
    json_path = BACKTEST_OUTPUT_DIR / f"{stem}.json"

    md_path.write_text(
        format_markdown(results, date_range, amounts, n_perm, seed, mode, range_label),
        encoding="utf-8",
    )

    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "mode": mode,
        "date_range": {
            "start": date_range.start if date_range else None,
            "end": date_range.end if date_range else None,
            "label": range_label,
        },
        "permutations": n_perm,
        "seed": seed,
        "method": "shuffle_buy_dates",
        "results": [asdict(r) for r in results],
    }
    json_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return str(md_path), str(json_path)


def _resolve_modes(mode: str) -> list[str]:
    if mode == "all":
        return ["buy", "trade", "rotation"]
    if mode not in MODE_LABELS:
        raise ValueError(f"未知模式: {mode}")
    return [mode]


def _default_output_stem(mode: str, output: str | None) -> str:
    if output:
        return output
    if mode == "trade":
        return TRADE_OUTPUT_STEM
    if mode == "rotation":
        return ROTATION_OUTPUT_STEM
    return DEFAULT_OUTPUT_STEM


def _run_mode(
    mode: str,
    args,
    date_range: BacktestRange,
    amounts,
    panels,
    trade_start: str,
    trade_end: str | None,
):
    from backtest_trade_signals import DEFAULT_START

    if mode == "buy":
        range_label = format_backtest_range_label(date_range)
        print(
            f"\n[{MODE_LABELS[mode]}] {range_label}，"
            f"{args.permutations} 次打乱买入日..."
        )
        results = run_all_permutations(
            date_range,
            amounts=amounts,
            panels=panels,
            n_perm=args.permutations,
            index_codes=args.index_codes,
            seed=args.seed,
            mode="buy",
        )
    elif mode == "rotation":
        start = args.start or trade_start or DEFAULT_START
        end = args.end or trade_end
        range_label = f"{start} 至 {end or '最新'}"
        print(
            f"\n[{MODE_LABELS[mode]}] {range_label}，"
            f"{args.permutations} 次打乱买入日..."
        )
        results = run_all_permutations(
            None,
            amounts=amounts,
            panels=panels,
            n_perm=args.permutations,
            index_codes=args.index_codes,
            seed=args.seed,
            mode="rotation",
            trade_start=start,
            trade_end=end,
        )
    else:
        start = args.start or trade_start or DEFAULT_START
        end = args.end or trade_end
        range_label = f"{start} 至 {end or '最新'}"
        print(
            f"\n[{MODE_LABELS[mode]}] {range_label}，"
            f"{args.permutations} 次打乱买入日..."
        )
        results = run_all_permutations(
            None,
            amounts=amounts,
            panels=panels,
            n_perm=args.permutations,
            index_codes=args.index_codes,
            seed=args.seed,
            mode="trade",
            trade_start=start,
            trade_end=end,
        )

    if not results:
        print("无可用指数结果")
        return 1

    print_results_table(results, args.permutations, mode)
    stem = _default_output_stem(mode, args.output if args.mode != "all" else None)
    md_path, json_path = save_results(
        results,
        date_range
        if mode == "buy"
        else BacktestRange(
            start=trade_start if mode != "rotation" else (args.start or trade_start or DEFAULT_START),
            end=trade_end,
            label=range_label,
        ),
        amounts,
        args.permutations,
        args.seed,
        mode,
        range_label,
        stem=stem,
    )
    print(f"\n结果已保存:\n  {md_path}\n  {json_path}")
    return 0


def main(argv=None) -> int:
    configure_stdout_utf8()
    parser = argparse.ArgumentParser(
        description="蒙特卡洛置换检验：打乱买入日判断回测是否运气（默认 200 次）"
    )
    parser.add_argument(
        "--mode",
        choices=["buy", "trade", "rotation", "all"],
        default="buy",
        help="回测模式：buy=仅买入；trade=波段买卖；rotation=智能轮动组合；all=全部（默认 buy）",
    )
    parser.add_argument(
        "--permutations",
        "-n",
        type=int,
        default=DEFAULT_PERMUTATIONS,
        help=f"置换次数（默认 {DEFAULT_PERMUTATIONS}）",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=None,
        help="随机种子（便于复现）",
    )
    parser.add_argument(
        "--index",
        action="append",
        dest="index_codes",
        metavar="CODE",
        help="仅检验指定指数，可多次指定",
    )
    parser.add_argument(
        "--start",
        default=None,
        help="起始日期 YYYY-MM-DD（buy 默认自基日；trade 默认 2015-01-01）",
    )
    parser.add_argument(
        "--end",
        default=None,
        help="结束日期 YYYY-MM-DD（默认最新）",
    )
    parser.add_argument(
        "--amount",
        type=float,
        default=None,
        help="统一覆盖所有指数单次买入金额（元）",
    )
    parser.add_argument(
        "--ranking",
        action="store_true",
        help="按全历史收益率排名分配买入金额",
    )
    parser.add_argument(
        "--no-tier",
        action="store_true",
        help="禁用涨跌缩放，固定使用基准单次金额",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="输出文件名（不含扩展名；all 模式下分别输出 buy/trade 默认名）",
    )
    args = parser.parse_args(argv)

    if args.permutations < 1:
        print("置换次数须 ≥ 1")
        return 1

    from config import BACKTEST_PRESENT_LABEL
    from backtest_trade_signals import DEFAULT_START

    if args.start:
        date_range = BacktestRange(
            start=args.start,
            end=args.end,
            label=f"{args.start}_to_{args.end or BACKTEST_PRESENT_LABEL}",
        )
        trade_start = args.start
        trade_end = args.end
    else:
        date_range = default_backtest_range()
        if args.end:
            date_range = BacktestRange(
                start=date_range.start,
                end=args.end,
                label=date_range.label,
            )
        trade_start = DEFAULT_START
        trade_end = args.end

    tier_enabled = not args.no_tier
    if args.amount is not None and args.amount <= 0:
        amounts = None
    elif args.amount is not None:
        amounts = resolve_backtest_amounts(args.amount, tier_enabled=tier_enabled)
    elif args.ranking:
        amounts = resolve_backtest_amounts(
            ranking_mode=True, tier_enabled=tier_enabled
        )
    else:
        amounts = resolve_backtest_amounts(tier_enabled=tier_enabled)

    print("正在加载数据（仅首次较慢）...")
    panels = get_panels()
    from buy_amount_ranking import _preload_ranking_panels

    _preload_ranking_panels(panels)

    try:
        modes = _resolve_modes(args.mode)
    except ValueError as exc:
        print(exc)
        return 1

    exit_code = 0
    for mode in modes:
        try:
            code = _run_mode(
                mode, args, date_range, amounts, panels, trade_start, trade_end
            )
            if code != 0:
                exit_code = code
        except Exception as exc:
            print(f"[{MODE_LABELS[mode]}] 检验失败: {exc}")
            exit_code = 1
    return exit_code


if __name__ == "__main__":
    sys.exit(main())

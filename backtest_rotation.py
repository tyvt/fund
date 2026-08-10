"""组合级轮动回测：共享资金池 + 轮动门控卖点，对比全持有与当前卖点。"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from dataclasses import dataclass, field

import pandas as pd

from backtest_metrics import CapitalTracker
from backtest_trade_signals import (
    DEFAULT_START,
    _cn_broad_trailing_cfg,
    _cn_broad_valuation_sell_fn,
    _cooldown_buy_fn,
    _dividend_trailing_cfg,
    _dividend_valuation_sell_fn,
    _filter_panel,
    _resolve_buy_amount,
    _resolve_trade_amount,
    _us_trailing_cfg,
    _us_valuation_sell_fn,
    backtest_all,
)
from config import (
    BACKTEST_OUTPUT_DIR,
    ROTATION_MARGINAL_HURDLE_ANN_PCT,
    format_backtest_amount_note,
    get_backtest_buy_amount,
    resolve_backtest_amounts,
)
from market_data import configure_stdout_utf8
from rotation_sell import annualized_position_return_pct, rotation_sell_allowed
from sell_trailing import rebuy_allowed_after_take_profit, trailing_sell_hit


@dataclass
class _Position:
    units: float = 0.0
    cost_basis: float = 0.0
    peak_since_buy: float = 0.0
    initial_units: float = 0.0
    last_buy_dt: object = None
    stages_triggered: set = field(default_factory=set)


@dataclass
class IndexSimResult:
    code: str
    name: str
    has_sell: bool
    buy_count: int = 0
    sell_count: int = 0
    new_money_in: float = 0.0
    buy_volume: float = 0.0
    sell_proceeds: float = 0.0
    final_units: float = 0.0
    final_price: float = 0.0
    final_date: object = None
    final_value: float = 0.0
    profit: float = 0.0
    return_pct: float | None = None
    buy_dates: list = field(default_factory=list)
    sell_dates: list = field(default_factory=list)


@dataclass
class PortfolioResult:
    mode: str
    total_new_money: float
    final_value: float
    profit: float
    return_pct: float | None
    xirr_pct: float | None
    buy_count: int
    sell_count: int
    cash_end: float
    pool_reused: float
    per_index: dict[str, IndexSimResult] = field(default_factory=dict)
    daily_history: list[dict] = field(default_factory=list)
    cashflows: list[tuple] = field(default_factory=list)


def _iter_rotation_configs(panels, amounts, start_date, end_date):
    """与波段回测一致的各指数配置。"""
    from backtest_trade_signals import (
        _cn_broad_signal_fns,
        _cyb_signal_fns,
    )
    from backtest_buy_signals import (
        CN_BROAD_BACKTEST_INDICES,
        US_INDEX_META,
        _us_buy_snapshot,
    )
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
        if amt <= 0:
            continue
        sim_amt = _resolve_trade_amount(
            code, amt, amounts, panel, start_date, end_date, buy_fn
        )
        sell_on = dividend_sell_enabled(code)
        yield {
            "code": code,
            "name": item["name"],
            "panel": panel,
            "buy_fn": buy_fn,
            "has_sell": sell_on,
            "trailing_cfg": _dividend_trailing_cfg(code) if sell_on else None,
            "valuation_sell_fn": _dividend_valuation_sell_fn(code) if sell_on else None,
            "valuation_price_col": "total_return_close",
            "sim_amt": sim_amt,
        }

    for item in CN_BROAD_BACKTEST_INDICES:
        code = item["code"]
        panel = panels.cn_broad_panel(code)
        amt = get_backtest_buy_amount(code, amounts)
        sell_on = cn_broad_sell_enabled(code)
        buy_fn, _ = _cn_broad_signal_fns(code, buy_only=not sell_on)
        if amt <= 0:
            continue
        sim_amt = _resolve_trade_amount(
            code, amt, amounts, panel, start_date, end_date, buy_fn
        )
        yield {
            "code": code,
            "name": item["name"],
            "panel": panel,
            "buy_fn": buy_fn,
            "has_sell": sell_on,
            "trailing_cfg": _cn_broad_trailing_cfg(code) if sell_on else None,
            "valuation_sell_fn": _cn_broad_valuation_sell_fn(code) if sell_on else None,
            "valuation_price_col": None,
            "sim_amt": sim_amt,
        }

    cyb_panel = panels.cyb_panel()
    cyb_code = CYB_INDEX["code"]
    cyb_amt = get_backtest_buy_amount(cyb_code, amounts)
    if cyb_amt > 0:
        cyb_buy, _ = _cyb_signal_fns(buy_only=True)
        sim_amt = _resolve_trade_amount(
            cyb_code, cyb_amt, amounts, cyb_panel, start_date, end_date, cyb_buy
        )
        yield {
            "code": cyb_code,
            "name": CYB_INDEX["name"],
            "panel": cyb_panel,
            "buy_fn": cyb_buy,
            "has_sell": False,
            "trailing_cfg": None,
            "valuation_sell_fn": None,
            "valuation_price_col": None,
            "sim_amt": sim_amt,
        }

    for key in US_INDEX_KEYS:
        daily, growth = panels.us_index_panel(key)
        meta = US_INDEX_META[key]
        code = meta["code"]
        amt = get_backtest_buy_amount(code, amounts)
        if amt <= 0:
            continue
        buy_fn = lambda r, k=key, g=growth: _us_buy_snapshot(k, r, g)
        sim_amt = _resolve_trade_amount(
            code, amt, amounts, daily, start_date, end_date, buy_fn
        )
        sell_on = us_index_sell_enabled(key)
        yield {
            "code": code,
            "name": meta["name"],
            "panel": daily,
            "buy_fn": buy_fn,
            "has_sell": sell_on,
            "trailing_cfg": _us_trailing_cfg(key) if sell_on else None,
            "valuation_sell_fn": _us_valuation_sell_fn(key, growth) if sell_on else None,
            "valuation_price_col": None,
            "sim_amt": sim_amt,
        }


def _prepare_index(cfg, start_date, end_date):
    code = cfg["code"]
    panel = cfg["panel"]
    val_col = cfg.get("valuation_price_col") or "close"
    buy_fn = _cooldown_buy_fn(
        panel,
        cfg["buy_fn"],
        code,
        start_date,
        end_date,
        price_col="close",
    )
    sample = _filter_panel(panel, start_date, end_date)
    if sample.empty:
        return None
    if val_col not in sample.columns:
        val_col = "close"

    trail = cfg.get("trailing_cfg") or {}
    stages = trail.get("stages") or []
    first_stage_gain = float(stages[0]["gain_pct"]) if stages else 0.50
    from config import SELL_REBUY_GATE_ENABLED, SELL_REBUY_MAX_GAIN_PCT

    rows_by_day = {}
    cols = sample.columns.tolist()
    for tup in sample.itertuples(index=False, name=None):
        row = dict(zip(cols, tup))
        day = row["_dt"].strftime("%Y-%m-%d")
        rows_by_day[day] = {
            "row": row,
            "price": float(row[val_col]),
            "dt": row["_dt"],
            "is_buy": bool(buy_fn(row)),
        }

    return {
        **cfg,
        "buy_fn": buy_fn,
        "val_col": val_col,
        "rows_by_day": rows_by_day,
        "trail": trail,
        "stages": stages,
        "use_trailing": trail.get("trailing_drawdown_pct") is not None,
        "valuation_sell_fn": cfg.get("valuation_sell_fn"),
        "first_stage_gain": first_stage_gain,
        "rebuy_max_gain": trail.get("rebuy_max_gain_pct", SELL_REBUY_MAX_GAIN_PCT),
        "rebuy_gate": trail.get("rebuy_gate_enabled", SELL_REBUY_GATE_ENABLED),
        "latest_day": max(rows_by_day.keys()),
        "latest_price": rows_by_day[max(rows_by_day.keys())]["price"],
        "latest_dt": rows_by_day[max(rows_by_day.keys())]["dt"],
    }


def _evaluate_sell(pos: _Position, meta, row, price, dt, allow_sell: bool):
    """返回 (partial_units, full_sell, is_trailing, is_valuation)。"""
    if not allow_sell or pos.units <= 0:
        return 0.0, False, False, False

    trail = meta["trail"]
    stages = meta["stages"]
    avg_cost = pos.cost_basis / pos.units
    days_since = (dt - pos.last_buy_dt).days if pos.last_buy_dt is not None else None
    gain = (price - avg_cost) / avg_cost if avg_cost > 0 else None

    partial_units = 0.0
    for stage in stages:
        stage_key = float(stage["gain_pct"])
        if stage_key in pos.stages_triggered:
            continue
        if gain is None or gain < stage_key:
            continue
        if min_hold := trail.get("min_hold_days"):
            if days_since is not None and days_since < min_hold:
                continue
        target = pos.initial_units * float(stage["fraction_of_initial"])
        partial_units = min(pos.units, target)
        if partial_units > 0:
            return partial_units, False, True, False

    is_trailing = False
    if partial_units <= 0 and meta["use_trailing"]:
        is_trailing = trailing_sell_hit(
            close=price,
            cost_basis=avg_cost,
            peak_price=pos.peak_since_buy,
            min_unrealized_gain_pct=trail.get("min_unrealized_gain_pct"),
            trailing_drawdown_pct=trail.get("trailing_drawdown_pct"),
            min_hold_days=trail.get("min_hold_days"),
            days_since_buy=days_since,
        )

    is_valuation = False
    if partial_units <= 0 and not is_trailing and meta["valuation_sell_fn"]:
        is_valuation = bool(meta["valuation_sell_fn"](row))

    full_sell = is_trailing or is_valuation
    return partial_units, full_sell, is_trailing, is_valuation


def _price_on_or_before(meta: dict, day: str) -> float:
    rows = meta["rows_by_day"]
    if day in rows:
        return float(rows[day]["price"])
    prior = [d for d in rows if d <= day]
    if not prior:
        return float(meta["latest_price"])
    return float(rows[max(prior)]["price"])


def _portfolio_value_on_day(
    positions: dict,
    meta_by_code: dict,
    cash_pool: float,
    day: str,
) -> float:
    total = cash_pool
    for code, pos in positions.items():
        if pos.units > 0:
            total += pos.units * _price_on_or_before(meta_by_code[code], day)
    return total


def simulate_portfolio(
    indices: list[dict],
    mode: str,
    *,
    use_pool: bool = False,
    rotation_gate: bool = False,
    record_daily: bool = False,
    regime_by_day: dict[str, str] | None = None,
    regime_config: object | None = None,
) -> PortfolioResult:
    """组合模拟。mode: hold | sell | rotation。"""
    all_days = sorted({d for m in indices for d in m["rows_by_day"]})
    positions = {m["code"]: _Position() for m in indices}
    meta_by_code = {m["code"]: m for m in indices}
    index_stats = {
        m["code"]: IndexSimResult(
            code=m["code"],
            name=m["name"],
            has_sell=bool(m.get("has_sell")),
        )
        for m in indices
    }

    cash_pool = 0.0
    pool_reused = 0.0
    total_new_money = 0.0
    buy_count = 0
    sell_count = 0
    capital = CapitalTracker()

    allow_sell = mode in ("sell", "rotation")
    use_rotation_gate = rotation_gate or mode == "rotation"
    daily_history: list[dict] = []

    for day in all_days:
        new_money_day = 0.0
        buy_count_day = 0
        sell_count_day = 0
        day_dt = pd.Timestamp(day)
        regime_params = None
        if regime_by_day is not None:
            from market_regime import REGIME_NEUTRAL, get_regime_params

            regime = regime_by_day.get(day, REGIME_NEUTRAL)
            regime_params = get_regime_params(regime, regime_config)
        buy_flags = {
            code: meta_by_code[code]["rows_by_day"].get(day, {}).get("is_buy", False)
            for code in meta_by_code
            if day in meta_by_code[code]["rows_by_day"]
        }

        # 先卖后买
        for code, meta in meta_by_code.items():
            if day not in meta["rows_by_day"]:
                continue
            item = meta["rows_by_day"][day]
            row, price, dt = item["row"], item["price"], item["dt"]
            pos = positions[code]

            if pos.units > 0:
                pos.peak_since_buy = max(pos.peak_since_buy, price)

            partial, full, is_trail, is_val = _evaluate_sell(
                pos, meta, row, price, dt, allow_sell and meta["has_sell"]
            )
            if partial <= 0 and not full:
                continue

            avg_cost = pos.cost_basis / pos.units if pos.units > 0 else None
            ann = annualized_position_return_pct(
                price, avg_cost, (dt - pos.last_buy_dt).days if pos.last_buy_dt else None
            )
            if use_rotation_gate and not rotation_sell_allowed(
                code,
                buy_flags,
                is_trailing=is_trail or partial > 0,
                is_valuation=is_val,
                annualized_gain_pct=ann,
                hurdle_ann_pct=(
                    regime_params["rotation_hurdle_ann_pct"]
                    if regime_params
                    else None
                ),
            ):
                continue

            if partial > 0:
                stage_key = None
                for stage in meta["stages"]:
                    sk = float(stage["gain_pct"])
                    if sk not in pos.stages_triggered:
                        stage_key = sk
                        break
                proceeds = partial * price
                pos.cost_basis *= 1.0 - partial / pos.units
                pos.units -= partial
                if stage_key is not None:
                    pos.stages_triggered.add(stage_key)
                sell_count += 1
                sell_count_day += 1
                index_stats[code].sell_count += 1
                index_stats[code].sell_dates.append(day)
                index_stats[code].sell_proceeds += proceeds
                if use_pool:
                    cash_pool += proceeds
                capital.record_sell(dt, proceeds)
            elif full:
                proceeds = pos.units * price
                sell_count += 1
                sell_count_day += 1
                index_stats[code].sell_count += 1
                index_stats[code].sell_dates.append(day)
                index_stats[code].sell_proceeds += proceeds
                if use_pool:
                    cash_pool += proceeds
                capital.record_sell(dt, proceeds)
                pos.units = 0.0
                pos.cost_basis = 0.0
                pos.peak_since_buy = 0.0
                pos.initial_units = 0.0
                pos.stages_triggered.clear()

        for code, meta in meta_by_code.items():
            if day not in meta["rows_by_day"]:
                continue
            item = meta["rows_by_day"][day]
            row, price, dt = item["row"], item["price"], item["dt"]
            pos = positions[code]

            if item["is_buy"]:
                avg_cost = pos.cost_basis / pos.units if pos.units > 0 else None
                blocked = False
                if pos.units > 0 and avg_cost is not None:
                    blocked = not rebuy_allowed_after_take_profit(
                        close=price,
                        cost_basis=avg_cost,
                        peak_price=pos.peak_since_buy,
                        stages_triggered=pos.stages_triggered,
                        max_gain_pct=meta["rebuy_max_gain"],
                        first_stage_gain_pct=meta["first_stage_gain"],
                        gate_enabled=meta["rebuy_gate"],
                    )
                if not blocked:
                    buy_amount = meta.get("buy_amount_by_day", {}).get(day)
                    if buy_amount is None:
                        buy_amount = float(_resolve_buy_amount(meta["sim_amt"], row))
                    buy_amount = float(buy_amount)
                    if regime_params:
                        buy_amount *= float(regime_params["buy_amount_mult"])
                    if buy_amount > 0:
                        new_money = buy_amount
                        if use_pool and cash_pool > 0:
                            from_pool = min(cash_pool, buy_amount)
                            cash_pool -= from_pool
                            pool_reused += from_pool
                            new_money = buy_amount - from_pool

                        total_new_money += new_money
                        new_money_day += new_money
                        if new_money > 0:
                            capital.record_buy(dt, new_money)

                        units_add = buy_amount / price
                        if pos.units <= 0:
                            pos.initial_units = 0.0
                            pos.stages_triggered.clear()
                        pos.units += units_add
                        pos.initial_units += units_add
                        pos.cost_basis += buy_amount
                        pos.last_buy_dt = dt
                        pos.peak_since_buy = price
                        buy_count += 1
                        buy_count_day += 1
                        ist = index_stats[code]
                        ist.buy_count += 1
                        ist.buy_dates.append(day)
                        ist.new_money_in += new_money
                        ist.buy_volume += buy_amount

            capital.record_day(pos.units * price)

        if record_daily:
            daily_history.append(
                {
                    "day": day,
                    "dt": day_dt,
                    "value": _portfolio_value_on_day(positions, meta_by_code, cash_pool, day),
                    "new_money_day": new_money_day,
                    "buy_count_day": buy_count_day,
                    "sell_count_day": sell_count_day,
                }
            )

    final_value = cash_pool
    for code, pos in positions.items():
        meta = meta_by_code[code]
        pos_value = pos.units * meta["latest_price"]
        final_value += pos_value
        ist = index_stats[code]
        ist.final_units = pos.units
        ist.final_price = meta["latest_price"]
        ist.final_date = meta["latest_dt"]
        ist.final_value = pos_value
        ist.profit = pos_value - ist.new_money_in
        ist.return_pct = (
            ist.profit / ist.new_money_in * 100 if ist.new_money_in > 0 else None
        )

    profit = final_value - total_new_money
    return_pct = profit / total_new_money * 100 if total_new_money > 0 else None
    last_dt = meta_by_code[indices[0]["code"]]["latest_dt"]
    cap = capital.finalize(last_dt, final_value, len(all_days), profit, total_new_money)

    return PortfolioResult(
        mode=mode,
        total_new_money=total_new_money,
        final_value=final_value,
        profit=profit,
        return_pct=return_pct,
        xirr_pct=cap.get("xirr_pct"),
        buy_count=buy_count,
        sell_count=sell_count,
        cash_end=cash_pool,
        pool_reused=pool_reused,
        per_index=index_stats,
        daily_history=daily_history,
        cashflows=capital.cashflows() if record_daily else [],
    )


def _baseline_from_backtest(results, *, hold: bool):
    total_bought = sum(r.total_bought for r in results)
    if hold:
        profit = sum(r.buy_only_profit for r in results)
        value = sum(r.buy_only_value for r in results)
        xirr_vals = [r.buy_only_xirr_pct for r in results if r.buy_only_xirr_pct is not None]
    else:
        profit = 0.0
        value = 0.0
        xirr_vals = []
        for r in results:
            if r.has_sell:
                profit += r.profit
                value += r.final_value
                if r.xirr_pct is not None:
                    xirr_vals.append(r.xirr_pct)
            else:
                profit += r.buy_only_profit
                value += r.buy_only_value
                if r.buy_only_xirr_pct is not None:
                    xirr_vals.append(r.buy_only_xirr_pct)
    ret = profit / total_bought * 100 if total_bought > 0 else None
    avg_xirr = sum(xirr_vals) / len(xirr_vals) if xirr_vals else None
    return PortfolioResult(
        mode="hold" if hold else "sell",
        total_new_money=total_bought,
        final_value=value,
        profit=profit,
        return_pct=ret,
        xirr_pct=avg_xirr,
        buy_count=sum(r.buy_count for r in results),
        sell_count=sum(r.sell_count for r in results),
        cash_end=0.0,
        pool_reused=0.0,
    )


def clone_rotation_indices(indices: list[dict]) -> list[dict]:
    """深拷贝组合指数状态，供置换检验反复模拟。"""
    cloned = []
    for meta in indices:
        item = dict(meta)
        item["rows_by_day"] = {
            day: dict(row_item) for day, row_item in meta["rows_by_day"].items()
        }
        item.pop("buy_amount_by_day", None)
        cloned.append(item)
    return cloned


def apply_rotation_buy_permutation(
    indices: list[dict],
    plans: dict[str, dict],
    perm_indices_by_code: dict[str, list],
) -> None:
    """将各指数买入日置换为随机交易日，并保留原买入金额序列。"""
    for meta in indices:
        code = meta["code"]
        plan = plans[code]
        day_list = plan["day_list"]
        amounts = [float(b["amount"]) for b in plan["buys"]]
        perm_sorted = sorted(int(i) for i in perm_indices_by_code[code])
        amount_by_day: dict[str, float] = {}
        for pos, day_idx in enumerate(perm_sorted):
            amount_by_day[day_list[day_idx]] = amounts[pos]
        for day, row_item in meta["rows_by_day"].items():
            row_item["is_buy"] = day in amount_by_day
        meta["buy_amount_by_day"] = amount_by_day


def extract_rotation_buy_plans(indices: list[dict]) -> dict[str, dict]:
    """提取各指数真实买入日下标与金额（用于置换）。"""
    from backtest_trade_signals import _resolve_buy_amount

    plans = {}
    for meta in indices:
        code = meta["code"]
        day_list = sorted(meta["rows_by_day"].keys())
        buys = []
        for day_idx, day in enumerate(day_list):
            item = meta["rows_by_day"][day]
            if not item["is_buy"]:
                continue
            buys.append(
                {
                    "day_idx": day_idx,
                    "amount": float(_resolve_buy_amount(meta["sim_amt"], item["row"])),
                }
            )
        plans[code] = {
            "day_list": day_list,
            "n_days": len(day_list),
            "buys": buys,
            "buy_count": len(buys),
        }
    return plans


def prepare_rotation_indices(panels, amounts, start_date, end_date):
    cfgs = list(_iter_rotation_configs(panels, amounts, start_date, end_date))
    return [x for x in (_prepare_index(c, start_date, end_date) for c in cfgs) if x]


def run_portfolio_rotation(
    start_date,
    end_date,
    amounts,
    panels,
    *,
    mode: str = "rotation",
    use_pool: bool = True,
    rotation_gate: bool = True,
    record_daily: bool = False,
    regime_by_day: dict[str, str] | None = None,
    regime_config: object | None = None,
) -> PortfolioResult:
    from backtest_buy_signals import get_panels
    from buy_amount_ranking import _preload_ranking_panels
    from market_regime import build_regime_by_day, default_regime_config

    panels = panels or get_panels()
    _preload_ranking_panels(panels)
    indices = prepare_rotation_indices(panels, amounts, start_date, end_date)
    cfg = regime_config or default_regime_config()
    if regime_by_day is None and cfg.enabled:
        regime_by_day = build_regime_by_day(
            panels, start_date, end_date, config=cfg
        )
    return simulate_portfolio(
        indices,
        mode,
        use_pool=use_pool,
        rotation_gate=rotation_gate,
        record_daily=record_daily,
        regime_by_day=regime_by_day,
        regime_config=cfg,
    )


def run_comparison(start_date, end_date, amounts, panels):
    from backtest_buy_signals import get_panels
    from buy_amount_ranking import _preload_ranking_panels

    panels = panels or get_panels()
    _preload_ranking_panels(panels)
    indices = prepare_rotation_indices(panels, amounts, start_date, end_date)

    trade_results, _ = backtest_all(
        start_date,
        end_date,
        amounts=amounts,
        panels=panels,
        rotation=False,
    )
    hold_base = _baseline_from_backtest(trade_results, hold=True)
    sell_base = _baseline_from_backtest(trade_results, hold=False)

    rotation = simulate_portfolio(
        indices, "rotation", use_pool=True, rotation_gate=True
    )
    sell_pool = simulate_portfolio(
        indices, "sell", use_pool=True, rotation_gate=False
    )

    return {
        "hold": hold_base,
        "sell_isolated": sell_base,
        "sell_pool": sell_pool,
        "rotation": rotation,
        "start": start_date,
        "end": end_date or "最新",
        "hurdle": ROTATION_MARGINAL_HURDLE_ANN_PCT,
    }


def _fmt_pct(v):
    return "—" if v is None else f"{v:+.1f}%"


def _fmt_money(v):
    return f"{v:,.0f}"


def format_markdown(data, amounts) -> str:
    from datetime import datetime

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines = [
        "# 轮动卖点回测对比（共享资金池）",
        "",
        f"> 生成时间：{now}  ",
        f"> 区间：{data['start']} 至 {data['end']}  ",
        f"> 买入金额：{format_backtest_amount_note(amounts)}  ",
        f"> 轮动门槛：持仓年化 < **{data['hurdle']:.0f}%** 且当日有其他指数买点（移动止盈仅需有买点）  ",
        f"> 卖出释放资金优先投入后续买点（共享资金池）  ",
        "",
        "## 策略说明",
        "",
        "| 策略 | 说明 |",
        "| --- | --- |",
        "| 全持有 | 各指数独立定投，不卖（对照基线） |",
        "| 当前卖点 | 各指数独立，卖点触发即卖，资金不跨指数复用 |",
        "| 卖点+资金池 | 卖点逻辑不变，但卖出资金可投入其他指数买点 |",
        "| **智能轮动** | 仅当同日有其他指数买点时卖出；估值卖需持仓年化低于门槛 |",
        "",
        "## 组合收益对比",
        "",
        "| 策略 | 净投入 | 期末市值 | 利润 | 总收益率 | XIRR(均) | 买入次 | 卖出次 | 池复用 |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    labels = {
        "hold": "全持有",
        "sell_isolated": "当前卖点",
        "sell_pool": "卖点+资金池",
        "rotation": "**智能轮动**",
    }
    for key in ("hold", "sell_isolated", "sell_pool", "rotation"):
        r = data[key]
        lines.append(
            f"| {labels[key]} | {_fmt_money(r.total_new_money)} | "
            f"{_fmt_money(r.final_value)} | {r.profit:+.0f} | "
            f"{_fmt_pct(r.return_pct)} | {_fmt_pct(r.xirr_pct)} | "
            f"{r.buy_count} | {r.sell_count} | {_fmt_money(r.pool_reused)} |"
        )

    rot = data["rotation"]
    hold = data["hold"]
    diff_ret = (rot.return_pct or 0) - (hold.return_pct or 0)
    diff_xirr = (rot.xirr_pct or 0) - (hold.xirr_pct or 0)
    lines.extend([
        "",
        "## 结论",
        "",
        f"- 智能轮动 vs 全持有：总收益率差 **{diff_ret:+.1f}** pct，"
        f"XIRR 差 **{diff_xirr:+.1f}** pct",
        f"- 智能轮动 vs 当前卖点：总收益率差 "
        f"**{(rot.return_pct or 0) - (data['sell_isolated'].return_pct or 0):+.1f}** pct",
        f"- 资金池复用金额：卖点+池 {_fmt_money(data['sell_pool'].pool_reused)}，"
        f"智能轮动 {_fmt_money(rot.pool_reused)}",
        "",
        "复现：`python backtest_rotation.py`",
        "",
    ])
    return "\n".join(lines)


def print_summary(data):
    print(
        f"\n=== 轮动回测 {data['start']} 至 {data['end']} "
        f"(门槛 {data['hurdle']:.0f}% 年化) ==="
    )
    labels = {
        "hold": "全持有",
        "sell_isolated": "当前卖点",
        "sell_pool": "卖点+资金池",
        "rotation": "智能轮动",
    }
    print(f"{'策略':<12} {'净投入':>10} {'收益率':>9} {'XIRR':>8} {'卖出':>5}")
    print("-" * 50)
    for key in ("hold", "sell_isolated", "sell_pool", "rotation"):
        r = data[key]
        ret = f"{r.return_pct:+.1f}%" if r.return_pct is not None else "—"
        xirr = f"{r.xirr_pct:+.1f}%" if r.xirr_pct is not None else "—"
        print(
            f"{labels[key]:<12} {r.total_new_money:>10.0f} {ret:>9} "
            f"{xirr:>8} {r.sell_count:>5}"
        )


def main(argv=None) -> int:
    configure_stdout_utf8()
    parser = argparse.ArgumentParser(description="轮动卖点 + 共享资金池回测对比")
    parser.add_argument("--start", default=DEFAULT_START)
    parser.add_argument("--end", default=None)
    parser.add_argument("--no-tier", action="store_true")
    args = parser.parse_args(argv)

    amounts = resolve_backtest_amounts(tier_enabled=not args.no_tier)
    from backtest_buy_signals import get_panels

    try:
        data = run_comparison(args.start, args.end, amounts, get_panels())
    except Exception as exc:
        print(f"回测失败: {exc}")
        return 1
    print_summary(data)

    BACKTEST_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    path = BACKTEST_OUTPUT_DIR / "rotation_compare.md"
    path.write_text(format_markdown(data, amounts), encoding="utf-8")
    print(f"\n报告已保存: {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

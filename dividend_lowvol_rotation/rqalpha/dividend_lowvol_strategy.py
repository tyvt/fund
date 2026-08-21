# -*- coding: utf-8 -*-
"""
红利低波轮动 — RQAlpha 策略（混合架构）。

选股：复用 dividend_lowvol_rotation 流水线
执行：handle_bar 内用当日 bar 价模拟整手再平衡 + order_shares
分红：RQAlpha 入账税前分红，after_trading 按派息日预扣税（与券商一致）
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pandas as pd

_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from rqalpha.apis import (  # noqa: E402
    get_positions,
    logger,
    order_shares,
    update_universe,
)

from dividend_lowvol_rotation.backtest import _collect_candidate_codes  # noqa: E402
from dividend_lowvol_rotation.corporate_actions import (  # noqa: E402
    apply_splits_to_holdings,
    build_split_index,
)
from dividend_lowvol_rotation.config import (  # noqa: E402
    BACKTEST_INITIAL_CAPITAL,
    BACKTEST_OUTPUT_DIR,
    BACKTEST_PREFETCH_SIZE,
    BACKTEST_REBALANCE_MODE,
    TOP_N_BUY,
)
from dividend_lowvol_rotation.rqalpha.bridge import (  # noqa: E402
    compute_rebalance_plan,
    prepare_rqalpha_context,
    resolve_rebalance_portfolio_metrics,
)
from dividend_lowvol_rotation.rqalpha.dividend_tax_sync import (  # noqa: E402
    init_dividend_cash_index,
    pay_dividend_tax_on_date,
)
from dividend_lowvol_rotation.rqalpha.execution_rules import resolve_min_hold_days
from dividend_lowvol_rotation.rqalpha.native_cash_ledger import (  # noqa: E402
    credit_payable_dividend,
    debit_native_cash,
    get_native_cash,
    init_native_cash,
    init_rebalance_anchor,
    rebalance_cash,
    refresh_cash_to_rebalance,
    roll_rebalance_anchor,
    set_native_cash,
)
from dividend_lowvol_rotation.rqalpha.native_rebalance import (  # noqa: E402
    init_dividend_index,
    simulate_native_rebalance,
    sort_share_orders_sell_first,
)
from dividend_lowvol_rotation.rqalpha.symbols_rq import (  # noqa: E402
    from_rqalpha_id,
    to_rqalpha_id,
    to_rqalpha_ids,
)


def _env_str(name: str, default: str) -> str:
    value = os.environ.get(name)
    return value.strip() if value else default


def _env_float(name: str, default: float) -> float:
    value = os.environ.get(name)
    if not value:
        return default
    try:
        return float(value)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    value = os.environ.get(name)
    return int(value) if value else default


def _current_shares(context) -> dict[str, int]:
    out: dict[str, int] = {}
    for pos in get_positions():
        if pos.quantity <= 0:
            continue
        out[from_rqalpha_id(pos.order_book_id)] = int(pos.quantity)
    return out


def _trade_share_base(context) -> dict[str, int]:
    return {
        str(code): int(shares)
        for code, shares in (getattr(context, "dlv_trade_shares", None) or {}).items()
        if shares and int(shares) > 0
    }


def _ledger_shares(context, holdings_rq: dict[str, int] | None = None) -> dict[str, int]:
    """成交股数台账（除权日已就地送股，与原生 lots.shares 同口径）。"""
    base = _trade_share_base(context)
    if base:
        return dict(base)
    if holdings_rq is not None:
        return dict(holdings_rq)
    return _current_shares(context)


def _apply_splits_today(context, today: pd.Timestamp) -> None:
    split_index = getattr(context, "dlv_split_index", None) or {}
    if not split_index:
        return
    if not hasattr(context, "dlv_trade_shares"):
        context.dlv_trade_shares = {}
    apply_splits_to_holdings(context.dlv_trade_shares, split_index, today)


def _record_trade_shares(context, code: str, delta: int) -> None:
    if not hasattr(context, "dlv_trade_shares"):
        context.dlv_trade_shares = {}
    code = str(code)
    delta = int(delta)
    if delta == 0:
        return
    new_sh = int(context.dlv_trade_shares.get(code, 0)) + delta
    if new_sh <= 0:
        context.dlv_trade_shares.pop(code, None)
        if hasattr(context, "dlv_buy_dates"):
            context.dlv_buy_dates.pop(code, None)
    else:
        context.dlv_trade_shares[code] = new_sh


def _holdings_for_metrics(context, holdings: dict[str, int] | None = None) -> dict[str, int]:
    """调仓前估值/仓位缩放：成交台账（除权日已送股）。"""
    return _ledger_shares(context, holdings)


def _holdings_for_dividend(context, holdings: dict[str, int] | None = None) -> dict[str, int]:
    """派息入账股数（除权日送股前快照，由调用方在送股前传入）。"""
    return _ledger_shares(context, holdings)


def _current_weight_map(context, holdings: dict[str, int] | None = None) -> dict[str, float]:
    """与 backtest 调仓前一致：store 收盘价 + 台账股数（非 RQ 市值）。"""
    holdings = holdings or _ledger_shares(context)
    if not holdings:
        return {}
    as_of = pd.Timestamp(context.now).normalize()
    store = context.dlv_bt_ctx.store
    panel = context.dlv_bt_ctx.panel_at(as_of, context.dlv_prefetch_size)
    cash = rebalance_cash(context)
    from dividend_lowvol_rotation.rqalpha.native_rebalance import (
        _trade_price as rebalance_trade_price,
    )

    equity = 0.0
    mv_map: dict[str, float] = {}
    for code, shares in holdings.items():
        if not shares or int(shares) <= 0:
            continue
        metrics = store.metrics_at(code, as_of)
        px = rebalance_trade_price(
            code, panel, as_of, "buy", store, metrics=metrics
        )
        if px is None or px <= 0:
            px = store.price_at(code, as_of)
        if px and px > 0:
            mv = int(shares) * float(px)
            equity += mv
            mv_map[str(code)] = mv
    total = cash + equity
    if total <= 0:
        return {}
    return {c: mv / total for c, mv in mv_map.items()}


def _compute_native_nav(
    context,
    today: pd.Timestamp,
) -> tuple[float, float, int]:
    """与 backtest 一致：round(全精度现金 + 收盘价×股数, 2)。"""
    cash_raw = get_native_cash(context)
    holdings = _ledger_shares(context)
    store = context.dlv_bt_ctx.store
    equity = 0.0
    for code, shares in holdings.items():
        if not shares or int(shares) <= 0:
            continue
        px = store.price_at(str(code), today)
        if px and px > 0:
            equity += int(shares) * float(px)
    port_value = cash_raw + equity
    return port_value, cash_raw, len(holdings)


def _record_native_nav(context, today: pd.Timestamp) -> None:
    port_value, cash_raw, n = _compute_native_nav(context, today)
    row = {
        "date": today.strftime("%Y-%m-%d"),
        "nav": round(port_value, 2),
        "cash": round(cash_raw, 2),
        "holdings_count": n,
    }
    rows = getattr(context, "dlv_native_nav_rows", None)
    if rows is None:
        rows = []
        context.dlv_native_nav_rows = rows
    rows.append(row)
    path = getattr(context, "dlv_native_nav_path", None)
    if path:
        write_header = not getattr(context, "dlv_native_nav_header_written", False)
        mode = "w" if write_header else "a"
        with open(path, mode, encoding="utf-8", newline="") as f:
            if write_header:
                f.write("date,nav,cash,holdings_count\n")
                context.dlv_native_nav_header_written = True
            f.write(
                f"{row['date']},{row['nav']:.2f},{row['cash']:.2f},{row['holdings_count']}\n"
            )


def _sync_buy_dates(context) -> None:
    """仅清理已清仓标的的 buy_date；勿用 today 回填（会破坏红利税持有期）。"""
    if not hasattr(context, "dlv_buy_dates"):
        context.dlv_buy_dates = {}
    active: set[str] = set(_trade_share_base(context))
    for pos in get_positions():
        if pos.quantity <= 0:
            continue
        active.add(from_rqalpha_id(pos.order_book_id))
    for code in list(context.dlv_buy_dates.keys()):
        if code not in active:
            del context.dlv_buy_dates[code]


def _codes_for_rebalance(plan, holdings: dict[str, int]) -> list[str]:
    codes: set[str] = set(holdings)
    if plan:
        codes.update(plan.sell_codes or [])
        codes.update(plan.target_codes or [])
        if plan.buy_pool is not None and not plan.buy_pool.empty and "code" in plan.buy_pool.columns:
            codes.update(plan.buy_pool["code"].astype(str).tolist())
        if plan.ranked is not None and not plan.ranked.empty and "code" in plan.ranked.columns:
            codes.update(plan.ranked["code"].astype(str).tolist())
    return sorted(codes)


def init(context):
    start = _env_str("DLV_RQALPHA_START", "2018-01-01")
    end = _env_str("DLV_RQALPHA_END", "2025-08-01")
    rebalance_mode = _env_str("DLV_RQALPHA_REBALANCE_MODE", BACKTEST_REBALANCE_MODE)
    prefetch_size = _env_int("DLV_RQALPHA_PREFETCH_SIZE", BACKTEST_PREFETCH_SIZE)
    top_n = _env_int("DLV_RQALPHA_TOP_N", TOP_N_BUY)
    min_hold = resolve_min_hold_days(rebalance_mode)

    logger.info(
        f"RQAlpha 红利低波：{start} ~ {end}，调仓={rebalance_mode}，"
        f"Top {top_n}，最短持有 {min_hold} 天，bar 价整手再平衡（实盘口径）"
    )

    bt_ctx, reb_dates = prepare_rqalpha_context(
        start,
        end,
        prefetch_size=prefetch_size,
        rebalance_mode=rebalance_mode,
        verbose=True,
    )
    reb_set = {d.normalize().date() for d in reb_dates}

    context.dlv_start = start
    context.dlv_end = end
    context.dlv_top_n = top_n
    context.dlv_prefetch_size = prefetch_size
    context.dlv_rebalance_mode = rebalance_mode
    context.dlv_min_hold_days = min_hold
    context.dlv_bt_ctx = bt_ctx
    context.dlv_split_index = build_split_index(bt_ctx.split_records)
    context.dlv_rebalance_dates = reb_set
    context.dlv_div_index = init_dividend_index(bt_ctx)
    context.dlv_div_cash_index = init_dividend_cash_index(bt_ctx.dividend_cash_records)
    context.dlv_prev_rebalance = None
    context.dlv_buy_dates = {}
    context.dlv_trade_shares = {}
    context.dlv_pending_plan = None
    context.dlv_rebalanced_today = False
    context.dlv_native_nav_rows = []
    nav_name = os.environ.get("DLV_RQALPHA_NATIVE_NAV_PATH", "rqalpha_native_nav.csv")
    nav_path = Path(nav_name) if os.path.isabs(nav_name) else BACKTEST_OUTPUT_DIR / nav_name
    nav_path.parent.mkdir(parents=True, exist_ok=True)
    if nav_path.exists():
        nav_path.unlink()
    context.dlv_native_nav_path = str(nav_path)
    context.dlv_native_nav_header_written = False
    init_native_cash(
        context,
        _env_float("DLV_RQALPHA_CAPITAL", BACKTEST_INITIAL_CAPITAL),
    )
    anchor_day = (
        pd.Timestamp(bt_ctx.calendar[0]).normalize()
        if bt_ctx.calendar
        else pd.Timestamp(start).normalize()
    )
    init_rebalance_anchor(
        context,
        _env_float("DLV_RQALPHA_CAPITAL", BACKTEST_INITIAL_CAPITAL),
        anchor_day,
    )

    # 首日即调仓时 before_trading 订阅来不及进 bar_dict，启动时一次性订阅候选池
    try:
        candidates = _collect_candidate_codes(
            bt_ctx.records,
            list(reb_dates),
            prefetch_size,
            ctx=bt_ctx,
        )
        held = [from_rqalpha_id(p.order_book_id) for p in get_positions() if p.quantity > 0]
        universe = to_rqalpha_ids(list(dict.fromkeys([*candidates, *held])))
        if universe:
            update_universe(universe)
    except Exception as exc:
        logger.warn(f"预订阅 universe 失败: {exc}")


def before_trading(context):
    _sync_buy_dates(context)
    context.dlv_rebalanced_today = False
    context.dlv_pending_plan = None

    today = pd.Timestamp(context.now).normalize()
    if today.date() not in context.dlv_rebalance_dates:
        return

    holdings_rq = _current_shares(context)
    holdings = _holdings_for_metrics(context, holdings_rq)
    current_weights = _current_weight_map(context, holdings)
    plan_stub = compute_rebalance_plan(
        context.dlv_bt_ctx,
        today,
        current_weights=current_weights,
        current_shares=holdings,
        top_n=context.dlv_top_n,
        prefetch_size=context.dlv_prefetch_size,
        rebalance_mode=context.dlv_rebalance_mode,
    )
    universe = set(to_rqalpha_ids(_codes_for_rebalance(plan_stub, holdings)))
    if plan_stub.buy_pool is not None and not plan_stub.buy_pool.empty:
        universe.update(to_rqalpha_ids(plan_stub.buy_pool["code"].astype(str).tolist()))
    if plan_stub.ranked is not None and not plan_stub.ranked.empty:
        universe.update(to_rqalpha_ids(plan_stub.ranked["code"].astype(str).tolist()))
    for pos in get_positions():
        if pos.quantity > 0:
            universe.add(pos.order_book_id)
    if universe:
        update_universe(list(universe))


def _settle_dividends_once(
    context,
    today: pd.Timestamp,
    holdings_rq: dict[str, int],
) -> float:
    share_pre_split = dict(_ledger_shares(context, holdings_rq))
    credit_payable_dividend(
        context,
        positions=get_positions(),
        buy_dates=context.dlv_buy_dates,
        code_from_obid=from_rqalpha_id,
        as_of=today,
        share_override=share_pre_split,
    )
    _apply_splits_today(context, today)
    share_post_split = dict(_ledger_shares(context, holdings_rq))
    return pay_dividend_tax_on_date(
        context,
        positions=get_positions(),
        buy_dates=context.dlv_buy_dates,
        code_from_obid=from_rqalpha_id,
        as_of=today,
        share_override=share_post_split,
    )


def _settle_dividends_today(context) -> float:
    """派息入账 + 红利税预扣，须在当日成交前完成（与原生 backtest 一致）。

    除权日顺序：先按除权前股数入账派息 → 再按除权后股数预扣红利税
    （对应 backtest：_credit_dividends → _apply_splits → _pay_dividend_tax）。

    每个交易日仅执行一次（含调仓日；原生在 rb_date 与 inter_days 互斥，不重复）。
    """
    today = pd.Timestamp(context.now).normalize()
    holdings_rq = _current_shares(context)
    tax = _settle_dividends_once(context, today, holdings_rq)
    if tax > 0:
        debit_native_cash(context, float(tax))
        logger.info(f"{today.date()} 派息日红利税 -{tax:,.2f}")
    return tax


def _prev_trading_day(context, today: pd.Timestamp) -> pd.Timestamp | None:
    cal = getattr(getattr(context, "dlv_bt_ctx", None), "calendar", None) or []
    today = pd.Timestamp(today).normalize()
    try:
        idx = next(i for i, d in enumerate(cal) if pd.Timestamp(d).normalize() == today)
    except StopIteration:
        return None
    if idx <= 0:
        return None
    return pd.Timestamp(cal[idx - 1]).normalize()


def handle_bar(context, bar_dict):
    today = pd.Timestamp(context.now).normalize()
    is_rb = today.date() in context.dlv_rebalance_dates
    prev_day = _prev_trading_day(context, today)

    # 调仓日：refresh 至昨日 + 当日增量 settle（与 backtest rb_date 顺序一致，避免同日派息漏计）
    if is_rb and prev_day is not None:
        refresh_cash_to_rebalance(context, prev_day)
        holdings_rq = _current_shares(context)
        tax = _settle_dividends_once(context, today, holdings_rq)
        if tax > 0:
            debit_native_cash(context, float(tax))
            logger.info(f"{today.date()} 派息日红利税 -{tax:,.2f}")
    else:
        refresh_cash_to_rebalance(context, today)
        holdings_rq = _current_shares(context)
        share_post = dict(_ledger_shares(context, holdings_rq))
        tax = pay_dividend_tax_on_date(
            context,
            positions=get_positions(),
            buy_dates=context.dlv_buy_dates,
            code_from_obid=from_rqalpha_id,
            as_of=today,
            share_override=share_post,
        )
        if tax > 0:
            logger.info(f"{today.date()} 派息日红利税 -{tax:,.2f}")

    if today.date() not in context.dlv_rebalance_dates:
        return
    if context.dlv_rebalanced_today:
        return

    _sync_buy_dates(context)
    holdings_rq = _current_shares(context)
    holdings = _holdings_for_metrics(context, holdings_rq)
    current_weights = _current_weight_map(context, holdings)
    plan = compute_rebalance_plan(
        context.dlv_bt_ctx,
        today,
        current_weights=current_weights,
        current_shares=holdings,
        top_n=context.dlv_top_n,
        prefetch_size=context.dlv_prefetch_size,
        rebalance_mode=context.dlv_rebalance_mode,
    )
    if not plan.target_codes and not plan.sell_codes:
        logger.info(f"{today.date()} 无调仓目标")
        context.dlv_prev_rebalance = today
        context.dlv_rebalanced_today = True
        return

    codes = _codes_for_rebalance(plan, holdings_rq)
    cash = rebalance_cash(context)

    debug_date = os.environ.get("DLV_DEBUG_CASH_DATE")
    if debug_date and today.date() == pd.Timestamp(debug_date).date():
        logger.info(
            f"[DEBUG] {today.date()} rebalance_cash={cash:,.2f} "
            f"holdings={len(holdings)} ledger={sorted(holdings.items())[:3]}..."
        )

    port_value, position_scale, scale_notes = resolve_rebalance_portfolio_metrics(
        context.dlv_bt_ctx,
        holdings,
        cash,
        plan.panel,
        today,
        rebalance_mode=context.dlv_rebalance_mode,
    )
    plan.position_scale = position_scale
    for note in scale_notes[:2]:
        if note not in plan.notes:
            plan.notes.append(note)

    # 调仓模拟与原生 backtest 一致：store 收盘价整手再平衡（非 bar 价）
    target_shares, sim_cash, sim_notes, share_orders, sim_lots = simulate_native_rebalance(
        context.dlv_bt_ctx,
        plan,
        holdings=holdings,
        buy_dates=context.dlv_buy_dates,
        cash=cash,
        top_n=context.dlv_top_n,
        min_hold_days=context.dlv_min_hold_days,
        dividend_topup=0.0,
        price_map=None,
        port_value_override=port_value if port_value > 0 else None,
        position_scale_override=position_scale,
    )
    set_native_cash(context, sim_cash, as_of=today)
    context.dlv_last_sim_lots = sim_lots
    # 调仓后以 simulate 结果为准覆盖台账（清除历史残留标的，避免 holdings_count 漂移）
    context.dlv_trade_shares = {
        str(code): int(getattr(lot, "shares", 0) or 0)
        for code, lot in sim_lots.items()
        if int(getattr(lot, "shares", 0) or 0) > 0
    }
    for code, lot in sim_lots.items():
        context.dlv_buy_dates[str(code)] = pd.Timestamp(lot.buy_date).normalize()

    share_orders = sort_share_orders_sell_first(share_orders)
    for note in sim_notes[:4]:
        logger.info(f"  模拟: {note}")
    for order in share_orders[:4]:
        side = "买" if order.delta_shares > 0 else "卖"
        logger.info(f"  计划{side} {order.code} {abs(order.delta_shares)}股")

    # 按计划股数直接下单：simulate_native_rebalance 已按原生逻辑逐笔校验现金/整手
    skipped = 0
    for order in share_orders:
        obid = to_rqalpha_id(order.code)
        if not obid or order.delta_shares == 0:
            continue
        shares = int(order.delta_shares)
        if shares == 0:
            skipped += 1
            continue
        order_shares(obid, shares)
        if shares > 0:
            context.dlv_buy_dates[order.code] = today

    context.dlv_pending_plan = plan
    context.dlv_rebalanced_today = True
    scale = plan.position_scale * 100 if plan else 100
    logger.info(
        f"{today.date()} 整手调仓：{len(share_orders)} 笔计划"
        f"{f'，跳过 {skipped} 笔' if skipped else ''}，模拟现金 {sim_cash:,.0f}，"
        f"仓位缩放 {scale:.0f}%"
    )


def after_trading(context):
    _sync_buy_dates(context)
    today = pd.Timestamp(context.now).normalize()
    _record_native_nav(context, today)
    if context.dlv_rebalanced_today and getattr(context, "dlv_last_sim_lots", None):
        roll_rebalance_anchor(
            context, today, lots=context.dlv_last_sim_lots, cash=get_native_cash(context)
        )
        context.dlv_last_sim_lots = None
    else:
        roll_rebalance_anchor(context, today)

    if context.dlv_rebalanced_today:
        total = float(context.portfolio.total_value or BACKTEST_INITIAL_CAPITAL)
        cash = float(context.portfolio.cash or 0)
        held = len([p for p in get_positions() if p.quantity > 0])
        logger.info(f"调仓后净值 {total:.2f}，现金 {cash:.0f}，持仓 {held} 只")
        plan = context.dlv_pending_plan
        if plan and plan.notes:
            for note in plan.notes[:3]:
                logger.info(f"  {note}")
        context.dlv_prev_rebalance = today
        context.dlv_pending_plan = None

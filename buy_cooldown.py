"""买入信号冷却期：避免连续交易日重复触发。"""

from __future__ import annotations

import pandas as pd

from config import (
    BUY_COOLDOWN_AMOUNT_LOOKBACK_DAYS,
    BUY_COOLDOWN_AMOUNT_MAX_MULTIPLIER,
    BUY_COOLDOWN_AMOUNT_SCALE_ENABLED,
    BUY_COOLDOWN_DAYS,
    BUY_COOLDOWN_DROP_OVERRIDE_ENABLED,
    BUY_COOLDOWN_DROP_OVERRIDE_PCT,
    buy_cooldown_enabled,
)


def _drop_since_last_buy(snapshot: dict) -> float | None:
    """自上次买入信号价以来的跌幅（正数表示下跌）。"""
    close = snapshot.get("close")
    last_price = snapshot.get("last_buy_signal_price")
    if last_price is None:
        last_price = resolve_last_buy_signal_price(snapshot)
    if close is None or last_price is None or float(last_price) <= 0:
        return None
    return (float(last_price) - float(close)) / float(last_price)


def drop_override_active(snapshot: dict) -> bool:
    """冷却期内价格跌幅达阈值时解除冷却。"""
    if not BUY_COOLDOWN_DROP_OVERRIDE_ENABLED:
        return False
    drop = _drop_since_last_buy(snapshot)
    if drop is None:
        return False
    return drop >= BUY_COOLDOWN_DROP_OVERRIDE_PCT


def format_drop_override_reason(snapshot: dict) -> str:
    drop = _drop_since_last_buy(snapshot)
    if drop is None:
        return "价格跌幅触发冷却解除"
    return (
        f"冷却已解除：自上次买入信号价下跌 {drop * 100:.1f}%"
        f"（阈值 {BUY_COOLDOWN_DROP_OVERRIDE_PCT * 100:.1f}%）"
    )


def check_cooldown(
    days_since_last_buy: int | float | None,
    *,
    snapshot: dict | None = None,
    index_code: str | None = None,
    cooldown_days: int | None = None,
) -> tuple[bool, int | None, str | None]:
    """返回 (冷却期已过, 剩余冷却交易日数, 解除原因)。"""
    code = index_code or (snapshot.get("code") if snapshot else None)
    if not buy_cooldown_enabled(code):
        return True, None, None
    cd = cooldown_days if cooldown_days is not None else BUY_COOLDOWN_DAYS
    if cd <= 0:
        return True, None, None
    if days_since_last_buy is None:
        return True, None, None
    days = int(days_since_last_buy)
    if days >= cd:
        return True, None, None
    if snapshot is not None and drop_override_active(snapshot):
        return True, None, format_drop_override_reason(snapshot)
    return False, cd - days, None


def apply_cooldown_to_mask(
    mask: pd.Series,
    prices: pd.Series | None = None,
    *,
    index_code: str | None = None,
    cooldown_days: int | None = None,
    drop_override_pct: float | None = None,
) -> pd.Series:
    """对布尔买入序列应用冷却期；大跌时可解除（回测用）。"""
    if not buy_cooldown_enabled(index_code):
        return mask
    cd = cooldown_days if cooldown_days is not None else BUY_COOLDOWN_DAYS
    drop_pct = drop_override_pct
    if drop_pct is None:
        drop_pct = (
            BUY_COOLDOWN_DROP_OVERRIDE_PCT
            if BUY_COOLDOWN_DROP_OVERRIDE_ENABLED
            else 0.0
        )
    if not buy_cooldown_enabled(index_code) or cd <= 0 or mask is None or mask.empty:
        return mask

    result = pd.Series(False, index=mask.index)
    last_buy_idx: int | None = None
    last_buy_price: float | None = None

    for i in range(len(mask)):
        if not bool(mask.iloc[i]):
            continue
        in_cooldown = last_buy_idx is not None and (i - last_buy_idx) < cd
        if in_cooldown:
            override = False
            if (
                drop_pct > 0
                and prices is not None
                and last_buy_price is not None
                and last_buy_price > 0
            ):
                cur = float(prices.iloc[i])
                if (last_buy_price - cur) / last_buy_price >= drop_pct:
                    override = True
            if override:
                result.iloc[i] = True
                last_buy_idx = i
                last_buy_price = float(prices.iloc[i]) if prices is not None else None
            else:
                result.iloc[i] = False
        else:
            result.iloc[i] = True
            last_buy_idx = i
            if prices is not None:
                last_buy_price = float(prices.iloc[i])
    return result


def resolve_last_buy_signal_price(
    snapshot: dict,
    *,
    buy_eval_fn=None,
    row_snapshot_fn=None,
    close_col: str = "close",
    date_col: str = "date",
) -> float | None:
    """从 snapshot 或历史面板获取最近一次买入信号日收盘价。"""
    price = snapshot.get("last_buy_signal_price")
    if price is not None:
        return float(price)

    panel = snapshot.get("panel")
    if panel is None or getattr(panel, "empty", True):
        return None

    from sell_trailing import (
        BUY_SIGNAL_COL,
        attach_buy_signal_column,
        last_buy_signal_price_from_column,
    )

    work = panel
    if buy_eval_fn is not None and BUY_SIGNAL_COL not in panel.columns:
        work = attach_buy_signal_column(
            panel, buy_eval_fn, row_snapshot_fn, col=BUY_SIGNAL_COL
        )

    col = BUY_SIGNAL_COL if BUY_SIGNAL_COL in work.columns else None
    if col is None:
        return None
    return last_buy_signal_price_from_column(work, col=col, close_col=close_col)


def resolve_days_since_last_buy(
    snapshot: dict,
    *,
    buy_eval_fn=None,
    row_snapshot_fn=None,
    date_col: str = "date",
) -> int | None:
    """从 snapshot 或历史面板推算距上次买入信号日的交易日数。"""
    days = snapshot.get("days_since_last_buy")
    if days is not None:
        return int(days)

    panel = snapshot.get("panel")
    if panel is None or getattr(panel, "empty", True):
        return None

    from sell_trailing import (
        BUY_SIGNAL_COL,
        attach_buy_signal_column,
        last_buy_date_from_column,
    )

    work = panel
    if buy_eval_fn is not None and BUY_SIGNAL_COL not in panel.columns:
        work = attach_buy_signal_column(
            panel, buy_eval_fn, row_snapshot_fn, col=BUY_SIGNAL_COL
        )

    col = BUY_SIGNAL_COL if BUY_SIGNAL_COL in work.columns else None
    if col is None:
        return None

    last_date = last_buy_date_from_column(work, col=col, date_col=date_col)
    if last_date is None:
        return None

    data_date = (
        snapshot.get("data_date") or snapshot.get("date") or snapshot.get("index_date")
    )
    if data_date is None:
        return None
    try:
        delta = pd.Timestamp(data_date) - pd.Timestamp(last_date)
        return int(delta.days)
    except (TypeError, ValueError):
        return None


def _raw_buy_mask_from_panel(
    panel: pd.DataFrame,
    *,
    buy_eval_fn=None,
    row_snapshot_fn=None,
    buy_mask_fn=None,
    close_col: str = "close",
) -> pd.Series | None:
    from sell_trailing import BUY_SIGNAL_COL, attach_buy_signal_column

    if buy_mask_fn is not None:
        mask = buy_mask_fn(panel)
        if mask is not None and not mask.empty:
            return mask.astype(bool)
    if buy_eval_fn is None:
        return None
    work = panel
    if BUY_SIGNAL_COL not in panel.columns:
        work = attach_buy_signal_column(
            panel, buy_eval_fn, row_snapshot_fn, col=BUY_SIGNAL_COL
        )
    if BUY_SIGNAL_COL not in work.columns:
        return None
    return work[BUY_SIGNAL_COL].astype(bool)


def estimate_cooldown_amount_multiplier(
    snapshot: dict,
    *,
    buy_eval_fn=None,
    row_snapshot_fn=None,
    buy_mask_fn=None,
    close_col: str = "close",
    lookback_days: int | None = None,
    index_code: str | None = None,
) -> float:
    """按历史买入频次压缩比上调单次金额，保持资金利用率。"""
    code = index_code or snapshot.get("code")
    if not buy_cooldown_enabled(code):
        return 1.0
    if not BUY_COOLDOWN_AMOUNT_SCALE_ENABLED:
        return 1.0

    panel = snapshot.get("panel")
    if panel is None or getattr(panel, "empty", True):
        return 1.0

    lb = lookback_days or BUY_COOLDOWN_AMOUNT_LOOKBACK_DAYS
    work = panel.tail(lb)
    if len(work) < 30:
        return 1.0

    raw = _raw_buy_mask_from_panel(
        work,
        buy_eval_fn=buy_eval_fn,
        row_snapshot_fn=row_snapshot_fn,
        buy_mask_fn=buy_mask_fn,
        close_col=close_col,
    )
    if raw is None or not raw.any():
        return 1.0

    prices = work[close_col] if close_col in work.columns else None
    cooled = apply_cooldown_to_mask(raw, prices=prices)
    raw_n = max(int(raw.sum()), 1)
    cooled_n = max(int(cooled.sum()), 1)
    ratio = raw_n / cooled_n
    return min(BUY_COOLDOWN_AMOUNT_MAX_MULTIPLIER, max(1.0, ratio))

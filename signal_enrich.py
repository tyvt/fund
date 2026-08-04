"""信号后处理：强度评分、冷却期、分级标签。"""

from __future__ import annotations

from buy_cooldown import (
    check_cooldown,
    estimate_cooldown_amount_multiplier,
    resolve_days_since_last_buy,
    resolve_last_buy_signal_price,
)
from config import (
    BUY_COOLDOWN_DAYS,
    BUY_COOLDOWN_DROP_OVERRIDE_PCT,
    SIGNAL_NEAR_BUY_MARGIN_PCT,
    SIGNAL_STRENGTH_ELIGIBLE_MIN,
    SIGNAL_STRENGTH_STRONG_MIN,
)
from signal_format import (
    SIGNAL_BUY,
    SIGNAL_ELIGIBLE,
    SIGNAL_HOLD,
    SIGNAL_NEAR_BUY,
    SIGNAL_SELL,
    strength_tier_label,
)
from signal_scoring import compute_signal_strength, detect_near_buy


def enrich_signal_eval(
    snapshot: dict,
    signal_eval: dict,
    *,
    buy_eval_fn=None,
    row_snapshot_fn=None,
    date_col: str = "date",
) -> dict:
    """附加强度分、冷却期与信号分级；更新 is_buy 为可执行买入。"""
    out = dict(signal_eval)
    is_sell = bool(out.get("is_sell"))

    criteria_met = bool(out.get("criteria_met", out.get("is_buy", False)))
    out["criteria_met"] = criteria_met

    criteria = [c for c in out.get("criteria", []) if c.get("applicable", True)]
    out["score"] = out.get("score", sum(1 for c in criteria if c.get("passed")))
    out["total"] = out.get("total", len(criteria))

    strength = compute_signal_strength(snapshot, out)
    out["signal_strength"] = strength
    out["strength_tier"] = strength_tier_label(strength)

    days_since = resolve_days_since_last_buy(
        snapshot,
        buy_eval_fn=buy_eval_fn,
        row_snapshot_fn=row_snapshot_fn,
        date_col=date_col,
    )
    last_buy_price = resolve_last_buy_signal_price(
        snapshot,
        buy_eval_fn=buy_eval_fn,
        row_snapshot_fn=row_snapshot_fn,
        date_col=date_col,
    )
    if last_buy_price is not None:
        out["last_buy_signal_price"] = last_buy_price

    cooldown_ok, days_left, override_reason = check_cooldown(
        days_since, snapshot=snapshot
    )
    out["cooldown_ok"] = cooldown_ok
    if days_left is not None:
        out["cooldown_days_left"] = days_left
    if override_reason:
        out["cooldown_override"] = True
        out["cooldown_note"] = override_reason

    amount_mult = estimate_cooldown_amount_multiplier(
        snapshot,
        buy_eval_fn=buy_eval_fn,
        row_snapshot_fn=row_snapshot_fn,
        index_code=snapshot.get("code"),
    )
    out["cooldown_amount_multiplier"] = amount_mult

    near_buy = detect_near_buy(
        {**out, "criteria_met": criteria_met},
        margin_pct=SIGNAL_NEAR_BUY_MARGIN_PCT,
    )
    out["near_buy"] = near_buy

    if is_sell:
        out["signal_short"] = SIGNAL_SELL
        out["is_buy"] = False
        return out

    strong = (
        criteria_met
        and cooldown_ok
        and strength >= SIGNAL_STRENGTH_STRONG_MIN
    )
    eligible = criteria_met and (
        not cooldown_ok or strength >= SIGNAL_STRENGTH_ELIGIBLE_MIN
    )

    if strong:
        out["signal_short"] = SIGNAL_BUY
        out["is_buy"] = True
    elif eligible:
        out["signal_short"] = SIGNAL_ELIGIBLE
        out["is_buy"] = False
        if not cooldown_ok:
            out["cooldown_note"] = (
                f"条件已达标，冷却期中（距上次买入 {days_since} 日，"
                f"需间隔 {BUY_COOLDOWN_DAYS} 日；"
                f"跌超 {BUY_COOLDOWN_DROP_OVERRIDE_PCT * 100:.0f}% 可提前解除）"
            )
        elif strength < SIGNAL_STRENGTH_STRONG_MIN:
            out["strength_note"] = (
                f"条件已达标，强度 {strength} 分（建议≥{SIGNAL_STRENGTH_STRONG_MIN}）"
            )
    elif near_buy:
        out["signal_short"] = SIGNAL_NEAR_BUY
        out["is_buy"] = False
    else:
        out["signal_short"] = SIGNAL_HOLD
        out["is_buy"] = False

    return out


def build_section_dict(snapshot: dict, signal_eval: dict, lines: list[str]) -> dict:
    """构造含对比表所需字段的 section 字典。"""
    return {
        "code": snapshot.get("code"),
        "name": snapshot.get("name"),
        "text": "\n".join(lines),
        "signal_short": signal_eval.get("signal_short"),
        "signal_strength": signal_eval.get("signal_strength"),
        "strength_tier": signal_eval.get("strength_tier"),
        "criteria_met": signal_eval.get("criteria_met"),
        "score": signal_eval.get("score"),
        "total": signal_eval.get("total"),
        "snapshot": snapshot,
    }

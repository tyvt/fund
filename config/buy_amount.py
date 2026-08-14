"""买入金额配置。"""

from config.env import (
    _env_bool,
    _env_float,
    _env_float_any,
    _env_int,
    _env_int_any,
    _build_annual_investment_budget_by_year,
)
from config.indices import CN_BROAD_INDICES, INDICES

# --- 回测/实盘单次买入金额（元）---
# 默认各指数统一基准 100；实际买入 = 基准 × 当日涨跌系数（见 buy_amount_change.py）
DIVIDEND_BUY_AMOUNT = _env_float("DIVIDEND_BUY_AMOUNT", 100)
CN_BROAD_BUY_AMOUNT = _env_float("CN_BROAD_BUY_AMOUNT", 100)
BACKTEST_OTHER_BUY_AMOUNT = _env_float("BACKTEST_OTHER_BUY_AMOUNT", 100)

# --- 投入预算：剩余可用额度（实盘展示用；默认不缩放回测单笔金额）---
REMAINING_INVESTMENT_BUDGET = _env_float_any(
    ("REMAINING_INVESTMENT_BUDGET", "ANNUAL_INVESTMENT_BUDGET"),
    50_000,
)
ANNUAL_INVESTMENT_TARGET = _env_float("ANNUAL_INVESTMENT_TARGET", 0)
ANNUAL_INVESTMENT_BUDGET = REMAINING_INVESTMENT_BUDGET
BUY_AMOUNT_REFERENCE_ANNUAL_BUDGET = _env_float(
    "BUY_AMOUNT_REFERENCE_ANNUAL_BUDGET", 72_577
)
ANNUAL_INVESTMENT_BUDGET_ENABLED = _env_bool("ANNUAL_INVESTMENT_BUDGET_ENABLED", True)
ANNUAL_INVESTMENT_BUDGET_BY_YEAR = _build_annual_investment_budget_by_year()

# --- 买入金额：按当日涨跌比例缩放（跌多买多，反弹少买）---
# amount = base * clamp(1 - sensitivity * daily_change_pct, min_mult, max_mult)
BUY_AMOUNT_CHANGE_SCALE_ENABLED = _env_bool("BUY_AMOUNT_CHANGE_SCALE_ENABLED", True)
BUY_AMOUNT_CHANGE_SENSITIVITY = _env_float("BUY_AMOUNT_CHANGE_SENSITIVITY", 10.0)
BUY_AMOUNT_CHANGE_MIN_MULT = _env_float("BUY_AMOUNT_CHANGE_MIN_MULT", 0.5)
BUY_AMOUNT_CHANGE_MAX_MULT = _env_float("BUY_AMOUNT_CHANGE_MAX_MULT", 2.0)
BUY_AMOUNT_POSITION_ALLOC_ENABLED = _env_bool(
    "BUY_AMOUNT_POSITION_ALLOC_ENABLED", True
)
BUY_AMOUNT_RANKING_ENABLED = _env_bool("BUY_AMOUNT_RANKING_ENABLED", False)
BUY_AMOUNT_RETURN_MAX = _env_bool("BUY_AMOUNT_RETURN_MAX", True)

# 各指数基准单次买入（元）；可用 {代码}_BUY_AMOUNT 覆盖
BUY_AMOUNT_DEFAULT = _env_float("BUY_AMOUNT_DEFAULT", 100)
BUY_AMOUNT_BASE_BY_CODE = {
    "NDX": _env_float("NDX_BUY_AMOUNT", BUY_AMOUNT_DEFAULT),
    "SPX": _env_float("SPX_BUY_AMOUNT", BUY_AMOUNT_DEFAULT),
    "399006": _env_float("399006_BUY_AMOUNT", BUY_AMOUNT_DEFAULT),
    "H30269": _env_float("H30269_BUY_AMOUNT", BUY_AMOUNT_DEFAULT),
    "000688": _env_float("000688_BUY_AMOUNT", BUY_AMOUNT_DEFAULT),
    "000852": _env_float("000852_BUY_AMOUNT", BUY_AMOUNT_DEFAULT),
}


def _env_buy_amount_for_code(code, default):
    return _env_float_any((f"{code}_BUY_AMOUNT", f"BACKTEST_{code}_BUY_AMOUNT"), default)


def get_buy_amount_reference(index_code):
    """参考基准金额（未按涨跌缩放）。"""
    if BUY_AMOUNT_POSITION_ALLOC_ENABLED:
        from buy_amount_allocation import get_position_allocation

        alloc = get_position_allocation()
        ref = alloc["reference_by_code"].get(index_code)
        if ref is not None:
            return _env_buy_amount_for_code(index_code, float(ref))
    if BUY_AMOUNT_RANKING_ENABLED:
        from buy_amount_ranking import get_ranking_allocation

        alloc = get_ranking_allocation()
        ref = alloc["reference_by_code"].get(index_code)
        if ref is not None:
            return _env_buy_amount_for_code(index_code, float(ref))
    default = BUY_AMOUNT_BASE_BY_CODE.get(index_code, BUY_AMOUNT_DEFAULT)
    return _env_buy_amount_for_code(index_code, default)


def is_index_recommended(index_code):
    """当前是否参与买入额度分配（有正数基准金额即视为可买）。"""
    return get_buy_amount_reference(index_code) > 0


def get_buy_amount_base(index_code, year=None):
    """单只指数基准单次买入金额（元）；启用年度预算时按当年总投入缩放。"""
    ref = get_buy_amount_reference(index_code)
    if BUY_AMOUNT_POSITION_ALLOC_ENABLED:
        return ref
    if not ANNUAL_INVESTMENT_BUDGET_ENABLED:
        return ref
    from buy_amount_budget import get_scaled_buy_amount_base

    return get_scaled_buy_amount_base(index_code, year)


def get_backtest_buy_amount(index_code, amounts=None):
    """读取单只指数单次买入金额（元）；0 表示不买入。"""
    if amounts is not None:
        by_code = amounts.get("by_code")
        if by_code is not None and index_code in by_code:
            return float(by_code[index_code])
        if amounts.get("unified"):
            return float(amounts["dividend"])
        if index_code in {i["code"] for i in INDICES}:
            return float(amounts.get("dividend", DIVIDEND_BUY_AMOUNT))
        if index_code in {i["code"] for i in CN_BROAD_INDICES}:
            return float(amounts.get("cn_broad", CN_BROAD_BUY_AMOUNT))
        return float(amounts.get("other", BACKTEST_OTHER_BUY_AMOUNT))
    return get_buy_amount_base(index_code)


def get_chart_buy_amount(index_code, amounts=None):
    """HTML 图表用单次买入金额。"""
    amt = get_backtest_buy_amount(index_code, amounts)
    if amt > 0:
        return amt
    if amounts is not None:
        by_code = amounts.get("by_code")
        if by_code is not None:
            return 0.0
    return get_buy_amount_base(index_code)


def _static_buy_amount_by_code():
    """回测用固定基准金额（不读取收益率排名，避免未来函数）。"""
    from buy_amount_config import ALL_BUY_INDEX_CODES

    return {
        code: _env_buy_amount_for_code(
            code, BUY_AMOUNT_BASE_BY_CODE.get(code, BUY_AMOUNT_DEFAULT)
        )
        for code in ALL_BUY_INDEX_CODES
    }


def resolve_backtest_amounts(
    unified_amount=None,
    return_max_mode=None,
    ranking_mode=None,
    change_scale=None,
    tier_enabled=None,
    portfolio_mode=False,
    panels=None,
    position_alloc_mode=None,
):
    """回测买入金额。默认位置分配 + 年度预算 + 涨跌缩放。"""

    if change_scale is None:
        if tier_enabled is not None:
            change_scale = bool(tier_enabled) and BUY_AMOUNT_CHANGE_SCALE_ENABLED
        else:
            change_scale = BUY_AMOUNT_CHANGE_SCALE_ENABLED

    def _pack(by_code=None, **extra):
        base = {
            "dividend": DIVIDEND_BUY_AMOUNT,
            "cn_broad": CN_BROAD_BUY_AMOUNT,
            "other": BACKTEST_OTHER_BUY_AMOUNT,
            "unified": False,
            "portfolio": False,
            "ranking": False,
            "position_alloc": False,
            "return_max": False,
            "by_code": by_code,
            "change_scale": change_scale,
            "tier_scheme": None,
            "tier_normalize": False,
            "annual_budget": ANNUAL_INVESTMENT_BUDGET_ENABLED,
            "annual_budget_default": ANNUAL_INVESTMENT_BUDGET,
            "reference_annual_budget": BUY_AMOUNT_REFERENCE_ANNUAL_BUDGET,
        }
        base.update(extra)
        return base

    if unified_amount is not None and unified_amount > 0:
        return _pack(
            unified=True,
            dividend=float(unified_amount),
            cn_broad=float(unified_amount),
            other=float(unified_amount),
        )
    if ranking_mode:
        from buy_amount_ranking import get_ranking_allocation

        alloc = get_ranking_allocation()
        return _pack(
            by_code=dict(alloc["by_code"]),
            ranking=True,
            reference_by_code=dict(alloc["reference_by_code"]),
            excluded_codes=alloc["excluded_codes"],
            ranking_rows=alloc["rows"],
            ranking_as_of=alloc.get("as_of"),
        )
    use_position = (
        BUY_AMOUNT_POSITION_ALLOC_ENABLED
        if position_alloc_mode is None
        else bool(position_alloc_mode)
    )
    if use_position:
        from buy_amount_allocation import compute_backtest_position_allocation

        alloc = compute_backtest_position_allocation(panels=panels)
        return _pack(
            by_code=dict(alloc["by_code"]),
            position_alloc=True,
            reference_by_code=dict(alloc["reference_by_code"]),
            allocation_rows=alloc.get("rows"),
            allocation_as_of=alloc.get("as_of"),
        )
    if return_max_mode is None:
        return_max_mode = BUY_AMOUNT_RETURN_MAX
    if return_max_mode:
        by_code = _static_buy_amount_by_code()
        return _pack(
            by_code=by_code,
            return_max=True,
            reference_by_code=dict(by_code),
        )
    return _pack()


def format_backtest_amount_note(amounts):
    """Markdown/控制台用的买入金额说明。"""
    if amounts is None:
        return "仅统计次数"
    change = amounts.get("change_scale", BUY_AMOUNT_CHANGE_SCALE_ENABLED)
    change_suffix = (
        f" + 涨跌缩放（敏感度 {BUY_AMOUNT_CHANGE_SENSITIVITY:g}，"
        f"{BUY_AMOUNT_CHANGE_MIN_MULT:g}–{BUY_AMOUNT_CHANGE_MAX_MULT:g}×）"
        if change
        else ""
    )
    if amounts.get("unified"):
        return f"每次买入 **{amounts['dividend']:.0f}** 元{change_suffix}"
    if amounts.get("ranking") and amounts.get("by_code"):
        active = {c: a for c, a in amounts["by_code"].items() if a and a > 0}
        parts = [f"{c} **{a:.0f}**" for c, a in sorted(active.items())]
        from buy_amount_ranking import format_ranking_note

        head = format_ranking_note(
            {
                "rows": amounts.get("ranking_rows") or [],
                "excluded_codes": amounts.get("excluded_codes") or frozenset(),
                "exclude_bottom_n": len(amounts.get("excluded_codes") or ()),
                "as_of": amounts.get("ranking_as_of"),
            }
        )
        return f"{head}{change_suffix}：{'；'.join(parts)}"
    if amounts.get("position_alloc") and amounts.get("by_code"):
        active = {c: a for c, a in amounts["by_code"].items() if a and a > 0}
        parts = [f"{c} **{a:.0f}**" for c, a in sorted(active.items())]
        from buy_amount_allocation import format_allocation_note

        head = "位置分配（回测按历史买入日低位程度）"
        if amounts.get("annual_budget"):
            from buy_amount_budget import format_annual_budget_note

            head = format_annual_budget_note() + "；" + head
        return head + change_suffix + "：" + "；".join(parts)
    if amounts.get("by_code"):
        active = {c: a for c, a in amounts["by_code"].items() if a and a > 0}
        parts = [f"{c} **{a:.0f}**" for c, a in sorted(active.items())]
        head = "固定基准金额"
        if amounts.get("annual_budget"):
            from buy_amount_budget import format_annual_budget_note

            head = format_annual_budget_note() + "；" + head
        return head + change_suffix + "：" + "；".join(parts)
    return f"模块默认金额{change_suffix}"

"""跨指数统一的买入意愿强度评分（0-100）。"""

from __future__ import annotations

import re


def _clamp(value: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, value))


def _invert_percentile(pct: float | None) -> float | None:
    """估值分位越低越利于买入。"""
    if pct is None:
        return None
    return _clamp(100.0 - float(pct))


def _direct_percentile(pct: float | None) -> float | None:
    """利差/股息率分位越高越利于买入。"""
    if pct is None:
        return None
    return _clamp(float(pct))


def _avg_scores(scores: list[float]) -> float | None:
    valid = [s for s in scores if s is not None]
    if not valid:
        return None
    return sum(valid) / len(valid)


def _valuation_component(snapshot: dict, signal_eval: dict) -> float:
    """估值维度 0-40：PE/PB/利差分位越低（或利差分位越高）得分越高。"""
    scores: list[float] = []
    for key in (
        "pe_percentile",
        "pb_percentile",
        "forward_pe_percentile",
        "trailing_pe_percentile",
    ):
        inverted = _invert_percentile(snapshot.get(key))
        if inverted is not None:
            scores.append(inverted)
    for key in ("spread_percentile", "dividend_yield_percentile"):
        direct = _direct_percentile(snapshot.get(key))
        if direct is not None:
            scores.append(direct)
    rate_pct = snapshot.get("us10y_percentile")
    if rate_pct is not None:
        scores.append(_invert_percentile(rate_pct))

    avg = _avg_scores(scores)
    if avg is None:
        criteria = [
            c
            for c in signal_eval.get("criteria", [])
            if c.get("applicable", True)
            and any(
                kw in c.get("name", "")
                for kw in ("PE", "PB", "利差", "股息", "PEG", "利率")
            )
        ]
        if criteria:
            passed = sum(1 for c in criteria if c.get("passed"))
            avg = passed / len(criteria) * 100
        else:
            avg = 50.0
    return avg * 0.40


def _price_position_component(snapshot: dict) -> float:
    """价格位置维度 0-30：年区间位置越低、距低点涨幅越小得分越高。"""
    parts: list[float] = []
    year_range = snapshot.get("year_range_position")
    if year_range is not None:
        parts.append((1.0 - _clamp(float(year_range), 0.0, 1.0)) * 100)

    pct_above_low = snapshot.get("pct_above_low")
    if pct_above_low is not None:
        # 距低点 0% → 100 分；距低点 50%+ → 0 分
        parts.append(_clamp((0.5 - float(pct_above_low)) / 0.5 * 100))

    pct_below_high = snapshot.get("pct_below_high")
    if pct_below_high is not None:
        # 距高点回撤越深得分越高
        parts.append(_clamp(float(pct_below_high) / 0.40 * 100))

    avg = _avg_scores(parts)
    if avg is None:
        avg = 50.0
    return avg * 0.30


def _trend_component(snapshot: dict) -> float:
    """趋势维度 0-20：MA 斜率向上得分高，低位震荡给予部分分数。"""
    slope = snapshot.get("ma_slope_pct")
    year_range = snapshot.get("year_range_position")
    if slope is None:
        base = 50.0
    else:
        # -3% ~ +3% 斜率映射到 0~100
        base = _clamp((float(slope) + 0.03) / 0.06 * 100)
    if year_range is not None and float(year_range) <= 0.25:
        base = max(base, 60.0)
    return base * 0.20


def _readiness_component(signal_eval: dict) -> float:
    """就绪度维度 0-10：条件达标比例。"""
    passed = signal_eval.get("score")
    total = signal_eval.get("total")
    if passed is None or total is None:
        criteria = [
            c for c in signal_eval.get("criteria", []) if c.get("applicable", True)
        ]
        passed = sum(1 for c in criteria if c.get("passed"))
        total = len(criteria)
    if not total:
        return 5.0
    return float(passed) / float(total) * 10.0


def compute_signal_strength(snapshot: dict, signal_eval: dict) -> int:
    """计算综合买入意愿分（0-100）。"""
    total = (
        _valuation_component(snapshot, signal_eval)
        + _price_position_component(snapshot)
        + _trend_component(snapshot)
        + _readiness_component(signal_eval)
    )
    return int(round(_clamp(total)))


def _parse_threshold(detail: str) -> tuple[str, float] | None:
    """从条件详情解析阈值，如「46.2%（需≤46%）」。"""
    if not detail or detail == "—":
        return None
    m = re.search(r"需([≥≤<>]+)\s*([\d.]+)%", detail)
    if m:
        return m.group(1), float(m.group(2))
    m = re.search(r"需([≥≤<>]+)\s*([\d.]+)\)", detail)
    if m:
        return m.group(1), float(m.group(2))
    return None


def _extract_metric_value(detail: str) -> float | None:
    """从条件详情提取当前指标值。"""
    if not detail or detail == "—":
        return None
    m = re.match(r"^([\d.]+)%", detail.strip())
    if m:
        return float(m.group(1))
    m = re.match(r"^([\d.]+)（", detail.strip())
    if m:
        return float(m.group(1))
    return None


def detect_near_buy(signal_eval: dict, *, margin_pct: float = 5.0) -> bool:
    """未达标但距触发阈值 ≤ margin_pct 时视为接近买入。"""
    if signal_eval.get("criteria_met") or signal_eval.get("is_sell"):
        return False
    criteria = [
        c for c in signal_eval.get("criteria", []) if c.get("applicable", True)
    ]
    if not criteria:
        return False
    failed = [c for c in criteria if not c.get("passed")]
    if not failed:
        return False
    # 多数条件已达标，仅少数未达标
    passed_ratio = (len(criteria) - len(failed)) / len(criteria)
    if passed_ratio < 0.6:
        return False
    for item in failed:
        parsed = _parse_threshold(item.get("detail", ""))
        current = _extract_metric_value(item.get("detail", ""))
        if parsed is None or current is None:
            continue
        op, threshold = parsed
        if op in ("≤", "<"):
            gap = current - threshold
            if gap <= margin_pct:
                return True
        elif op in ("≥", ">"):
            gap = threshold - current
            if gap <= margin_pct:
                return True
    return False

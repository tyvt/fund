"""市场牛熊状态：基于现有面板字段（年区间位置、MA 斜率），无额外数据源。"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

import pandas as pd

from config import (
    MARKET_REGIME_BEAR_BUY_MULT,
    MARKET_REGIME_BEAR_MA_SLOPE_MAX,
    MARKET_REGIME_BEAR_RANGE_MAX,
    MARKET_REGIME_BEAR_ROTATION_HURDLE,
    MARKET_REGIME_BULL_BUY_MULT,
    MARKET_REGIME_BULL_MA_SLOPE_MIN,
    MARKET_REGIME_BULL_RANGE_MIN,
    MARKET_REGIME_BULL_ROTATION_HURDLE,
    MARKET_REGIME_ENABLED,
    MARKET_REGIME_PROXY_CODES,
    ROTATION_MARGINAL_HURDLE_ANN_PCT,
)

REGIME_BULL = "bull"
REGIME_BEAR = "bear"
REGIME_NEUTRAL = "neutral"
REGIME_LABELS = {
    REGIME_BULL: "牛市",
    REGIME_BEAR: "熊市",
    REGIME_NEUTRAL: "震荡",
}


@dataclass
class RegimeConfig:
    """牛熊判定阈值与策略乘数（优化脚本可覆盖，默认读 config）。"""

    enabled: bool = True
    proxy_codes: tuple[str, ...] = field(
        default_factory=lambda: tuple(MARKET_REGIME_PROXY_CODES)
    )
    bull_range_min: float = MARKET_REGIME_BULL_RANGE_MIN
    bear_range_max: float = MARKET_REGIME_BEAR_RANGE_MAX
    bull_ma_slope_min: float = MARKET_REGIME_BULL_MA_SLOPE_MIN
    bear_ma_slope_max: float = MARKET_REGIME_BEAR_MA_SLOPE_MAX
    bull_buy_mult: float = MARKET_REGIME_BULL_BUY_MULT
    bear_buy_mult: float = MARKET_REGIME_BEAR_BUY_MULT
    neutral_buy_mult: float = 1.0
    bull_rotation_hurdle: float = MARKET_REGIME_BULL_ROTATION_HURDLE
    bear_rotation_hurdle: float = MARKET_REGIME_BEAR_ROTATION_HURDLE
    neutral_rotation_hurdle: float = ROTATION_MARGINAL_HURDLE_ANN_PCT
    position_alloc: bool = True

    @classmethod
    def from_dict(cls, data: dict | None) -> RegimeConfig:
        if not data:
            return default_regime_config()
        base = asdict(default_regime_config())
        base.update({k: v for k, v in data.items() if k in base})
        if "proxy_codes" in data and data["proxy_codes"]:
            base["proxy_codes"] = tuple(data["proxy_codes"])
        return cls(**base)

    def label(self) -> str:
        if not self.enabled:
            return "无牛熊"
        return (
            f"牛≥{self.bull_range_min:.2f} 熊≤{self.bear_range_max:.2f} "
            f"买{self.bear_buy_mult:.2f}/{self.bull_buy_mult:.2f} "
            f"轮{self.bear_rotation_hurdle:.0f}/{self.bull_rotation_hurdle:.0f}%"
        )


def default_regime_config() -> RegimeConfig:
    return RegimeConfig(enabled=MARKET_REGIME_ENABLED)


def is_market_regime_enabled() -> bool:
    return MARKET_REGIME_ENABLED


def _row_field(row, key):
    if row is None:
        return None
    if hasattr(row, "get"):
        val = row.get(key)
    else:
        val = getattr(row, key, None)
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return None
    return float(val)


def detect_regime_from_metrics(
    year_range_position: float | None,
    ma_slope_pct: float | None,
    config: RegimeConfig | None = None,
) -> str:
    cfg = config or default_regime_config()
    bull_pos = (
        year_range_position is not None
        and year_range_position >= cfg.bull_range_min
    )
    bull_trend = (
        ma_slope_pct is not None and ma_slope_pct >= cfg.bull_ma_slope_min
    )
    if bull_pos and bull_trend:
        return REGIME_BULL

    bear_pos = (
        year_range_position is not None
        and year_range_position <= cfg.bear_range_max
    )
    bear_trend = (
        ma_slope_pct is not None and ma_slope_pct <= cfg.bear_ma_slope_max
    )
    if bear_pos or bear_trend:
        return REGIME_BEAR

    return REGIME_NEUTRAL


def detect_regime_from_row(row, config: RegimeConfig | None = None) -> str:
    return detect_regime_from_metrics(
        _row_field(row, "year_range_position"),
        _row_field(row, "ma_slope_pct"),
        config,
    )


def get_regime_params(regime: str, config: RegimeConfig | None = None) -> dict:
    cfg = config or default_regime_config()
    if regime == REGIME_BULL:
        return {
            "buy_amount_mult": cfg.bull_buy_mult,
            "rotation_hurdle_ann_pct": cfg.bull_rotation_hurdle,
        }
    if regime == REGIME_BEAR:
        return {
            "buy_amount_mult": cfg.bear_buy_mult,
            "rotation_hurdle_ann_pct": cfg.bear_rotation_hurdle,
        }
    return {
        "buy_amount_mult": cfg.neutral_buy_mult,
        "rotation_hurdle_ann_pct": cfg.neutral_rotation_hurdle,
    }


def _proxy_rows_by_day(panels, code: str, start_date, end_date) -> dict[str, dict]:
    from backtest_trade_signals import _filter_panel

    if code == "NDX":
        daily, _ = panels.us_index_panel("ndx")
    elif code == "SPX":
        daily, _ = panels.us_index_panel("spx")
    elif code == "399006":
        daily = panels.cyb_panel()
    elif code == "H30269":
        daily = panels.dividend_panel(code)
    elif code in ("000852", "000688"):
        daily = panels.cn_broad_panel(code)
    else:
        return {}

    sample = _filter_panel(daily, start_date, end_date)
    if sample is None or sample.empty:
        return {}

    out = {}
    cols = sample.columns.tolist()
    for tup in sample.itertuples(index=False, name=None):
        row = dict(zip(cols, tup))
        day = row["_dt"].strftime("%Y-%m-%d")
        out[day] = row
    return out


def build_regime_by_day(
    panels,
    start_date: str,
    end_date: str | None = None,
    *,
    config: RegimeConfig | None = None,
) -> dict[str, str]:
    """合成多指数代理的逐日牛熊序列（多数表决）。"""
    cfg = config or default_regime_config()
    votes: dict[str, list[str]] = {}
    for code in cfg.proxy_codes:
        try:
            rows = _proxy_rows_by_day(panels, code, start_date, end_date)
        except Exception:
            continue
        for day, row in rows.items():
            votes.setdefault(day, []).append(detect_regime_from_row(row, cfg))

    regime_by_day: dict[str, str] = {}
    for day, regs in votes.items():
        if not regs:
            regime_by_day[day] = REGIME_NEUTRAL
            continue
        bull = regs.count(REGIME_BULL)
        bear = regs.count(REGIME_BEAR)
        if bull > bear and bull >= len(regs) / 2:
            regime_by_day[day] = REGIME_BULL
        elif bear > bull and bear >= len(regs) / 2:
            regime_by_day[day] = REGIME_BEAR
        else:
            regime_by_day[day] = REGIME_NEUTRAL
    return regime_by_day


def summarize_regime_series(regime_by_day: dict[str, str]) -> dict[str, int]:
    out = {REGIME_BULL: 0, REGIME_BEAR: 0, REGIME_NEUTRAL: 0}
    for r in regime_by_day.values():
        out[r] = out.get(r, 0) + 1
    return out

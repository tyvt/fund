"""买卖点阈值参数目录：供回测筛选与搜索使用。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

import config


@dataclass(frozen=True)
class SignalParam:
    """单个可回测阈值。"""

    id: str
    label: str
    side: str  # buy | sell | rotation | alloc
    index: str | None
    default: float
    sweep: tuple[float, ...]
  # 值越大越宽松（买：更易触发；卖：更难触发）
    higher_is_looser: bool = True
    apply: Callable[[float], dict] | None = None

    def verdict_threshold_pct(self) -> float:
        """判定「有影响」的最小验证段利差变化（百分点）。"""
        if self.side == "rotation":
            return 0.20
        if self.side == "alloc":
            return 0.30
        return 0.15


def _sweep(default: float, deltas: tuple[float, ...]) -> tuple[float, ...]:
    vals = sorted({round(default + d, 6) for d in deltas})
    return tuple(v for v in vals if v > 0 or default <= 0)


def _env_patch(*pairs: tuple[str, Any]) -> dict:
    return {"env": {k: str(v) for k, v in pairs}}


def _config_attr(attr: str, value: Any) -> dict:
    return {"config_attrs": {attr: value}}


def _cyb_attr(attr: str, value: Any) -> dict:
    return {"config_attrs": {attr: value}, "cyb_attrs": {attr: value}}


def _cn_broad(code: str, key: str, suffix: str, default: float, **kw) -> SignalParam:
    env_name = f"CN_BROAD_{code}_{suffix}"
    label = kw.pop("label", f"{code} {key}")
    return SignalParam(
        id=f"cn_broad.{code}.{key}",
        label=label,
        index=code,
        apply=lambda v, n=env_name: _env_patch((n, v)),
        default=default,
        **kw,
    )


def _dividend(code: str, key: str, suffix: str, default: float, **kw) -> SignalParam:
    env_name = f"DIVIDEND_{code}_{suffix}"
    label = kw.pop("label", f"{code} {key}")
    return SignalParam(
        id=f"dividend.{code}.{key}",
        label=label,
        index=code,
        apply=lambda v, n=env_name: _env_patch((n, v)),
        default=default,
        **kw,
    )


def _div_sell(code: str, key: str, suffix: str, default: float, **kw) -> SignalParam:
    env_name = f"DIVIDEND_{code}_{suffix}"
    label = kw.pop("label", f"{code} 卖 {key}")
    return SignalParam(
        id=f"dividend_sell.{code}.{key}",
        label=label,
        index=code,
        side="sell",
        apply=lambda v, n=env_name: _env_patch((n, v)),
        default=default,
        **kw,
    )


def _cyb(key: str, attr: str, default: float, **kw) -> SignalParam:
    label = kw.pop("label", f"CYB {key}")
    return SignalParam(
        id=f"cyb.{key}",
        label=label,
        index="399006",
        apply=lambda v, a=attr: _cyb_attr(a, v),
        default=default,
        **kw,
    )


def _us(key: str, us_key: str, suffix: str, default: float, **kw) -> SignalParam:
    attr = f"{us_key.upper()}_{suffix}"
    label = kw.pop("label", f"{us_key.upper()} {key}")
    return SignalParam(
        id=f"us.{us_key}.{key}",
        label=label,
        index=us_key.upper(),
        apply=lambda v, a=attr: _config_attr(a, v),
        default=default,
        **kw,
    )


def _us_sell_env(
    key: str, us_key: str, suffix: str, config_key: str, default: float, **kw
) -> SignalParam:
    env_name = f"{us_key.upper()}_{suffix}"
    label = kw.pop("label", f"{us_key.upper()} 卖 {key}")
    return SignalParam(
        id=f"us_sell.{us_key}.{config_key}",
        label=label,
        index=us_key.upper(),
        side="sell",
        apply=lambda v, n=env_name: _env_patch((n, v)),
        default=default,
        **kw,
    )


def _load_cn_defaults(code: str) -> dict:
    return config.get_cn_broad_signal_config(code)


def _load_div_buy(code: str) -> dict:
    return config.get_dividend_signal_config(code)


def _load_div_sell(code: str) -> dict:
    return config.get_dividend_sell_config(code)


def build_signal_param_catalog() -> list[SignalParam]:
    """构建全部买卖点阈值（逐个筛选用）。"""
    params: list[SignalParam] = []

    for code in ("000852", "000688"):
        cfg = _load_cn_defaults(code)
        params.extend([
            _cn_broad(
                code, "buy_spread_percentile_min", "BUY_SPREAD_PERCENTILE_MIN",
                cfg["buy_spread_percentile_min"],
                side="buy", label=f"{code} 利差分位买",
                sweep=_sweep(cfg["buy_spread_percentile_min"], (-8, 0, 8)),
                higher_is_looser=True,
            ),
            _cn_broad(
                code, "buy_max_above_low_pct", "BUY_MAX_ABOVE_LOW_PCT",
                cfg["buy_max_above_low_pct"],
                side="buy", label=f"{code} 距低点涨幅买",
                sweep=_sweep(cfg["buy_max_above_low_pct"], (-0.02, 0, 0.02)),
                higher_is_looser=True,
            ),
            _cn_broad(
                code, "buy_min_drawdown_from_high_pct", "BUY_MIN_DRAWDOWN_FROM_HIGH_PCT",
                cfg["buy_min_drawdown_from_high_pct"],
                side="buy", label=f"{code} 距高点回撤买",
                sweep=_sweep(cfg["buy_min_drawdown_from_high_pct"], (-0.04, 0, 0.04)),
                higher_is_looser=False,
            ),
            _cn_broad(
                code, "buy_max_year_range_pct", "BUY_MAX_YEAR_RANGE_PCT",
                cfg["buy_max_year_range_pct"],
                side="buy", label=f"{code} 年区间位置买",
                sweep=_sweep(cfg["buy_max_year_range_pct"], (-0.06, 0, 0.06)),
                higher_is_looser=True,
            ),
            _cn_broad(
                code, "buy_trend_min_ma_slope_pct", "BUY_TREND_MIN_MA_SLOPE_PCT",
                cfg["buy_trend_min_ma_slope_pct"],
                side="buy", label=f"{code} MA斜率买",
                sweep=_sweep(cfg["buy_trend_min_ma_slope_pct"], (-0.01, 0, 0.01)),
                higher_is_looser=True,
            ),
            _cn_broad(
                code, "sell_trailing_drawdown_pct", "SELL_TRAILING_DRAWDOWN_PCT",
                cfg["sell_trailing_drawdown_pct"] or 0.10,
                side="sell", label=f"{code} 移动止盈回撤",
                sweep=(0.08, 0.10, 0.12, 0.14),
                higher_is_looser=True,
            ),
            _cn_broad(
                code, "sell_min_unrealized_gain_pct", "SELL_MIN_UNREALIZED_GAIN_PCT",
                cfg["sell_min_unrealized_gain_pct"],
                side="sell", label=f"{code} 移动止盈浮盈",
                sweep=_sweep(cfg["sell_min_unrealized_gain_pct"], (-0.10, 0, 0.10)),
                higher_is_looser=False,
            ),
        ])

    div_buy = _load_div_buy("H30269")
    div_sell = _load_div_sell("H30269")
    params.extend([
        _dividend(
            "H30269", "buy_spread_min", "BUY_SPREAD_MIN",
            div_buy["buy_spread_min"],
            side="buy", label="H30269 绝对利差买",
            sweep=_sweep(div_buy["buy_spread_min"], (-0.006, 0, 0.006)),
            higher_is_looser=True,
        ),
        _dividend(
            "H30269", "buy_spread_percentile_min", "BUY_SPREAD_PERCENTILE_MIN",
            div_buy["buy_spread_percentile_min"],
            side="buy", label="H30269 利差分位买",
            sweep=_sweep(div_buy["buy_spread_percentile_min"], (-8, 0, 8)),
            higher_is_looser=True,
        ),
        _dividend(
            "H30269", "buy_pe_percentile_max", "BUY_PE_PERCENTILE_MAX",
            div_buy["buy_pe_percentile_max"],
            side="buy", label="H30269 PE分位买",
            sweep=_sweep(div_buy["buy_pe_percentile_max"], (-6, 0, 6)),
            higher_is_looser=True,
        ),
        _dividend(
            "H30269", "buy_max_above_low_pct", "BUY_MAX_ABOVE_LOW_PCT",
            div_buy["buy_max_above_low_pct"],
            side="buy", label="H30269 距低点涨幅买",
            sweep=_sweep(div_buy["buy_max_above_low_pct"], (-0.02, 0, 0.02)),
            higher_is_looser=True,
        ),
        _dividend(
            "H30269", "buy_min_drawdown_from_high_pct", "BUY_MIN_DRAWDOWN_FROM_HIGH_PCT",
            div_buy["buy_min_drawdown_from_high_pct"],
            side="buy", label="H30269 距高点回撤买",
            sweep=_sweep(div_buy["buy_min_drawdown_from_high_pct"], (-0.04, 0, 0.04)),
            higher_is_looser=False,
        ),
        _div_sell(
            "H30269", "sell_trailing_drawdown_pct", "SELL_TRAILING_DRAWDOWN_PCT",
            div_sell["sell_trailing_drawdown_pct"],
            label="H30269 移动止盈回撤",
            sweep=(0.08, 0.10, 0.12),
            higher_is_looser=True,
        ),
        _div_sell(
            "H30269", "sell_min_unrealized_gain_pct", "SELL_MIN_UNREALIZED_GAIN_PCT",
            div_sell["sell_min_unrealized_gain_pct"],
            label="H30269 移动止盈浮盈",
            sweep=_sweep(div_sell["sell_min_unrealized_gain_pct"], (-0.10, 0, 0.10)),
            higher_is_looser=False,
        ),
    ])

    params.extend([
        _cyb(
            "buy_peg_hist_max", "CYB_BUY_PEG_HIST_MAX",
            config.CYB_BUY_PEG_HIST_MAX,
            side="buy", label="CYB PEG买",
            sweep=_sweep(config.CYB_BUY_PEG_HIST_MAX, (-0.4, 0, 0.4)),
        ),
        _cyb(
            "buy_max_above_low_pct", "CYB_BUY_MAX_ABOVE_LOW_PCT",
            config.CYB_BUY_MAX_ABOVE_LOW_PCT,
            side="buy", label="CYB 距低点涨幅买",
            sweep=_sweep(config.CYB_BUY_MAX_ABOVE_LOW_PCT, (-0.02, 0, 0.02)),
        ),
        _cyb(
            "buy_trend_min_ma_slope_pct", "CYB_BUY_TREND_MIN_MA_SLOPE_PCT",
            config.CYB_BUY_TREND_MIN_MA_SLOPE_PCT,
            side="buy", label="CYB MA斜率买",
            sweep=_sweep(config.CYB_BUY_TREND_MIN_MA_SLOPE_PCT, (-0.01, 0, 0.01)),
        ),
    ])

    for us_key in ("ndx", "spx"):
        prefix = us_key.upper()
        sell_cfg = config.get_us_index_sell_config(us_key)
        params.extend([
            _us(
                "buy_forward_pe_percentile_max", us_key, "BUY_FORWARD_PE_PERCENTILE_MAX",
                getattr(config, f"{prefix}_BUY_FORWARD_PE_PERCENTILE_MAX"),
                side="buy", label=f"{prefix} Forward PE买",
                sweep=_sweep(
                    getattr(config, f"{prefix}_BUY_FORWARD_PE_PERCENTILE_MAX"), (-6, 0, 6)
                ),
            ),
            _us(
                "buy_max_year_range_pct", us_key, "BUY_MAX_YEAR_RANGE_PCT",
                getattr(config, f"{prefix}_BUY_MAX_YEAR_RANGE_PCT"),
                side="buy", label=f"{prefix} 年区间位置买",
                sweep=_sweep(
                    getattr(config, f"{prefix}_BUY_MAX_YEAR_RANGE_PCT"), (-0.06, 0, 0.06)
                ),
            ),
            _us(
                "buy_trend_min_ma_slope_pct", us_key, "BUY_TREND_MIN_MA_SLOPE_PCT",
                getattr(config, f"{prefix}_BUY_TREND_MIN_MA_SLOPE_PCT"),
                side="buy", label=f"{prefix} MA斜率买",
                sweep=_sweep(
                    getattr(config, f"{prefix}_BUY_TREND_MIN_MA_SLOPE_PCT"), (-0.01, 0, 0.01)
                ),
            ),
            _us_sell_env(
                "sell_trailing_pe_percentile_min", us_key,
                "SELL_TRAILING_PE_PERCENTILE_MIN",
                "sell_trailing_pe_percentile_min",
                sell_cfg["sell_trailing_pe_percentile_min"],
                label=f"{prefix} TTM PE卖",
                sweep=_sweep(sell_cfg["sell_trailing_pe_percentile_min"], (-5, 0, 5)),
                higher_is_looser=False,
            ),
            _us_sell_env(
                "sell_trailing_drawdown_pct", us_key, "SELL_TRAILING_DRAWDOWN_PCT",
                "sell_trailing_drawdown_pct",
                sell_cfg["sell_trailing_drawdown_pct"],
                label=f"{prefix} 移动止盈回撤",
                sweep=(0.10, 0.12, 0.14),
                higher_is_looser=True,
            ),
            _us_sell_env(
                "sell_min_unrealized_gain_pct", us_key, "SELL_MIN_UNREALIZED_GAIN_PCT",
                "sell_min_unrealized_gain_pct",
                sell_cfg["sell_min_unrealized_gain_pct"],
                label=f"{prefix} 移动止盈浮盈",
                sweep=_sweep(sell_cfg["sell_min_unrealized_gain_pct"], (-0.10, 0, 0.10)),
                higher_is_looser=False,
            ),
        ])

    params.append(
        SignalParam(
            id="rotation.hurdle_ann_pct",
            label="轮动边际门槛（年化%）",
            side="rotation",
            index=None,
            default=config.ROTATION_MARGINAL_HURDLE_ANN_PCT,
            sweep=(7.0, 10.0, 12.0, 14.0, 16.0),
            higher_is_looser=True,
            apply=lambda v: {
                "config_attrs": {"ROTATION_MARGINAL_HURDLE_ANN_PCT": v},
                "rotation_sell_attrs": {"ROTATION_MARGINAL_HURDLE_ANN_PCT": v},
            },
        )
    )

    return params


def catalog_by_id() -> dict[str, SignalParam]:
    return {p.id: p for p in build_signal_param_catalog()}


def ablate_value(param: SignalParam) -> float:
    """关闭该条件：买入放宽为恒通过，卖出放宽为恒不触发。"""
    key = param.id.rsplit(".", 1)[-1]
    _BUY_OFF = {
        "buy_spread_min": 0.0,
        "buy_spread_percentile_min": 0.0,
        "buy_pe_percentile_max": 100.0,
        "buy_pb_percentile_max": 100.0,
        "buy_forward_pe_percentile_max": 100.0,
        "buy_trailing_pe_percentile_max": 100.0,
        "buy_max_above_low_pct": 5.0,
        "buy_min_drawdown_from_high_pct": 0.0,
        "buy_max_year_range_pct": 1.0,
        "buy_trend_min_ma_slope_pct": -1.0,
        "buy_peg_hist_max": 50.0,
        "buy_peg_forward_max": 50.0,
    }
    _SELL_OFF = {
        "sell_pe_percentile_min": 101.0,
        "sell_pb_percentile_min": 101.0,
        "sell_spread_percentile_max": -1.0,
        "sell_max_above_low_pct": 99.0,
        "sell_trailing_drawdown_pct": 1.0,
        "sell_min_unrealized_gain_pct": 99.0,
        "sell_forward_pe_percentile_min": 101.0,
        "sell_trailing_pe_percentile_min": 101.0,
        "sell_peg_hist_min": 999.0,
    }
    if param.side == "buy" or key.startswith("buy_"):
        return _BUY_OFF.get(key, 100.0 if param.higher_is_looser else 0.0)
    if param.side == "sell" or key.startswith("sell_"):
        return _SELL_OFF.get(key, 101.0)
    return param.default


def active_search_space(screen_results: list[dict] | None = None) -> dict[str, list[float]]:
    """从筛选结果生成随机搜索空间（仅保留有影响的参数）。"""
    if not screen_results:
        return {
            p.id: list(p.sweep)
            for p in build_signal_param_catalog()
            if p.side in ("buy", "sell", "rotation")
        }
    space: dict[str, list[float]] = {}
    for row in screen_results:
        if row.get("verdict") != "keep":
            continue
        pid = row["param_id"]
        param = catalog_by_id().get(pid)
        if param:
            space[pid] = list(param.sweep)
    return space

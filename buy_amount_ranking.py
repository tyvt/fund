"""按自基日策略收益率排名，为全部指数按收益率权重分配年度买入额度。"""

from __future__ import annotations

from datetime import date

import pandas as pd

from buy_amount_config import ALL_BUY_INDEX_CODES

_RANKING_CACHE: dict | None = None

# 排名对比用固定单次买入（元），仅用于收益率排序
_RANKING_SIM_AMOUNT = 100.0


def _panel_fingerprint(panels) -> str:
    parts = []
    for cfg in _iter_configs(panels):
        panel = cfg["panel"]
        if panel is None or panel.empty:
            parts.append(f"{cfg['code']}:0")
            continue
        date_col = cfg["date_col"]
        col = (
            "total_return_close"
            if cfg.get("valuation_price_col") == "total_return_close"
            else cfg.get("price_col", "close")
        )
        work = panel.dropna(subset=[date_col if date_col != "date_only" else "date_only"])
        if work.empty:
            parts.append(f"{cfg['code']}:0")
            continue
        if date_col == "date_only":
            last = work.iloc[-1]["date_only"]
        else:
            last = work.iloc[-1][date_col]
        parts.append(f"{cfg['code']}:{last}:{len(work)}:{work.iloc[-1].get(col)}")
    return "|".join(parts)


def _iter_configs(panels):
    from backtest_buy_signals import _iter_backtest_configs

    for cfg in _iter_backtest_configs(panels):
        code = cfg["code"]
        if code in ALL_BUY_INDEX_CODES:
            if code in {i["code"] for i in __import__("config").INDICES}:
                cfg = {**cfg, "valuation_price_col": "total_return_close"}
            yield cfg


def compute_index_ranking(panels=None, *, force: bool = False):
    """计算各指数自基日买入持有收益率排名，并分配年度额度。"""
    global _RANKING_CACHE
    from backtest_buy_signals import (
        BacktestPanels,
        _simulate_dca_returns,
        default_backtest_range,
    )
    from config import (
        ANNUAL_INVESTMENT_BUDGET,
        BUY_AMOUNT_RANKING_ENABLED,
    )

    if not BUY_AMOUNT_RANKING_ENABLED:
        return _empty_ranking()

    today = date.today().isoformat()
    if (
        not force
        and panels is None
        and _RANKING_CACHE is not None
        and _RANKING_CACHE.get("day") == today
    ):
        return _RANKING_CACHE["result"]

    panels = panels or BacktestPanels()
    fingerprint = _panel_fingerprint(panels)
    if (
        not force
        and _RANKING_CACHE is not None
        and _RANKING_CACHE.get("fingerprint") == fingerprint
    ):
        return _RANKING_CACHE["result"]

    date_range = default_backtest_range()
    rows = []
    returns: dict[str, float] = {}
    buy_counts: dict[str, int] = {}

    for cfg in _iter_configs(panels):
        code = cfg["code"]
        val_col = cfg.get("valuation_price_col")
        result = _simulate_dca_returns(
            cfg["panel"],
            date_range,
            cfg["buy_fn"],
            amount=_RANKING_SIM_AMOUNT,
            date_col=cfg["date_col"],
            price_col=cfg["price_col"],
            valuation_price_col=val_col,
        )
        ret = result.get("return_pct") if result else None
        buys = int(result.get("buy_days", 0)) if result else 0
        score = max(float(ret), 0.01) if ret is not None else 0.01
        returns[code] = score
        buy_counts[code] = buys
        rows.append(
            {
                "code": code,
                "name": cfg["name"],
                "return_pct": ret,
                "buy_days": buys,
                "score": score,
            }
        )

    rows.sort(key=lambda r: (r["score"], r["code"]))
    active = list(rows)

    denom = sum(buy_counts.get(r["code"], 0) * returns[r["code"]] for r in active)
    budget = float(ANNUAL_INVESTMENT_BUDGET)
    by_code: dict[str, float] = {}
    reference_by_code: dict[str, float] = {}
    for code in ALL_BUY_INDEX_CODES:
        if code not in returns or denom <= 0:
            by_code[code] = 0.0
            reference_by_code[code] = 0.0
            continue
        r = returns[code]
        amt = budget * r / denom
        by_code[code] = max(10.0, round(amt))
        reference_by_code[code] = by_code[code]

    for r in rows:
        r["recommended"] = True
        r["amount"] = by_code.get(r["code"], 0.0)

    val_dates = []
    for cfg in _iter_configs(panels):
        panel = cfg["panel"]
        if panel is None or panel.empty:
            continue
        date_col = cfg["date_col"]
        raw = panel.iloc[-1].get(
            "date_only" if date_col == "date_only" else date_col
        )
        if raw is not None:
            val_dates.append(pd.Timestamp(raw))
    as_of = max(val_dates).strftime("%Y-%m-%d") if val_dates else None

    result = {
        "rows": rows,
        "returns": returns,
        "buy_counts": buy_counts,
        "excluded_codes": frozenset(),
        "by_code": by_code,
        "reference_by_code": reference_by_code,
        "as_of": as_of,
        "exclude_bottom_n": 0,
    }
    _RANKING_CACHE = {"fingerprint": fingerprint, "day": today, "result": result}
    return result


def _empty_ranking():
    from config import BUY_AMOUNT_BASE_BY_CODE, _env_buy_amount_for_code

    by_code = {
        code: _env_buy_amount_for_code(code, BUY_AMOUNT_BASE_BY_CODE.get(code, 0))
        for code in ALL_BUY_INDEX_CODES
    }
    return {
        "rows": [],
        "returns": {},
        "buy_counts": {},
        "excluded_codes": frozenset(),
        "by_code": by_code,
        "reference_by_code": dict(by_code),
        "as_of": None,
        "exclude_bottom_n": 0,
    }


def get_ranking_allocation(panels=None, *, force: bool = False):
    """获取当前排名与分指数基准买入金额（带缓存）。"""
    return compute_index_ranking(panels=panels, force=force)


def is_index_recommended(index_code: str, panels=None) -> bool:
    from config import BUY_AMOUNT_RANKING_ENABLED, get_buy_amount_reference

    if not BUY_AMOUNT_RANKING_ENABLED:
        return get_buy_amount_reference(index_code) > 0
    return get_buy_amount_reference(index_code) > 0


def format_ranking_note(alloc=None) -> str:
    alloc = alloc or get_ranking_allocation()
    as_of = alloc.get("as_of") or "最新"
    return f"收益率排名（截至 {as_of}）；全部指数按收益率权重分配年度额度"


def format_ranking_markdown_table(alloc=None) -> list[str]:
    alloc = alloc or get_ranking_allocation()
    lines = [
        "## 收益率排名与买入额度",
        "",
        f"> {format_ranking_note(alloc)}",
        "",
        "| 排名 | 指数 | 代码 | 策略收益 | 买入次 | 单次买入 |",
        "| ---: | --- | --- | ---: | ---: | ---: |",
    ]
    for rank, row in enumerate(reversed(alloc.get("rows", [])), start=1):
        ret = row.get("return_pct")
        ret_text = f"{ret:.1f}%" if ret is not None else "—"
        amt = row.get("amount", 0)
        amt_text = f"{amt:.0f}" if amt and amt > 0 else "—"
        lines.append(
            f"| {rank} | {row['name']} | {row['code']} | {ret_text} | "
            f"{row.get('buy_days', 0)} | {amt_text} |"
        )
    lines.append("")
    return lines

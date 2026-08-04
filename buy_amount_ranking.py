"""按自基日策略收益率排名，为全部指数按收益率权重分配年度买入额度。"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date

import pandas as pd

from buy_amount_config import ALL_BUY_INDEX_CODES

_RANKING_CACHE: dict | None = None
_RANKING_DISK_NAME = "ranking_allocation.json"


def _ranking_disk_path():
    from config import DATA_CACHE_DIR

    return DATA_CACHE_DIR / _RANKING_DISK_NAME


def _load_ranking_disk_cache(fingerprint: str):
    from data_cache import load_json

    payload = load_json(_ranking_disk_path())
    if not payload:
        return None
    if payload.get("day") != date.today().isoformat():
        return None
    if payload.get("fingerprint") != fingerprint:
        return None
    rows = payload.get("rows") or []
    excluded = payload.get("excluded_codes") or []
    return {
        "rows": rows,
        "returns": {k: float(v) for k, v in (payload.get("returns") or {}).items()},
        "buy_counts": {
            k: int(v) for k, v in (payload.get("buy_counts") or {}).items()
        },
        "excluded_codes": frozenset(excluded),
        "by_code": {k: float(v) for k, v in (payload.get("by_code") or {}).items()},
        "reference_by_code": {
            k: float(v) for k, v in (payload.get("reference_by_code") or {}).items()
        },
        "as_of": payload.get("as_of"),
        "exclude_bottom_n": int(payload.get("exclude_bottom_n") or 0),
    }


def _save_ranking_disk_cache(fingerprint: str, result: dict) -> None:
    from data_cache import save_json

    save_json(
        _ranking_disk_path(),
        {
            "day": date.today().isoformat(),
            "fingerprint": fingerprint,
            "rows": result.get("rows") or [],
            "returns": result.get("returns") or {},
            "buy_counts": result.get("buy_counts") or {},
            "excluded_codes": sorted(result.get("excluded_codes") or []),
            "by_code": result.get("by_code") or {},
            "reference_by_code": result.get("reference_by_code") or {},
            "as_of": result.get("as_of"),
            "exclude_bottom_n": result.get("exclude_bottom_n") or 0,
        },
    )


def _preload_ranking_panels(panels) -> None:
    """并行预热全部回测面板，避免串行拉取。"""
    from concurrent.futures import ThreadPoolExecutor

    from backtest_buy_signals import CN_BROAD_BACKTEST_INDICES
    from config import INDICES, US_INDEX_KEYS

    tasks = []
    with ThreadPoolExecutor(max_workers=8) as executor:
        for item in INDICES:
            tasks.append(executor.submit(panels.dividend_panel, item["code"]))
        for item in CN_BROAD_BACKTEST_INDICES:
            tasks.append(executor.submit(panels.cn_broad_panel, item["code"]))
        tasks.append(executor.submit(panels.cyb_panel))
        for key in US_INDEX_KEYS:
            tasks.append(executor.submit(panels.us_index_panel, key))
        for task in tasks:
            task.result()

# 排名对比用固定单次买入（元），仅用于收益率排序
_RANKING_SIM_AMOUNT = 100.0


_CACHE_FINGERPRINT_SKIP = frozenset(
    {
        _RANKING_DISK_NAME,
        "position_allocation.json",
    }
)


def _data_cache_fingerprint() -> str:
    """用本地缓存文件元数据生成指纹，避免为校验缓存而加载全部面板。"""
    from config import DATA_CACHE_DIR, US_DATA_CACHE_DIR

    parts = []
    for root in (DATA_CACHE_DIR, US_DATA_CACHE_DIR):
        if not root.exists():
            continue
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            if path.name in _CACHE_FINGERPRINT_SKIP:
                continue
            stat = path.stat()
            parts.append(
                f"{path.relative_to(root)}:{stat.st_mtime_ns}:{stat.st_size}"
            )
    return "|".join(parts)


def _iter_configs(panels):
    from backtest_buy_signals import _iter_backtest_configs

    dividend_codes = {i["code"] for i in __import__("config").INDICES}
    for cfg in _iter_backtest_configs(panels):
        code = cfg["code"]
        if code in ALL_BUY_INDEX_CODES:
            if code in dividend_codes:
                cfg = {**cfg, "valuation_price_col": "total_return_close"}
            yield cfg


def _rank_one_config(cfg, date_range):
    from backtest_buy_signals import _simulate_dca_returns

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
        buy_mask_fn=cfg.get("buy_mask_fn"),
    )
    ret = result.get("return_pct") if result else None
    buys = int(result.get("buy_days", 0)) if result else 0
    score = max(float(ret), 0.01) if ret is not None else 0.01
    return {
        "code": code,
        "name": cfg["name"],
        "return_pct": ret,
        "buy_days": buys,
        "score": score,
        "panel": cfg["panel"],
        "date_col": cfg["date_col"],
    }


def compute_index_ranking(panels=None, *, force: bool = False):
    """计算各指数自基日买入持有收益率排名，并分配年度额度。"""
    global _RANKING_CACHE
    from backtest_buy_signals import default_backtest_range, get_panels
    from config import ANNUAL_INVESTMENT_BUDGET, BUY_AMOUNT_RANKING_ENABLED

    if not BUY_AMOUNT_RANKING_ENABLED:
        return _empty_ranking()

    today = date.today().isoformat()
    fingerprint = _data_cache_fingerprint()
    if (
        not force
        and panels is None
        and _RANKING_CACHE is not None
        and _RANKING_CACHE.get("day") == today
        and _RANKING_CACHE.get("fingerprint") == fingerprint
    ):
        return _RANKING_CACHE["result"]

    if not force and panels is None:
        disk_cached = _load_ranking_disk_cache(fingerprint)
        if disk_cached is not None:
            _RANKING_CACHE = {
                "fingerprint": fingerprint,
                "day": today,
                "result": disk_cached,
            }
            return disk_cached

    if (
        not force
        and _RANKING_CACHE is not None
        and _RANKING_CACHE.get("fingerprint") == fingerprint
    ):
        return _RANKING_CACHE["result"]

    # 与回测共用全局面板缓存，避免排名与回测各建一套
    panels = panels or get_panels()
    _preload_ranking_panels(panels)
    fingerprint = _data_cache_fingerprint()
    date_range = default_backtest_range()
    configs = list(_iter_configs(panels))
    workers = min(8, max(1, len(configs)))

    with ThreadPoolExecutor(max_workers=workers) as executor:
        ranked = list(
            executor.map(
                lambda cfg: _rank_one_config(cfg, date_range),
                configs,
            )
        )

    rows = []
    returns: dict[str, float] = {}
    buy_counts: dict[str, int] = {}
    for item in ranked:
        code = item["code"]
        returns[code] = item["score"]
        buy_counts[code] = item["buy_days"]
        rows.append(
            {
                "code": code,
                "name": item["name"],
                "return_pct": item["return_pct"],
                "buy_days": item["buy_days"],
                "score": item["score"],
            }
        )

    rows.sort(key=lambda r: (r["score"], r["code"]))
    active = list(rows)

    denom = sum(buy_counts.get(r["code"], 0) * returns[r["code"]] for r in active)
    from buy_amount_budget import get_annual_budget

    budget = float(get_annual_budget())
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
    for cfg, item in zip(configs, ranked):
        panel = item["panel"]
        if panel is None or panel.empty:
            continue
        date_col = item["date_col"]
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
    _save_ranking_disk_cache(fingerprint, result)
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

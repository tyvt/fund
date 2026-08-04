"""按当前指数位置与买入就绪度，在剩余年度额度内分配单次买入金额。"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date

from buy_amount_config import ALL_BUY_INDEX_CODES

from buy_amount_budget import (
    estimate_annual_investment,
    get_remaining_investment_budget,
    year_time_info,
)

_ALLOCATION_CACHE: dict | None = None
_ALLOCATION_DISK_NAME = "position_allocation.json"


def _allocation_disk_path():
    from config import DATA_CACHE_DIR

    return DATA_CACHE_DIR / _ALLOCATION_DISK_NAME


def _position_factor(year_range_position: float | None) -> float:
    if year_range_position is None:
        return 0.5
    pos = max(0.0, min(1.0, float(year_range_position)))
    return max(0.05, 1.0 - pos)


def _readiness_factor(signal_eval: dict) -> float:
    strength = signal_eval.get("signal_strength")
    if strength is not None:
        return max(0.05, float(strength) / 100.0)
    if signal_eval.get("is_buy"):
        return 1.0
    passed = signal_eval.get("score")
    total = signal_eval.get("total")
    if passed is None or total is None:
        criteria = [
            c for c in signal_eval.get("criteria", []) if c.get("applicable", True)
        ]
        passed = sum(1 for c in criteria if c.get("passed"))
        total = len(criteria)
    if not total:
        return 0.15
    return max(0.05, float(passed) / float(total))


def _return_factor(return_pct: float | None, max_return: float) -> float:
    if return_pct is None or max_return <= 0:
        return 0.5
    score = max(float(return_pct), 0.01)
    return 0.25 + 0.75 * (score / max_return)


def compute_allocation_weight(
    snapshot: dict,
    signal_eval: dict,
    *,
    return_pct: float | None = None,
    max_return: float = 1.0,
) -> float:
    """位置越低、越接近买入条件、历史收益越高 → 权重越大。"""
    pos = snapshot.get("year_range_position")
    readiness = _readiness_factor(signal_eval)
    pos_factor = _position_factor(pos)

    if not signal_eval.get("is_buy"):
        if readiness < 0.35 and (pos is None or float(pos) > 0.72):
            return 0.02

    return pos_factor * readiness * _return_factor(return_pct, max_return)


def _load_allocation_disk_cache(fingerprint: str):
    from data_cache import load_json

    payload = load_json(_allocation_disk_path())
    if not payload:
        return None
    if payload.get("day") != date.today().isoformat():
        return None
    if payload.get("fingerprint") != fingerprint:
        return None
    return payload.get("result")


def _save_allocation_disk_cache(fingerprint: str, result: dict) -> None:
    from data_cache import save_json

    payload = {
        **result,
        "excluded_codes": sorted(result.get("excluded_codes") or []),
    }
    save_json(
        _allocation_disk_path(),
        {
            "day": date.today().isoformat(),
            "fingerprint": fingerprint,
            "result": payload,
        },
    )


def _fetch_one_index_state(task: dict, live_quotes=None) -> dict | None:
    kind = task["kind"]
    code = task["code"]
    try:
        if kind == "dividend":
            from config import INDICES
            from core import build_dividend_signal_eval
            from dividend_data import evaluate_buy_signal, get_index_data
            from live_snapshot import maybe_apply_live
            from market_data import get_gov_bond_yield, get_gov_bond_yield_history

            index = next(i for i in INDICES if i["code"] == code)
            bond_history = task.get("bond_history")
            if bond_history is None:
                bond_history = get_gov_bond_yield_history()
            bond_yield, _ = get_gov_bond_yield()
            pe, dividend_yield, _index_date = get_index_data(index["code"])
            if pe is None or dividend_yield is None or bond_yield is None:
                return None
            buy_eval = evaluate_buy_signal(
                index["code"], pe, dividend_yield, bond_yield, bond_history
            )
            panel = buy_eval.get("panel")
            if panel is None or getattr(panel, "empty", False):
                return None
            buy_eval["code"] = code
            buy_eval = maybe_apply_live(buy_eval, live_quotes)
            spread = buy_eval.get("spread")
            spread_pct = buy_eval.get("spread_percentile")
            pe_pct = buy_eval.get("pe_percentile")
            signal_eval = build_dividend_signal_eval(
                code, buy_eval, spread, spread_pct, pe_pct
            )
            return {
                "code": code,
                "name": index["name"],
                "snapshot": buy_eval,
                "signal_eval": signal_eval,
            }

        if kind == "cn_broad":
            from cn_broad_data import fetch_cn_broad_snapshot
            from cn_broad_signal import evaluate_cn_broad_buy
            from live_snapshot import maybe_apply_live

            bond_history = task.get("bond_history")
            snapshot = fetch_cn_broad_snapshot(code, bond_history)
            snapshot = maybe_apply_live(snapshot, live_quotes)
            signal_eval = evaluate_cn_broad_buy(snapshot)
            return {
                "code": code,
                "name": snapshot.get("name", code),
                "snapshot": snapshot,
                "signal_eval": signal_eval,
            }

        if kind == "cyb":
            from config import CYB_EXPECTED_GROWTH
            from cyb_data import fetch_cyb_snapshot
            from cyb_signal import evaluate_cyb_signal
            from live_snapshot import maybe_apply_live

            snapshot = fetch_cyb_snapshot(expected_growth=CYB_EXPECTED_GROWTH)
            snapshot = maybe_apply_live(snapshot, live_quotes)
            signal_eval = evaluate_cyb_signal(snapshot)
            return {"code": code, "name": "创业板指", "snapshot": snapshot, "signal_eval": signal_eval}

        if kind == "us":
            from config import CYB_EXPECTED_GROWTH
            from live_snapshot import maybe_apply_live
            from us_index_data import fetch_snapshot
            from us_index_signal import evaluate_signal

            key = task["us_key"]
            snapshot = fetch_snapshot(key, expected_growth=CYB_EXPECTED_GROWTH)
            snapshot = maybe_apply_live(snapshot, live_quotes)
            signal_eval = evaluate_signal(key, snapshot)
            name = {"ndx": "纳斯达克100", "spx": "标普500"}[key]
            return {"code": code, "name": name, "snapshot": snapshot, "signal_eval": signal_eval}
    except Exception:
        return None
    return None


def _build_index_tasks():
    from config import CN_BROAD_INDICES, INDICES, US_INDEX_KEYS

    tasks = []
    for item in INDICES:
        tasks.append({"kind": "dividend", "code": item["code"]})
    for item in CN_BROAD_INDICES:
        tasks.append({"kind": "cn_broad", "code": item["code"]})
    tasks.append({"kind": "cyb", "code": "399006"})
    for key in US_INDEX_KEYS:
        code = {"ndx": "NDX", "spx": "SPX"}[key]
        tasks.append({"kind": "us", "code": code, "us_key": key})
    return tasks


def collect_index_states(live_quotes=None) -> list[dict]:
    from market_data import get_gov_bond_yield_history

    tasks = _build_index_tasks()
    bond_history = get_gov_bond_yield_history()
    for task in tasks:
        if task["kind"] in ("dividend", "cn_broad"):
            task["bond_history"] = bond_history

    workers = min(8, max(1, len(tasks)))
    with ThreadPoolExecutor(max_workers=workers) as executor:
        rows = list(
            executor.map(
                lambda task: _fetch_one_index_state(task, live_quotes),
                tasks,
            )
        )
    return [row for row in rows if row and row["code"] in ALL_BUY_INDEX_CODES]


def _live_quotes_fingerprint(live_quotes) -> str:
    if not live_quotes:
        return "nolive"
    parts = []
    for code in sorted(live_quotes):
        quote = live_quotes[code]
        price = getattr(quote, "price", None)
        if price is None:
            continue
        parts.append(f"{code}:{round(float(price), 2)}")
    return "|".join(parts) if parts else "nolive"


def _light_allocation_fingerprint(live_quotes=None) -> str:
    """不依赖全量状态的轻量指纹，用于在拉数前判断磁盘缓存是否可用。"""
    from buy_amount_ranking import _data_cache_fingerprint

    return f"{_data_cache_fingerprint()}#live:{_live_quotes_fingerprint(live_quotes)}"


def compute_position_allocation(
    live_quotes=None,
    *,
    force: bool = False,
) -> dict:
    """按位置与就绪度分配剩余额度，并给出预计全年投入。"""
    global _ALLOCATION_CACHE
    from buy_amount_budget import get_remaining_investment_budget
    from buy_amount_ranking import compute_index_ranking

    today = date.today().isoformat()
    light_fp = _light_allocation_fingerprint(live_quotes)

    # 同进程复用：enrich 无 live_quotes 时直接复用；有 live 时指纹一致才复用
    if not force and _ALLOCATION_CACHE is not None and _ALLOCATION_CACHE.get("day") == today:
        if live_quotes is None or _ALLOCATION_CACHE.get("fingerprint") == light_fp:
            return _ALLOCATION_CACHE["result"]

    if not force:
        disk = _load_allocation_disk_cache(light_fp)
        if disk is not None:
            _ALLOCATION_CACHE = {
                "day": today,
                "fingerprint": light_fp,
                "result": disk,
            }
            return disk

    states = collect_index_states(live_quotes=live_quotes)
    ranking = compute_index_ranking(force=False)
    returns = ranking.get("returns") or {}
    buy_counts = ranking.get("buy_counts") or {}
    max_return = max(returns.values()) if returns else 1.0

    weights: dict[str, float] = {}
    state_by_code = {row["code"]: row for row in states}
    buy_values = [float(buy_counts.get(code, 0)) for code in ALL_BUY_INDEX_CODES if buy_counts.get(code, 0) > 0]
    median_buys = sorted(buy_values)[len(buy_values) // 2] if buy_values else 1.0
    for code in ALL_BUY_INDEX_CODES:
        row = state_by_code.get(code)
        if not row:
            weights[code] = 0.01
            continue
        base_weight = compute_allocation_weight(
            row["snapshot"],
            row["signal_eval"],
            return_pct=returns.get(code),
            max_return=max_return,
        )
        annual_buys = max(float(buy_counts.get(code, 1)), 1.0)
        freq_factor = min(1.0, (annual_buys / median_buys) ** 0.5) if median_buys > 0 else 1.0
        weights[code] = base_weight * max(freq_factor, 0.12)

    remaining_budget = get_remaining_investment_budget()
    time_info = year_time_info()
    estimated_annual = estimate_annual_investment(remaining_budget)
    total_weight = sum(weights.values()) or 1.0
    frac_rest = time_info["fraction_remaining"]

    by_code: dict[str, float] = {}
    reference_by_code: dict[str, float] = {}
    rows = []
    for code in ALL_BUY_INDEX_CODES:
        row = state_by_code.get(code)
        weight = weights.get(code, 0.01)
        share = remaining_budget * weight / total_weight
        annual_buys = max(float(buy_counts.get(code, 1)), 1.0)
        expected_buys_rest = max(5.0, annual_buys * frac_rest)
        snap = row["snapshot"] if row else {}
        sig = row["signal_eval"] if row else {}
        raw_amt = share / expected_buys_rest
        cap_amt = share / max(2.0, expected_buys_rest * 0.35)
        signal_cap = 250.0 if sig.get("is_buy") else 120.0
        amt = max(
            10.0,
            round(min(raw_amt, cap_amt, remaining_budget * 0.08, signal_cap)),
        )
        by_code[code] = amt
        reference_by_code[code] = amt
        rows.append(
            {
                "code": code,
                "name": row["name"] if row else code,
                "return_pct": returns.get(code),
                "buy_days": buy_counts.get(code, 0),
                "amount": amt,
                "weight": weight,
                "budget_share": share,
                "year_range_position": snap.get("year_range_position"),
                "readiness": _readiness_factor(sig) if sig else None,
                "is_buy": bool(sig.get("is_buy")) if sig else False,
                "recommended": amt > 0,
            }
        )

    rows.sort(key=lambda item: (item.get("weight") or 0, item.get("code")))
    as_of = ranking.get("as_of")

    result = {
        "rows": rows,
        "returns": dict(returns),
        "buy_counts": dict(buy_counts),
        "excluded_codes": frozenset(),
        "by_code": by_code,
        "reference_by_code": reference_by_code,
        "as_of": as_of,
        "exclude_bottom_n": 0,
        "remaining_budget": remaining_budget,
        "estimated_annual_investment": estimated_annual,
        "year_time": time_info,
        "allocation_mode": "position",
    }
    _ALLOCATION_CACHE = {"day": today, "fingerprint": light_fp, "result": result}
    _save_allocation_disk_cache(light_fp, result)
    return result


def get_position_allocation(live_quotes=None, *, force: bool = False) -> dict:
    return compute_position_allocation(live_quotes=live_quotes, force=force)


def format_allocation_note(alloc=None) -> str:
    alloc = alloc or get_position_allocation()
    as_of = alloc.get("as_of") or "最新"
    remaining = alloc.get("remaining_budget", 0)
    estimated = alloc.get("estimated_annual_investment", 0)
    time_info = alloc.get("year_time") or {}
    days_left = time_info.get("days_remaining", 0)
    return (
        f"剩余可用 **{remaining:,.0f}** 元（{time_info.get('year', '')} 年尚余约 "
        f"**{days_left}** 天）；按当前位置与买入就绪度分配；"
        f"预计全年投入 **{estimated:,.0f}** 元（截至 {as_of}）"
    )


def format_allocation_markdown_table(alloc=None) -> list[str]:
    alloc = alloc or get_position_allocation()
    lines = [
        "## 买入额度分配（位置 × 就绪度）",
        "",
        f"> {format_allocation_note(alloc)}",
        "",
        "| 指数 | 代码 | 区间位置 | 就绪度 | 策略收益 | 权重 | 单次买入 |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in reversed(alloc.get("rows", [])):
        pos = row.get("year_range_position")
        pos_text = f"{pos * 100:.0f}%" if pos is not None else "—"
        readiness = row.get("readiness")
        ready_text = f"{readiness * 100:.0f}%" if readiness is not None else "—"
        if row.get("is_buy"):
            ready_text = "买入"
        ret = row.get("return_pct")
        ret_text = f"{ret:.1f}%" if ret is not None else "—"
        weight = row.get("weight") or 0
        amt = row.get("amount", 0)
        lines.append(
            f"| {row['name']} | {row['code']} | {pos_text} | {ready_text} | "
            f"{ret_text} | {weight:.2f} | {amt:.0f} |"
        )
    lines.append("")
    return lines

# -*- coding: utf-8 -*-
"""H30269 风格组合：股息率优先选股 + 股息率加权仓位。"""

from __future__ import annotations

import pandas as pd

from dividend_lowvol_rotation.config import MAX_SINGLE_STOCK_WEIGHT


def _rank_yield_column(df: pd.DataFrame) -> str:
    return "dividend_yield_pct"


def index_rank_panel(df: pd.DataFrame, *, yield_col: str | None = None) -> pd.DataFrame:
    """指数式排序：股息率 → 低波。"""
    out = df.copy()
    ycol = yield_col or _rank_yield_column(out)
    out["yield_rank"] = out[ycol].rank(ascending=False, method="min")
    out["vol_rank"] = out["ann_vol_pct"].rank(ascending=True, method="min")
    out["spread_pct_rank"] = 1
    sort_keys = ["yield_rank", "vol_rank", "code"]
    out = out.sort_values(sort_keys)
    out["composite_score"] = out["yield_rank"]
    out["rank"] = range(1, len(out) + 1)
    return out.reset_index(drop=True)


def build_index_target_codes(
    held_codes: list[str],
    buy_pool: pd.DataFrame,
    top_n: int,
    ranked: pd.DataFrame | None = None,
) -> list[str]:
    """按排名保留老成分并补足空位，严格不超过 top_n。"""
    held = {str(c) for c in held_codes}
    order: list[str] = []
    if buy_pool is not None and not buy_pool.empty:
        order = buy_pool["code"].astype(str).tolist()
    elif ranked is not None and not ranked.empty:
        order = ranked["code"].astype(str).tolist()

    target: list[str] = []
    seen: set[str] = set()
    for code in order:
        if code in held and code not in seen:
            target.append(code)
            seen.add(code)
        if len(target) >= top_n:
            return target[:top_n]

    for code in held:
        if code not in seen:
            target.append(code)
            seen.add(code)
        if len(target) >= top_n:
            return target[:top_n]

    if buy_pool is not None and not buy_pool.empty:
        for code in buy_pool["code"].astype(str):
            if code not in seen:
                target.append(code)
                seen.add(code)
            if len(target) >= top_n:
                break
    return target[:top_n]


def yields_for_codes(
    codes: list[str],
    ranked: pd.DataFrame,
    panel: pd.DataFrame | None = None,
) -> dict[str, float]:
    """从排名/面板取股息率，用于加权。"""
    lookup: dict[str, float] = {}

    def _ingest(row: pd.Series) -> None:
        code = str(row["code"])
        if code in lookup:
            return
        yld = pd.to_numeric(row.get("dividend_yield_pct"), errors="coerce")
        if pd.notna(yld) and float(yld) > 0:
            lookup[code] = float(yld)

    if not ranked.empty and "code" in ranked.columns:
        for _, row in ranked.iterrows():
            _ingest(row)
    if panel is not None and not panel.empty:
        for _, row in panel.iterrows():
            _ingest(row)
    return {c: lookup[c] for c in codes if c in lookup and lookup[c] > 0}


def capped_dividend_yield_weights(
    yields: dict[str, float],
    *,
    max_weight: float = MAX_SINGLE_STOCK_WEIGHT,
) -> dict[str, float]:
    """股息率加权，单股上限后迭代再分配。"""
    codes = [c for c, y in yields.items() if y and y > 0]
    if not codes:
        return {}
    if len(codes) == 1:
        return {codes[0]: 1.0}

    raw = {c: float(yields[c]) for c in codes}
    total = sum(raw.values())
    weights = {c: raw[c] / total for c in codes}

    cap = max(max_weight, 1.0 / len(codes))
    for _ in range(30):
        over = [c for c, w in weights.items() if w > cap + 1e-9]
        if not over:
            break
        excess = sum(weights[c] - cap for c in over)
        for c in over:
            weights[c] = cap
        under = [c for c in weights if c not in over]
        under_sum = sum(weights[c] for c in under)
        if under_sum <= 0 or excess <= 1e-12:
            break
        for c in under:
            weights[c] += excess * (weights[c] / under_sum)

    s = sum(weights.values())
    if s <= 0:
        eq = 1.0 / len(codes)
        return {c: eq for c in codes}
    return {c: w / s for c, w in weights.items()}


def target_weights_for_portfolio(
    target_codes: list[str],
    ranked: pd.DataFrame,
    panel: pd.DataFrame | None = None,
    *,
    max_weight: float = MAX_SINGLE_STOCK_WEIGHT,
) -> dict[str, float]:
    yields = yields_for_codes(target_codes, ranked, panel)
    missing = [c for c in target_codes if c not in yields]
    if missing:
        for code in missing:
            yields[code] = 1.0
    return capped_dividend_yield_weights(yields, max_weight=max_weight)


def classify_index_portfolio(
    holdings: list[str],
    target_codes: list[str],
    panel: pd.DataFrame,
) -> dict[str, list]:
    """index_rules 模式：相对目标组合与保留规则分类持仓。"""
    from dividend_lowvol_rotation.index_retention import index_retention_fail_reason

    held = [str(c) for c in holdings if c]
    target = [str(c) for c in target_codes if c]
    target_set = set(target)
    panel_idx = (
        panel.drop_duplicates("code").set_index(panel["code"].astype(str))
        if not panel.empty and "code" in panel.columns
        else None
    )

    hold_ok: list[str] = []
    sell_watch: list[str] = []
    for code in held:
        row = panel_idx.loc[code] if panel_idx is not None and code in panel_idx.index else None
        reason = index_retention_fail_reason(row)
        if reason:
            sell_watch.append(f"{code}（{reason}）")
        elif code in target_set:
            hold_ok.append(code)
        else:
            sell_watch.append(f"{code}（调出目标组合）")

    buy_new = [c for c in target if c not in set(held)]
    not_in_pool = [
        c for c in held if panel_idx is None or c not in panel_idx.index
    ]
    return {
        "buy_new": buy_new,
        "hold_ok": hold_ok,
        "sell_watch": sell_watch,
        "not_in_pool": not_in_pool,
        "target_codes": target,
    }


def target_portfolio_table(
    target_codes: list[str],
    ranked: pd.DataFrame,
    *,
    panel: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """目标组合明细（保留 ranked 中的字段与排名）。"""
    if not target_codes or ranked.empty:
        return pd.DataFrame()
    lookup = ranked.drop_duplicates("code").set_index(ranked["code"].astype(str))
    rows = []
    for i, code in enumerate(target_codes, start=1):
        if code not in lookup.index:
            if panel is not None and not panel.empty:
                sub = panel[panel["code"].astype(str) == str(code)]
                if sub.empty:
                    continue
                row = sub.iloc[0].to_dict()
            else:
                continue
        else:
            row = lookup.loc[str(code)]
            if isinstance(row, pd.DataFrame):
                row = row.iloc[0]
            row = row.to_dict() if hasattr(row, "to_dict") else dict(row)
        row = dict(row)
        row["portfolio_rank"] = i
        rows.append(row)
    if not rows:
        return pd.DataFrame()
    out = pd.DataFrame(rows)
    codes = out["code"].astype(str).tolist()
    weights = target_weights_for_portfolio(codes, ranked, panel)
    if weights:
        out["target_weight_pct"] = out["code"].astype(str).map(
            lambda c: weights.get(c, 0) * 100.0
        )
    return out

"""基本面指标：fhps 字段。"""

from __future__ import annotations

import pandas as pd


def roe_from_eps_bps(eps, bps) -> float | None:
    try:
        eps_f = float(eps)
        bps_f = float(bps)
    except (TypeError, ValueError):
        return None
    if bps_f <= 0:
        return None
    return eps_f / bps_f * 100.0


def attach_fundamentals_from_fhps(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    if "eps" in out.columns and "bps" in out.columns:
        out["roe_pct"] = out.apply(
            lambda r: roe_from_eps_bps(r.get("eps"), r.get("bps")), axis=1
        )
    if "profit_yoy_pct" not in out.columns:
        out["profit_yoy_pct"] = None
    return out

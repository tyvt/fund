"""Shared fixed-weight return-path calculation."""

from __future__ import annotations

import numpy as np
import pandas as pd

def _fixed_weight_path(
    returns: pd.DataFrame,
    events: list[tuple[pd.Timestamp, pd.Series]],
    shifts: np.ndarray | None = None,
) -> np.ndarray:
    dates = returns.index
    output = np.zeros(len(dates), dtype=float)
    locations: list[int] = []
    for index, (day, _) in enumerate(events):
        location = int(dates.searchsorted(day))
        if shifts is not None:
            location += int(shifts[index])
        locations.append(min(max(location, 0), len(dates) - 1))
    locations = list(np.maximum.accumulate(locations))
    for index, ((_, weights), start) in enumerate(zip(events, locations)):
        stop = locations[index + 1] if index + 1 < len(locations) else len(dates)
        if stop <= start:
            continue
        active = weights.index.intersection(returns.columns)
        if active.empty:
            continue
        w = weights.reindex(active).fillna(0.0).to_numpy(dtype=float)
        w /= w.sum()
        values = returns.iloc[start:stop][active].fillna(0.0).to_numpy(dtype=float)
        # Buy-and-hold within each annual segment.  A direct ``values @ w``
        # would silently rebalance every day and materially overstate the test.
        asset_growth = np.cumprod(1.0 + values, axis=0)
        segment_nav = asset_growth @ w
        previous = np.r_[1.0, segment_nav[:-1]]
        output[start:stop] = segment_nav / previous - 1.0
    return output

__all__ = ['_fixed_weight_path']

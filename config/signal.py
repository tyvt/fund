"""信号强度与冷却期。"""

import os
from config.env import _env_bool, _env_float, _env_int

# --- 信号强度与冷却期（默认关闭：频次由硬门槛自然决定，不再人为限频）---
BUY_COOLDOWN_ENABLED = _env_bool("BUY_COOLDOWN_ENABLED", False)
BUY_COOLDOWN_DAYS = _env_int("BUY_COOLDOWN_DAYS", 10)
BUY_COOLDOWN_DROP_OVERRIDE_ENABLED = _env_bool("BUY_COOLDOWN_DROP_OVERRIDE_ENABLED", True)
BUY_COOLDOWN_DROP_OVERRIDE_PCT = _env_float("BUY_COOLDOWN_DROP_OVERRIDE_PCT", 0.05)
BUY_COOLDOWN_AMOUNT_SCALE_ENABLED = _env_bool("BUY_COOLDOWN_AMOUNT_SCALE_ENABLED", True)
BUY_COOLDOWN_AMOUNT_MAX_MULTIPLIER = _env_float("BUY_COOLDOWN_AMOUNT_MAX_MULTIPLIER", 3.0)
BUY_COOLDOWN_AMOUNT_LOOKBACK_DAYS = _env_int("BUY_COOLDOWN_AMOUNT_LOOKBACK_DAYS", 756)
# 冷却开启时，这些指数仍可单独豁免（历史兼容；默认关闭冷却后无效）
BUY_COOLDOWN_DISABLED_CODES = frozenset(
    c.strip().upper()
    for c in os.environ.get("BUY_COOLDOWN_DISABLED_CODES", "NDX,SPX").split(",")
    if c.strip()
)
SIGNAL_STRENGTH_STRONG_MIN = _env_int("SIGNAL_STRENGTH_STRONG_MIN", 60)
SIGNAL_STRENGTH_ELIGIBLE_MIN = _env_int("SIGNAL_STRENGTH_ELIGIBLE_MIN", 40)
SIGNAL_NEAR_BUY_MARGIN_PCT = _env_float("SIGNAL_NEAR_BUY_MARGIN_PCT", 5.0)
SIGNAL_COMPARISON_ENABLED = _env_bool("SIGNAL_COMPARISON_ENABLED", True)


def buy_cooldown_enabled(index_code: str | None = None) -> bool:
    """单只指数是否应用买入冷却期。"""
    if not BUY_COOLDOWN_ENABLED:
        return False
    if index_code and index_code.upper() in BUY_COOLDOWN_DISABLED_CODES:
        return False
    return True


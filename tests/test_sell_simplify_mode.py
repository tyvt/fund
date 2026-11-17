"""卖出端简化模式守卫的单测：验证 off/a/b 三分支传给底层模拟器的参数。"""
import os
import pytest

import backtest_trade_signals as bts


@pytest.fixture(autouse=True)
def _clean_mode(monkeypatch):
    monkeypatch.delenv("SELL_SIMPLIFY_MODE", raising=False)
    yield


def _capture(monkeypatch):
    """替换两个模拟器，记录 _run_index_trades 实际传入的守卫后参数。"""
    seen = {}

    def fake_trailing(panel, start, end, **kw):
        seen["path"] = "trailing"
        seen.update(kw)
        return {"sentinel": 1}

    def fake_plain(panel, start, end, **kw):
        seen["path"] = "plain"
        seen.update(kw)
        return {"sentinel": 2}

    monkeypatch.setattr(bts, "simulate_trades_trailing", fake_trailing)
    monkeypatch.setattr(bts, "simulate_trades", fake_plain)
    return seen


def _call(has_sell=True, trailing_cfg=None, valuation_sell_fn=None):
    bts._run_index_trades(
        panel=object(), code="000688", start_date="2020-01-01", end_date="2020-12-31",
        sim_amt=100.0, buy_fn=None, sell_fn=lambda row: False, has_sell=has_sell,
        trailing_cfg=trailing_cfg, valuation_sell_fn=valuation_sell_fn,
    )


def test_sell_simplify_mode_default_and_invalid(monkeypatch):
    assert bts._sell_simplify_mode() == "off"
    monkeypatch.setenv("SELL_SIMPLIFY_MODE", "A")
    assert bts._sell_simplify_mode() == "a"
    monkeypatch.setenv("SELL_SIMPLIFY_MODE", "b")
    assert bts._sell_simplify_mode() == "b"
    monkeypatch.setenv("SELL_SIMPLIFY_MODE", "garbage")
    assert bts._sell_simplify_mode() == "off"


def test_mode_off_keeps_original_cfg(monkeypatch):
    seen = _capture(monkeypatch)
    monkeypatch.setenv("SELL_SIMPLIFY_MODE", "off")
    cfg = {"trailing_drawdown_pct": 0.12}
    _call(has_sell=True, trailing_cfg=cfg, valuation_sell_fn=lambda row: True)
    assert seen["path"] == "trailing"
    assert seen["trailing_cfg"] is cfg
    assert callable(seen["valuation_sell_fn"])


def test_mode_a_single_trailing_only(monkeypatch):
    seen = _capture(monkeypatch)
    monkeypatch.setenv("SELL_SIMPLIFY_MODE", "a")
    _call(has_sell=True, trailing_cfg={"tier_sell_fn": lambda row: ["trend"]},
          valuation_sell_fn=lambda row: True)
    assert seen["path"] == "trailing"
    cfg = seen["trailing_cfg"]
    assert cfg["trailing_drawdown_pct"] == 0.15
    assert cfg["min_unrealized_gain_pct"] == 0.50
    assert cfg["min_hold_days"] == 90
    assert "tier_sell_fn" not in cfg
    assert seen["valuation_sell_fn"] is None


def test_mode_b_no_active_sell(monkeypatch):
    seen = _capture(monkeypatch)
    monkeypatch.setenv("SELL_SIMPLIFY_MODE", "b")
    _call(has_sell=True, trailing_cfg={"trailing_drawdown_pct": 0.12},
          valuation_sell_fn=lambda row: True)
    assert seen["path"] == "plain"
    assert seen["has_sell"] is False
    # simulate_trades(fallback) 不接收 trailing_cfg/valuation_sell_fn，
    # 两者不被传入即证明 mode=b 彻底关闭了移动止盈与估值卖点。
    assert "trailing_cfg" not in seen
    assert "valuation_sell_fn" not in seen

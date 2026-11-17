"""A 方案（单一粗移动止盈）单测：trailing_sell_hit 边界 + 三信号卖出口径字段。"""
from sell_trailing import trailing_sell_hit


def _hit(close, cost, peak, days=None):
    return trailing_sell_hit(
        close=close,
        cost_basis=cost,
        peak_price=peak,
        min_unrealized_gain_pct=0.50,
        trailing_drawdown_pct=0.15,
        min_hold_days=90,
        days_since_buy=days,
    )


def test_gain_boundary_exactly_50pct_not_sold():
    # 浮盈刚好=50%，但未回撤 -> 不卖
    assert _hit(close=150.0, cost=100.0, peak=150.0, days=200) is False
    # 浮盈<50% 即便回撤大也不卖（未达浮盈门槛）
    assert _hit(close=120.0, cost=100.0, peak=200.0, days=200) is False


def test_drawdown_boundary_exactly_15pct():
    # 峰值 200，回撤刚好 15% -> close=170，浮盈 70% -> 触发
    assert _hit(close=170.0, cost=100.0, peak=200.0, days=200) is True
    # 回撤 14.9%（close=170.2）->
    assert _hit(close=170.2, cost=100.0, peak=200.0, days=200) is False


def test_hold_days_boundary_90():
    # 浮盈/回撤均达标，持有 89 天 -> 不卖；90 天 -> 卖
    assert _hit(close=170.0, cost=100.0, peak=200.0, days=89) is False
    assert _hit(close=170.0, cost=100.0, peak=200.0, days=90) is True


def test_missing_peak_no_false_sell():
    assert _hit(close=170.0, cost=100.0, peak=None, days=200) is False
    assert _hit(close=None, cost=100.0, peak=200.0, days=200) is False
    assert _hit(close=170.0, cost=None, peak=200.0, days=200) is False


def test_cn_broad_sell_fields():
    from cn_broad_signal import evaluate_cn_broad_sell

    snap = {
        "code": "000688",
        "close": 170.0,
        "recent_signal_buy_avg": 100.0,
        "peak_since_last_buy": 200.0,
        "days_since_last_buy": 200,
    }
    out = evaluate_cn_broad_sell(snap)
    assert out["is_sell"] is True
    assert out["sell_summary"] and "移动止盈" in out["sell_summary"]
    assert out["sell_criteria"][0]["passed"] is True

    # 未达条件
    snap2 = dict(snap, close=120.0)
    out2 = evaluate_cn_broad_sell(snap2)
    assert out2["is_sell"] is False
    assert out2["sell_summary"] is None


def test_cyb_sell_decision_fields():
    from cyb_signal import cyb_valuation_sell_decision

    snap = {
        "close": 170.0,
        "recent_signal_buy_avg": 100.0,
        "peak_since_last_buy": 200.0,
        "days_since_last_buy": 200,
    }
    is_sell, reasons = cyb_valuation_sell_decision(snap)
    assert is_sell is True
    assert reasons and "移动止盈" in reasons[0]

    is_sell2, reasons2 = cyb_valuation_sell_decision(dict(snap, close=120.0))
    assert is_sell2 is False
    assert reasons2 == []

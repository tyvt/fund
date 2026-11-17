from valuation_trend_sell import drawdown_amount_multiplier


def test_drawdown_bands():
    assert drawdown_amount_multiplier(0.20) == 1.0
    assert drawdown_amount_multiplier(0.31) == 1.5
    assert drawdown_amount_multiplier(0.45) == 2.0
    assert drawdown_amount_multiplier(None) == 1.0

from valuation_trend_sell import (
    TIER_DIV_SPREAD,
    TIER_DIV_YIELD,
    TIER_EXTREME,
    TIER_TREND,
    TIER_VALUATION,
    active_sell_tiers,
    dividend_sell_tiers,
    drawdown_amount_multiplier,
    units_for_new_tiers,
)


def test_buy_day_blocks_sell():
    tiers = active_sell_tiers(
        pe_percentile=95,
        spread_percentile=5,
        below_ma=True,
        ma_slope_pct=-0.05,
        is_buy=True,
    )
    assert tiers == []


def test_extreme_covers_partial_tiers():
    tiers = active_sell_tiers(pe_percentile=91, below_ma=True, ma_slope_pct=-0.03)
    assert tiers == [TIER_EXTREME]


def test_valuation_and_trend_can_both_fire():
    tiers = active_sell_tiers(
        pe_percentile=82, spread_percentile=50, below_ma=True, ma_slope_pct=-0.03
    )
    assert tiers == [TIER_VALUATION, TIER_TREND]


def test_same_tier_sells_once():
    already = set()
    sold, full, marked = units_for_new_tiers(
        [TIER_VALUATION], already, units=90, initial_units=90
    )
    assert not full
    assert abs(sold - 30) < 1e-9
    already.update(marked)
    sold_again, _, marked_again = units_for_new_tiers(
        [TIER_VALUATION], already, units=60, initial_units=90
    )
    assert sold_again == 0
    assert marked_again == []


def test_extreme_exits_remainder():
    sold, full, _ = units_for_new_tiers(
        [TIER_EXTREME], {TIER_VALUATION}, units=60, initial_units=90
    )
    assert full
    assert sold == 60


def test_progressive_pe_bands():
    from valuation_trend_sell import TIER_PE_MID, pe_sell_bounds

    assert pe_sell_bounds("000688") == (80.0, 85.0, 90.0)
    assert pe_sell_bounds("399006") == (80.0, 85.0, 90.0)
    assert active_sell_tiers(pe_percentile=82) == [TIER_VALUATION]
    assert active_sell_tiers(pe_percentile=86) == [TIER_VALUATION, TIER_PE_MID]
    sell, full, marked = units_for_new_tiers(
        [TIER_VALUATION, TIER_PE_MID], set(), 90, 90
    )
    assert (sell, full) == (60, False)
    assert set(marked) == {TIER_VALUATION, TIER_PE_MID}


def test_dividend_spread_and_yield_tiers():
    assert dividend_sell_tiers(dividend_yield=0.046, spread=0.024) == [TIER_DIV_SPREAD]
    assert dividend_sell_tiers(dividend_yield=0.034, spread=0.024) == [
        TIER_DIV_SPREAD,
        TIER_DIV_YIELD,
    ]
    assert dividend_sell_tiers(dividend_yield=0.029, spread=0.02) == [TIER_EXTREME]
    assert dividend_sell_tiers(dividend_yield=0.04, spread=0.014) == [TIER_EXTREME]
    assert dividend_sell_tiers(dividend_yield=0.02, spread=0.01, is_buy=True) == []
    assert dividend_sell_tiers(dividend_yield=None, spread=None) == []


def test_drawdown_bands():
    assert drawdown_amount_multiplier(0.20) == 1.0
    assert drawdown_amount_multiplier(0.31) == 1.5
    assert drawdown_amount_multiplier(0.45) == 2.0
    assert drawdown_amount_multiplier(None) == 1.0

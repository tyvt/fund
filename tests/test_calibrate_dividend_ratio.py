from __future__ import annotations

import pandas as pd

import cn_broad_data
import dividend_data


def _sample():
    return pd.DataFrame(
        {
            "date": ["20240101", "20240102"],
            "dividend_yield": [0.05, 0.03],
            "pe": [10.0, 20.0],
        }
    )


def test_calibrate_dividend_ratio_basic():
    df = _sample()
    # (0.05*10 + 0.03*20) / 2 = (0.5 + 0.6) / 2 = 0.55
    assert dividend_data.calibrate_dividend_ratio(df) == 0.55


def test_calibrate_dividend_ratio_none_and_empty():
    assert dividend_data.calibrate_dividend_ratio(None) is None
    assert dividend_data.calibrate_dividend_ratio(pd.DataFrame()) is None


def test_cn_broad_reuses_same_implementation():
    # 合并后 cn_broad_data 复用 dividend_data 的实现，行为必须一致
    assert (
        cn_broad_data.calibrate_dividend_ratio
        is dividend_data.calibrate_dividend_ratio
    )
    df = _sample()
    assert cn_broad_data.calibrate_dividend_ratio(df) == dividend_data.calibrate_dividend_ratio(df)

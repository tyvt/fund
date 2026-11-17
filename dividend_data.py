# -*- coding: utf-8 -*-
"""股息率校准系数估算（供科创50等链路复用）。"""

import pandas as pd


def calibrate_dividend_ratio(indicator):
    """用官方股息率与 PE 校准估算系数。"""
    if indicator is None or indicator.empty:
        return None
    return float((indicator["dividend_yield"] * indicator["pe"]).mean())

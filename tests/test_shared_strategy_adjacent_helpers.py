from __future__ import annotations

import numpy as np
import pandas as pd

from scripts.baseline_validation import _fixed_weight_path
from scripts.rqalpha_paths import DEFAULT_RQALPHA_BUNDLE_PATH, resolve_rqalpha_bundle_path
from scripts.rqalpha_symbols import from_rqalpha_id, normalize_stock_code, to_rqalpha_id


def test_rqalpha_symbol_conversions():
    assert normalize_stock_code("1") == "000001"
    assert to_rqalpha_id("600000") == "600000.XSHG"
    assert to_rqalpha_id("000001") == "000001.XSHE"
    assert from_rqalpha_id("000001.XSHE") == "000001"


def test_bundle_path_resolution(monkeypatch, tmp_path):
    monkeypatch.delenv("RQALPHA_BUNDLE_PATH", raising=False)
    assert resolve_rqalpha_bundle_path() == DEFAULT_RQALPHA_BUNDLE_PATH
    monkeypatch.setenv("RQALPHA_BUNDLE_PATH", "")
    assert resolve_rqalpha_bundle_path() == DEFAULT_RQALPHA_BUNDLE_PATH
    missing = str(tmp_path / "missing-bundle")
    assert resolve_rqalpha_bundle_path(missing) == missing
    custom = str(tmp_path / "custom-bundle")
    assert resolve_rqalpha_bundle_path(custom) == custom
    monkeypatch.setenv("RQALPHA_BUNDLE_PATH", custom)
    assert resolve_rqalpha_bundle_path() == custom


def test_fixed_weight_path_remains_available():
    dates = pd.date_range("2024-01-01", periods=2, freq="D")
    returns = pd.DataFrame({"A": [0.1, -0.1]}, index=dates)
    path = _fixed_weight_path(returns, [(dates[0], pd.Series({"A": 1.0}))])
    assert np.allclose(path, [0.1, -0.1])

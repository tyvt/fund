"""walk-forward 窗口切分的边界单测（不跑回测，纯函数）。"""
import pytest

from scripts.walk_forward_validation import build_windows


def test_basic_rolling():
    # 2015-2020, window=3, step=1 -> 3 窗: 15-17,16-18,17-19? 末窗截断到 20
    w = build_windows(2015, 2020, 3, 1)
    assert w[0] == (0, 2017, "2015-01-01", "2017-12-31")
    assert w[1] == (1, 2018, "2016-01-01", "2018-12-31")
    # 末窗 cursor=2018 -> 2018-2020（截断到 end_year）
    assert w[-1][1] == 2020
    assert w[-1][2].startswith("2018")
    assert w[-1][3].endswith("2020-12-31")


def test_step_equals_window_no_overlap():
    w = build_windows(2015, 2020, 3, 3)
    assert w[0] == (0, 2017, "2015-01-01", "2017-12-31")
    assert w[1][2].startswith("2018")


def test_window_larger_than_range_truncates():
    w = build_windows(2015, 2016, 5, 1)
    assert len(w) == 1
    assert w[0] == (0, 2016, "2015-01-01", "2016-12-31")


def test_single_year_range_invalid():
    assert build_windows(2015, 2015, 3, 1) == []


def test_invalid_params():
    assert build_windows(2015, 2020, 0, 1) == []
    assert build_windows(2015, 2020, 3, 0) == []
    assert build_windows(2020, 2015, 3, 1) == []


def test_no_gap_or_overlap_beyond_window():
    w = build_windows(2015, 2026, 3, 1)
    for idx, end_year, s, e in w:
        assert s[:4].isdigit() and e[:4].isdigit()
        assert int(s[:4]) <= int(e[:4])
        assert int(e[:4]) - int(s[:4]) + 1 <= 3
        assert int(e[:4]) <= 2026

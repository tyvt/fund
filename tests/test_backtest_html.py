"""backtest_html 当日建议区块与刷新逻辑单测。"""

from backtest_html import (
    DAILY_ADVICE_END,
    DAILY_ADVICE_START,
    _advice_change_and_amount,
    _write_block,
    render_daily_advice_block,
)


def test_advice_amount_and_multiplier():
    up = _advice_change_and_amount({"text": "实时 1,221.19 +0.62% 20261117"})
    down = _advice_change_and_amount({"text": "实时 2,292.20 -1.20% 20261117"})
    missing = _advice_change_and_amount({"text": "无实时数据"})
    assert round(up[1], 2) == 0.94 and up[2] == 47
    assert round(down[1], 2) == 1.12 and down[2] == 56
    assert missing[1] == 1.0 and missing[2] == 50


def _html(with_advice=True, twice=False):
    block = render_daily_advice_block()
    advice = (block + block) if twice else (block if with_advice else "")
    return (
        "<!DOCTYPE html><html><body>"
        f"{advice}"
        '<div id="chart"></div><script>const ALL_DATA = {{"000688": {{}}}};</script>'
        "</body></html>"
    )


def test_render_block_has_unique_markers():
    block = render_daily_advice_block()
    assert block.count(DAILY_ADVICE_START) == 1
    assert block.count(DAILY_ADVICE_END) == 1
    assert 'id="daily-advice"' in block


def test_write_block_replace_marker(tmp_path):
    path = tmp_path / "a.html"
    original = _html()
    path.write_text(original, encoding="utf-8")
    new_block = render_daily_advice_block([{"code": "000688", "name": "科创50", "signal_short": "买入", "text": "买入文本"}])
    _write_block(path, new_block, strict=False, original=original)
    out = path.read_text(encoding="utf-8")
    assert out.count(DAILY_ADVICE_START) == 1
    assert "科创50" in out
    assert "const ALL_DATA" in out


def test_write_block_insert_when_missing(tmp_path):
    path = tmp_path / "b.html"
    original = _html(with_advice=False)
    path.write_text(original, encoding="utf-8")
    new_block = render_daily_advice_block()
    _write_block(path, new_block, strict=False, original=original)
    out = path.read_text(encoding="utf-8")
    assert out.index(DAILY_ADVICE_START) < out.index('<div id="chart"></div>')
    assert "const ALL_DATA" in out


def test_write_block_abort_on_duplicate_markers(tmp_path):
    path = tmp_path / "c.html"
    original = _html(twice=True)
    path.write_text(original, encoding="utf-8")
    try:
        _write_block(path, render_daily_advice_block(), strict=False, original=original)
    except RuntimeError:
        pass
    else:
        raise AssertionError("多标记应抛错中止")
    assert path.read_text(encoding="utf-8") == original


def test_nested_content_preserved(tmp_path):
    path = tmp_path / "d.html"
    original = _html()
    path.write_text(original, encoding="utf-8")
    nested = render_daily_advice_block([{"code": "X", "name": "带嵌套", "signal_short": "观望", "text": "<section>nested</section>"}])
    _write_block(path, nested, strict=False, original=original)
    out = path.read_text(encoding="utf-8")
    assert "带嵌套" in out
    assert "const ALL_DATA" in out

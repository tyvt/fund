# -*- coding: utf-8 -*-
"""验证排雷因子是否在暴雷前剔除标的（无未来信息）。"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dividend_lowvol_rotation.dividend import load_fhps_all_records
from dividend_lowvol_rotation.risk_screening import (
    batch_load_risk_history,
    check_risk_exclusion_timeline,
    risk_filter_mask,
    attach_risk_from_records,
    merge_risk_history,
    build_dividend_year_index,
)

CASES = [
    ("600518", "康美药业", "2018-01-01", "2019-06-30", "2019-04-30"),
    ("002450", "康得新", "2018-01-01", "2020-06-30", "2020-05-06"),
]


def main() -> int:
    records = load_fhps_all_records(refresh=False, backtest_start="2015-01-01")
    for code, name, start, end, event in CASES:
        hist = batch_load_risk_history([code])
        timeline = check_risk_exclusion_timeline(
            code, records, hist, start=start, end=end, event_date=event
        )
        first_fail = timeline[timeline["passed_risk"] == False]
        first_fail_date = first_fail["as_of"].iloc[0] if not first_fail.empty else None
        print(f"{code} {name} 暴雷参考日 {event}")
        print(f"  首次未通过排雷: {first_fail_date}")
        if first_fail_date:
            ok = first_fail_date < event
            print(f"  暴雷前剔除: {'是' if ok else '否（存在未来信息风险）'}")
        else:
            print("  区间内始终通过排雷（或数据不足）")
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

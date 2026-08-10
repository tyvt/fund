"""数据源交叉验证：baostock 行情、国债补全、ETF 跟踪、美股 PE 多源比对。"""



from __future__ import annotations



import sys

from datetime import date, timedelta

from pathlib import Path



import baostock as bs

import pandas as pd



PROJECT_DIR = Path(__file__).resolve().parent

sys.path.insert(0, str(PROJECT_DIR))



from config import CYB_INDEX, KC50_INDEX, ZZ1000_INDEX, INDICES

from cyb_data import fetch_cyb_pe_szse_official, fetch_cyb_price_history

from data_crosscheck import (

    bond_history_coverage,

    check_cyb_pe_against_szse_index,

    check_index_etf_return_correlation,

    compare_us_forward_pe_sources,

)

from dividend_data import build_signal_history

from market_data import (

    get_gov_bond_yield_history,

    get_index_perf_history,

    read_indicator_history,

)



BAOSTOCK_INDEX_MAP = {

    ZZ1000_INDEX["code"]: ("sh.000852", "中证1000"),

    CYB_INDEX["code"]: ("sz.399006", "创业板指"),

}



UNVALIDATED_BY_BAOSTOCK = {

    INDICES[0]["code"]: "中证红利低波动（H30269）— baostock 无此策略指数，改用 ETF 日收益抽检",

    KC50_INDEX["code"]: "科创50（000688）— baostock 无指数 K 线（sh.000688 为个股）",

}





def _fetch_baostock_kline(bs_code: str, start: str, end: str) -> pd.DataFrame:

    rs = bs.query_history_k_data_plus(

        bs_code,

        "date,close",

        start_date=start,

        end_date=end,

        frequency="d",

        adjustflag="3",

    )

    rows = []

    while rs.error_code == "0" and rs.next():

        rows.append(rs.get_row_data())

    if not rows:

        return pd.DataFrame(columns=["date", "close"])

    out = pd.DataFrame(rows, columns=rs.fields)

    out["date"] = pd.to_datetime(out["date"]).dt.date

    out["close"] = pd.to_numeric(out["close"], errors="coerce")

    return out.dropna(subset=["date", "close"])





def _load_project_close(index_code: str, source: str) -> pd.DataFrame:

    if source == "perf":

        df = get_index_perf_history(index_code, years=None)

        if df is None or df.empty:

            return pd.DataFrame(columns=["date", "close"])

        out = df[["date", "close"]].copy()

        out["date"] = pd.to_datetime(out["date"]).dt.date

        return out.dropna()

    if source == "cyb_price":

        df = fetch_cyb_price_history()

        out = df[["date", "close"]].copy()

        out["date"] = pd.to_datetime(out["date"]).dt.date

        return out.dropna()

    raise ValueError(source)





def compare_close(

    index_code: str,

    bs_code: str,

    name: str,

    project_source: str,

    years: int = 5,

) -> dict:

    end = date.today()

    start = (end - timedelta(days=365 * years)).strftime("%Y-%m-%d")

    end_s = end.strftime("%Y-%m-%d")



    proj = _load_project_close(index_code, project_source)

    bs_df = _fetch_baostock_kline(bs_code, start, end_s)



    proj = proj[(proj["date"] >= pd.Timestamp(start).date()) & (proj["date"] <= end)]

    merged = proj.merge(bs_df, on="date", how="inner", suffixes=("_proj", "_bs"))

    if merged.empty:

        return {

            "index": index_code,

            "name": name,

            "bs_code": bs_code,

            "overlap_days": 0,

            "status": "无重叠样本",

        }



    merged["diff_pct"] = (merged["close_proj"] - merged["close_bs"]).abs() / merged["close_bs"]

    tol = 0.001

    bad = merged[merged["diff_pct"] > tol]

    max_diff = float(merged["diff_pct"].max())

    mean_diff = float(merged["diff_pct"].mean())

    med_diff = float(merged["diff_pct"].median())



    status = "可靠"

    if max_diff > 0.01:

        status = "偏差较大"

    elif max_diff > tol:

        status = "轻微偏差"



    return {

        "index": index_code,

        "name": name,

        "bs_code": bs_code,

        "overlap_days": len(merged),

        "proj_days": len(proj),

        "bs_days": len(bs_df),

        "max_diff_pct": max_diff * 100,

        "mean_diff_pct": mean_diff * 100,

        "median_diff_pct": med_diff * 100,

        "bad_days": len(bad),

        "status": status,

    }





def check_indicator_vs_perf(index_code: str, name: str) -> dict:

    indicator = read_indicator_history(index_code)

    perf = get_index_perf_history(index_code, years=1)

    if indicator is None or indicator.empty or perf is None or perf.empty:

        return {"index": index_code, "name": name, "status": "数据缺失"}



    ind = indicator.copy()

    ind["date"] = pd.to_datetime(ind["date"]).dt.date

    perf = perf.copy()

    perf["date"] = pd.to_datetime(perf["date"]).dt.date

    merged = ind.merge(perf[["date", "close", "rolling_pe"]], on="date", how="inner")

    if merged.empty:

        return {"index": index_code, "name": name, "status": "无重叠日期"}



    merged["pe_diff_pct"] = (merged["pe"] - merged["rolling_pe"]).abs() / merged["rolling_pe"]

    merged["div_implied"] = merged["dividend_yield"] * merged["pe"]

    return {

        "index": index_code,

        "name": name,

        "overlap_days": len(merged),

        "pe_max_diff_pct": float(merged["pe_diff_pct"].max() * 100),

        "pe_mean_diff_pct": float(merged["pe_diff_pct"].mean() * 100),

        "div_pe_ratio_mean": float(merged["div_implied"].mean()),

        "status": "可靠" if merged["pe_diff_pct"].max() < 0.05 else "PE口径有差异",

    }





def check_bond_yield(*, refresh: bool = False) -> dict:

    if refresh:

        from data_cache import cache_path, save_dataframe

        from market_data import _fetch_gov_bond_yield_history



        fresh = _fetch_gov_bond_yield_history()

        if fresh is not None and not fresh.empty:

            save_dataframe(cache_path("bond_yield_history", subdir="cn"), fresh)



    hist = get_gov_bond_yield_history()

    if hist is None or hist.empty:

        return {"status": "数据缺失"}



    hist = hist.copy()

    hist["date"] = pd.to_datetime(hist["date"])

    latest = hist.iloc[-1]

    gaps = hist["date"].diff().dt.days

    big_gaps = int((gaps > 5).sum())



    panel = build_signal_history(INDICES[0]["code"])

    coverage = (

        bond_history_coverage(panel["date"])

        if panel is not None and not panel.empty

        else {}

    )

    return {

        "rows": len(hist),

        "start": str(hist["date"].min().date()),

        "latest_date": str(latest["date"].date()),

        "latest_yield_pct": float(latest["bond_yield"] * 100),

        "big_gap_count": big_gaps,

        "h30269_fallback_pct": coverage.get("fallback_pct"),

        "status": "可靠（日频全历史）" if len(hist) > 3000 and big_gaps < 30 else "需检查",

    }





def check_cyb_valuation() -> dict:

    from cyb_data import (

        fetch_cyb_dividend_history,

        fetch_cyb_pb_history,

        fetch_cyb_price_history,

    )



    pe = fetch_cyb_pe_szse_official()

    pb = fetch_cyb_pb_history()

    div = fetch_cyb_dividend_history()

    price = fetch_cyb_price_history()

    szse_check = check_cyb_pe_against_szse_index()



    pe_gap = pe["date"].diff().dt.days.max() if len(pe) > 1 else 0

    return {

        "pe_rows": len(pe),

        "pe_source": "深交所创业板（乐咕 marketId=4）",

        "pe_max_gap_days": float(pe_gap) if pd.notna(pe_gap) else None,

        "pb_rows": len(pb),

        "div_rows": len(div),

        "price_rows": len(price),

        "szse_sanity": szse_check,

        "status": "深交所官方口径 + 收盘价日度折算",

    }





def main(argv=None) -> int:

    import argparse



    parser = argparse.ArgumentParser(description="数据源交叉验证")

    parser.add_argument(

        "--refresh-bond",

        action="store_true",

        help="强制重新分页拉取国债历史并覆盖本地缓存",

    )

    args = parser.parse_args(argv)



    print("=" * 60)

    print("数据源可靠性验证")

    print("=" * 60)



    lg = bs.login()

    if lg.error_code != "0":

        print(f"baostock 登录失败: {lg.error_msg}")

        return 1



    print("\n## 1. 收盘价（baostock vs 项目）\n")

    for code, (bs_code, name) in BAOSTOCK_INDEX_MAP.items():

        source = "perf" if code != CYB_INDEX["code"] else "cyb_price"

        r = compare_close(code, bs_code, name, source, years=5)

        print(

            f"  {name} ({code}): 重叠 {r.get('overlap_days', 0)} 天, "

            f"最大偏差 {r.get('max_diff_pct', '—'):.4f}%, {r.get('status', '—')}"

        )



    print("\n## 2. H30269 跟踪 ETF 日收益相关性\n")

    for r in check_index_etf_return_correlation(INDICES[0]["code"], years=5):

        print(f"  ETF {r.get('etf', '—')}: {r.get('note', '')}")

        if "return_corr" in r:

            print(

                f"    重叠 {r['overlap_days']} 天, 相关系数 {r['return_corr']}, "

                f"判定 {r['status']}"

            )

        else:

            print(f"    状态: {r.get('status', '—')}")



    print("\n## 3. baostock 无法直接覆盖的指数\n")

    for code, note in UNVALIDATED_BY_BAOSTOCK.items():

        print(f"  - {code}: {note}")



    print("\n## 4. 估值指标内部一致性\n")

    for code, name in [

        (INDICES[0]["code"], INDICES[0]["name"]),

        (ZZ1000_INDEX["code"], ZZ1000_INDEX["name"]),

        (KC50_INDEX["code"], KC50_INDEX["name"]),

    ]:

        r = check_indicator_vs_perf(code, name)

        if "overlap_days" in r:

            print(

                f"  {name}: PE最大偏差 {r['pe_max_diff_pct']:.2f}%, {r['status']}"

            )

        else:

            print(f"  {name}: {r['status']}")



    print("\n## 5. 国债收益率（分页补全后）\n")

    bond = check_bond_yield(refresh=args.refresh_bond)

    for k, v in bond.items():

        print(f"  {k}: {v}")



    print("\n## 6. 创业板估值（深交所 PE）\n")

    cyb = check_cyb_valuation()

    for k, v in cyb.items():

        print(f"  {k}: {v}")



    print("\n## 7. 美股 Forward PE 多源比对\n")

    for key in ("spx", "ndx"):

        r = compare_us_forward_pe_sources(key)

        print(f"  [{key.upper()}] {r.get('status', '—')}")

        for k in (

            "months",

            "latest_month",

            "latest_hom_forward_date",

            "latest_hom_forward_pe",

            "latest_hom_trailing_pe",

            "barrons_as_of",

            "barrons_forward_pe",

            "barrons_trailing_pe",

            "barrons_vs_hom_forward_diff",

            "barrons_vs_hom_forward_diff_pct",

            "barrons_status",

            "latest_multpl_trailing_pe",

            "hom_trailing_vs_multpl_max_abs_diff",

            "hom_forward_vs_trailing_latest",

            "multpl_status",

            "yardeni_note",

        ):

            if k in r:

                print(f"    {k}: {r[k]}")



    bs.logout()

    print("\n验证完成。")

    if not args.refresh_bond:

        print("提示: 若国债行数仍偏少，请运行 python validate_data_baostock.py --refresh-bond")

    return 0





if __name__ == "__main__":

    raise SystemExit(main())


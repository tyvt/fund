"""项目数据源地址注册表与说明。

本文件汇总所有脚本用到的行情、估值、推送接口地址。
业务阈值、策略参数仍在 config.py；本文件仅负责数据源元信息与 URL 模板。

使用方式
--------
- 脚本通过 config.py 导入 URL 常量（config 默认值来自本文件，可被 push.env 覆盖）
- 查阅 DATA_SOURCES 可了解每个接口的提供方、用途与调用入口
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# 中证指数（CSIndex）
# ---------------------------------------------------------------------------
CSINDEX_INDICATOR_BASE_URL = (
    "https://oss-ch.csindex.com.cn/static/html/csindex/public/"
    "uploads/file/autofile/indicator"
)
"""中证指数官方指标 Excel 根路径。完整地址：{BASE}/{指数代码}indicator.xls"""

CSINDEX_CLOSEWEIGHT_BASE_URL = (
    "https://oss-ch.csindex.com.cn/static/html/csindex/public/"
    "uploads/file/autofile/closeweight"
)
"""中证指数成分股权重 Excel 根路径。完整地址：{BASE}/{指数代码}closeweight.xls"""

INDEX_PERF_URL = "https://www.csindex.com.cn/csindex-home/perf/index-perf"
"""中证指数历史行情 API（日频收盘价、滚动 PE、成交额等）。GET，参数 indexCode / startDate / endDate"""


# ---------------------------------------------------------------------------
# 东方财富（East Money）
# ---------------------------------------------------------------------------
BOND_YIELD_URL = "https://datacenter.eastmoney.com/api/data/get"
"""中国 10 年期国债收益率历史。type=RPTA_WEB_TREASURYYIELD，字段 EMM00166466"""


# ---------------------------------------------------------------------------
# 美国联邦储备经济数据库（FRED）
# ---------------------------------------------------------------------------
FRED_CSV_BASE_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv"
"""FRED 公开 CSV 导出接口。完整地址：{BASE}?id={系列代码}"""

FRED_NASDAQ100_SERIES = "NASDAQ100"
"""纳斯达克 100 价格指数日频序列（1986 年起）"""

FRED_SP500_SERIES = "SP500"
"""标普 500 价格指数日频序列"""

FRED_US10Y_SERIES = "DGS10"
"""美国 10 年期国债收益率日频序列（1962 年起，百分比数值）"""


# ---------------------------------------------------------------------------
# History of Market（美股指数估值）
# ---------------------------------------------------------------------------
HISTORY_OF_MARKET_NDX_FORWARD_PE_URL = (
    "https://historyofmarket.com/api/ndx/forward-pe.json"
)
"""纳斯达克 100 市值加权 TTM PE（日频）+ Bloomberg 一致预期 Forward PE（月频，2001 年起）"""

HISTORY_OF_MARKET_SPX_FORWARD_PE_URL = (
    "https://historyofmarket.com/api/sp500/forward-pe.json"
)
"""标普 500 市值加权 TTM PE（日频）+ Bloomberg 一致预期 Forward PE（月频）"""

NASDAQ_ETF_SUMMARY_URL = (
    "https://api.nasdaq.com/api/quote/{symbol}/summary?assetclass=etf"
)
"""纳斯达克 ETF 摘要（用于 QQQ 股息率代理 NDX）"""


# ---------------------------------------------------------------------------
# Yale Shiller 长期市场数据
# ---------------------------------------------------------------------------
SHILLER_IE_DATA_URL = "http://www.econ.yale.edu/~shiller/data/ie_data.xls"
"""Robert Shiller《Irrational Exuberance》配套 Excel；含 S&P Composite 月频价格（1871 年起）"""

MULTPL_SP500_PE_TABLE_URL = "https://www.multpl.com/s-p-500-pe-ratio/table/by-month"
"""Multpl.com 标普 500 滚动市盈率月频表（用于交叉校验，非 Forward PE）"""

YARDENI_FORWARD_PE_CHART_URL = "https://yardeni.com/charts/forward-p-es/"
"""Yardeni Forward P/E 图表页（无公开 CSV；仅人工对照）"""

BARRONS_PE_YIELDS_URL = "https://www.barrons.com/market-data/stocks/us/pe-yields"
"""Barron's / WSJ Birinyi P/E & Yields 页；含 S&P 500 / NASDAQ 100 Forward PE（周频，需 VPN）"""


# ---------------------------------------------------------------------------
# 新浪财经（A 股 / 美股指数）
# ---------------------------------------------------------------------------
SINA_US_INDEX_URL = "https://finance.sina.com.cn/staticdata/us/{symbol}"
"""美股指数静态历史数据。symbol 示例：.INX（标普 500）、.NDX（纳斯达克 100）"""

SINA_A_INDEX_HIST_URL = (
    "https://finance.sina.com.cn/realstock/company/{symbol}/hisdata/klc_kl.js"
)
"""A 股指数日线历史。symbol 示例：sh000001、sz399006"""


# ---------------------------------------------------------------------------
# 乐咕乐股（Legulegu）
# ---------------------------------------------------------------------------
LEGULEGU_MARKET_PE_API = "https://legulegu.com/api/stock-data/market-pe"
"""板块滚动市盈率（月度）。创业板等，经 akshare stock_market_pe_lg 调用；marketId=4 为深交所创业板"""

LEGULEGU_INDEX_PB_API = "https://legulegu.com/api/stockdata/index-basic-pb"
"""板块市净率（日频）。经 akshare stock_market_pb_lg 调用"""

LEGULEGU_DIVIDEND_API = "https://legulegu.com/api/stockdata/guxilv"
"""A 股板块股息率（日频）。经 akshare stock_a_gxl_lg 调用"""

LEGULEGU_CYB_PE_PAGE = "https://legulegu.com/stockdata/cybPE"
"""深交所创业板平均滚动 PE 页面（marketId=4；经 akshare stock_market_pe_lg 调用）"""


# ---------------------------------------------------------------------------
# 腾讯财经（预留）
# ---------------------------------------------------------------------------
TENCENT_QUOTE_URL = "https://qt.gtimg.cn/q="
"""腾讯证券实时行情接口前缀。完整地址：{URL}{sh|sz}{code} 或批量逗号分隔"""


# ---------------------------------------------------------------------------
# Server 酱（微信推送）
# ---------------------------------------------------------------------------
SERVERCHAN_API_URL = "https://sctapi.ftqq.com"
"""Server 酱消息推送 API 根路径。完整地址：{BASE}/{SendKey}.send"""

# ---------------------------------------------------------------------------
# 本地 StockDB（收盘后行情服务，非公网 HTTP）
# ---------------------------------------------------------------------------
STOCKDB_DEFAULT_HOST = "127.0.0.1"
STOCKDB_DEFAULT_PORT = 7899
"""本地 StockDB TCP 服务（SDK：D:\\repository\\stockdb\\pybao）。由 sync_stockdb_to_duckdb 写入 DuckDB。"""


# ---------------------------------------------------------------------------
# URL 构造辅助函数
# ---------------------------------------------------------------------------
def fred_csv_url(series_id: str) -> str:
    """生成 FRED CSV 下载地址。"""
    return f"{FRED_CSV_BASE_URL}?id={series_id}"


def indicator_xls_url(index_code: str) -> str:
    """中证指数指标文件地址（近约 20 个交易日 PE、股息率）。"""
    return f"{CSINDEX_INDICATOR_BASE_URL}/{index_code}indicator.xls"


def closeweight_xls_url(index_code: str) -> str:
    """中证指数成分股权重文件地址。"""
    return f"{CSINDEX_CLOSEWEIGHT_BASE_URL}/{index_code}closeweight.xls"


def sina_us_index_url(symbol: str) -> str:
    """新浪财经美股指数历史数据地址。"""
    return SINA_US_INDEX_URL.format(symbol=symbol)


def sina_a_index_hist_url(symbol: str) -> str:
    """新浪财经 A 股指数日线历史地址。"""
    return SINA_A_INDEX_HIST_URL.format(symbol=symbol)


# ---------------------------------------------------------------------------
# 数据源注册表（说明文档）
# ---------------------------------------------------------------------------
DATA_SOURCES = [
    {
        "id": "csindex_indicator",
        "name": "中证指数官方指标",
        "url": f"{CSINDEX_INDICATOR_BASE_URL}/{{code}}indicator.xls",
        "provider": "中证指数有限公司",
        "frequency": "日频（文件仅保留近约 20 个交易日）",
        "fields": "PE、股息率",
        "used_by": [
            "market_data.read_indicator_history",
            "dividend_data",
            "cn_broad_data",
        ],
        "indices": "H30269、000852 等",
        "env_override": "CSINDEX_INDICATOR_BASE_URL",
        "notes": "直接读取 Excel，无需 API Key。",
    },
    {
        "id": "csindex_perf",
        "name": "中证指数历史行情",
        "url": INDEX_PERF_URL,
        "provider": "中证指数有限公司",
        "frequency": "日频",
        "fields": "收盘价、滚动 PE(peg)、成交额",
        "used_by": [
            "market_data.get_index_perf_history",
            "dividend_data.build_signal_history",
            "cn_broad_data.build_cn_broad_valuation_history",
        ],
        "indices": "000001、000300、000852、H30269 等",
        "env_override": "INDEX_PERF_URL",
        "notes": "A 股宽基与中证策略指数的主力日频来源。",
    },
    {
        "id": "csindex_closeweight",
        "name": "中证指数成分股权重",
        "url": f"{CSINDEX_CLOSEWEIGHT_BASE_URL}/{{code}}closeweight.xls",
        "provider": "中证指数有限公司",
        "frequency": "调仓日",
        "fields": "成分股及权重",
        "used_by": ["data_sources.closeweight_xls_url（预留）"],
        "env_override": "CSINDEX_CLOSEWEIGHT_BASE_URL",
        "notes": "当前脚本未直接拉取，已登记供后续扩展。",
    },
    {
        "id": "eastmoney_bond",
        "name": "10 年期国债收益率",
        "url": BOND_YIELD_URL,
        "provider": "东方财富数据中心",
        "frequency": "日频",
        "fields": "10Y 国债收益率（EMM00166466）",
        "used_by": [
            "market_data.get_gov_bond_yield",
            "market_data.get_gov_bond_yield_history",
            "dividend_data",
            "cn_broad_data",
        ],
        "env_override": "BOND_YIELD_URL / BOND_YIELD_TOKEN",
        "notes": "用于股债利差；历史缺口优先用日度分页数据，仍缺失时由 config.BOND_YIELD_FALLBACK_BY_YEAR 按年回填。",
    },
    {
        "id": "fred_us10y",
        "name": "美国 10 年期国债收益率",
        "url": fred_csv_url(FRED_US10Y_SERIES),
        "provider": "美联储圣路易斯分行 FRED",
        "frequency": "日频",
        "fields": "DGS10 收益率（%）",
        "used_by": ["us_index_data.fetch_us10y_history"],
        "env_override": "FRED_CSV_BASE_URL",
        "notes": "纳斯达克 100 估值信号中的利率环境因子；百分比需除以 100。",
    },
    {
        "id": "hom_ndx_forward_pe",
        "name": "纳斯达克 100 TTM / Forward PE",
        "url": HISTORY_OF_MARKET_NDX_FORWARD_PE_URL,
        "provider": "History of Market（TTM 成分加权 + Bloomberg BEst Forward PE）",
        "frequency": "TTM 日频 / Forward 月频",
        "fields": "trailing_pe、forward_pe、历史序列",
        "used_by": ["us_index_data.fetch_pe_payload", "us_index_signal"],
        "env_override": "NDX_FORWARD_PE_URL",
        "notes": "Forward PE 为 12 个月一致预期；TTM PE 自 2025 年中起日频积累。",
    },
    {
        "id": "hom_spx_forward_pe",
        "name": "标普 500 TTM / Forward PE",
        "url": HISTORY_OF_MARKET_SPX_FORWARD_PE_URL,
        "provider": "History of Market（TTM 成分加权 + Bloomberg BEst Forward PE）",
        "frequency": "TTM 日频 / Forward 月频",
        "fields": "trailing_pe、forward_pe、历史序列",
        "used_by": ["us_index_data.fetch_pe_payload（spx）", "us_index_signal"],
        "env_override": "SPX_FORWARD_PE_URL",
        "notes": "与 NDX 同源；Forward PE 为 12 个月一致预期。",
    },
    {
        "id": "fred_sp500",
        "name": "标普 500 价格指数",
        "url": fred_csv_url(FRED_SP500_SERIES),
        "provider": "美联储圣路易斯分行 FRED",
        "frequency": "日频",
        "fields": "SP500 收盘价",
        "used_by": ["us_index_data.fetch_price_history（spx）", "美股宽基回测"],
        "env_override": "FRED_CSV_BASE_URL",
        "notes": "与 akshare 新浪 .INX 拼接；价格指数不含分红再投资。",
    },
    {
        "id": "nasdaq_qqq_summary",
        "name": "QQQ ETF 摘要",
        "url": NASDAQ_ETF_SUMMARY_URL.format(symbol="QQQ"),
        "provider": "Nasdaq.com API",
        "frequency": "日频",
        "fields": "股息率（Yield）",
        "used_by": ["us_index_data.fetch_dividend_yield_proxy（ndx）"],
        "env_override": "NDX_DIVIDEND_PROXY_SYMBOL",
        "notes": "以 QQQ 股息率近似 NDX；成长指数分红参考价值有限，仅作辅助展示。",
    },
    {
        "id": "nasdaq_spy_summary",
        "name": "SPY ETF 摘要",
        "url": NASDAQ_ETF_SUMMARY_URL.format(symbol="SPY"),
        "provider": "Nasdaq.com API",
        "frequency": "日频",
        "fields": "股息率（Yield）",
        "used_by": ["us_index_data.fetch_dividend_yield_proxy（spx）"],
        "env_override": "SPX_DIVIDEND_PROXY_SYMBOL",
        "notes": "以 SPY 股息率近似 SPX；仅作辅助展示。",
    },
    {
        "id": "fred_nasdaq100",
        "name": "纳斯达克 100 价格指数",
        "url": fred_csv_url(FRED_NASDAQ100_SERIES),
        "provider": "美联储圣路易斯分行 FRED（源自纳斯达克）",
        "frequency": "日频",
        "fields": "NASDAQ100 收盘价",
        "used_by": ["美股宽基回测（纳斯达克 100，基日 1985-01-31）", "us_index_data.fetch_price_history"],
        "env_override": "FRED_CSV_BASE_URL / FRED_NASDAQ100_SERIES",
        "notes": "价格指数，不含分红再投资；FRED 首条数据为 1986-01-02。",
    },
    {
        "id": "shiller_sp",
        "name": "S&P Composite 月频价格",
        "url": SHILLER_IE_DATA_URL,
        "provider": "Yale / Robert Shiller",
        "frequency": "月频",
        "fields": "S&P Composite 收盘价（与标普 500 自 1957 年起高度一致）",
        "used_by": ["美股宽基回测（标普 500，1957–2003 段）"],
        "env_override": "SHILLER_IE_DATA_URL",
        "notes": "月频数据；与新浪日频拼接用于标普 500 长期回测。",
    },
    {
        "id": "sina_us_inx",
        "name": "标普 500 指数（新浪）",
        "url": sina_us_index_url(".INX"),
        "provider": "新浪财经",
        "frequency": "日频",
        "fields": "标普 500 收盘价",
        "used_by": [
            "美股宽基回测（标普 500，2004–今 段）",
        ],
        "akshare": "ak.index_us_stock_sina(symbol='.INX')",
        "notes": "经 akshare 封装；2004-01-02 起有完整日频。",
    },
    {
        "id": "sina_us_ndx",
        "name": "纳斯达克 100 指数（新浪）",
        "url": sina_us_index_url(".NDX"),
        "provider": "新浪财经",
        "frequency": "日频",
        "fields": "纳斯达克 100 收盘价",
        "used_by": ["交叉校验 FRED NASDAQ100"],
        "akshare": "ak.index_us_stock_sina(symbol='.NDX')",
        "notes": "2014 年起有数据；长期回测优先用 FRED。",
    },
    {
        "id": "sina_a_index",
        "name": "A 股指数日线（新浪）",
        "url": sina_a_index_hist_url("{sh|sz}{code}"),
        "provider": "新浪财经",
        "frequency": "日频",
        "fields": "开高低收、成交量",
        "used_by": [
            "cyb_data.fetch_cyb_price_history",
        ],
        "akshare": "ak.stock_zh_index_daily(symbol='sz399006')",
        "notes": "深证/创业板等不在中证 perf API 覆盖范围的指数。",
    },
    {
        "id": "legulegu_pe",
        "name": "创业板板块市盈率",
        "url": LEGULEGU_MARKET_PE_API,
        "provider": "乐咕乐股",
        "frequency": "月度",
        "fields": "创业板平均滚动 PE",
        "used_by": ["cyb_data.fetch_cyb_pe_history"],
        "akshare": "ak.stock_market_pe_lg(symbol='创业板')",
        "notes": "月度发布；marketId=4 为深交所创业板官方口径（经乐咕乐股聚合）。cyb_data 按指数收盘价折算为日度 PE。",
    },
    {
        "id": "legulegu_pb",
        "name": "创业板板块市净率",
        "url": LEGULEGU_INDEX_PB_API,
        "provider": "乐咕乐股",
        "frequency": "日频",
        "fields": "加权/等权/中位数 PB",
        "used_by": ["cyb_data.fetch_cyb_pb_history"],
        "akshare": "ak.stock_market_pb_lg(symbol='创业板')",
        "notes": "经 akshare 封装，需乐咕 CSRF Cookie。",
    },
    {
        "id": "legulegu_dividend",
        "name": "创业板板块股息率",
        "url": LEGULEGU_DIVIDEND_API,
        "provider": "乐咕乐股",
        "frequency": "日频",
        "fields": "板块股息率（%）",
        "used_by": ["cyb_data.fetch_cyb_dividend_history"],
        "akshare": "ak.stock_a_gxl_lg(symbol='创业板')",
        "notes": "经 akshare 封装，需乐咕 CSRF Cookie。",
    },
    {
        "id": "legulegu_cyb_pe_page",
        "name": "深交所创业板 PE 页面",
        "url": LEGULEGU_CYB_PE_PAGE,
        "provider": "乐咕乐股（聚合深交所创业板 marketId=4）",
        "frequency": "月度",
        "fields": "创业板平均滚动 PE、指数点位",
        "used_by": ["cyb_data.fetch_cyb_pe_szse_official"],
        "akshare": "ak.stock_market_pe_lg(symbol='创业板')",
        "notes": "API 入口见 legulegu_pe；页面为人工对照参考。",
    },
    {
        "id": "tencent_quote",
        "name": "腾讯证券实时行情",
        "url": TENCENT_QUOTE_URL,
        "provider": "腾讯财经",
        "frequency": "实时",
        "fields": "最新价、昨收、行情时间",
        "used_by": [
            "realtime_quote.fetch_live_quotes（指数）",
            "dividend_lowvol_rotation.quotes.fetch_stock_quotes（个股）",
            "live_snapshot",
        ],
        "env_override": "TENCENT_QUOTE_URL",
        "notes": "免费、无需 API Key；指数代码见 realtime_quote.TENCENT_SYMBOL_BY_INDEX。",
    },
    {
        "id": "stockdb",
        "name": "本地 StockDB 行情服务",
        "url": f"tcp://{STOCKDB_DEFAULT_HOST}:{STOCKDB_DEFAULT_PORT}",
        "provider": "本地 StockDB（pybao SDK）",
        "frequency": "日频（收盘后同步）",
        "fields": "A 股日 K、交易日历、股票列表、市值/成交额",
        "used_by": [
            "sync_stockdb_to_duckdb.sync_all",
            "dividend_lowvol_rotation.prices（DuckDB 缺失时回退）",
            "dividend_lowvol_rotation.market_cap",
        ],
        "env_override": "STOCKDB_HOST / STOCKDB_PORT / STOCKDB_SDK_PATH",
        "notes": "非公网 HTTP；需本机运行 StockDB 服务。同步脚本：python sync_stockdb_to_duckdb.py。",
    },
    {
        "id": "akshare_em_fhps",
        "name": "A 股分红方案（东方财富）",
        "url": "akshare: ak.stock_fhps_em(date=报告期)",
        "provider": "东方财富（经 akshare）",
        "frequency": "按报告期批次",
        "fields": "除权除息日、每股派息、方案进度、EPS 等",
        "used_by": ["dividend_lowvol_rotation.dividend.load_fhps_all_records"],
        "akshare": "ak.stock_fhps_em(date='YYYYMMDD')",
        "notes": "红利低波策略分红分子与 TTM 累计；缓存 cache/dividend_lowvol/fhps_*.csv。",
    },
    {
        "id": "akshare_em_financial_abstract",
        "name": "A 股财务摘要（东方财富）",
        "url": "akshare: ak.stock_financial_abstract(symbol=代码)",
        "provider": "东方财富（经 akshare）",
        "frequency": "季/年报",
        "fields": "ROE、资产负债率、净利润、经营现金流/净利润等",
        "used_by": [
            "dividend_lowvol_rotation.risk_screening.fetch_risk_history",
        ],
        "akshare": "ak.stock_financial_abstract(symbol='600000')",
        "notes": "排雷与质量因子；逐股拉取，有 FINANCIAL_FETCH_SLEEP_SEC 限速。",
    },
    {
        "id": "akshare_em_profit_sheet",
        "name": "A 股利润表（东方财富）",
        "url": "akshare: ak.stock_profit_sheet_by_report_em(symbol=代码)",
        "provider": "东方财富（经 akshare）",
        "frequency": "季/年报",
        "fields": "营业利润、财务费用（利息保障倍数回退）",
        "used_by": ["dividend_lowvol_rotation.risk_screening._fetch_interest_coverage_akshare"],
        "akshare": "ak.stock_profit_sheet_by_report_em(symbol='600000')",
        "notes": "财务摘要无利息保障倍数时回退计算。",
    },
    {
        "id": "akshare_sw_industry",
        "name": "申万一级行业成分",
        "url": "akshare: ak.index_realtime_sw + ak.index_component_sw",
        "provider": "申万宏源（经 akshare）",
        "frequency": "周缓存",
        "fields": "申万一级行业名称、成分股",
        "used_by": ["dividend_lowvol_rotation.industry._fetch_sw_industry_table"],
        "akshare": "ak.index_realtime_sw(); ak.index_component_sw(symbol=sw_code)",
        "notes": "行业分散约束；映射为当前时点成分，非历史调仓。",
    },
    {
        "id": "baostock_industry",
        "name": "证监会行业分类",
        "url": "baostock: bs.query_stock_industry()",
        "provider": "Baostock（证券宝）",
        "frequency": "周缓存",
        "fields": "证监会行业名称",
        "used_by": ["dividend_lowvol_rotation.industry._load_csrc_industry_table（降级）"],
        "notes": "bs.query_stock_industry()；INDUSTRY_SOURCE=csrc 时使用；申万不可用时降级。",
    },
    {
        "id": "baostock_kline",
        "name": "Baostock 日 K 线",
        "url": "baostock: bs.query_history_k_data_plus()",
        "provider": "Baostock（证券宝）",
        "frequency": "日频",
        "fields": "开高低收、成交量",
        "used_by": ["scripts/validate_data_baostock.py（交叉验证）"],
        "notes": "bs.query_history_k_data_plus(...)；业务回测 K 线优先 DuckDB/StockDB。",
    },
    {
        "id": "akshare_etf_sina",
        "name": "ETF 日 K（新浪）",
        "url": "akshare: ak.fund_etf_hist_sina(symbol=代码)",
        "provider": "新浪财经（经 akshare）",
        "frequency": "日频",
        "fields": "ETF 收盘价",
        "used_by": ["scripts/data_crosscheck.py（指数 ETF 跟踪校验）"],
        "akshare": "ak.fund_etf_hist_sina(symbol='sh510880')",
        "notes": "用于 H30269 等策略指数无直接 K 线时的 ETF 代理抽检。",
    },
    {
        "id": "sina_us_bond",
        "name": "美国国债收益率（新浪）",
        "url": "akshare bond_zh_us_rate()",
        "provider": "新浪财经（经 akshare）",
        "frequency": "日频",
        "fields": "美国国债收益率10年",
        "used_by": ["us_index_data.fetch_us10y_history（FRED 失败时回退）"],
        "akshare": "ak.bond_zh_us_rate()",
        "notes": "字段单位为 %；FRED DGS10 不可用时启用。",
    },
    {
        "id": "multpl_sp500_pe",
        "name": "Multpl 标普 500 滚动 PE",
        "url": MULTPL_SP500_PE_TABLE_URL,
        "provider": "Multpl.com",
        "frequency": "月频",
        "fields": "S&P 500 Trailing PE",
        "used_by": ["scripts.data_crosscheck.compare_us_forward_pe_sources"],
        "notes": "与 History of Market Trailing PE 交叉校验；非 Forward PE。",
    },
    {
        "id": "yardeni_forward_pe",
        "name": "Yardeni Forward P/E 图表",
        "url": YARDENI_FORWARD_PE_CHART_URL,
        "provider": "Yardeni Research",
        "frequency": "日/周/月",
        "fields": "Forward P/E（图表）",
        "used_by": ["scripts.data_crosscheck.compare_us_forward_pe_sources（人工对照）"],
        "notes": "无公开 CSV 下载，仅作人工核对参考。",
    },
    {
        "id": "barrons_forward_pe",
        "name": "Barron's Birinyi Forward P/E",
        "url": BARRONS_PE_YIELDS_URL,
        "provider": "Barron's / WSJ Market Data（Birinyi Associates）",
        "frequency": "周频",
        "fields": "S&P 500 / NASDAQ 100 Trailing & Forward PE",
        "used_by": ["scripts.data_crosscheck.fetch_barrons_forward_pe_snapshot", "compare_us_forward_pe_sources"],
        "notes": "Operating earnings 口径；国内 Python requests 常 SSL 失败，抓取走 curl。",
    },
    {
        "id": "serverchan",
        "name": "Server 酱微信推送",
        "url": f"{SERVERCHAN_API_URL}/{{SendKey}}.send",
        "provider": "方糖 Server 酱",
        "frequency": "按需",
        "fields": "推送标题与正文",
        "used_by": ["notify.send_wechat"],
        "env_override": "SERVERCHAN_API_URL",
        "notes": "非行情数据；注册 https://sct.ftqq.com 获取 SendKey。",
    },
]


def list_data_sources(category: str | None = None) -> list[dict]:
    """返回数据源列表；category 预留扩展，当前返回全部。"""
    return list(DATA_SOURCES)


def print_data_sources():
    """在终端打印数据源摘要（供人工查阅）。"""
    for item in DATA_SOURCES:
        print(f"\n[{item['id']}] {item['name']}")
        print(f"  地址: {item['url']}")
        print(f"  提供方: {item['provider']} | 频率: {item['frequency']}")
        print(f"  用途: {', '.join(item['used_by'])}")
        if item.get("akshare"):
            print(f"  akshare: {item['akshare']}")
        if item.get("env_override"):
            print(f"  环境变量: {item['env_override']}")
        if item.get("notes"):
            print(f"  说明: {item['notes']}")


if __name__ == "__main__":
    print_data_sources()

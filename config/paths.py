"""项目路径与目录常量。"""

from pathlib import Path

_CONFIG_DIR = Path(__file__).resolve().parent
PROJECT_DIR = _CONFIG_DIR.parent
CONFIG_FILE = _CONFIG_DIR / "push.env"
LOGS_DIR = PROJECT_DIR / "logs"
DATA_DIR = PROJECT_DIR / "data"
DATA_CACHE_DIR = PROJECT_DIR / "cache"
US_DATA_CACHE_DIR = DATA_CACHE_DIR / "us"
MARKET_DUCKDB_PATH = DATA_DIR / "market.duckdb"
STOCKDB_SDK_PATH = Path(r"D:\repository\stockdb\pybao")
STOCKDB_HOST = "127.0.0.1"
STOCKDB_PORT = 7899
BACKTEST_OUTPUT_DIR = PROJECT_DIR / "output" / "backtest"
BACKTEST_PRESENT_LABEL = "inception_present"

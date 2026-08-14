"""环境变量读取与 push.env 加载。"""

import os

ENV_BOOL_TRUE = {"1", "true", "yes", "on"}

def _load_env_files():
    from config.paths import CONFIG_FILE
    """将 push.env 中的配置写入环境变量（不覆盖已有环境变量）。"""
    for path in [CONFIG_FILE]:
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


def _env_str(name, default):
    return os.environ.get(name, default)


def _env_float(name, default):
    value = os.environ.get(name)
    if value is None or value == "":
        return default
    return float(value)


def _env_int(name, default):
    value = os.environ.get(name)
    if value is None or value == "":
        return default
    return int(value)


def _env_float_any(names, default):
    """按顺序读取环境变量，支持新旧变量名兼容。"""
    if isinstance(names, str):
        names = (names,)
    for name in names:
        value = os.environ.get(name)
        if value is not None and value != "":
            return float(value)
    return default


def _env_int_any(names, default):
    if isinstance(names, str):
        names = (names,)
    for name in names:
        value = os.environ.get(name)
        if value is not None and value != "":
            return int(value)
    return default


def _env_bool(name, default=True):
    value = os.environ.get(name)
    if value is None or value == "":
        return default
    return value.strip().lower() in ENV_BOOL_TRUE


def _build_annual_investment_budget_by_year():
    """从环境变量 ANNUAL_INVESTMENT_BUDGET_{年份} 读取各年覆盖值。"""
    out = {}
    for year in range(2015, 2036):
        value = os.environ.get(f"ANNUAL_INVESTMENT_BUDGET_{year}")
        if value is not None and value != "":
            out[year] = float(value)
    return out


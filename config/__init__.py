"""项目公共配置：默认值、环境变量与 push.env 覆盖。"""

from config.paths import *  # noqa: F403
from config.env import *  # noqa: F403
from config.indices import *  # noqa: F403
from config.price_position import *  # noqa: F403
from config.buy_amount import *  # noqa: F403
from config.dividend import *  # noqa: F403
from config.cn_broad import *  # noqa: F403
from config.cyb import *  # noqa: F403
from config.us import *  # noqa: F403
from config.signal import *  # noqa: F403
from config.backtest_risk import *  # noqa: F403
from config.network import *  # noqa: F403

from config.env import _load_env_files
from config.paths import CONFIG_FILE

_load_env_files()

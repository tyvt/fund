"""微信推送（Server酱），与报告生成解耦。"""

import requests

from config import CONFIG_FILE, PUSH_REQUEST_TIMEOUT, SERVERCHAN_API_URL


def build_push_title(sections):
    parts = [f"{section['code']}{section['signal_short']}" for section in sections]
    return "投资信号 " + " | ".join(parts)


def push_to_wechat(title, content, config):
    sendkey = config.get("serverchan_sendkey", "").strip()
    if not sendkey:
        print("未配置 SERVERCHAN_SENDKEY，跳过微信推送。")
        print(f"请在 {CONFIG_FILE} 中配置，参考 {CONFIG_FILE.with_suffix('.example.env')}")
        return False

    try:
        response = requests.post(
            f"{SERVERCHAN_API_URL}/{sendkey}.send",
            data={"title": title, "desp": content},
            timeout=PUSH_REQUEST_TIMEOUT,
        )
        result = response.json()
        if result.get("code") == 0:
            print("微信推送成功。")
            return True

        print(f"微信推送失败: {result.get('message', result)}")
        return False
    except Exception as e:
        print(f"微信推送出错: {e}")
        return False

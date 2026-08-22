import json
import os
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent
SECRETS_PATH = PROJECT_DIR / "secrets.local.json"


def _secret(env_name: str, json_key: str) -> str:
    value = os.environ.get(env_name)
    if value:
        return value
    if SECRETS_PATH.exists():
        value = json.loads(SECRETS_PATH.read_text()).get(json_key)
        if value:
            return value
    raise RuntimeError(
        f"缺少 {env_name}：設環境變數，或在 {SECRETS_PATH} 放 {{\"{json_key}\": ...}}"
    )


# Bot 沿用 Docker/alisha 專案的 alishatw_bot，chat_id 是 @REDACTED 的 telegram_user_id
# （從 Docker/alisha/data/alisha.db 的 orders 表查到）。實際值放在 secrets.local.json，
# 不進版控（這個 repo 會 push 到 GitHub）。
BOT_TOKEN = _secret("SIDELOADLY_MONITOR_BOT_TOKEN", "bot_token")
CHAT_ID = _secret("SIDELOADLY_MONITOR_CHAT_ID", "chat_id")

SIDELOADLY_DB_PATH = Path.home() / "Library/Application Support/sideloadly/installations.db"
STATE_PATH = PROJECT_DIR / "state.json"
BOT_OFFSET_PATH = PROJECT_DIR / "bot_offset.txt"
EVENTS_DB_PATH = PROJECT_DIR / "events.db"
MUTE_PATH = PROJECT_DIR / "mute_until.txt"
RESTART_LABEL = "io.sideloadly.daemon"

# 過期判定改用 installations 表的 known_ttl（憑證有效天數）與 refresh_at_hours
# （sideloadly 自己認為該刷新的時數）。這兩個欄位是 0/NULL 時才退回下列預設。
DEFAULT_KNOWN_TTL_DAYS = 7
DEFAULT_REFRESH_AT_HOURS = 96

# 超過 refresh_at_hours 之後再寬限這麼久才算逾期，避免刷新稍微慢一點就告警。
OVERDUE_GRACE_HOURS = 12

# devices.last_seen 超過這麼久沒更新就視為裝置離線（離線期間刷新一定失敗）。
DEVICE_OFFLINE_HOURS = 24

# monitor 每小時跑一次；state.json 的 last_run 超過這麼久沒更新，
# 代表監控自己死了（launchd job 掛掉、Mac 睡著、python 噴錯）。
HEARTBEAT_STALE_HOURS = 3
# 心跳告警的重複提醒間隔，避免監控長期掛著時每 30 秒轟炸一次。
HEARTBEAT_REPEAT_HOURS = 6

# perform_restart() 送出 kickstart 後，隔多久確認一次 daemon 有沒有回到 running。
RESTART_VERIFY_ATTEMPTS = 5
RESTART_VERIFY_INTERVAL_SECONDS = 2

# Telegram 單則訊息上限 4096 字元，送出前依此切段。
MESSAGE_CHUNK_LIMIT = 3900
# 報表會再包一層 <pre> 並做 HTML escape，字數會膨脹，留多一點餘裕。
REPORT_CHUNK_LIMIT = 3400

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
RESTART_LABEL = "io.sideloadly.daemon"
OVERDUE_THRESHOLD_DAYS = 5

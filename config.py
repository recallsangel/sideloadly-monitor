import json
import os
import pwd
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


# chat_id 是要接收通知的使用者的 telegram_user_id。實際值放在 secrets.local.json，
# 不進版控（這個 repo 會 push 到 GitHub）。
BOT_TOKEN = _secret("SIDELOADLY_MONITOR_BOT_TOKEN", "bot_token")
CHAT_ID = _secret("SIDELOADLY_MONITOR_CHAT_ID", "chat_id")

SIDELOADLY_DB_PATH = Path.home() / "Library/Application Support/sideloadly/installations.db"
# 各已登入 Apple ID 本週還剩多少 App ID 額度（免費帳號一週 10 個）。
# sideloadly 自己維護，不是這個專案寫的。
ACCOUNT_APPIDS_PATH = Path.home() / "Library/Application Support/sideloadly/account-appids.json"
# 免費 Apple 開發者帳號的每週 App ID 額度上限，只用來在報表裡顯示「剩 X / 10」。
WEEKLY_APPID_QUOTA = 10
# /accounts 報表裡用來標記「快用完」的門檻（含）。
LOW_QUOTA_THRESHOLD = 2
STATE_PATH = PROJECT_DIR / "state.json"
BOT_OFFSET_PATH = PROJECT_DIR / "bot_offset.txt"
EVENTS_DB_PATH = PROJECT_DIR / "events.db"
MUTE_PATH = PROJECT_DIR / "mute_until.txt"
# /forget 忘記的裝置/app 清單，見 ignore.py。跟 installations.db 無關，純粹是本專案
# 自己的「不想再看到」名單，過濾發生在讀出來之後那一層。
IGNORED_PATH = PROJECT_DIR / "ignored.json"
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

# ---------------------------------------------------------------- usbmuxd
# usbmuxd 是 macOS 負責探索 iOS 裝置的系統服務，Sideloadly 找不找得到裝置，
# 最底層完全取決於它。它偶爾會卡死：socket 還在、握手照樣回成功，但裝置一台都
# 不回報，Sideloadly 於是變成瞎子——重啟 Sideloadly daemon 對這種狀況沒有用，
# 得把 usbmuxd 砍掉讓 launchd 重新拉起來。
USBMUXD_PROCESS_NAME = "usbmuxd"
USBMUXD_SOCKET = "/var/run/usbmuxd"
# 送給 usbmuxd 的自我介紹字串，只會出現在它自己的日誌裡。
USBMUXD_CLIENT_NAME = "sideloadly-monitor"
USBMUXD_TIMEOUT_SECONDS = 5

# 重啟後隔多久確認一次。Wi-Fi 裝置要等 Bonjour 重新探索完才會回來，比 USB 慢得
# 多，所以查到 0 台不算失敗——等到有裝置就提早收工，次數用完才回報 0。
USBMUXD_VERIFY_ATTEMPTS = 8
USBMUXD_VERIFY_INTERVAL_SECONDS = 2

# 「連得上但一台都看不到」要在冊至少這麼多台裝置才判定成 usbmuxd 壞掉。單台裝置
# 關機或帶出門是常態，全部一起不見才是這個服務的問題；只有一台裝置的環境不該
# 因為那台出門就整天收到 usbmuxd 告警。
USBMUXD_MIN_DEVICES_FOR_ALERT = 2

# 重啟 usbmuxd 要 root，但 bot 是一般使用者的 LaunchAgent，沒有 tty 也無從輸入
# 密碼，所以靠一條只放行這一道指令的 NOPASSWD 規則。安裝方式見 README；沒裝的
# 話 /usbmuxd 不會有任何動作，只會把該補的那行回給你。
SUDOERS_PATH = "/etc/sudoers.d/sideloadly-monitor-usbmuxd"
# 刻意寫死完整參數，不留萬用字元——放行的只有「砍掉名字剛好是 usbmuxd 的行程」，
# 不是「以 root 執行任意 pkill」。
SUDOERS_RULE = (
    f"{pwd.getpwuid(os.getuid()).pw_name} ALL=(root) NOPASSWD: "
    f"/usr/bin/pkill -x {USBMUXD_PROCESS_NAME}"
)

# /forget、/forgotten、/redeploy 的選單訊息最多列幾顆按鈕，避免裝置/app 一多
# 整則訊息炸開（Telegram 單則訊息的按鈕數也有上限）。
PICKER_MAX_BUTTONS = 20

# 報表裡的 Apple ID 只留本地端前幾個字（見 common.short_apple_id）。一行要同時
# 塞下 app 名、到期倒數、帳號，完整帳號動輒 20 字以上會把表格擠到要橫向捲。
APPLE_ID_LOCAL_WIDTH = 5

# Telegram 單則訊息上限 4096 字元，送出前依此切段。
MESSAGE_CHUNK_LIMIT = 3900
# 報表會再包一層 <pre> 並做 HTML escape，字數會膨脹，留多一點餘裕。
REPORT_CHUNK_LIMIT = 3400

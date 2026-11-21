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
MUTE_UNTIL_PATH = PROJECT_DIR / "mute_until.txt"
# /forget 忘記的裝置/app 清單，見 forget.py。跟 installations.db 無關，純粹是本專案
# 自己的「不想再看到」名單，過濾發生在讀出來之後那一層。
FORGOTTEN_PATH = PROJECT_DIR / "forgotten.json"
# Sideloadly daemon 的 launchd label，查狀態（daemon_state）和重啟都靠它。
DAEMON_LABEL = "io.sideloadly.daemon"

# 過期判定改用 installations 表的 known_ttl（憑證有效天數）與 refresh_at_hours
# （sideloadly 自己認為該刷新的時數）。這兩個欄位是 0/NULL 時才退回下列預設。
DEFAULT_KNOWN_TTL_DAYS = 7
DEFAULT_REFRESH_AT_HOURS = 96

# 超過 refresh_at_hours 之後再寬限這麼久才算逾期，避免刷新稍微慢一點就告警。
OVERDUE_GRACE_HOURS = 12

# devices.last_seen 超過這麼久沒更新就視為裝置離線（離線期間刷新一定失敗）。
DEVICE_OFFLINE_HOURS = 24

# 錯誤旗標還亮著、但 last_failure_at 已經這麼久沒往前動，就當成舊帳而不是
# 「現在正在失敗」。
#
# 為什麼需要這道界線：Sideloadly 要到下一次刷新**成功**才會清掉 last_error /
# failures_count，而下一次刷新排在 last_updated + refresh_at_hours（目前 96
# 小時），所以一筆失敗會讓旗標亮好幾天。restart.py 直接讀那個旗標的話，同一筆
# 舊錯誤會讓 04:00 那支每天重啟一次——而重啟既清不掉旗標，也不會讓刷新提早。
# 2026-09-13 與 09-14 就是這樣為同一筆 09-12 04:58 的 Cancelled 各重啟了一次。
#
# 12 小時是從兩端夾出來的：daemon 每分鐘 tick 一次，真的卡在重試的話這個時間戳
# 會一直被推新，所以窗口不必長；而 2026-09-08 那次「Cancelled by user」是失敗後
# 約 3.7 小時的那次重啟救回來的，所以也不能短到把它排除掉。
FAILURE_STALE_HOURS = 12

# 重啟 daemon 解不掉的兩種失敗。restart.py 不拿它們當重啟理由，/status 也不給
# 重新部署按鈕（那顆按鈕做的就是重啟）。
#
# 1. failures_count 到 SIDELOADLY_GIVE_UP_FAILURES：Sideloadly daemon 從此每次 tick
#    都印「This app is in Failed state, ignoring」，不再重試。計數存在資料庫裡，
#    重啟不會歸零——2026-11-08～09 daemon 重啟過三次，那幾個 app 照樣每次被略過。
#    門檻 3 是觀察來的：failures_count 1 的 app 沒被略過，3 的全被略過。
# 2. last_error 含 RESTART_CANT_FIX_ERRORS 任一字串：毛病在 anisette（Apple 登入
#    要的裝置驗證資料），不在 daemon。2026-11-09 04:00 為「Anisette failed: No OTP」
#    重啟過一次，什麼都沒解決；根因是 macOS 27 擋掉 Local Anisette，要到 Sideloadly
#    設定改 anisette 模式。
SIDELOADLY_GIVE_UP_FAILURES = 3
RESTART_CANT_FIX_ERRORS = ("Anisette failed",)

# daemon 的記憶體（footprint）超過這個值，restart.py 就把它算成重啟理由。
#
# daemon 會漏記憶體：2026-09-19 啟動後連跑 20 天，到 10-09 長到 8.2 GB（約每天
# +400 MB），16 GB 的機器被它擠得天天用 swap。上面那些理由沒有一個看記憶體，所以
# 那 20 天每天 04:00 都回「一切正常」。3 GB 大約是漏一週的量，也就是每週最多多
# 重啟一次——偶爾打斷一次刷新，比整台天天換頁划算。
DAEMON_MEMORY_LIMIT_BYTES = 3 * 1024**3

# monitor 每小時跑一次；state.json 的 last_run 超過這麼久沒更新，
# 代表監控自己死了（launchd job 掛掉、Mac 睡著、python 噴錯）。
HEARTBEAT_STALE_HOURS = 3
# 心跳告警的重複提醒間隔，避免監控長期掛著時每 30 秒轟炸一次。
HEARTBEAT_REPEAT_HOURS = 6

# ---------------------------------------------------------------- usbmuxd
# usbmuxd 是 macOS 管 iPhone/iPad 連線（USB 與 Wi-Fi）的系統服務，Sideloadly 透過它
# 找裝置。它偶爾會把一台還在 Wi-Fi 上的裝置弄丟：確認那台裝置的 Bonjour 紀錄時
# 出錯（日誌「DNSServiceReconfirmRecord(<MAC>@…) failed -65541」，緊接著斷線），之後
# 只有那台裝置「重新出現」在 Bonjour 上才會被接回來——一直待在 Wi-Fi 上的手機
# 不會重新出現，於是一直接不回來，Sideloadly 也就一直看不到它。2026-11-09 23:22
# Joy 就是這樣卡了半小時，到期前的刷新跟著失敗，`sudo killall usbmuxd` 之後立刻接回。
#
# 弄丟超過這麼久才告警：同一種錯誤 11-08～09 出現 5 次，另外 4 次都自己接回來了，
# 3 次在 3、10、38 分鐘內（裝置離開又回到 Wi-Fi），1 次隔了 6 小時（那段時間它在不在
# Wi-Fi 上已經查不到）。太早告警只會叫人去按一顆不必按的按鈕；還沒接回來的裝置也
# 要先確認它真的還在 Wi-Fi 上才告警，見 BONJOUR_RESOLVE_SECONDS。
USBMUXD_DROP_MINUTES = 30
# 第一次跑、或 monitor 停過一陣子時，最多往回讀多久的 usbmuxd 日誌。平常只讀上一輪
# 之後的部分，但 24 小時也只要 8 秒左右。
USBMUXD_LOG_MAX_HOURS = 24
# 用 dns-sd -L 確認被弄丟的裝置還在 Wi-Fi 上，等它回應最多等幾秒。還在的裝置一秒內
# 就回；不在的要等到逾時，而 dns-sd 自己不會結束，一定會等滿這麼久。
BONJOUR_RESOLVE_SECONDS = 3
# 弄丟的紀錄最多帶幾天，超過就從 state.json 拿掉，不管它接回來了沒。
#
# 裝置的 Wi-Fi MAC 會換（iOS 的私人 Wi-Fi 位址）：iPhone +1 11-14 被弄丟時是
# 9b:b3:…，11-21 已經換成 e6:33:…；iPhone Air 11-19 的 9a:93:… 也換掉了。換了 MAC
# 的裝置是用新 MAC 接上，舊 MAC 那筆永遠等不到接上的日誌，只會一直留著，而每一筆每輪
# 都要等 dns-sd -L 逾時（BONJOUR_RESOLVE_SECONDS）才知道它不在。2026-11-21 的
# state.json 已經留了 11-10、11-14、11-19 三筆，monitor 每輪白等 9 秒。
# 7 天：免費憑證只有 7 天，真的卡這麼久還沒接回來的裝置，上面的 app 也已經過期，改由
# 過期告警接手；這段期間弄丟告警也每天提醒過了。
USBMUXD_DROP_FORGET_DAYS = 7

# perform_restart() 送出 kickstart 後，隔多久確認一次 daemon 有沒有回到 running。
RESTART_VERIFY_ATTEMPTS = 5
RESTART_VERIFY_INTERVAL_SECONDS = 2

# ------------------------------------------------------------------- 日誌
# Sideloadly 內建的日誌功能從來沒運作過：它想在「目前工作目錄」建
# sideloadlydaemon.log，而 launchd 啟動的行程工作目錄是 /，macOS 的系統卷唯讀，
# 於是每次啟動都寫失敗然後一聲不吭繼續跑。改成在它的 LaunchAgent 加
# StandardErrorPath 把 stderr 接住（~/Library/LaunchAgents/io.sideloadly.daemon.plist，
# 原檔備份成同名 .bak）。這是「卡住的時候到底發生什麼」唯一的證據來源。
DAEMON_ERR_LOG_PATH = Path.home() / "Library/Logs/sideloadly-daemon.err.log"
# LaunchAgent 也設了 StandardOutPath 的話 stdout 寫在這裡；沒設就不存在，輪替時略過。
DAEMON_OUT_LOG_PATH = Path.home() / "Library/Logs/sideloadly-daemon.out.log"
# 本專案三個 launchd job 的 *.log（plist 的 StandardOutPath / StandardErrorPath）。
LOG_DIR = PROJECT_DIR
# monitor 每輪檢查上面這些日誌，超過上限就留一段尾巴、原地清空（見 common.rotate_log）。
# daemon 的 stderr 每個 tick 都會把每台裝置上全部已安裝 app 列一遍（一台就五十幾個），
# 實測約 200 MB/天；其他日誌長得慢，但斷網時 bot.err.log 每 5 秒多一行，一樣要有上限。
LOG_MAX_BYTES = 20 * 1024 * 1024
# 清空前先留一份尾巴，否則剛好在卡住之後才輪替，等於把要查的證據丟掉。
LOG_KEEP_BYTES = 2 * 1024 * 1024

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

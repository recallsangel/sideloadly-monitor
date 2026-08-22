#!/usr/bin/env python3
"""偵測邏輯的端對端測試。

在複製出來的資料庫上跑，state / events / mute 都指到暫存目錄，
send_message 被換掉，所以不會碰真實狀態也不會發 Telegram。

用法：./test_monitor.py [-v]
"""
import json
import shutil
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

VERBOSE = "-v" in sys.argv[1:]
TMP = Path(tempfile.mkdtemp(prefix="sideloadly-test-"))

import config

REAL_DB = config.SIDELOADLY_DB_PATH
if not REAL_DB.exists():
    sys.exit(f"找不到 sideloadly 資料庫：{REAL_DB}")

DB = TMP / "installations.db"
shutil.copy(REAL_DB, DB)
config.SIDELOADLY_DB_PATH = DB
config.STATE_PATH = TMP / "state.json"
config.EVENTS_DB_PATH = TMP / "events.db"
config.MUTE_PATH = TMP / "mute_until.txt"

import bot
import common
import history
import monitor

SENT: list[str] = []
common.send_message = lambda text, chat_id=None: SENT.append(text)


def db(sql, *params):
    con = sqlite3.connect(DB)
    with con:
        con.execute(sql, params)
    con.close()


def ts(**kw):
    """產生 sideloadly 格式的時間字串（相對現在）。"""
    return (datetime.now(timezone.utc) + timedelta(**kw)).strftime(
        "%Y-%m-%d %H:%M:%S.%f+00:00"
    )


def run(name) -> str:
    SENT.clear()
    monitor.main()
    body = "\n\n".join(SENT)
    if VERBOSE:
        print(f"\n--- {name} ---\n{body or '(沒有推送)'}")
    return body


def check(name, condition):
    if not condition:
        sys.exit(f"❌ {name}")
    print(f"✓ {name}")


# ---------------------------------------------------------------- 基準與去重

check("首次執行只建立基準，不推送", not run("first run"))
check("state.json 寫入 last_run 心跳",
      json.loads(config.STATE_PATH.read_text()).get("last_run"))
check("無變化時不推送", not run("no change"))

# ---------------------------------------------------------------- 各種偵測

db("UPDATE installations SET last_updated = ? WHERE id = 1", ts(minutes=-1))
db("UPDATE installations SET last_error = 'anisette server unreachable', "
   "failures_count = 3, last_failure_at = ? WHERE id = 2", ts(minutes=-5))
db("UPDATE installations SET last_updated = ? WHERE id = 3", ts(days=-5))
db("UPDATE installations SET last_updated = ? WHERE id = 4", ts(days=-9))
db("UPDATE devices SET last_seen = ? WHERE rowid = 1", ts(days=-3))

body = run("mixed changes")
for label in ("刷新完成", "刷新失敗", "anisette", "逾期未刷新", "已過期", "裝置離線"):
    check(f"偵測到 {label}", label in body)

check("同日重跑不重複提醒逾期/離線", not run("same day"))

# ------------------------------------------------------------------- 恢復

db("UPDATE installations SET last_error = '', failures_count = 0 WHERE id = 2")
db("UPDATE devices SET last_seen = ? WHERE rowid = 1", ts(minutes=-2))
body = run("recovery")
check("偵測到錯誤解除", "錯誤已解除" in body)
check("偵測到裝置回線", "裝置回線" in body)

# ------------------------------------------------------------------- 靜音

common.set_mute(1)
before = len(history.recent(200))
db("UPDATE installations SET last_updated = ? WHERE id = 1", ts(minutes=-1))
check("靜音期間不推送", not run("muted"))
check("靜音期間仍寫入歷史", len(history.recent(200)) > before)
common.clear_mute()

# ------------------------------------------------------- 舊版 state 格式遷移

# 舊版把 last_updated 存成 DB 原始字串，新版存 isoformat。遷移沒處理好，
# 升級後第一輪會把每個 app 都誤判成剛刷新。
con = sqlite3.connect(DB)
raw = {str(i): u for i, u in con.execute("SELECT id, last_updated FROM installations")}
con.close()
config.STATE_PATH.write_text(json.dumps(
    {"installations": raw, "overdue_notified": {}}, ensure_ascii=False))
check("舊版 state 遷移不誤報刷新", "刷新完成" not in run("legacy state"))

# ------------------------------------------------------------------- 心跳


def set_last_run(**kw):
    config.STATE_PATH.write_text(json.dumps({
        "installations": {},
        "last_run": (datetime.now(timezone.utc) + timedelta(**kw)).isoformat(),
    }))


set_last_run(hours=-9)
SENT.clear()
bot.check_heartbeat()
check("偵測到 monitor 停擺", "停擺" in "\n".join(SENT))

SENT.clear()
bot.check_heartbeat()
check("冷卻期內不重複告警", not SENT)

set_last_run(seconds=0)
SENT.clear()
bot.check_heartbeat()
check("偵測到 monitor 恢復", "恢復" in "\n".join(SENT))

# ------------------------------------------------------------------- 報表

for name, fn in (("/status", common.build_status_report),
                 ("/devices", common.build_device_report),
                 ("/log", history.build_log_report),
                 ("/stats", history.build_stats_report)):
    output = fn()
    check(f"{name} 產出報表", isinstance(output, str) and output)
    if VERBOSE:
        print(f"\n--- {name} ---\n{output}")

# 超過 Telegram 上限要切段
check("長訊息會切段", len(common._chunk("x\n" * 4000)) > 1)

shutil.rmtree(TMP, ignore_errors=True)
print("\n✅ 全部通過")

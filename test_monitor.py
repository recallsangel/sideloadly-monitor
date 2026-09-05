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
config.IGNORED_PATH = TMP / "ignored.json"

import bot
import common
import history
import ignore
import monitor

SENT: list[dict] = []


def fake_api(method, **params):
    """攔在最底層，send_message / send_report / answerCallbackQuery 都會經過。"""
    SENT.append({"method": method, **params})
    return {"ok": True}


common.api = fake_api


def sent_text() -> str:
    return "\n\n".join(s.get("text", "") for s in SENT if s["method"] == "sendMessage")


def sent_markups() -> list:
    return [s.get("reply_markup") for s in SENT if s["method"] == "sendMessage"]


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
    body = sent_text()
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

# 這份 DB 是即時複製正在用的機器上的真實資料，install 數量會隨時間增減
# （Sideloadly 自己會清掉舊列）。寫死 id=1..4 曾經在 id 被清到只剩 3 筆時
# 直接讓下面每個 check 都失效卻不出錯（UPDATE 對不存在的 id 就是靜靜更新
# 0 筆）。改成量測目前實際有的 id，不夠 4 筆就複製第一筆湊數，讓這段場景
# 不再依賴某個特定時間點的真實資料長什麼樣子。
con = sqlite3.connect(DB)
install_ids = [row[0] for row in con.execute("SELECT id FROM installations ORDER BY id")]
while len(install_ids) < 4:
    with con:
        con.execute(
            "INSERT INTO installations (name, ipa_id, device_udid, last_updated, "
            "known_ttl, refresh_at_hours, failures_count) "
            "SELECT name, ipa_id, device_udid, last_updated, known_ttl, "
            "refresh_at_hours, failures_count FROM installations WHERE id = ?",
            (install_ids[0],),
        )
    install_ids = [row[0] for row in con.execute("SELECT id FROM installations ORDER BY id")]
con.close()
id1, id2, id3, id4 = install_ids[:4]

db("UPDATE installations SET last_updated = ? WHERE id = ?", ts(minutes=-1), id1)
db("UPDATE installations SET last_error = 'anisette server unreachable', "
   "failures_count = 3, last_failure_at = ? WHERE id = ?", ts(minutes=-5), id2)
db("UPDATE installations SET last_updated = ? WHERE id = ?", ts(days=-5), id3)
db("UPDATE installations SET last_updated = ? WHERE id = ?", ts(days=-9), id4)
db("UPDATE devices SET last_seen = ? WHERE rowid = 1", ts(days=-3))

body = run("mixed changes")
for label in ("刷新完成", "刷新失敗", "anisette", "逾期未刷新", "已過期", "裝置離線"):
    check(f"偵測到 {label}", label in body)

check("同日重跑不重複提醒逾期/離線", not run("same day"))

# ------------------------------------------------------------------- 恢復

db("UPDATE installations SET last_error = '', failures_count = 0 WHERE id = ?", id2)
db("UPDATE devices SET last_seen = ? WHERE rowid = 1", ts(minutes=-2))
body = run("recovery")
check("偵測到錯誤解除", "錯誤已解除" in body)
check("偵測到裝置回線", "裝置回線" in body)

# ------------------------------------------------------------------- 靜音

common.set_mute(1)
before = len(history.recent(200))
db("UPDATE installations SET last_updated = ? WHERE id = ?", ts(minutes=-1), id1)
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
check("偵測到 monitor 停擺", "停擺" in sent_text())

SENT.clear()
bot.check_heartbeat()
check("冷卻期內不重複告警", not SENT)

set_last_run(seconds=0)
SENT.clear()
bot.check_heartbeat()
check("偵測到 monitor 恢復", "恢復" in sent_text())

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
check("長訊息會切段", len(common._chunk("x\n" * 4000, 3400)) > 1)

# 表格欄位要對齊：中文字寬度算 2 才不會歪
check("寬度計算把中文算兩格", common.display_width("裝置ab") == 6)
check("padding 依顯示寬度補齊", common.display_width(common.pad("裝置", 10)) == 10)


# ------------------------------------------------------------- 選單與按鈕

def press(action_or_data: str):
    SENT.clear()
    bot.handle_callback({
        "id": "cb1",
        "data": action_or_data,
        "message": {"chat": {"id": int(config.CHAT_ID)}},
    })


def say(text: str):
    SENT.clear()
    bot.handle_message({
        "chat": {"id": int(config.CHAT_ID)},
        "text": text,
    })


common.perform_restart = lambda verify=True: (True, "已重啟（測試）")

say("/menu")
check("/menu 送出選單", "選單" in sent_text())
check("選單帶按鈕", any(m and "inline_keyboard" in m for m in sent_markups()))

press("status")
check("按鈕 status 有回報表", "個 app" in sent_text())
check("報表回覆也帶按鈕", any(m and "inline_keyboard" in m for m in sent_markups()))
check("按鈕按下有 answerCallbackQuery",
      any(s["method"] == "answerCallbackQuery" for s in SENT))

press("restart")
check("重啟先要確認", "確定要重啟" in sent_text())
press("restart:go")
check("確認後才真的重啟", "已重啟" in sent_text())

press("restart:go")
check("沒有待確認時拒絕重啟", "逾時" in sent_text())

say("/restart")
check("文字指令也走確認流程", "確定要重啟" in sent_text())
say("/confirm")
check("/confirm 仍可用", "已重啟" in sent_text())

press("mute:8")
check("按鈕可靜音", "已靜音 8 小時" in sent_text())
press("unmute")
check("按鈕可解除靜音", "已解除靜音" in sent_text())
common.clear_mute()

SENT.clear()
bot.handle_message({"chat": {"id": 99999999}, "text": "/status"})
check("非授權 chat 不回應", not SENT)

say("/nonsense")
check("未知指令回說明", "/menu" in sent_text())

check("指令清單有註冊項目", len(bot.BOT_COMMANDS) >= 8)
check(
    "指令清單含 forget/forgotten/redeploy",
    {"forget", "forgotten", "redeploy"} <= {c["command"] for c in bot.BOT_COMMANDS},
)


def find_callback(prefix: str) -> str | None:
    """從最後一次送出的訊息裡找第一個以 prefix 開頭的 callback_data。"""
    for markup in reversed(sent_markups()):
        if not markup:
            continue
        for row in markup.get("inline_keyboard", []):
            for btn in row:
                if btn.get("callback_data", "").startswith(prefix):
                    return btn["callback_data"]
    return None


# ---------------------------------------------------------- forget 過濾（不經 bot）

target_device = common.fetch_devices()[0]

check("重複忘記同一台裝置第二次回 False（不重複寫入）",
      ignore.ignore_device(target_device.udid, target_device.name)
      and not ignore.ignore_device(target_device.udid, target_device.name))
check("忘記裝置後唯讀來源 fetch_devices 不受影響（不寫 Sideloadly 的 DB）",
      any(d.udid == target_device.udid for d in common.fetch_devices()))
check("忘記裝置後 visible_devices 看不到它",
      not any(d.udid == target_device.udid for d in common.visible_devices()))
check("忘記裝置後 visible_installs 連帶看不到它底下的 app",
      not any(i.device_udid == target_device.udid for i in common.visible_installs()))
check("忘記裝置後 /devices 報表不再提到它",
      target_device.name not in common.build_device_report())

ignore.unignore_device(target_device.udid)
check("復原後 visible_devices 恢復看得到",
      any(d.udid == target_device.udid for d in common.visible_devices()))

target_install = common.fetch_installs()[0]
ignore.ignore_install(target_install.device_udid, target_install.device_name, target_install.app_name)
check("忘記單一 app 後 visible_installs 看不到它",
      not any(i.id == target_install.id for i in common.visible_installs()))
other_installs_same_device = [
    i for i in common.fetch_installs()
    if i.device_udid == target_install.device_udid and i.id != target_install.id
]
check("忘記單一 app 不會連帶忘記整台裝置",
      any(d.udid == target_install.device_udid for d in common.visible_devices()))
check("忘記單一 app 不影響同裝置上的其他 app",
      all(
          any(v.id == other.id for v in common.visible_installs())
          for other in other_installs_same_device
      ))
ignore.unignore_install(target_install.device_udid, target_install.app_name)
check("復原後 visible_installs 恢復看得到",
      any(i.id == target_install.id for i in common.visible_installs()))

# ------------------------------------------------------- 忘記/復原（走 bot 指令）

say("/forget")
check("/forget 列出可忘記清單", "選一個要忘記" in sent_text())
forget_cb = find_callback("forget:")
check("清單裡有可忘記的按鈕", forget_cb is not None)

press(forget_cb)
check("按下後有已忘記的確認訊息", "已忘記" in sent_text())

say("/forgotten")
check("/forgotten 顯示已忘記清單", "已忘記" in sent_text())
unforget_cb = find_callback("forgotten:")
check("忘記清單裡有可復原的按鈕", unforget_cb is not None)

press(unforget_cb)
check("取消忘記有確認訊息", "取消忘記" in sent_text())

say("/forgotten")
check("復原後忘記清單清空", "目前沒有忘記" in sent_text())

# --------------------------------------------------------- 重新部署（/redeploy）

say("/redeploy")
check("/redeploy 列出可選 app", "選一個 app" in sent_text())
redeploy_cb = find_callback("redeploy:")
check("清單裡有可重新部署的按鈕", redeploy_cb is not None)

press(redeploy_cb)
check("redeploy 按鈕先要求確認", "重新部署" in sent_text())
check("確認訊息老實說這是整顆 daemon 重啟", "整顆 Sideloadly daemon" in sent_text())

press("restart:go")
check("確認後真的重啟", "已重啟" in sent_text())

last_restart = history.recent(5, kinds=["restart"])[0]
check("歷史紀錄點名是為了哪個 app 觸發的", "為了" in (last_restart["detail"] or ""))

# /status 在有問題的 app 存在時，應該附上對應的重新部署按鈕（見前面「mixed
# changes」段落留下的過期/逾期/失敗資料，DB 沒有被改回去，此時仍算有問題）。
press("status")
check("/status 有問題時附帶重新部署按鈕", find_callback("redeploy:") is not None)

shutil.rmtree(TMP, ignore_errors=True)
print("\n✅ 全部通過")

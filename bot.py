#!/usr/bin/env python3
"""Telegram 指令介面。

除了查詢與重啟，還兼任監控的看門狗：每輪 getUpdates 回來時檢查
state.json 的 last_run，發現 monitor 停擺就告警——否則
「一切正常」和「監控自己死了」長得一模一樣。
"""
import json
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone

import common
import config
import history

# 註冊給 Telegram，聊天室輸入框旁就會出現指令選單。
BOT_COMMANDS = [
    {"command": "menu", "description": "功能選單"},
    {"command": "status", "description": "各 app 到期倒數"},
    {"command": "devices", "description": "裝置連線狀態"},
    {"command": "accounts", "description": "Apple ID 額度狀態"},
    {"command": "log", "description": "最近異常紀錄"},
    {"command": "stats", "description": "刷新統計"},
    {"command": "restart", "description": "重啟 Sideloadly daemon"},
    {"command": "mute", "description": "暫停通知（預設 8 小時）"},
    {"command": "unmute", "description": "解除靜音"},
    {"command": "help", "description": "說明"},
]

MENU_KEYBOARD = {
    "inline_keyboard": [
        [
            {"text": "📊 狀態", "callback_data": "status"},
            {"text": "📱 裝置", "callback_data": "devices"},
        ],
        [
            {"text": "🆔 Apple ID 額度", "callback_data": "accounts"},
        ],
        [
            {"text": "📜 異常紀錄", "callback_data": "log"},
            {"text": "📈 統計", "callback_data": "stats"},
        ],
        [
            {"text": "🔇 靜音 8 小時", "callback_data": "mute:8"},
            {"text": "🔔 解除靜音", "callback_data": "unmute"},
        ],
        [{"text": "🔄 重啟 daemon", "callback_data": "restart"}],
    ]
}

CONFIRM_KEYBOARD = {
    "inline_keyboard": [
        [
            {"text": "✅ 確定重啟", "callback_data": "restart:go"},
            {"text": "取消", "callback_data": "menu"},
        ]
    ]
}

MENU_TEXT = (
    "Sideloadly 監控選單\n"
    "點按鈕，或直接輸入指令（輸入框旁的選單也有）。"
)

HELP_TEXT = (
    "Sideloadly 監控\n\n"
    "/menu - 功能選單（按鈕）\n"
    "/status - 各 app 到期倒數與問題\n"
    "/devices - 裝置連線狀態\n"
    "/accounts - 各 Apple ID 本週 App ID 額度\n"
    "/log [n] - 最近異常紀錄（預設 15 筆）\n"
    "/stats [天數] - 刷新統計與平均間隔（預設 7 天）\n"
    "/restart - 重啟 daemon（需確認）\n"
    "/mute [小時] - 暫停主動通知（預設 8 小時）\n"
    "/unmute - 解除靜音\n\n"
    "過期倒數是依資料庫的憑證有效天數算的。\n"
    "靜音只擋主動通知，指令回覆照常，事件仍會記錄。"
)

CONFIRM_WINDOW = timedelta(seconds=60)
DEFAULT_MUTE_HOURS = 8

_pending_restart: datetime | None = None
_heartbeat_alerted_at: datetime | None = None
_heartbeat_was_stale = False


def load_offset() -> int:
    if config.BOT_OFFSET_PATH.exists():
        return int(config.BOT_OFFSET_PATH.read_text().strip() or 0)
    return 0


def save_offset(offset: int):
    config.BOT_OFFSET_PATH.write_text(str(offset))


def get_updates(offset: int, timeout: int = 30) -> list[dict]:
    url = f"https://api.telegram.org/bot{config.BOT_TOKEN}/getUpdates"
    query = f"offset={offset}&timeout={timeout}"
    with urllib.request.urlopen(f"{url}?{query}", timeout=timeout + 10) as resp:
        body = json.loads(resp.read())
    return body.get("result", [])


def _int_arg(args: list[str], default: int, lo: int, hi: int) -> int:
    if not args:
        return default
    try:
        return max(lo, min(hi, int(args[0])))
    except ValueError:
        return default


# ----------------------------------------------------------------- 動作分派

def dispatch(action: str, args: list[str]):
    """文字指令和按鈕共用同一套動作。"""
    global _pending_restart

    if action == "menu":
        common.send_message(MENU_TEXT, reply_markup=MENU_KEYBOARD)

    elif action == "status":
        common.send_report(common.build_status_report(), reply_markup=MENU_KEYBOARD)

    elif action == "devices":
        common.send_report(common.build_device_report(), reply_markup=MENU_KEYBOARD)

    elif action == "accounts":
        common.send_report(common.build_account_report(), reply_markup=MENU_KEYBOARD)

    elif action == "log":
        common.send_report(
            history.build_log_report(_int_arg(args, 15, 1, 50)),
            reply_markup=MENU_KEYBOARD,
        )

    elif action == "stats":
        common.send_report(
            history.build_stats_report(_int_arg(args, 7, 1, 90)),
            reply_markup=MENU_KEYBOARD,
        )

    elif action == "restart":
        _pending_restart = datetime.now(timezone.utc) + CONFIRM_WINDOW
        common.send_message(
            "⚠ 確定要重啟 Sideloadly daemon？\n"
            "正在進行的刷新會被打斷。60 秒內確認，或直接忽略。",
            reply_markup=CONFIRM_KEYBOARD,
        )

    elif action == "restart:go":
        if _pending_restart is None or datetime.now(timezone.utc) > _pending_restart:
            _pending_restart = None
            common.send_message("確認已逾時，請重新操作。", reply_markup=MENU_KEYBOARD)
            return
        _pending_restart = None
        common.send_message("正在重啟，稍候…")
        ok, result = common.perform_restart()
        history.record("restart", detail=f"telegram: {result}")
        common.send_message(
            ("✅ " if ok else "❌ ") + result, reply_markup=MENU_KEYBOARD
        )

    elif action == "mute":
        hours = _int_arg(args, DEFAULT_MUTE_HOURS, 1, 720)
        common.set_mute(hours)
        common.send_message(
            f"🔇 已靜音 {hours} 小時，期間不主動推送。\n事件仍會記錄，之後可用 /log 補看。",
            reply_markup=MENU_KEYBOARD,
        )

    elif action == "unmute":
        common.clear_mute()
        common.send_message("🔔 已解除靜音。", reply_markup=MENU_KEYBOARD)

    elif action == "help":
        common.send_message(HELP_TEXT, reply_markup=MENU_KEYBOARD)

    else:
        common.send_message(
            "看不懂這個指令。\n\n" + HELP_TEXT, reply_markup=MENU_KEYBOARD
        )


def handle_message(message: dict):
    if str(message.get("chat", {}).get("id", "")) != str(config.CHAT_ID):
        return

    parts = (message.get("text") or "").strip().split()
    if not parts:
        return
    # 群組裡 Telegram 會把指令寫成 /status@some_bot。
    command = parts[0].split("@", 1)[0].lower().lstrip("/")
    args = parts[1:]

    if command in ("start", "help"):
        dispatch("help", args)
    elif command == "confirm":
        dispatch("restart:go", args)
    else:
        dispatch(command, args)


def handle_callback(callback: dict):
    # 按鈕按下後一定要回應，否則 Telegram 端會一直轉圈。
    common.answer_callback(callback.get("id", ""))
    chat_id = str(callback.get("message", {}).get("chat", {}).get("id", ""))
    if chat_id != str(config.CHAT_ID):
        return

    action, _, arg = (callback.get("data") or "").partition(":")
    if action == "restart" and arg == "go":
        dispatch("restart:go", [])
    else:
        dispatch(action, [arg] if arg else [])


# ----------------------------------------------------------------- 看門狗

def check_heartbeat():
    """monitor 每小時應更新一次 state.json 的 last_run，停擺就告警。"""
    global _heartbeat_alerted_at, _heartbeat_was_stale

    if not config.STATE_PATH.exists():
        return
    try:
        last_run = common.parse_ts(
            json.loads(config.STATE_PATH.read_text()).get("last_run")
        )
    except (json.JSONDecodeError, OSError):
        return
    if last_run is None:
        return

    now = datetime.now(timezone.utc)
    stale_for = (now - last_run).total_seconds()
    stale = stale_for > config.HEARTBEAT_STALE_HOURS * 3600

    if stale:
        due = (
            _heartbeat_alerted_at is None
            or (now - _heartbeat_alerted_at).total_seconds()
            > config.HEARTBEAT_REPEAT_HOURS * 3600
        )
        if due:
            _heartbeat_alerted_at = now
            _heartbeat_was_stale = True
            detail = f"已 {common.human_delta(stale_for)}沒有更新"
            history.record("monitor_stale", detail=detail)
            common.notify(
                "⚠ Sideloadly 監控停擺",
                f"monitor {detail}，刷新狀態可能已經沒人看著了。",
            )
    elif _heartbeat_was_stale:
        _heartbeat_was_stale = False
        _heartbeat_alerted_at = None
        common.notify("✅ Sideloadly 監控恢復", "monitor 已重新開始更新。")


def main():
    offset = load_offset()
    if common.api("setMyCommands", commands=BOT_COMMANDS):
        print("已註冊指令選單", flush=True)
    print(f"sideloadly bot 啟動，offset={offset}", flush=True)

    while True:
        try:
            updates = get_updates(offset)
        except Exception as exc:
            print(f"getUpdates 失敗: {exc}", file=sys.stderr, flush=True)
            time.sleep(5)
            continue

        for update in updates:
            offset = update["update_id"] + 1
            try:
                if "message" in update:
                    handle_message(update["message"])
                elif "callback_query" in update:
                    handle_callback(update["callback_query"])
            except Exception as exc:
                print(f"處理 update 失敗: {exc}", file=sys.stderr, flush=True)
        save_offset(offset)

        try:
            check_heartbeat()
        except Exception as exc:
            print(f"心跳檢查失敗: {exc}", file=sys.stderr, flush=True)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Telegram 指令介面。除了查詢與重啟，還兼任監控的看門狗：
每輪 getUpdates 回來時檢查 state.json 的 last_run，
發現 monitor 停擺就告警——否則「一切正常」和「監控自己死了」長得一樣。
"""
import json
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone

import common
import config
import history

HELP_TEXT = (
    "可用指令：\n"
    "/status - 所有 app 的過期倒數與錯誤\n"
    "/devices - 各裝置連線狀態\n"
    "/log [n] - 最近的異常紀錄（預設 15 筆）\n"
    "/stats [天數] - 刷新/失敗次數與平均間隔（預設 7 天）\n"
    "/restart - 重啟 Sideloadly daemon（需 /confirm）\n"
    "/mute [小時] - 暫停主動通知（預設 8 小時）\n"
    "/unmute - 解除靜音\n"
    "/help - 顯示這個說明"
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


def parse_command(text: str) -> tuple[str, list[str]]:
    parts = text.split()
    if not parts:
        return "", []
    # 群組裡 Telegram 會把指令寫成 /status@some_bot。
    return parts[0].split("@", 1)[0].lower(), parts[1:]


def _int_arg(args: list[str], default: int, lo: int, hi: int) -> int:
    if not args:
        return default
    try:
        return max(lo, min(hi, int(args[0])))
    except ValueError:
        return default


# --------------------------------------------------------------- 指令處理

def handle_message(message: dict):
    global _pending_restart

    chat_id = str(message.get("chat", {}).get("id", ""))
    if chat_id != str(config.CHAT_ID):
        return

    command, args = parse_command((message.get("text") or "").strip())

    if command == "/status":
        common.send_message(common.build_status_report())

    elif command == "/devices":
        common.send_message(common.build_device_report())

    elif command == "/log":
        common.send_message(history.build_log_report(_int_arg(args, 15, 1, 50)))

    elif command == "/stats":
        common.send_message(history.build_stats_report(_int_arg(args, 7, 1, 90)))

    elif command == "/restart":
        _pending_restart = datetime.now(timezone.utc) + CONFIRM_WINDOW
        common.send_message(
            "⚠ 確定要重啟 Sideloadly daemon？\n60 秒內傳 /confirm 執行，或直接忽略。"
        )

    elif command == "/confirm":
        if _pending_restart is None or datetime.now(timezone.utc) > _pending_restart:
            _pending_restart = None
            common.send_message("沒有待確認的操作（或已逾時）。")
        else:
            _pending_restart = None
            common.send_message("正在重啟，稍候…")
            ok, result = common.perform_restart()
            history.record("restart", detail=f"telegram: {result}")
            common.send_message(("✅ " if ok else "❌ ") + result)

    elif command == "/mute":
        hours = _int_arg(args, DEFAULT_MUTE_HOURS, 1, 720)
        common.set_mute(hours)
        common.send_message(f"🔇 已靜音 {hours} 小時，期間不主動推送。/unmute 可提前解除。")

    elif command == "/unmute":
        common.clear_mute()
        common.send_message("🔔 已解除靜音。")

    elif command in ("/start", "/help"):
        common.send_message(HELP_TEXT)

    else:
        common.send_message("看不懂這個指令。\n\n" + HELP_TEXT)


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
            message = update.get("message")
            if message:
                try:
                    handle_message(message)
                except Exception as exc:
                    print(f"處理訊息失敗: {exc}", file=sys.stderr, flush=True)
        save_offset(offset)

        try:
            check_heartbeat()
        except Exception as exc:
            print(f"心跳檢查失敗: {exc}", file=sys.stderr, flush=True)


if __name__ == "__main__":
    main()

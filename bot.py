#!/usr/bin/env python3
"""Telegram 指令介面。

除了查詢與重啟，還兼任監控的看門狗：每輪 getUpdates 回來時檢查
state.json 的 last_run，發現 monitor 停擺就告警——否則
「一切正常」和「監控自己死了」長得一模一樣。
"""
import json
import sys
import time
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from functools import partial

import common
import config
import forget
import history

DEFAULT_MUTE_HOURS = 8
# /restart、/redeploy 送出確認後，多久內要按「確定重啟」或回 /confirm。
CONFIRM_SECONDS = 60
# getUpdates 的 long polling 秒數：沒有新訊息時，Telegram 最多讓連線掛這麼久才回。
POLL_SECONDS = 30

# 註冊給 Telegram，聊天室輸入框旁就會出現指令選單。
BOT_COMMANDS = [
    {"command": "menu", "description": "功能選單"},
    {"command": "status", "description": "裝置、app 到期倒數與問題"},
    {"command": "accounts", "description": "Apple ID 額度狀態"},
    {"command": "log", "description": "最近異常紀錄"},
    {"command": "stats", "description": "刷新統計"},
    {"command": "restart", "description": "重啟 Sideloadly daemon"},
    {"command": "redeploy", "description": "為某個 app 重新部署（重啟 daemon）"},
    {"command": "forget", "description": "忘記某個裝置或 app"},
    {"command": "forgotten", "description": "查看/復原忘記清單"},
    {"command": "mute", "description": f"暫停通知（預設 {DEFAULT_MUTE_HOURS} 小時）"},
    {"command": "unmute", "description": "解除靜音"},
    {"command": "help", "description": "說明"},
]

MENU_KEYBOARD = {
    "inline_keyboard": [
        [
            {"text": "📊 狀態", "callback_data": "status"},
            {"text": "🆔 Apple ID 額度", "callback_data": "accounts"},
        ],
        [
            {"text": "📜 異常紀錄", "callback_data": "log"},
            {"text": "📈 統計", "callback_data": "stats"},
        ],
        [
            {"text": f"🔇 靜音 {DEFAULT_MUTE_HOURS} 小時", "callback_data": "mute"},
            {"text": "🔔 解除靜音", "callback_data": "unmute"},
        ],
        [
            {"text": "🙈 忘記裝置/app", "callback_data": "forget"},
            {"text": "🔔 忘記清單", "callback_data": "forgotten"},
        ],
        [
            {"text": "🔄 重啟 daemon", "callback_data": "restart"},
            {"text": "🔁 重新部署", "callback_data": "redeploy"},
        ],
    ]
}

CONFIRM_KEYBOARD = {
    "inline_keyboard": [
        [
            {"text": "✅ 確定重啟", "callback_data": "restart:go"},
            {"text": "取消", "callback_data": "restart:cancel"},
        ]
    ]
}

# 選單清單類訊息（/forget /forgotten /redeploy 的選項列表）按鈕下方統一加這顆，
# 不整包附上 MENU_KEYBOARD——選項本來就可能有一大串，再疊六顆選單按鈕只會更亂。
BACK_TO_MENU_KEYBOARD_ROW = [{"text": "◀️ 選單", "callback_data": "menu"}]

MENU_TEXT = (
    "Sideloadly 監控選單\n"
    "點按鈕，或直接輸入指令（輸入框旁的選單也有）。"
)

HELP_TEXT = (
    "Sideloadly 監控\n\n"
    "/menu - 功能選單（按鈕）\n"
    "/status - 每台裝置的連線狀態、上面有哪些 app（誰簽的、剩多久）與問題\n"
    "/accounts - 各 Apple ID 本週 App ID 額度\n"
    "/log [n] - 最近異常紀錄（預設 15 筆）\n"
    "/stats [天數] - 刷新統計與平均間隔（預設 7 天）\n"
    "/restart - 重啟 daemon（需確認）\n"
    "/redeploy - 為某個 app 重新部署（需確認；動作跟 /restart 一樣是整顆"
    "daemon 重啟，只是訊息和紀錄會點名是為了哪個 app）\n"
    "/forget - 忘記某個裝置或 app，之後不再收到它的告警（只是本機清單，"
    "不會動到 Sideloadly 自己的資料）\n"
    "/forgotten - 查看已忘記清單，可以復原\n"
    f"/mute [小時] - 暫停主動通知（預設 {DEFAULT_MUTE_HOURS} 小時）\n"
    "/unmute - 解除靜音\n\n"
    "過期倒數是依資料庫的憑證有效天數算的。\n"
    "靜音只擋主動通知，指令回覆照常，事件仍會記錄。"
)

# {"until": datetime, "reason": str | None} — reason 是 /redeploy 點名的 app，
# 一般 /restart 沒有 reason。兩者共用同一段確認流程與同一個 restart:go 按鈕。
_pending_restart: dict | None = None
_heartbeat_alerted_at: datetime | None = None
_heartbeat_was_stale = False

# /forget、/forgotten 選單的「按鈕代號 → (按下去要做的事, 顯示用的名字)」，選單
# 訊息送出時重建，只在下一次按鈕按下之前有效（跟 _pending_restart 一樣是進程內的
# 暫存狀態，這個 bot 本來就是單一 chat 常駐一個進程，不需要更持久的存法）。
# 代號用流水號，因為 callback_data 上限 64 bytes，塞不下 udid 加 app 名。
_forget_candidates: dict[str, tuple[Callable[[], bool], str]] = {}
_unforget_candidates: dict[str, tuple[Callable[[], bool], str]] = {}


def _picker_keyboard(action: str, buttons: list[tuple[str, str]]) -> dict:
    """選項清單的按鈕：一個選項一列，超過上限的不列，最下面加回選單。
    buttons 是 (callback 參數, 按鈕文字)。"""
    rows = [
        [{"text": text, "callback_data": f"{action}:{key}"}]
        for key, text in buttons[: config.PICKER_MAX_BUTTONS]
    ]
    return {"inline_keyboard": [*rows, BACK_TO_MENU_KEYBOARD_ROW]}


def _start_restart_confirm(reason: str | None):
    """/restart 與 /redeploy 共用的確認流程，差別只在訊息措辭跟事後紀錄要不要
    點名是哪個 app——實際動作兩邊完全一樣，都是整顆 daemon 重啟。"""
    global _pending_restart
    _pending_restart = {
        "until": datetime.now(timezone.utc) + timedelta(seconds=CONFIRM_SECONDS),
        "reason": reason,
    }
    if reason:
        text = (
            f"⚠ 確定要為了「{reason}」重新部署？\n"
            "這個動作是重啟整顆 Sideloadly daemon（目前沒有辦法只重簽單一 app），"
            f"其他裝置／app 正在進行的刷新也會被一起打斷。{CONFIRM_SECONDS} 秒內確認，"
            "或直接忽略。"
        )
    else:
        text = (
            "⚠ 確定要重啟 Sideloadly daemon？\n"
            f"正在進行的刷新會被打斷。{CONFIRM_SECONDS} 秒內確認，或直接忽略。"
        )
    common.send_message(text, reply_markup=CONFIRM_KEYBOARD)


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
    global _pending_restart, _forget_candidates, _unforget_candidates

    if action == "menu":
        common.send_message(MENU_TEXT, reply_markup=MENU_KEYBOARD)

    elif action == "status":
        problem_keyboard = common.status_action_keyboard()
        markup = (
            {"inline_keyboard": problem_keyboard["inline_keyboard"] + MENU_KEYBOARD["inline_keyboard"]}
            if problem_keyboard
            else MENU_KEYBOARD
        )
        common.send_report(common.build_status_report(), reply_markup=markup)

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

    elif action == "restart" and args[:1] == ["go"]:
        if _pending_restart is None or datetime.now(timezone.utc) > _pending_restart["until"]:
            _pending_restart = None
            common.send_message(
                "沒有待確認的重啟（已逾時或已取消），請重新操作。", reply_markup=MENU_KEYBOARD
            )
            return
        reason = _pending_restart.get("reason")
        _pending_restart = None
        common.send_message("正在重啟，稍候…")
        ok, result = common.perform_restart()
        history.record_restart(ok, result, f"telegram，為了 {reason}" if reason else "telegram")
        common.send_message(
            ("✅ " if ok else "❌ ") + result, reply_markup=MENU_KEYBOARD
        )

    elif action == "restart" and args[:1] == ["cancel"]:
        # 只換成選單不夠：舊訊息上的「確定重啟」還按得到，不清掉就等於沒取消。
        _pending_restart = None
        common.send_message("已取消重啟。", reply_markup=MENU_KEYBOARD)

    elif action == "restart":
        _start_restart_confirm(None)

    elif action == "redeploy":
        target = args[0] if args else None
        if not target:
            installs = common.fetch_installs()
            if not installs:
                common.send_message(
                    "沒有任何裝置資料，沒有東西可以重新部署。", reply_markup=MENU_KEYBOARD
                )
                return
            common.send_message(
                "選一個 app：實際動作是重啟整顆 daemon，這裡只是讓訊息和紀錄"
                "點名是為了哪個 app。",
                reply_markup=_picker_keyboard("redeploy", [(i.id, i.label) for i in installs]),
            )
            return
        match = next((i for i in common.fetch_installs() if i.id == target), None)
        if match is None:
            common.send_message(
                "找不到這個 app 了，可能已經被刪除或重簽過。", reply_markup=MENU_KEYBOARD
            )
            return
        _start_restart_confirm(match.label)

    elif action == "forget":
        if args and args[0] in _forget_candidates:
            apply, label = _forget_candidates.pop(args[0])
            text = (
                f"🙈 已忘記「{label}」，之後不會再收到它的告警。"
                if apply()
                else f"「{label}」本來就已經忘記了。"
            )
            common.send_message(text + "\n可用 /forgotten 查看或復原。", reply_markup=MENU_KEYBOARD)
            return

        options = [
            (partial(forget.forget_device, d.udid, d.name), d.name)
            for d in common.visible_devices()
        ] + [
            (partial(forget.forget_install, i.device_udid, i.device_name, i.app_name), i.label)
            for i in common.visible_installs()
        ]
        _forget_candidates = {str(n): option for n, option in enumerate(options, start=1)}
        if not options:
            common.send_message("目前沒有可以忘記的裝置或 app 了。", reply_markup=MENU_KEYBOARD)
            return
        common.send_message(
            "選一個要忘記的裝置或 app（忘記後不會再看到它的告警，可用 "
            "/forgotten 復原）：",
            reply_markup=_picker_keyboard(
                "forget", [(key, label) for key, (_, label) in _forget_candidates.items()]
            ),
        )

    elif action == "forgotten":
        if args and args[0] in _unforget_candidates:
            apply, label = _unforget_candidates.pop(args[0])
            text = (
                f"🔔 已取消忘記「{label}」，之後會恢復告警。"
                if apply()
                else f"「{label}」不在忘記清單裡（可能已經復原過了）。"
            )
            common.send_message(text, reply_markup=MENU_KEYBOARD)
            return

        options = [
            (partial(forget.unforget_device, d.udid), d.name)
            for d in forget.forgotten_devices()
        ] + [
            (partial(forget.unforget_install, i.device_udid, i.app_name), i.label)
            for i in forget.forgotten_installs()
        ]
        _unforget_candidates = {str(n): option for n, option in enumerate(options, start=1)}
        report = common.build_forgotten_report()
        if not options:
            common.send_message(report, reply_markup=MENU_KEYBOARD)
            return
        common.send_message(
            report + "\n\n點按鈕可以取消忘記：",
            reply_markup=_picker_keyboard(
                "forgotten",
                [(key, f"🔔 {label}") for key, (_, label) in _unforget_candidates.items()],
            ),
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
        dispatch("restart", ["go"])
    else:
        dispatch(command, args)


def handle_callback(callback: dict):
    # 按鈕按下後一定要回應，否則 Telegram 端會一直轉圈。
    common.api("answerCallbackQuery", callback_query_id=callback.get("id", ""))
    chat_id = str(callback.get("message", {}).get("chat", {}).get("id", ""))
    if chat_id != str(config.CHAT_ID):
        return

    action, _, arg = (callback.get("data") or "").partition(":")
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
    offset = 0
    if config.BOT_OFFSET_PATH.exists():
        offset = int(config.BOT_OFFSET_PATH.read_text().strip() or 0)
    if common.api("setMyCommands", commands=BOT_COMMANDS):
        print("已註冊指令選單", flush=True)
    print(f"sideloadly bot 啟動，offset={offset}", flush=True)

    while True:
        # HTTP 的逾時要比 long polling 長，否則沒有新訊息的每一輪都會變成逾時錯誤。
        body = common.api(
            "getUpdates", http_timeout=POLL_SECONDS + 10, offset=offset, timeout=POLL_SECONDS
        )
        if body is None:
            time.sleep(5)
            continue

        for update in body.get("result", []):
            offset = update["update_id"] + 1
            try:
                if "message" in update:
                    handle_message(update["message"])
                elif "callback_query" in update:
                    handle_callback(update["callback_query"])
            except Exception as exc:
                print(f"處理 update 失敗: {exc}", file=sys.stderr, flush=True)
        config.BOT_OFFSET_PATH.write_text(str(offset))

        try:
            check_heartbeat()
        except Exception as exc:
            print(f"心跳檢查失敗: {exc}", file=sys.stderr, flush=True)


if __name__ == "__main__":
    main()

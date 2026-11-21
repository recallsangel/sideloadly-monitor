#!/usr/bin/env python3
"""每小時比對 sideloadly 資料庫，把變化彙整成一則通知推出去。

偵測項目：刷新完成、刷新失敗、錯誤解除、逾期未刷新、已過期、裝置離線/回線、
usbmuxd 弄丟了還在 Wi-Fi 上的裝置（附一顆重啟 usbmuxd 的按鈕）。
daemon 沒在跑的話當場重啟，不等 04:00 的 restart.py。
每次執行都會更新 state.json 的 last_run，bot 端靠它判斷監控是否停擺。
"""
import json
from datetime import date, datetime, timedelta, timezone

import common
import config
import history
import restart

USBMUXD_KEYBOARD = {
    "inline_keyboard": [[{"text": "🔌 重啟 usbmuxd", "callback_data": "usbmuxd"}]]
}


def main():
    if not config.SIDELOADLY_DB_PATH.exists():
        return

    # 順手收日誌：Sideloadly daemon 的 stderr/stdout，加上本專案自己的 *.log。
    # 不推播——這是例行家務，不是告警；印出來就好，launchd 會收進 monitor.log。
    logs = [
        config.DAEMON_ERR_LOG_PATH,
        config.DAEMON_OUT_LOG_PATH,
        *sorted(config.LOG_DIR.glob("*.log")),
    ]
    for path in logs:
        rotated = common.rotate_log(path)
        if rotated:
            common.log(rotated)

    # daemon 停了就沒人在刷新。04:00 的 restart.py 也會把它拉起來，但最久要等將近一天：
    # 2026-10-21 00:38 它跟著 Sideloadly 主程式一起結束（它的 LaunchAgent 沒設
    # KeepAlive），停了 3.4 小時才被 04:00 那支救回。啟動一顆沒在跑的 daemon 不會打斷
    # 任何刷新，所以每小時看一次，停了就提早跑 restart.py 那一套。
    # 查不到服務（None）代表它根本沒載入，那通常是刻意卸載的；硬要重啟只會每小時推一次失敗。
    daemon_state = common.daemon_state()
    if daemon_state is not None and daemon_state != "running":
        restart.main(source="monitor")

    prev_state = (
        json.loads(config.STATE_PATH.read_text()) if config.STATE_PATH.exists() else {}
    )
    # 看 last_run 而不是看上輪有沒有 app：app 全被刪掉或忘記時快照會一直是空的，
    # 那樣每一輪都會被當成第一次，連裝置離線都不會推。
    first_run = "last_run" not in prev_state
    prev_installs = prev_state.get("installs", {})
    prev_overdue = prev_state.get("overdue_notified", {})
    prev_expired = prev_state.get("expired_notified", {})
    prev_offline = prev_state.get("device_offline_notified", {})
    prev_usbmuxd = prev_state.get("usbmuxd_notified", {})
    # 「每天最多提醒一次」照本機時區換日。用 UTC 的話台灣是早上 8 點換日，
    # 那前後一小時內可能收到兩次同樣的提醒。
    today = date.today().isoformat()

    # 用 visible_* 而不是 fetch_*：被 /forget 忘記的裝置/app 不該再觸發告警。
    installs = common.visible_installs()
    devices = common.visible_devices()
    quotas = common.fetch_account_quotas()

    refreshed, failed, recovered, overdue, expired = [], [], [], [], []
    offline, back_online, lost = [], [], []
    # 新的 state 每輪從頭建，資料庫裡已經沒有（或被忘記）的 app 與裝置就不會留下來。
    # 留著的話快照只會一直累積，Sideloadly 重用 id 時還會把新 app 誤判成刷新完成。
    snapshot, overdue_notified, expired_notified, device_offline_notified = {}, {}, {}, {}
    usbmuxd_notified = {}

    for inst in installs:
        prev = prev_installs.get(inst.id, {})
        last_updated = inst.last_updated.isoformat() if inst.last_updated else None

        if prev and last_updated != prev.get("last_updated"):
            refreshed.append(f"  · {inst.label}（{inst.expiry_text()}）")
            history.record("refresh", inst.device_name, inst.app_name, inst.version)

        new_failure = inst.failures_count > prev.get("failures_count", 0) or (
            inst.last_error and inst.last_error != prev.get("last_error")
        )
        if prev and new_failure:
            failed.append(f"  · {inst.label}：{inst.failure_text()}")
            history.record("failure", inst.device_name, inst.app_name, inst.last_error)

            # App ID 額度用完是失敗的常見原因之一，但 Sideloadly 沒公開失敗原因的
            # 分類，只能拿週配額當旁證，附一個「可以換這個帳號試試」的提示——
            # 是提示，不是診斷。
            quota = quotas.get(inst.apple_id)
            if quota is not None and quota.remaining <= 0:
                alternates = [
                    q for q in quotas.values()
                    if q.apple_id != inst.apple_id and q.remaining > 0
                ]
                if alternates:
                    alt = max(alternates, key=lambda q: q.remaining)
                    failed.append(
                        f"    ↳ {inst.apple_id} 本週 App ID 額度已用完，"
                        f"可試著切到 {alt.apple_id}（剩 {alt.remaining} 個）"
                    )
                else:
                    failed.append(
                        f"    ↳ {inst.apple_id} 本週 App ID 額度已用完，"
                        f"其他已綁定帳號也沒有剩餘額度"
                    )
        # 這裡讀的是原始的 failing，不是 failing_now：錯誤「變舊」不是「解除」，
        # 拿 failing_now 比會在旗標放了 FAILURE_STALE_HOURS 之後憑空推一則
        # 「錯誤已解除」，而那時候什麼都還沒解決。
        elif prev and (prev.get("last_error") or prev.get("failures_count")) and not inst.failing:
            recovered.append(f"  · {inst.label}")
            history.record("recovery", inst.device_name, inst.app_name)

        snapshot[inst.id] = {
            "last_updated": last_updated,
            "failures_count": inst.failures_count,
            "last_error": inst.last_error,
        }

        # 逾期／過期每天最多提醒一次。
        if inst.expired:
            expired_notified[inst.id] = today
            if prev_expired.get(inst.id) != today:
                expired.append(f"  · {inst.label}：{inst.expiry_text()}")
                history.record("expired", inst.device_name, inst.app_name, inst.expiry_text())
        elif inst.overdue:
            overdue_notified[inst.id] = today
            if prev_overdue.get(inst.id) != today:
                overdue.append(f"  · {inst.label}：{inst.expiry_text()}")
                history.record("overdue", inst.device_name, inst.app_name, inst.expiry_text())

    for device in devices:
        if device.offline:
            device_offline_notified[device.udid] = today
            if prev_offline.get(device.udid) != today:
                offline.append(f"  · {device.name}：最後連線 {device.seen_text()}")
                history.record("device_offline", device.name, detail=device.seen_text())
        elif device.udid in prev_offline:
            back_online.append(f"  · {device.name}")
            history.record("device_online", device.name)

    # usbmuxd 弄丟的裝置，見 config.USBMUXD_DROP_MINUTES 上方說明。每輪只讀上一輪之後
    # 的日誌，所以弄丟的紀錄要存進 state.json 一路帶著，直到它重新接上。多往回讀 10
    # 分鐘，是為了不漏掉上一輪讀完日誌到寫下 last_run 之間的那幾秒。
    now = datetime.now(timezone.utc)
    oldest = now - timedelta(hours=config.USBMUXD_LOG_MAX_HOURS)
    last_run = common.parse_ts(prev_state.get("last_run"))
    since = max(last_run - timedelta(minutes=10), oldest) if last_run else oldest
    usbmuxd_drops = common.track_usbmuxd_drops(
        prev_state.get("usbmuxd_drops", {}), common.usbmuxd_log(since)
    )
    # 弄丟太久的不再帶著，見 config.USBMUXD_DROP_FORGET_DAYS 上方說明。時間讀不出來的
    # 那筆永遠不會告警、也永遠等不到這個期限，一起拿掉。
    forget_before = now - timedelta(days=config.USBMUXD_DROP_FORGET_DAYS)
    usbmuxd_drops = {
        mac: drop
        for mac, drop in usbmuxd_drops.items()
        if (dropped_at := common.parse_ts(drop["dropped_at"])) and dropped_at >= forget_before
    }
    devices_by_udid = {d.udid: d for d in devices}
    for mac, drop in usbmuxd_drops.items():
        dropped_at = common.parse_ts(drop["dropped_at"])
        if now - dropped_at < timedelta(minutes=config.USBMUXD_DROP_MINUTES):
            continue
        # 認得出是哪台、卻不在 visible_devices 裡：被 /forget 忘記了，或者根本不是
        # Sideloadly 在用的裝置，它接不接得回來都跟刷新無關。
        if drop["udid"] and drop["udid"] not in devices_by_udid:
            continue
        # 裝置真的離開 Wi-Fi 時也會留下同一筆錯誤，那種重啟 usbmuxd 也接不回來。
        host = common.wifi_sync_host(drop["instance"])
        if host is None:
            continue
        name = devices_by_udid[drop["udid"]].name if drop["udid"] else host
        lost_for = common.human_delta((now - dropped_at).total_seconds())
        usbmuxd_notified[mac] = today
        if prev_usbmuxd.get(mac) != today:
            lost.append(f"  · {name}：已經 {lost_for}沒接上，但它還在 Wi-Fi 上")
            history.record("usbmuxd_drop", name, detail=f"已經 {lost_for}沒接上")
    if lost:
        lost.append("    ↳ 按下面的按鈕重啟 usbmuxd 就會接回來（其他裝置也會斷線幾秒）")

    config.STATE_PATH.write_text(
        json.dumps(
            {
                "installs": snapshot,
                "overdue_notified": overdue_notified,
                "expired_notified": expired_notified,
                "device_offline_notified": device_offline_notified,
                "usbmuxd_drops": usbmuxd_drops,
                "usbmuxd_notified": usbmuxd_notified,
                "last_run": datetime.now(timezone.utc).isoformat(),
            },
            indent=2,
            ensure_ascii=False,
        )
    )

    if first_run:
        return

    sections = [
        (common.EXPIRED_HEADING, expired),
        (common.FAILING_HEADING, failed),
        (common.OVERDUE_HEADING, overdue),
        ("🔌 Mac 弄丟了 Wi-Fi 上的裝置", lost),
        (common.OFFLINE_HEADING, offline),
        ("✅ 刷新完成", refreshed),
        ("🔄 錯誤已解除", recovered),
        ("📶 裝置回線", back_online),
    ]
    body = []
    for title, items in sections:
        if items:
            body.append(title)
            body.extend(items)
    if body:
        common.notify(
            "Sideloadly 監控",
            "\n".join(body),
            reply_markup=USBMUXD_KEYBOARD if lost else None,
        )


if __name__ == "__main__":
    main()

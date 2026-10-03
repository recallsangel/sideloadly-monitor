#!/usr/bin/env python3
"""每小時比對 sideloadly 資料庫，把變化彙整成一則通知推出去。

偵測項目：刷新完成、刷新失敗、錯誤解除、逾期未刷新、已過期、裝置離線/回線。
每次執行都會更新 state.json 的 last_run，bot 端靠它判斷監控是否停擺。
"""
import json
from datetime import datetime, timezone

import common
import config
import history


def main():
    if not config.SIDELOADLY_DB_PATH.exists():
        return

    # 順手收 Sideloadly daemon 的日誌。不推播——這是例行家務，不是告警；
    # 印出來就好，launchd 會收進 monitor.log。
    rotated = common.rotate_daemon_log()
    if rotated:
        print(rotated, flush=True)

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
    today = datetime.now(timezone.utc).date().isoformat()

    # 用 visible_* 而不是 fetch_*：被 /forget 忘記的裝置/app 不該再觸發告警。
    installs = common.visible_installs()
    devices = common.visible_devices()
    quotas = common.fetch_account_quotas()

    refreshed, failed, recovered, overdue, expired = [], [], [], [], []
    offline, back_online = [], []
    # 新的 state 每輪從頭建，資料庫裡已經沒有（或被忘記）的 app 與裝置就不會留下來。
    # 留著的話快照只會一直累積，Sideloadly 重用 id 時還會把新 app 誤判成刷新完成。
    snapshot, overdue_notified, expired_notified, device_offline_notified = {}, {}, {}, {}

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

    config.STATE_PATH.write_text(
        json.dumps(
            {
                "installs": snapshot,
                "overdue_notified": overdue_notified,
                "expired_notified": expired_notified,
                "device_offline_notified": device_offline_notified,
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
        common.notify("Sideloadly 監控", "\n".join(body))


if __name__ == "__main__":
    main()

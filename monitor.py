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


def load_state() -> dict:
    if config.STATE_PATH.exists():
        state = json.loads(config.STATE_PATH.read_text())
    else:
        state = {}
    state.setdefault("installations", {})
    state.setdefault("overdue_notified", {})
    state.setdefault("expired_notified", {})
    state.setdefault("device_offline_notified", {})
    # 舊版把值存成單一 last_updated 字串，且用的是 DB 原始格式
    # （"2026-08-22 17:29:37.53582+08:00"）。這裡一併轉成 dict 與 isoformat，
    # 否則升級後第一輪會因格式不同而把每個 app 都誤判成剛刷新。
    for iid, entry in list(state["installations"].items()):
        if isinstance(entry, str):
            ts = common.parse_ts(entry)
            state["installations"][iid] = {
                "last_updated": ts.isoformat() if ts else None,
                "failures": 0,
                "error": None,
            }
    return state


def save_state(state: dict):
    state["last_run"] = datetime.now(timezone.utc).isoformat()
    config.STATE_PATH.write_text(json.dumps(state, indent=2, ensure_ascii=False))


def main():
    if not config.SIDELOADLY_DB_PATH.exists():
        return

    state = load_state()
    first_run = not state["installations"]
    today = datetime.now(timezone.utc).date().isoformat()

    installs = common.fetch_installs()
    devices = common.fetch_devices()
    quotas = common.fetch_account_quotas()

    refreshed, failed, recovered, overdue, expired = [], [], [], [], []
    offline, back_online = [], []

    for inst in installs:
        prev = state["installations"].get(inst.id, {})
        prev_updated = prev.get("last_updated")
        prev_failures = prev.get("failures", 0)
        prev_error = prev.get("error")

        raw_updated = inst.last_updated.isoformat() if inst.last_updated else None

        if prev and raw_updated != prev_updated:
            refreshed.append(f"  · {inst.label} ({inst.expiry_text()})")
            history.record("refresh", inst.device_name, inst.app_name, inst.version)

        new_failure = inst.failures_count > prev_failures or (
            inst.last_error and inst.last_error != prev_error
        )
        if prev and new_failure:
            failed.append(
                f"  · {inst.label}: {inst.last_error or '未知錯誤'} "
                f"(failures={inst.failures_count})"
            )
            history.record("failure", inst.device_name, inst.app_name, inst.last_error)

            # apple_id 額度用完是常見的假期到——見不到明確錯誤訊息分類，
            # 只能拿 App ID 週配額當旁證，附一個「可以換這個帳號試試」的提示。
            current_quota = quotas.get(inst.apple_id) if inst.apple_id else None
            if inst.apple_id and current_quota is not None and current_quota.remaining <= 0:
                alt = common.suggest_alternate_account(inst.apple_id, quotas)
                if alt:
                    failed.append(
                        f"    ↳ {inst.apple_id} 本週 App ID 額度已用完，"
                        f"可試著切到 {alt.apple_id}（剩 {alt.remaining} 個）"
                    )
                else:
                    failed.append(
                        f"    ↳ {inst.apple_id} 本週 App ID 額度已用完，"
                        f"其他已綁定帳號也沒有剩餘額度"
                    )
        elif prev and (prev_error or prev_failures) and not inst.failing:
            recovered.append(f"  · {inst.label}")
            history.record("recovery", inst.device_name, inst.app_name)

        state["installations"][inst.id] = {
            "last_updated": raw_updated,
            "failures": inst.failures_count,
            "error": inst.last_error,
        }

        # 逾期／過期每天最多提醒一次。
        if inst.expired:
            if state["expired_notified"].get(inst.id) != today:
                expired.append(f"  · {inst.label}: {inst.expiry_text()}")
                history.record("expired", inst.device_name, inst.app_name, inst.expiry_text())
                state["expired_notified"][inst.id] = today
        else:
            state["expired_notified"].pop(inst.id, None)

        if inst.overdue and not inst.expired:
            if state["overdue_notified"].get(inst.id) != today:
                overdue.append(f"  · {inst.label}: {inst.expiry_text()}")
                history.record("overdue", inst.device_name, inst.app_name, inst.expiry_text())
                state["overdue_notified"][inst.id] = today
        else:
            state["overdue_notified"].pop(inst.id, None)

    for device in devices:
        was_offline = device.udid in state["device_offline_notified"]
        if device.offline:
            if state["device_offline_notified"].get(device.udid) != today:
                offline.append(f"  · {device.name}: 最後連線 {device.seen_text()}")
                history.record("device_offline", device.name, detail=device.seen_text())
                state["device_offline_notified"][device.udid] = today
        elif was_offline:
            back_online.append(f"  · {device.name}")
            history.record("device_online", device.name)
            state["device_offline_notified"].pop(device.udid, None)

    save_state(state)

    if first_run:
        return

    sections = [
        ("🔴 已過期", expired),
        ("❌ 刷新失敗", failed),
        ("⚠ 逾期未刷新", overdue),
        ("📵 裝置離線", offline),
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

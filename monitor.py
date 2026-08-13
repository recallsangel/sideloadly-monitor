#!/usr/bin/env python3
import json
from datetime import datetime, timezone

import common
import config


def load_state() -> dict:
    if config.STATE_PATH.exists():
        return json.loads(config.STATE_PATH.read_text())
    return {"installations": {}, "overdue_notified": {}}


def save_state(state: dict):
    config.STATE_PATH.write_text(json.dumps(state, indent=2, ensure_ascii=False))


def main():
    if not config.SIDELOADLY_DB_PATH.exists():
        return

    state = load_state()
    first_run = not state["installations"]
    today = datetime.now(timezone.utc).date().isoformat()

    rows = common.fetch_installations()
    device_latest = {}

    for row in rows:
        iid = str(row["id"])
        last_updated = row["last_updated"]
        device_name = row["device_name"] or row["device_udid"]
        app_name = row["app_name"]

        ts = common.parse_ts(last_updated)
        if ts is not None:
            udid = row["device_udid"]
            if udid not in device_latest or ts > device_latest[udid][0]:
                device_latest[udid] = (ts, device_name)

        prev = state["installations"].get(iid)
        if prev != last_updated:
            if not first_run and prev is not None:
                common.notify("Sideloadly 刷新完成", f"{device_name} - {app_name}")
            state["installations"][iid] = last_updated

    for udid, (ts, device_name) in device_latest.items():
        age = common.age_days(ts)
        if age is not None and age >= config.OVERDUE_THRESHOLD_DAYS:
            if state["overdue_notified"].get(udid) != today:
                common.notify(
                    "Sideloadly 裝置逾期未刷新",
                    f"{device_name} 已 {age:.1f} 天未刷新",
                )
                state["overdue_notified"][udid] = today
        else:
            state["overdue_notified"].pop(udid, None)

    save_state(state)


if __name__ == "__main__":
    main()

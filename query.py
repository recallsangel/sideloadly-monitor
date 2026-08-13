#!/usr/bin/env python3
import common
import config


def main():
    rows = common.fetch_installations()
    by_device: dict[str, list] = {}
    for row in rows:
        by_device.setdefault(row["device_udid"], []).append(row)

    if not by_device:
        print("沒有任何裝置資料。")
        return

    for udid, installs in by_device.items():
        first = installs[0]
        device_name = first["device_name"] or udid
        last_seen = common.parse_ts(first["last_seen"])
        last_seen_age = common.age_days(last_seen)

        print(f"\n[{device_name}]  udid={udid}")
        if last_seen_age is not None:
            print(f"  最後連線: {first['last_seen']}（{last_seen_age:.1f} 天前）")
        if first["device_last_error"]:
            print(f"  裝置錯誤: {first['device_last_error']} (failures={first['device_failures_count']})")

        for inst in installs:
            ts = common.parse_ts(inst["last_updated"])
            age = common.age_days(ts)
            flag = ""
            if age is not None and age >= config.OVERDUE_THRESHOLD_DAYS:
                flag = "  ⚠ 逾期未刷新"
            age_str = f"{age:.1f} 天前" if age is not None else "無紀錄"
            print(f"    - {inst['app_name']}: 最後刷新 {age_str}{flag}")
            if inst["install_last_error"]:
                print(f"      錯誤: {inst['install_last_error']} (failures={inst['install_failures_count']})")


if __name__ == "__main__":
    main()

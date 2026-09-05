#!/usr/bin/env python3
"""在終端機看報表，不經過 Telegram。

用法：query.py [status|devices|log|stats|daemon]
"""
import sys

import common
import history

REPORTS = {
    "status": common.build_status_report,
    "devices": common.build_device_report,
    "log": history.build_log_report,
    "stats": history.build_stats_report,
    "forgotten": common.build_ignored_report,
    "daemon": lambda: f"daemon state = {common.daemon_state()}",
}

if __name__ == "__main__":
    which = sys.argv[1] if len(sys.argv) > 1 else "status"
    if which not in REPORTS:
        sys.exit(f"未知的報表 {which!r}，可用：{', '.join(REPORTS)}")
    print(REPORTS[which]())

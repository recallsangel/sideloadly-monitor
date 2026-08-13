#!/usr/bin/env python3
import os
import subprocess

import common
import config


def main():
    target = f"gui/{os.getuid()}/{config.RESTART_LABEL}"
    result = subprocess.run(
        ["launchctl", "kickstart", "-k", target],
        capture_output=True,
        text=True,
    )

    if result.returncode == 0:
        print(f"已重啟 {config.RESTART_LABEL}")
        common.notify("Sideloadly Daemon 已重啟", "手動觸發重啟成功")
    else:
        error = result.stderr.strip() or result.stdout.strip()
        print(f"重啟失敗: {error}")
        common.notify("Sideloadly Daemon 重啟失敗", error)


if __name__ == "__main__":
    main()

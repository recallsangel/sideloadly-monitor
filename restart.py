#!/usr/bin/env python3
"""重啟 Sideloadly daemon。

每天 4am 由 launchd 呼叫，但只在真的有問題時才動手——無條件重啟會在
daemon 正在刷新時把它打斷。加 --force 可以無視判斷直接重啟。
"""
import sys

import common
import config
import history


def restart_reasons() -> tuple[list[str], list[str]]:
    """回傳（重啟解得掉的問題, 解不掉但值得記一筆的舊帳）。

    只有第一份算重啟的理由。錯誤旗標本身不算——Sideloadly 要到下一次刷新成功
    才會清掉它，而重啟既清不掉旗標也不會讓刷新提早，於是同一筆舊錯誤會讓這支
    每天 04:00 重啟一次，什麼都沒解決還每天打斷 daemon 一次（restart.py 存在的
    理由第一句就是不要那樣做）。判準見 common.Install.stale_failure。
    """
    reasons = []
    stale = []

    state = common.daemon_state()
    if state != "running":
        reasons.append(f"daemon 不在執行中（state={state}）")

    # daemon 會漏記憶體，而重啟是唯一放得掉它的辦法（見 config.DAEMON_MEMORY_LIMIT_BYTES）。
    footprint = common.daemon_footprint()
    if footprint is not None and footprint > config.DAEMON_MEMORY_LIMIT_BYTES:
        reasons.append(
            f"daemon 記憶體 {footprint / 1024**3:.1f} GB，"
            f"超過 {config.DAEMON_MEMORY_LIMIT_BYTES / 1024**3:.0f} GB 上限"
        )

    # visible_installs()，不是 fetch_installs()：一個被 /forget 忘記的裝置/app
    # 不該再逼著每天 4am 的自動重啟去處理它。
    for inst in common.visible_installs():
        if inst.expired:
            reasons.append(f"{inst.label} {inst.expiry_text()}")
        elif inst.overdue:
            reasons.append(f"{inst.label} 逾期未刷新（{inst.expiry_text()}）")
        if inst.failing_now:
            reasons.append(f"{inst.label} 有錯誤：{inst.failure_text()}")
        elif inst.failing:
            stale.append(f"{inst.label} 有舊錯誤：{inst.failure_text()}")

    return reasons, stale


def main():
    force = "--force" in sys.argv[1:]
    reasons, stale = restart_reasons()

    # 舊帳只進 log，不推播也不重啟：它沒有變化，而每天為它發一則通知就是把
    # 「有事發生」的意思磨掉。
    for item in stale:
        print(f"（重啟解不掉，略過）{item}")

    if not force and not reasons:
        print("沒有重啟解得掉的問題，不重啟。" if stale else "一切正常，不重啟。")
        return

    if force:
        print("--force：略過判斷直接重啟。")
    else:
        print("需要重啟的原因：")
        for reason in reasons:
            print(f"  - {reason}")

    ok, message = common.perform_restart()
    print(message)

    history.record_restart(ok, message, "; ".join(reasons) if reasons else "force")

    body = message if force else message + "\n\n原因：\n" + "\n".join(
        f"· {r}" for r in reasons
    )
    if ok:
        common.notify("Sideloadly Daemon 已重啟", body)
    else:
        common.notify("Sideloadly Daemon 重啟失敗", body)


if __name__ == "__main__":
    main()
